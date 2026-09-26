import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, mkdtemp, readdir, realpath, rm, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";

import { HandoffPublisher } from "../dist/src/handoff.js";
import { TrustedIngressCapture } from "../dist/src/trusted-ingress.js";

const repositoryRoot = resolve(import.meta.dirname, "../../..");
const python = process.env.FINANCE_TEST_PYTHON;
if (!python) throw new Error("Set FINANCE_TEST_PYTHON to the Finance test Python 3.12 executable.");
const temporary = await mkdtemp(join(tmpdir(), "d3-bridge-core-reclaim-"));
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
const calls = [];
let loseNextCaptureResponse = false;
const runner = {
  async run(request, _deadline, fd) {
    calls.push(request.command);
    const result = spawnSync(python, ["-m", "finance_core.openclaw_staging_bridge.cli"], {
      input: JSON.stringify(request), cwd: repositoryRoot, env: environment, encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe", fd === undefined ? "ignore" : fd],
    });
    assert.ok(result.stdout.trim().length > 0,
      `${request.command}: ${result.stderr} (exit ${result.status})`);
    const response = JSON.parse(result.stdout);
    if (request.command === "capture" && loseNextCaptureResponse && response.status === "ok") {
      loseNextCaptureResponse = false;
      throw new Error("synthetic response loss after Core commit");
    }
    return response;
  },
};
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const jpeg = (index) => Buffer.from([0xff, 0xd8, 0xff, 0xe0, index & 255, index >> 8, 0xff, 0xd9]);
const png = (index) => Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, index & 255, index >> 8]);
let downloads = 0;
const media = { async acquire(metadata) {
  downloads += 1;
  const bytes = metadata.syntheticBytes;
  return {
    bytes, byteSize: bytes.length, contentHash: sha256(bytes),
    detectedMimeType: bytes[0] === 0xff ? "image/jpeg" : "image/png",
    canonicalExtension: bytes[0] === 0xff ? ".jpg" : ".png",
  };
} };
const binding = {
  bindingId: "bind-1", pluginId: "finance-bridge", pluginRoot: "/synthetic",
  channel: "telegram", accountId: "finance", conversationId: "111",
  parentConversationId: "111", data: { senderId: "111" },
};
function turn(messageId, bytes, content = "receipt") {
  const ingress = {
    channel: "telegram", accountId: "finance", updateId: 10_000 + messageId,
    chatId: "111", messageId: String(messageId), senderId: "111", bindingId: "bind-1",
    payloadSha256: sha256(`host-update-${messageId}-${content}`),
    attachmentSha256: sha256(bytes),
  };
  return {
    event: {
      content, timestamp: 1_750_000_000_000, channel: "telegram", accountId: "finance",
      conversationId: "111", parentConversationId: "111", senderId: "111",
      messageId: String(messageId), isGroup: false, commandAuthorized: true,
      senderIsOwner: true, financeIngress: ingress,
      metadata: { mediaUrl: `synthetic://${messageId}`, syntheticBytes: bytes },
    },
    context: {
      channelId: "telegram", accountId: "finance", conversationId: "111",
      senderId: "111", messageId: String(messageId), pluginBinding: binding,
    },
  };
}
function bridge(handoff = new HandoffPublisher(workspace)) {
  return new TrustedIngressCapture(workspace, runner, media, handoff);
}
async function assertHandoffEmpty() {
  const names = await readdir(join(workspace, "handoff"));
  assert.deepEqual(names, [".finance-bridge.lock.v1"]);
}
try {
  const claimant = bridge();
  const adopted = [];
  for (let index = 0; index < 40; index += 1) {
    const input = turn(200 + index, index % 2 === 0 ? jpeg(index) : png(index));
    const result = await claimant.handle(input.event, input.context);
    assert.equal(result.handled, true, `photo ${index}: ${JSON.stringify(result)}`);
    assert.equal(result.adoption?.attachmentStatus, "stored");
    adopted.push({ input, receipt: result.adoption });
    await assertHandoffEmpty();
  }
  assert.equal(downloads, 40);
  assert.equal(calls.filter((command) => command === "capture").length, 40);
  const restart = bridge();
  await restart.resumePendingReclaims(); // host may already have ACKed every update
  const duplicate = adopted[0];
  const beforeDuplicate = { downloads, captures: calls.filter((value) => value === "capture").length };
  const replay = await restart.handle(duplicate.input.event, duplicate.input.context);
  assert.deepEqual(replay.adoption, duplicate.receipt);
  assert.equal(downloads, beforeDuplicate.downloads);
  assert.equal(calls.filter((value) => value === "capture").length, beforeDuplicate.captures);

  loseNextCaptureResponse = true;
  const lostInput = turn(240, jpeg(40));
  const lost = await bridge().handle(lostInput.event, lostInput.context);
  assert.equal(lost.adoption?.attachmentStatus, "stored", JSON.stringify(lost));
  await assertHandoffEmpty();
  const lostReplay = await bridge().handle(lostInput.event, lostInput.context);
  assert.equal(lostReplay.adoption?.jobId, lost.adoption.jobId);

  const rejected = turn(241, png(41), "完成");
  const refusal = await bridge().handle(rejected.event, rejected.context);
  assert.deepEqual(refusal, { handled: false });
  const pending = await new HandoffPublisher(workspace).pendingReclaims();
  assert.equal(pending.length, 1);
  await bridge().resumePendingReclaims();
  assert.equal((await new HandoffPublisher(workspace).pendingReclaims()).length, 1);
  const retry = await bridge().handle(rejected.event, rejected.context);
  assert.deepEqual(retry, { handled: false });

  // The failed caption's retained original consumes one slot, but does not
  // block another message while capacity remains.
  const next = await bridge().handle(turn(242, png(42)).event, turn(242, png(42)).context);
  assert.equal(next.adoption?.attachmentStatus, "stored");
  assert.equal((await new HandoffPublisher(workspace).pendingReclaims()).length, 1);

  const changed = adopted[0].input;
  const forged = {
    ...changed.event,
    financeIngress: { ...changed.event.financeIngress, payloadSha256: "b".repeat(64) },
  };
  assert.deepEqual(await bridge().handle(forged, changed.context), { handled: false });

  const crashPhases = [
    "after-reclaim-payload-unlink", "after-reclaim-payload-fsync",
    "after-reclaim-record-unlink", "after-reclaim-record-fsync",
    "after-reclaim-intent-unlink", "after-reclaim-intent-fsync",
  ];
  for (let index = 0; index < crashPhases.length; index += 1) {
    const crashInput = turn(250 + index, index % 2 ? png(50 + index) : jpeg(50 + index));
    const crashingHandoff = new HandoffPublisher(workspace, {
      hook: async (phase) => {
        if (phase === crashPhases[index]) throw new Error(`synthetic crash at ${phase}`);
      },
    });
    const first = await bridge(crashingHandoff).handle(crashInput.event, crashInput.context);
    assert.deepEqual(first, { handled: false });
    // No new host event: startup recovery uses only the durable intent + Core.
    await bridge().resumePendingReclaims();
    const before = calls.filter((value) => value === "capture").length;
    const recovered = await bridge().handle(crashInput.event, crashInput.context);
    assert.equal(recovered.adoption?.attachmentStatus, "stored");
    assert.equal(calls.filter((value) => value === "capture").length, before);
  }

  const tamperedInput = turn(260, jpeg(60));
  const tamperHandoff = new HandoffPublisher(workspace, {
    hook: async (phase) => {
      if (phase === "after-reclaim-payload-unlink") throw new Error("synthetic crash before Core tamper");
    },
  });
  assert.deepEqual(await bridge(tamperHandoff).handle(tamperedInput.event, tamperedInput.context),
    { handled: false });
  const originalHash = tamperedInput.event.financeIngress.attachmentSha256;
  const coreOriginal = join(workspace, "attachments", originalHash.slice(0, 2),
    `${originalHash}.jpg`);
  await chmod(coreOriginal, 0o600);
  await writeFile(coreOriginal, Buffer.from("tampered Core original"));
  const pendingBeforeTamper = await new HandoffPublisher(workspace).pendingReclaims();
  await bridge().resumePendingReclaims();
  const pendingAfterTamper = await new HandoffPublisher(workspace).pendingReclaims();
  assert.equal(pendingAfterTamper.length, pendingBeforeTamper.length);
  assert.ok(pendingAfterTamper.some((claim) => claim.attachmentContentHash === originalHash));

  const unknown = join(workspace, "handoff", "unknown-residue");
  await writeFile(unknown, "foreign", { mode: 0o600 });
  await assert.rejects(bridge().resumePendingReclaims(), /unknown reclaim residue/u);
  await unlink(unknown);

  // A torn intent is retained for controlled repair. It cannot authorize
  // deletion, even though the original record and image are still present.
  const partialClaim = pendingAfterTamper.find((claim) =>
    claim.rawIntakePublicId !== pendingBeforeTamper.find((candidate) =>
      candidate.attachmentContentHash === originalHash)?.rawIntakePublicId);
  assert.ok(partialClaim);
  const intentPath = join(workspace, "handoff", `${partialClaim.rawIntakePublicId}.reclaim.json`);
  await writeFile(intentPath, '{"schema_version":', { mode: 0o600 });
  assert.deepEqual(await bridge().handle(rejected.event, rejected.context), { handled: false });
  const retainedNames = await readdir(join(workspace, "handoff"));
  assert.ok(retainedNames.some((name) => name === `${partialClaim.rawIntakePublicId}.handoff.json`));
  assert.ok(retainedNames.some((name) => name === `${partialClaim.rawIntakePublicId}.png`));
  assert.ok(retainedNames.includes(`${partialClaim.rawIntakePublicId}.reclaim.json`));
  process.stdout.write(JSON.stringify({
    photosAdopted: 48, uniquePhotosBeforeQuota: 40, rejectedCaptionRetained: 1,
    crashStagesRecovered: crashPhases.length, coreTamperPreservedIntent: true,
    tornIntentPreservedOriginalWithoutAdoption: true,
    captureCalls: calls.filter((value) => value === "capture").length,
  }) + "\n");
  passed = true;
} finally {
  if (passed) await rm(temporary, { recursive: true, force: true });
  else process.stderr.write(`Failed synthetic workspace retained: ${workspace}\n`);
}
