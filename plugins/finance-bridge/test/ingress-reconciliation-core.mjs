import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, mkdir, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { HandoffPublisher } from "../dist/src/handoff.js";
import { FinanceIngressReconciliationV1 } from "../dist/src/ingress-reconciliation-v1.js";
import { ReceiptMediaAdapter } from "../dist/src/media.js";
import { TrustedIngressCapture } from "../dist/src/trusted-ingress.js";

const repositoryRoot = resolve(import.meta.dirname, "../../..");
const python = process.env.FINANCE_TEST_PYTHON;
if (!python) throw new Error("Set FINANCE_TEST_PYTHON to the Finance test Python 3.12 executable.");
const temporary = await mkdtemp(join(tmpdir(), "d3-bridge-reconciliation-"));
const hostMediaRoot = join(temporary, "host-media");
const hostInbound = join(hostMediaRoot, "inbound");
await mkdir(hostInbound, { recursive: true, mode: 0o700 });
await chmod(hostMediaRoot, 0o700);
await chmod(hostInbound, 0o700);
const environment = {
  ...process.env,
  PYTHONPATH: `${repositoryRoot}:${join(repositoryRoot, "tests")}`,
  FINANCE_RUNTIME_ROOT: repositoryRoot,
};
const bootstrap = spawnSync(python, ["-c",
  "from pathlib import Path; import sys; import openclaw_staging_bridge_support_v1 as s; print(s.create_bridge_workspace(Path(sys.argv[1]), name='actual').workspace_path)",
  temporary], { encoding: "utf8", env: environment });
assert.equal(bootstrap.status, 0, bootstrap.stderr);
const workspace = bootstrap.stdout.trim();
const canonicalTemporary = await realpath(temporary);
assert.ok(workspace.startsWith(`${canonicalTemporary}/`) && workspace !== join(repositoryRoot, "database"),
  "Core integration must use a newly created temporary workspace.");

let passed = false;
const commands = [];
const runner = {
  async run(request, _deadline, fd) {
    commands.push(request.command);
    const result = spawnSync(python, ["-m", "finance_core.openclaw_staging_bridge.cli"], {
      input: JSON.stringify(request), cwd: repositoryRoot, env: environment, encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe", fd === undefined ? "ignore" : fd],
    });
    assert.ok(result.stdout.trim().length > 0,
      `${request.command}: ${result.stderr} (exit ${result.status})`);
    return JSON.parse(result.stdout);
  },
};
const sha256 = (value) => createHash("sha256").update(value).digest("hex");
const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x00, 0xff, 0xd9]);
const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x01, 0x02]);
const binding = {
  bindingId: "bind-1", pluginId: "finance-bridge", pluginRoot: "/synthetic",
  channel: "telegram", accountId: "finance", conversationId: "111",
  parentConversationId: "111", data: { senderId: "111" },
};

async function original(messageId, content, bytes) {
  let metadata;
  if (bytes !== undefined) {
    const mime = bytes[0] === 0xff ? "image/jpeg" : "image/png";
    const extension = mime === "image/jpeg" ? "jpg" : "png";
    const mediaPath = join(hostInbound, `telegram-${messageId}.${extension}`);
    await writeFile(mediaPath, bytes, { mode: 0o644 });
    metadata = {
      mediaPath, mediaUrl: mediaPath, mediaPaths: [mediaPath], mediaUrls: [mediaPath],
      mediaType: mime, mediaTypes: [mime],
    };
  }
  const ingress = {
    channel: "telegram", accountId: "finance", updateId: 10_000 + messageId,
    chatId: "111", messageId: String(messageId), senderId: "111", bindingId: "bind-1",
    payloadSha256: sha256(`host-update-${messageId}-${content}`),
    ...(bytes === undefined ? {} : { attachmentSha256: sha256(bytes) }),
  };
  return {
    event: {
      content, timestamp: 1_750_000_000_000, channel: "telegram", accountId: "finance",
      conversationId: "111", parentConversationId: "111", senderId: "111",
      messageId: String(messageId), isGroup: false, commandAuthorized: true,
      senderIsOwner: true, financeIngress: ingress,
      ...(metadata === undefined ? {} : { metadata }),
    },
    context: {
      channelId: "telegram", accountId: "finance", conversationId: "111",
      senderId: "111", messageId: String(messageId), pluginBinding: binding,
    },
    nonce: `nonce-${String(messageId).padStart(20, "0")}`,
  };
}

