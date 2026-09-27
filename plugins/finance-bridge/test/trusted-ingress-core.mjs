import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, writeFileSync } from "node:fs";
import { chmod, mkdir, mkdtemp, open, readdir, realpath, rm, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { flock } from "fs-ext";

import { HandoffPublisher } from "../dist/src/handoff.js";
import { ReceiptMediaAdapter } from "../dist/src/media.js";
import { canonicalCaptureKey, captureIdentities, createBridgeRequest } from "../dist/src/protocol.js";
import { TrustedIngressCapture } from "../dist/src/trusted-ingress.js";

const repositoryRoot = resolve(import.meta.dirname, "../../..");
const python = process.env.FINANCE_TEST_PYTHON;
if (!python) throw new Error("Set FINANCE_TEST_PYTHON to the Finance test Python 3.12 executable.");
const temporary = await mkdtemp(join(tmpdir(), "d3-bridge-core-reclaim-"));
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
const calls = [];
const captureArguments = [];
let loseNextCaptureResponse = false;
let loseNextTextResponse = false;
const runner = {
  async run(request, _deadline, fd) {
    calls.push(request.command);
    if (request.command === "capture") captureArguments.push(request.arguments);
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
    if (request.command === "capture_interaction" && loseNextTextResponse && response.status === "ok") {
      loseNextTextResponse = false;
      throw new Error("synthetic text response loss after Core commit");
    }
    return response;
  },
};
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const jpeg = (index) => Buffer.from([0xff, 0xd8, 0xff, 0xe0, index & 255, index >> 8, 0xff, 0xd9]);
const png = (index) => Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, index & 255, index >> 8]);
let downloads = 0;
const actualMedia = new ReceiptMediaAdapter(() => hostMediaRoot);
const media = { async acquireTrustedInbound(metadata, timeoutMs) {
  downloads += 1;
  return await actualMedia.acquireTrustedInbound(metadata, timeoutMs);
} };
const binding = {
  bindingId: "bind-1", pluginId: "finance-bridge", pluginRoot: "/synthetic",
  channel: "telegram", accountId: "finance", conversationId: "111",
  parentConversationId: "111", data: { senderId: "111" },
};
function turn(messageId, bytes, content = "receipt") {
  const mime = bytes[0] === 0xff ? "image/jpeg" : "image/png";
  const extension = mime === "image/jpeg" ? "jpg" : "png";
  const mediaPath = join(hostInbound, `telegram-${messageId}.${extension}`);
  writeFileSync(mediaPath, bytes, { mode: 0o644 });
  chmodSync(mediaPath, 0o644);
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
      metadata: {
        mediaPath, mediaUrl: mediaPath, mediaPaths: [mediaPath], mediaUrls: [mediaPath],
        mediaType: mime, mediaTypes: [mime],
      },
    },
    context: {
      channelId: "telegram", accountId: "finance", conversationId: "111",
      senderId: "111", messageId: String(messageId), pluginBinding: binding,
    },
  };
}
function textTurn(messageId, content) {
  const photo = turn(messageId, jpeg(messageId), content);
  delete photo.event.financeIngress.attachmentSha256;
  delete photo.event.metadata;
  return photo;
}
function seedHistoricalText(input) {
  const script = [
    "import hashlib, json, sys",
    "from pathlib import Path",
    "from openclaw_staging_bridge_support_v1 import BridgeWorkspace, open_database",
    "from finance_core.intake.capture_jobs import ensure_capture_job",
    "from finance_core.intake.raw_text_repository import create_raw_intake_record",
    "from finance_core.intake.telegram_text_adapter import validate_telegram_text_update",
    "from finance_core.telegram_source_context import TelegramSourceContext, record_telegram_source_context",
    "workspace_path, database_path, source_json = sys.argv[1:]",
    "source = json.loads(source_json)",
    "ingress = source['ingress']",
    "update = source['update']",
    "validated = validate_telegram_text_update(update)",
    "digest = hashlib.sha256(json.dumps(ingress, sort_keys=True, separators=(',', ':')).encode()).hexdigest()",
    "workspace = BridgeWorkspace(Path(workspace_path), Path(database_path))",
    "with open_database(workspace) as conn:",
    "    conn.execute('BEGIN IMMEDIATE')",
    "    intake = create_raw_intake_record(conn, validated.text, source_channel='telegram', source_metadata=validated.source_metadata)",
    "    record_telegram_source_context(conn, raw_intake_record_id=int(intake['id']), context=TelegramSourceContext(authenticated_actor_id='111', account_id='finance', conversation_id='111', binding_id='bind-1', message_id=str(ingress['messageId'])), captured_at=str(intake['received_at']))",
    "    ensure_capture_job(conn, intake_id=int(intake['id']), capture_kind='text', ingress_identity_digest=digest)",
    "    conn.commit()",
  ].join("\n");
  const ingress = { ...input.event.financeIngress, chatId: 111, messageId: Number(input.event.messageId), senderId: 111 };
  const update = {
    update_id: ingress.updateId,
    message: { message_id: ingress.messageId, chat: { id: 111, type: "private" },
      date: input.event.timestamp / 1_000, from: { id: 111 }, text: input.event.content },
  };
  const outcome = spawnSync(python, ["-c", script, workspace,
    join(workspace, "database", "staging.sqlite"), JSON.stringify({ ingress, update })],
  { cwd: repositoryRoot, env: environment, encoding: "utf8" });
  assert.equal(outcome.status, 0, outcome.stderr);
}
function seedPartialPhotoIntake(input) {
  const key = canonicalCaptureKey("111", String(input.event.messageId));
  const script = [
    "import json, sys",
    "from pathlib import Path",
    "from openclaw_staging_bridge_support_v1 import BridgeWorkspace, open_database",
    "from finance_core.intake.raw_text_repository import create_raw_intake_record, TELEGRAM_PHOTO_FINGERPRINT_VERSION",
    "from finance_core.telegram_source_context import TelegramSourceContext, record_telegram_source_context",
    "workspace_path, database_path, source_json = sys.argv[1:]",
    "source = json.loads(source_json)",
    "message_id = str(source['message_id'])",
    "workspace = BridgeWorkspace(Path(workspace_path), Path(database_path))",
    "with open_database(workspace) as conn:",
    "    conn.execute('BEGIN IMMEDIATE')",
    "    intake = create_raw_intake_record(conn, source['caption'], source_type='telegram_image', source_channel='telegram', source_metadata={'chat_id': '111', 'message_id': message_id, 'source_message_id': message_id, 'idempotency_key': 'raw-intake:telegram:111:' + message_id, 'handoff_filename': source['handoff_filename'], 'attachment_hash': source['attachment_hash'], 'telegram_update_id': str(source['update_id']), 'sender_id': '111'}, public_id=source['intake_id'], fingerprint_version=TELEGRAM_PHOTO_FINGERPRINT_VERSION)",
    "    record_telegram_source_context(conn, raw_intake_record_id=int(intake['id']), context=TelegramSourceContext(authenticated_actor_id='111', account_id='finance', conversation_id='111', binding_id='bind-1', message_id=message_id), captured_at=str(intake['received_at']))",
    "    conn.commit()",
    "    assert conn.execute('SELECT count(*) FROM finance_capture_jobs WHERE raw_intake_record_id = ?', (int(intake['id']),)).fetchone()[0] == 0",
  ].join("\n");
  const source = {
    message_id: Number(input.event.messageId), caption: input.event.content,
    intake_id: captureIdentities(key).rawIntakePublicId,
    handoff_filename: `${captureIdentities(key).rawIntakePublicId}.jpg`,
    attachment_hash: input.event.financeIngress.attachmentSha256,
    update_id: input.event.financeIngress.updateId,
  };
  const outcome = spawnSync(python, ["-c", script, workspace,
    join(workspace, "database", "staging.sqlite"), JSON.stringify(source)],
  { cwd: repositoryRoot, env: environment, encoding: "utf8" });
  assert.equal(outcome.status, 0, outcome.stderr);
}
function bridge(handoff = new HandoffPublisher(workspace)) {
  return new TrustedIngressCapture(workspace, runner, media, handoff);
}
async function assertHandoffEmpty() {
  const names = await readdir(join(workspace, "handoff"));
  assert.deepEqual(names, [".finance-bridge.lock.v1"]);
}
function coreRawInputs() {
  const database = join(workspace, "database", "staging.sqlite");
  const query = spawnSync(python, ["-c", [
    "import json, sqlite3, sys",
    "from pathlib import Path",
    "uri = Path(sys.argv[1]).resolve().as_uri() + '?mode=ro'",
    "with sqlite3.connect(uri, uri=True) as conn:",
    "    print(json.dumps(dict(conn.execute('SELECT public_id, raw_input FROM raw_intake_records'))))",
  ].join("\n"), database], { cwd: repositoryRoot, env: environment, encoding: "utf8" });
  assert.equal(query.status, 0, query.stderr);
  return JSON.parse(query.stdout);
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
  const alteredCaption = { ...duplicate.input.event, content: "different receipt caption" };
  assert.deepEqual(await restart.handle(alteredCaption, duplicate.input.context), { handled: false });
  assert.equal(downloads, beforeDuplicate.downloads);
  assert.equal(calls.filter((value) => value === "capture").length, beforeDuplicate.captures);

  loseNextCaptureResponse = true;
  const lostInput = turn(240, jpeg(40));
  const lost = await bridge().handle(lostInput.event, lostInput.context);
  assert.equal(lost.adoption?.attachmentStatus, "stored", JSON.stringify(lost));
  await assertHandoffEmpty();
  const lostReplay = await bridge().handle(lostInput.event, lostInput.context);
  assert.equal(lostReplay.adoption?.jobId, lost.adoption.jobId);
  const alteredAfterLoss = { ...lostInput.event, content: "changed after response loss" };
  assert.deepEqual(await bridge().handle(alteredAfterLoss, lostInput.context), { handled: false });

  for (const [index, phase] of [
    "after-record-fsync", "after-record-publish", "after-payload-fsync",
  ].entries()) {
    const messageId = 340 + index;
    const input = turn(messageId, jpeg(140 + index), "receipt with committed Core job");
    const first = await bridge().handle(input.event, input.context);
    assert.equal(first.handled, true);
    const key = canonicalCaptureKey("111", String(messageId));
    const intakeId = first.adoption?.intakeId;
    assert.ok(intakeId);
    await assert.rejects(new HandoffPublisher(workspace, {
      hook: (candidate) => { if (candidate === phase) throw new Error("synthetic prior-job crash"); },
    }).publish(key, intakeId, {
      bytes: jpeg(140 + index), byteSize: jpeg(140 + index).length,
      contentHash: sha256(jpeg(140 + index)), detectedMimeType: "image/jpeg",
      canonicalExtension: ".jpg",
    }), /synthetic prior-job crash/u);
    const recovered = await bridge().handle(input.event, input.context);
    assert.equal(recovered.handled, true, `${phase}: ${JSON.stringify(recovered)}`);
    await assertHandoffEmpty();
  }

  const raced = turn(350, jpeg(150), "raced receipt");
  let releaseStaleDiscovery;
  let signalStaleDiscovery;
  const staleDiscoveryReached = new Promise((resolve) => { signalStaleDiscovery = resolve; });
  const releaseStale = new Promise((resolve) => { releaseStaleDiscovery = resolve; });
  let pauseDiscovery = true;
  const staleRunner = {
    async run(request, deadline, fd) {
      const response = await runner.run(request, deadline, fd);
      if (pauseDiscovery && request.command === "get_capture_job_for_message") {
        pauseDiscovery = false;
        assert.equal(response.result.candidate, null);
        signalStaleDiscovery();
        await releaseStale;
      }
      return response;
    },
  };
  let stalePublishWrites = 0;
  const staleHandoff = new HandoffPublisher(workspace, {
    hook: (phase) => { if (phase === "after-record-fsync") stalePublishWrites += 1; },
  });
  const staleBridge = new TrustedIngressCapture(workspace, staleRunner, media, staleHandoff);
  const firstRacer = staleBridge.handle(raced.event, raced.context);
  await staleDiscoveryReached;
  const secondRacer = await bridge().handle(raced.event, raced.context);
  assert.equal(secondRacer.adoption?.attachmentStatus, "stored");
  await assertHandoffEmpty();
  const capturesAfterSecond = calls.filter((value) => value === "capture").length;
  releaseStaleDiscovery();
  assert.deepEqual(await firstRacer, { handled: false });
  assert.equal(stalePublishWrites, 0);
  assert.equal(calls.filter((value) => value === "capture").length, capturesAfterSecond);
  await assertHandoffEmpty();
  assert.equal((await bridge().handle(raced.event, raced.context)).adoption?.jobId,
    secondRacer.adoption.jobId);
  await assertHandoffEmpty();

  const partial = turn(355, jpeg(155), "partial intake receipt");
  const partialKey = canonicalCaptureKey("111", "355");
  const partialIntakeId = captureIdentities(partialKey).rawIntakePublicId;
  await new HandoffPublisher(workspace).publish(partialKey, partialIntakeId, {
    bytes: jpeg(155), byteSize: jpeg(155).length, contentHash: sha256(jpeg(155)),
    detectedMimeType: "image/jpeg", canonicalExtension: ".jpg",
  });
  seedPartialPhotoIntake(partial);
  const intakeCountBeforePartialReplay = Object.keys(coreRawInputs()).length;
  const partialStatus = await runner.run(createBridgeRequest("get_status", {
    workspace_path: workspace, intake_public_id: partialIntakeId,
  }));
  assert.equal(partialStatus.status, "ok");
  assert.equal(partialStatus.result.capture_job, null);
  const partialRecovered = await bridge().handle(partial.event, partial.context);
  assert.equal(partialRecovered.adoption?.intakeId, partialIntakeId,
    JSON.stringify(partialRecovered));
  await assertHandoffEmpty();
  const raw = coreRawInputs();
  assert.equal(Object.keys(raw).length, intakeCountBeforePartialReplay);
  assert.equal(Object.keys(raw).filter((id) => id === partialIntakeId).length, 1);

  const lockHandle = await open(join(workspace, "handoff", ".finance-bridge.lock.v1"), "r+");
  const lock = (operation) => new Promise((resolve, reject) =>
    flock(lockHandle.fd, operation, (error) => error ? reject(error) : resolve()));
  await lock("ex");
  try {
    assert.deepEqual(await bridge(new HandoffPublisher(workspace, { lockTimeoutMs: 20 }))
      .handle(raced.event, raced.context), { handled: false });
  } finally {
    await lock("un");
    await lockHandle.close();
  }
  await assertHandoffEmpty();

  for (const [index, phase] of [
    "after-record-fsync", "after-record-publish", "after-payload-fsync",
  ].entries()) {
    const messageId = 280 + index;
    const original = turn(messageId, jpeg(80 + index), "original caption");
    const crashed = await bridge(new HandoffPublisher(workspace, {
      hook: (candidate) => { if (candidate === phase) throw new Error("synthetic publish crash"); },
    })).handle(original.event, original.context);
    assert.deepEqual(crashed, { handled: false });
    const pendingPublisher = new HandoffPublisher(workspace);
    assert.deepEqual(await pendingPublisher.pendingReclaims(), []);
    await bridge().resumePendingReclaims();
    const beforeCapture = calls.filter((value) => value === "capture").length;
    const unrelatedText = textTurn(290 + index, "other lunch 2.00");
    assert.deepEqual(await bridge().handle(unrelatedText.event, unrelatedText.context), { handled: false });
    const unrelatedPhoto = turn(300 + index, jpeg(90 + index));
    assert.deepEqual(await bridge().handle(unrelatedPhoto.event, unrelatedPhoto.context), { handled: false });
    const otherImage = turn(messageId, png(100 + index), "original caption");
    assert.deepEqual(await bridge().handle(otherImage.event, otherImage.context), { handled: false });
    assert.equal(calls.filter((value) => value === "capture").length, beforeCapture);
    const recovered = await bridge().handle(original.event, original.context);
    assert.equal(recovered.adoption?.attachmentStatus, "stored", JSON.stringify(recovered));
    assert.equal(calls.filter((value) => value === "capture").length, beforeCapture + 1);
    await assertHandoffEmpty();
  }

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

  const emptyCaption = turn(243, jpeg(43), "");
  const emptyResult = await bridge().handle(emptyCaption.event, emptyCaption.context);
  assert.equal(emptyResult.adoption?.attachmentStatus, "stored");
  assert.equal("caption" in captureArguments.at(-1), false);
  assert.equal(coreRawInputs()[emptyResult.adoption.intakeId], "");
  const spoofedEmpty = { ...emptyCaption.event, content: "[telegram receipt image]" };
  assert.deepEqual(await bridge().handle(spoofedEmpty, emptyCaption.context), { handled: false });
  const literalOldMarker = turn(247, jpeg(47), "[telegram receipt image]");
  const literalOldResult = await bridge().handle(literalOldMarker.event, literalOldMarker.context);
  assert.equal(literalOldResult.adoption?.attachmentStatus, "stored");
  assert.equal(coreRawInputs()[literalOldResult.adoption.intakeId], "[telegram receipt image]");
  assert.deepEqual(await bridge().handle({ ...literalOldMarker.event, content: "" },
    literalOldMarker.context), { handled: false });
  const literalMarker = turn(244, png(44), "<media:image>");
  const markerResult = await bridge().handle(literalMarker.event, literalMarker.context);
  assert.equal(markerResult.adoption?.attachmentStatus, "stored");
  assert.equal(captureArguments.at(-1).caption, "<media:image>");
  const rawInputs = coreRawInputs();
  assert.equal(rawInputs[markerResult.adoption.intakeId], "<media:image>");
  const beforeWhitespace = calls.length;
  const downloadsBeforeWhitespace = downloads;
  const whitespacePhoto = turn(245, jpeg(45), " \t ");
  assert.deepEqual(await bridge().handle(whitespacePhoto.event, whitespacePhoto.context), { handled: false });
  assert.equal(calls.length, beforeWhitespace);
  assert.equal(downloads, downloadsBeforeWhitespace);
  const validSpaced = turn(246, png(46), "  shop  ");
  const spacedResult = await bridge().handle(validSpaced.event, validSpaced.context);
  assert.equal(spacedResult.adoption?.attachmentStatus, "stored");
  assert.equal(coreRawInputs()[spacedResult.adoption.intakeId], "  shop  ");

  const ordinaryText = textTurn(270, "lunch 12.50");
  const textResult = await bridge().handle(ordinaryText.event, ordinaryText.context);
  assert.equal(textResult.adoption?.attachmentStatus, "none");
  assert.equal((await bridge().handle(ordinaryText.event, ordinaryText.context)).adoption?.jobId,
    textResult.adoption.jobId);
  const controlText = textTurn(271, "完成");
  loseNextTextResponse = true;
  const controlResult = await bridge().handle(controlText.event, controlText.context);
  assert.equal(controlResult.adoption?.attachmentStatus, "none");
  assert.equal((await bridge().handle(controlText.event, controlText.context)).adoption?.jobId,
    controlResult.adoption.jobId);
  assert.equal(calls.filter((command) => command === "get_interaction_route").length, 4);

  const historical = textTurn(272, "historical lunch 12.50");
  seedHistoricalText(historical);
  const beforeHistoricalCapture = calls.filter((command) => command === "capture_interaction").length;
  assert.deepEqual(await bridge().handle(historical.event, historical.context), { handled: false });
  assert.deepEqual(calls.slice(-3), [
    "get_capture_job_for_message", "get_status", "get_interaction_route",
  ]);
  assert.equal(calls.filter((command) => command === "capture_interaction").length,
    beforeHistoricalCapture);
  const afterControlRawInputs = coreRawInputs();
  assert.equal(afterControlRawInputs[controlResult.adoption.intakeId], "完成");
  const controlRoute = await runner.run(createBridgeRequest("get_interaction_route", {
    workspace_path: workspace, operator_actor_id: "111", telegram_account_id: "finance",
    telegram_conversation_id: "111", conversation_binding_id: "bind-1",
    telegram_message_id: 271,
  }));
  assert.equal(controlRoute.status, "ok");
  assert.equal(controlRoute.result.found, true);
  assert.equal(controlRoute.result.final_transaction_created, false);
  assert.equal(controlRoute.result.interaction_route.route_kind, "control_refused");
  assert.equal(controlRoute.result.interaction_route.raw_text_sha256, sha256("完成"));

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
  await assert.rejects(bridge().resumePendingReclaims(), /unknown (?:reclaim residue|entry)/u);
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
    photosAdopted: 50, uniquePhotosBeforeQuota: 40, rejectedCaptionRetained: 1,
    crashStagesRecovered: crashPhases.length, coreTamperPreservedIntent: true,
    tornIntentPreservedOriginalWithoutAdoption: true,
    captureCalls: calls.filter((value) => value === "capture").length,
  }) + "\n");
  passed = true;
} finally {
  if (passed) await rm(temporary, { recursive: true, force: true });
  else process.stderr.write(`Failed synthetic workspace retained: ${workspace}\n`);
}