function counts() {
  const database = join(workspace, "database", "staging.sqlite");
  const query = spawnSync(python, ["-c", [
    "import json, sqlite3, sys",
    "from pathlib import Path",
    "uri = Path(sys.argv[1]).resolve().as_uri() + '?mode=ro'",
    "with sqlite3.connect(uri, uri=True) as conn:",
    "    print(json.dumps({table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('raw_intake_records', 'finance_capture_jobs', 'transactions', 'ai_fallback_invocation_claims')}))",
  ].join("\n"), database], { cwd: repositoryRoot, env: environment, encoding: "utf8" });
  assert.equal(query.status, 0, query.stderr);
  return JSON.parse(query.stdout);
}

try {
  const handoff = new HandoffPublisher(workspace);
  const capture = new TrustedIngressCapture(
    workspace, runner, new ReceiptMediaAdapter(() => hostMediaRoot), handoff,
  );
  const reconcile = new FinanceIngressReconciliationV1(workspace, runner, handoff);
  const inputs = [
    await original(801, "lunch 12.50"),
    await original(802, "receipt JPEG", jpeg),
    await original(803, "receipt PNG", png),
  ];
  const adopted = [];
  for (const input of inputs) {
    const result = await capture.handle(input.event, input.context);
    assert.equal(result.handled, true, JSON.stringify(result));
    assert.ok(result.adoption?.jobId);
    adopted.push(result.adoption);
  }
  const before = counts();
  assert.deepEqual(before, {
    raw_intake_records: 3, finance_capture_jobs: 3,
    transactions: 0, ai_fallback_invocation_claims: 0,
  });
  const beforeCommands = commands.length;
  for (const [index, input] of inputs.entries()) {
    const result = await reconcile.reconcile(input);
    assert.equal(result.kind, "matched", JSON.stringify(result));
    assert.deepEqual(result.adoption, adopted[index]);
    assert.equal(result.captureStatus, "captured");
    assert.equal(result.financialState, "unposted");
    assert.equal(result.replyState, "none");
  }
  const changedText = { ...inputs[0], event: { ...inputs[0].event, content: " lunch 12.50" } };
  assert.equal((await reconcile.reconcile(changedText)).kind, "refused");
  for (const input of inputs.slice(1)) {
    const changedCaption = { ...input, event: { ...input.event, content: `${input.event.content} ` } };
    assert.equal((await reconcile.reconcile(changedCaption)).kind, "refused");
    const changedOriginal = {
      ...input,
      event: { ...input.event, financeIngress: {
        ...input.event.financeIngress, attachmentSha256: "a".repeat(64),
      } },
    };
    assert.equal((await reconcile.reconcile(changedOriginal)).kind, "refused");
  }
  assert.deepEqual(counts(), before);
  assert.ok(commands.slice(beforeCommands).every((command) => [
    "get_capture_job_for_message", "get_status", "get_interaction_route", "get_capture_recovery",
  ].includes(command)));
  assert.equal(commands.filter((command) => command === "capture").length, 2);
  assert.equal(commands.filter((command) => command === "capture_interaction").length, 1);
  process.stdout.write(JSON.stringify({ verifiedExisting: 3, alteredOriginalsRefused: 5,
    extraCapture: 0, finalFacts: 0, modelCalls: 0 }) + "\n");
  passed = true;
} finally {
  if (passed) await rm(temporary, { recursive: true, force: true });
  else process.stderr.write(`Failed synthetic workspace retained: ${workspace}\n`);
}
