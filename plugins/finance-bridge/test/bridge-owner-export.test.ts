import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmod,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  unlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";

import { exportBridgeOwnerState } from "../src/owner-state-export-v1.js";
import { withExclusiveBridgeCut } from "../src/bridge-export-boundary.js";
import {
  HANDOFF_PENDING_RECORD,
  HANDOFF_PENDING_PAYLOAD,
  HandoffPublisher,
  type HandoffHook,
  type ReclaimClaim,
} from "../src/handoff.js";
import { initializeProfileGate, openProfileGate } from "../src/profile-gate.js";
import type { ValidatedMedia } from "../src/media.js";
import { canonicalCaptureKey, captureIdentities } from "../src/protocol.js";

const IMAGE_BYTES = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x01, 0x02, 0x03]);
const MEDIA: ValidatedMedia = {
  bytes: IMAGE_BYTES,
  byteSize: IMAGE_BYTES.byteLength,
  contentHash: sha256(IMAGE_BYTES),
  detectedMimeType: "image/jpeg",
  canonicalExtension: ".jpg",
  originalFilename: "synthetic.jpg",
};
const RECORD_SUFFIX = ".handoff.json";

interface SyntheticProfile {
  applicationSupportRoot: string;
  profileId: string;
  profileRoot: string;
  runtimeRoot: string;
  workspaceRoot: string;
  handoffRoot: string;
  workRoot: string;
  backupRoot: string;
}

async function syntheticProfile(t: TestContext): Promise<SyntheticProfile> {
  const root = await realpath(await mkdtemp(join(tmpdir(), "finance-owner-export-")));
  t.after(async () => rm(root, { recursive: true, force: true }));

  const applicationSupportRoot = join(root, "Application Support");
  const profileId = "synthetic";
  const profileRoot = join(applicationSupportRoot, "Finance-Codex", "profiles", profileId);
  const runtimeRoot = join(profileRoot, "runtime");
  const workspaceRoot = join(profileRoot, "workspace");
  const handoffRoot = join(workspaceRoot, "handoff");
  const workRoot = join(profileRoot, "work");
  const backupRoot = join(profileRoot, "backups");
  for (const directory of [
    applicationSupportRoot,
    join(applicationSupportRoot, "Finance-Codex"),
    join(applicationSupportRoot, "Finance-Codex", "profiles"),
    profileRoot,
    runtimeRoot,
    join(runtimeRoot, "database"),
    workspaceRoot,
    join(workspaceRoot, "database"),
    handoffRoot,
    backupRoot,
    workRoot,
    join(profileRoot, "restore"),
  ]) {
    await mkdir(directory, { mode: 0o700 });
    await chmod(directory, 0o700);
  }
  await writeFile(
    join(profileRoot, "profile.json"),
    JSON.stringify({ profile_id: profileId, runtime_root: runtimeRoot, workspace_root: workspaceRoot }),
    { encoding: "utf8", mode: 0o600 },
  );
  initializeProfileGate(profileRoot);
  return {
    applicationSupportRoot,
    profileId,
    profileRoot,
    runtimeRoot,
    workspaceRoot,
    handoffRoot,
    workRoot,
    backupRoot,
  };
}

function sha256(value: Buffer | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function captureKey(messageId: string): { key: string; rawIntakePublicId: string } {
  const key = canonicalCaptureKey("111", messageId);
  return { key, rawIntakePublicId: captureIdentities(key).rawIntakePublicId };
}

function reclaimClaim(key: string, rawIntakePublicId: string): ReclaimClaim {
  return {
    rawIntakePublicId,
    jobPublicId: `fcj_${sha256(`finance-capture-job-v1\0${rawIntakePublicId}`).slice(0, 40)}`,
    canonicalKeyHash: sha256(key),
    ingressIdentityDigest: "a".repeat(64),
    attachmentContentHash: MEDIA.contentHash,
    intakeFingerprint: "b".repeat(64),
  };
}

async function exportProfile(profile: SyntheticProfile) {
  return await exportBridgeOwnerState({
    applicationSupportRoot: profile.applicationSupportRoot,
    profileId: profile.profileId,
    runtimeRoot: profile.runtimeRoot,
  }, { waitMs: 1_000, maxHoldMs: 10_000 });
}

async function exportProfileWithHook(profile: SyntheticProfile, hook: HandoffHook) {
  return await withExclusiveBridgeCut({
    applicationSupportRoot: profile.applicationSupportRoot,
    profileId: profile.profileId,
    runtimeRoot: profile.runtimeRoot,
  }, async (cut, sink) => await new HandoffPublisher(cut.workspaceRoot, { hook })
    .exportFrozen(cut, sink), { waitMs: 1_000, maxHoldMs: 10_000 });
}

async function assertManifestMatchesStage(
  receipt: Awaited<ReturnType<typeof exportProfile>>,
): Promise<Buffer> {
  const bytes = await readFile(join(receipt.stagePath, receipt.manifest.relativeName));
  assert.equal(bytes.byteLength, receipt.manifest.byteSize);
  assert.equal(sha256(bytes), receipt.manifest.sha256);
  assert.equal(receipt.manifestSha256, sha256(bytes));
  assert.deepEqual(JSON.parse(bytes.toString("utf8")), receipt.handoff);
  assert.equal(receipt.handoff.profileId, receipt.profileId);
  assert.equal(receipt.handoff.cutId, receipt.cutId);
  return bytes;
}

async function assertStageFilesMatch(
  profile: SyntheticProfile,
  receipt: Awaited<ReturnType<typeof exportProfile>>,
): Promise<void> {
  for (const file of receipt.handoff.files) {
    const bytes = await readFile(join(receipt.stagePath, file.frozenName));
    assert.equal(bytes.byteLength, file.byteSize);
    assert.equal(sha256(bytes), file.sha256);
  }
  assert.equal((await lstat(receipt.stagePath)).mode & 0o777, 0o700);
  assert.equal((await lstat(join(receipt.stagePath, receipt.manifest.relativeName))).mode & 0o777, 0o600);
  assert.deepEqual(await readdir(profile.backupRoot), []);
}

async function assertFailedStageIsPreserved(profile: SyntheticProfile): Promise<void> {
  const names = await readdir(profile.workRoot);
  assert.equal(names.length, 1);
  assert.match(names[0] ?? "", /^owner-export-[0-9a-f]{32}$/u);
  const stagePath = join(profile.workRoot, names[0]!);
  assert.equal((await lstat(stagePath)).isDirectory(), true);
  await assert.rejects(lstat(join(stagePath, "bridge-owner-export-v1.json")), { code: "ENOENT" });
  assert.deepEqual(await readdir(profile.backupRoot), []);
}

test("owner export stages one verified retained handoff under the fixed profile", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("901");
  const published = await new HandoffPublisher(profile.workspaceRoot).publish(
    key, rawIntakePublicId, MEDIA,
  );

  const receipt = await exportProfile(profile);
  assert.equal(receipt.contractVersion, "finance-bridge-owner-export-receipt-v1");
  assert.equal(receipt.profileId, profile.profileId);
  assert.match(receipt.cutId, /^[0-9a-f]{32}$/u);
  assert.equal(receipt.stagePath, join(profile.workRoot, `owner-export-${receipt.cutId}`));
  assert.equal(receipt.handoff.contractVersion, "finance-bridge-handoff-export-v1");
  assert.equal(receipt.handoff.owner, "finance-bridge");
  assert.equal(receipt.handoff.pendingPublicationKeyHash, null);
  assert.equal(receipt.handoff.slots.length, 1);
  assert.equal(receipt.handoff.slots[0]?.state, "retained");
  assert.equal(receipt.handoff.slots[0]?.attachmentSha256, MEDIA.contentHash);
  assert.equal(receipt.handoff.files.length, 2);
  assert.deepEqual(receipt.handoff.files.map((file) => file.role).sort(), ["payload", "record"]);

  const frozenPayload = receipt.handoff.files.find((file) => file.role === "payload");
  assert.ok(frozenPayload);
  assert.deepEqual(
    await readFile(join(receipt.stagePath, frozenPayload.frozenName)),
    MEDIA.bytes,
  );
  assert.deepEqual(await readFile(published.payloadPath), MEDIA.bytes);
  await assertManifestMatchesStage(receipt);
  await assertStageFilesMatch(profile, receipt);
});

test("owner export uses the explicit locator when FINANCE_RUNTIME_ROOT is absent, mismatched, or changes while waiting", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("915");
  await new HandoffPublisher(profile.workspaceRoot).publish(key, rawIntakePublicId, MEDIA);
  const previousRuntimeRoot = process.env.FINANCE_RUNTIME_ROOT;
  const mismatchedRuntimeRoot = join(profile.profileRoot, "unselected-runtime");
  try {
    delete process.env.FINANCE_RUNTIME_ROOT;
    const withoutEnvironment = await exportProfile(profile);
    assert.equal(withoutEnvironment.profileId, profile.profileId);
    assert.equal(withoutEnvironment.handoff.slots.length, 1);

    process.env.FINANCE_RUNTIME_ROOT = mismatchedRuntimeRoot;
    const withMismatchedEnvironment = await exportProfile(profile);
    assert.equal(withMismatchedEnvironment.profileId, profile.profileId);
    assert.equal(withMismatchedEnvironment.handoff.slots.length, 1);

    const gate = openProfileGate(profile.profileRoot);
    const held = await gate.acquireExclusive(1_000, 10_000);
    try {
      process.env.FINANCE_RUNTIME_ROOT = profile.runtimeRoot;
      const pendingExport = exportProfile(profile);
      process.env.FINANCE_RUNTIME_ROOT = mismatchedRuntimeRoot;
      held.close();
      const afterEnvironmentChanged = await pendingExport;
      assert.equal(afterEnvironmentChanged.profileId, profile.profileId);
      assert.equal(afterEnvironmentChanged.handoff.slots.length, 1);
    } finally {
      held.close();
      gate.close();
    }
  } finally {
    if (previousRuntimeRoot === undefined) delete process.env.FINANCE_RUNTIME_ROOT;
    else process.env.FINANCE_RUNTIME_ROOT = previousRuntimeRoot;
  }
});

test("owner export refuses a blank profile until its handoff lock is provisioned", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  await assert.rejects(exportProfile(profile), /openat|ENOENT|handoff lock/u);
  await assertFailedStageIsPreserved(profile);
  assert.deepEqual(await readdir(profile.handoffRoot), []);
});

test("owner export rejects a FIFO promptly and releases the handoff EX lock", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("913");
  await new HandoffPublisher(profile.workspaceRoot).publish(key, rawIntakePublicId, MEDIA);

  const fifoPath = join(profile.handoffRoot, "operator-note.fifo");
  execFileSync("mkfifo", [fifoPath]);
  await chmod(fifoPath, 0o600);

  const exportModuleUrl = new URL("../src/owner-state-export-v1.js", import.meta.url).href;
  const childProgram = [
    'import { openSync, closeSync } from "node:fs";',
    'import { flock } from "fs-ext";',
    `const { exportBridgeOwnerState } = await import(${JSON.stringify(exportModuleUrl)});`,
    "const locator = {",
    "  applicationSupportRoot: process.env.FINANCE_TEST_APPLICATION_SUPPORT_ROOT,",
    "  profileId: process.env.FINANCE_TEST_PROFILE_ID,",
    "  runtimeRoot: process.env.FINANCE_TEST_RUNTIME_ROOT,",
    "};",
    "try {",
    "  await exportBridgeOwnerState(locator, { waitMs: 1_000, maxHoldMs: 10_000 });",
    '  process.stdout.write(JSON.stringify({ ok: true }));',
    "  process.exitCode = 2;",
    "} catch (error) {",
    '  const lockFd = openSync(process.env.FINANCE_TEST_LOCK_PATH, "r+");',
    '  const lockError = await new Promise((resolve) => flock(lockFd, "exnb", (cause) => resolve(cause?.code ?? null)));',
    '  if (lockError === null) await new Promise((resolve, reject) => flock(lockFd, "un", (cause) => cause ? reject(cause) : resolve()));',
    "  closeSync(lockFd);",
    "  process.stdout.write(JSON.stringify({",
    "    ok: false,",
    '    error: error instanceof Error ? error.message : String(error),',
    "    lockReleased: lockError === null,",
    "  }));",
    "  if (lockError !== null) process.exitCode = 3;",
    "}",
  ].join("\n");
  const startedAt = performance.now();
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", childProgram], {
    cwd: process.cwd(),
    env: {
      ...process.env,
      FINANCE_TEST_APPLICATION_SUPPORT_ROOT: profile.applicationSupportRoot,
      FINANCE_TEST_PROFILE_ID: profile.profileId,
      FINANCE_TEST_RUNTIME_ROOT: profile.runtimeRoot,
      FINANCE_TEST_LOCK_PATH: join(profile.handoffRoot, ".finance-bridge.lock.v1"),
    },
    encoding: "utf8",
    killSignal: "SIGKILL",
    timeout: 2_000,
  });
  const elapsedMs = performance.now() - startedAt;
  assert.equal(child.error, undefined, child.error?.message ?? "");
  assert.equal(child.signal, null, child.stderr);
  assert.equal(child.status, 0, child.stderr);
  assert.ok(elapsedMs < 1_500, `FIFO refusal took ${elapsedMs.toFixed(0)} ms`);
  const result = JSON.parse(child.stdout) as {
    ok: boolean;
    error?: string;
    lockReleased?: boolean;
  };
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /unknown|non-file|FIFO|pipe|regular/u);
  assert.equal(result.lockReleased, true);
  await assertFailedStageIsPreserved(profile);
});

test("owner export preserves a standalone pending record and marks it incomplete", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("902");
  const interrupted = new HandoffPublisher(profile.workspaceRoot, {
    hook: async (phase) => {
      if (phase === "after-record-fsync") throw new Error("synthetic publication interruption");
    },
  });
  await assert.rejects(interrupted.publish(key, rawIntakePublicId, MEDIA), /synthetic publication interruption/u);

  const sourcePending = await readFile(join(profile.handoffRoot, HANDOFF_PENDING_RECORD));
  const pendingRecord = JSON.parse(sourcePending.toString("utf8")) as Record<string, unknown>;
  assert.equal(pendingRecord.raw_intake_public_id, rawIntakePublicId);
  const receipt = await exportProfile(profile);
  assert.equal(receipt.handoff.pendingPublicationKeyHash, sha256(key));
  assert.equal(receipt.handoff.slots.length, 1);
  assert.equal(receipt.handoff.slots[0]?.rawIntakePublicId, rawIntakePublicId);
  assert.equal(receipt.handoff.slots[0]?.state, "incomplete");
  const frozenPending = receipt.handoff.files.find((file) => file.role === "pending_record");
  assert.ok(frozenPending);
  assert.deepEqual(await readFile(join(receipt.stagePath, frozenPending.frozenName)), sourcePending);
  assert.deepEqual(await readFile(join(profile.handoffRoot, HANDOFF_PENDING_RECORD)), sourcePending);
  await assertManifestMatchesStage(receipt);
  await assertStageFilesMatch(profile, receipt);
});

test("owner export rejects a pending record whose canonical key changes after inventory", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("909");
  const interrupted = new HandoffPublisher(profile.workspaceRoot, {
    hook: async (phase) => {
      if (phase === "after-record-fsync") throw new Error("synthetic publication interruption");
    },
  });
  await assert.rejects(interrupted.publish(key, rawIntakePublicId, MEDIA), /synthetic publication interruption/u);

  const pendingPath = join(profile.handoffRoot, HANDOFF_PENDING_RECORD);
  const originalRecord = JSON.parse(await readFile(pendingPath, "utf8")) as Record<string, unknown>;
  assert.equal(originalRecord.canonical_key_hash, sha256(key));
  const replacementHash = "c".repeat(64);
  await assert.rejects(exportProfileWithHook(profile, async (phase) => {
    if (phase !== "after-export-inventory") return;
    const changed = JSON.parse(await readFile(pendingPath, "utf8")) as Record<string, unknown>;
    changed.canonical_key_hash = replacementHash;
    await writeFile(pendingPath, `${JSON.stringify(changed)}\n`, { mode: 0o600 });
  }), /pending|publication|canonical|hash|changed|inventory/u);

  const preserved = JSON.parse(await readFile(pendingPath, "utf8")) as Record<string, unknown>;
  assert.equal(preserved.canonical_key_hash, replacementHash);
  await assertFailedStageIsPreserved(profile);
});

test("owner export preserves reclaim intent without reclaiming the retained original", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("903");
  const claim = reclaimClaim(key, rawIntakePublicId);
  const publisher = new HandoffPublisher(profile.workspaceRoot);
  await assert.rejects(
    publisher.withPublished(
      key,
      rawIntakePublicId,
      MEDIA,
      async () => { throw new Error("synthetic Core response lost"); },
      30_000,
      claim,
    ),
    /synthetic Core response lost/u,
  );

  const receipt = await exportProfile(profile);
  assert.equal(receipt.handoff.slots.length, 1);
  assert.equal(receipt.handoff.slots[0]?.state, "reclaiming");
  assert.equal(receipt.handoff.slots[0]?.coreCustodyRequired, true);
  assert.deepEqual(receipt.handoff.files.map((file) => file.role).sort(), [
    "payload", "reclaim_intent", "record",
  ]);
  assert.deepEqual(await publisher.pendingReclaims(), [claim]);
  assert.equal((await readdir(profile.handoffRoot)).some((name) => name.endsWith(".reclaim.json")), true);
  await assertManifestMatchesStage(receipt);
  await assertStageFilesMatch(profile, receipt);
});

test("owner export rejects a reclaim intent rebound after inventory while intent-only", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("910");
  const claim = reclaimClaim(key, rawIntakePublicId);
  await assert.rejects(new HandoffPublisher(profile.workspaceRoot).withPublished(
    key,
    rawIntakePublicId,
    MEDIA,
    async () => { throw new Error("synthetic Core response lost"); },
    30_000,
    claim,
  ), /synthetic Core response lost/u);

  const reclaimCrash = new HandoffPublisher(profile.workspaceRoot, {
    hook: async (phase) => {
      if (phase === "after-reclaim-record-fsync") throw new Error("synthetic reclaim interruption");
    },
  });
  await assert.rejects(reclaimCrash.reclaimVerified(claim, async () => true), /synthetic reclaim interruption/u);
  const intentName = `${rawIntakePublicId}.reclaim.json`;
  const intentPath = join(profile.handoffRoot, intentName);
  assert.deepEqual((await readdir(profile.handoffRoot)).filter((name) => name.endsWith(".reclaim.json")), [intentName]);
  await assert.rejects(lstat(join(profile.handoffRoot, `${rawIntakePublicId}.handoff.json`)), { code: "ENOENT" });
  await assert.rejects(lstat(join(profile.handoffRoot, `${rawIntakePublicId}.jpg`)), { code: "ENOENT" });

  const replacement = captureKey("911");
  const replacementClaim = reclaimClaim(replacement.key, replacement.rawIntakePublicId);
  const initialIntent = JSON.parse(await readFile(intentPath, "utf8")) as Record<string, unknown>;
  const reboundIntent = {
    ...initialIntent,
    claim: replacementClaim,
    record_basename: `${replacement.rawIntakePublicId}.handoff.json`,
    payload_basename: `${replacement.rawIntakePublicId}.jpg`,
  };
  await assert.rejects(exportProfileWithHook(profile, async (phase) => {
    if (phase !== "after-export-inventory") return;
    await writeFile(intentPath, `${JSON.stringify(reboundIntent)}\n`, { mode: 0o600 });
  }), /intent|reclaim|identity|changed|inventory|slot/u);

  const preservedIntent = JSON.parse(await readFile(intentPath, "utf8")) as Record<string, unknown>;
  assert.equal((preservedIntent.claim as Record<string, unknown>).rawIntakePublicId,
    replacement.rawIntakePublicId);
  assert.equal(preservedIntent.record_basename, `${replacement.rawIntakePublicId}.handoff.json`);
  assert.equal(preservedIntent.payload_basename, `${replacement.rawIntakePublicId}.jpg`);
  await assertFailedStageIsPreserved(profile);
});

test("owner export rejects a pending record that duplicates an intent-only reclaim slot", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("914");
  const claim = reclaimClaim(key, rawIntakePublicId);
  const publisher = new HandoffPublisher(profile.workspaceRoot);
  await assert.rejects(
    publisher.withPublished(
      key,
      rawIntakePublicId,
      MEDIA,
      async () => { throw new Error("synthetic Core response lost"); },
      30_000,
      claim,
    ),
    /synthetic Core response lost/u,
  );

  const reclaimCrash = new HandoffPublisher(profile.workspaceRoot, {
    hook: async (phase) => {
      if (phase === "after-reclaim-record-fsync") throw new Error("synthetic reclaim interruption");
    },
  });
  await assert.rejects(reclaimCrash.reclaimVerified(claim, async () => true), /synthetic reclaim interruption/u);
  const intentPath = join(profile.handoffRoot, `${rawIntakePublicId}.reclaim.json`);
  const intentBefore = await readFile(intentPath);
  await assert.rejects(lstat(join(profile.handoffRoot, `${rawIntakePublicId}.handoff.json`)), { code: "ENOENT" });
  await assert.rejects(lstat(join(profile.handoffRoot, `${rawIntakePublicId}.jpg`)), { code: "ENOENT" });

  const pendingCrash = new HandoffPublisher(profile.workspaceRoot, {
    hook: async (phase) => {
      if (phase === "after-record-fsync") throw new Error("synthetic pending publication interruption");
    },
  });
  await assert.rejects(
    pendingCrash.publish(key, rawIntakePublicId, MEDIA),
    /synthetic pending publication interruption/u,
  );
  const pendingPath = join(profile.handoffRoot, HANDOFF_PENDING_RECORD);
  const pendingBefore = await readFile(pendingPath);
  assert.equal(
    (JSON.parse(pendingBefore.toString("utf8")) as Record<string, unknown>).raw_intake_public_id,
    rawIntakePublicId,
  );
  assert.deepEqual(await readFile(intentPath), intentBefore);

  await assert.rejects(exportProfile(profile), /pending|slot|reclaim|duplicate|conflict|identity/u);
  await assertFailedStageIsPreserved(profile);
  assert.deepEqual(await readFile(pendingPath), pendingBefore);
  assert.deepEqual(await readFile(intentPath), intentBefore);
});

test("owner export rejects an unexplained missing image and keeps its failed stage", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("904");
  const published = await new HandoffPublisher(profile.workspaceRoot).publish(
    key, rawIntakePublicId, MEDIA,
  );
  await unlink(published.payloadPath);

  await assert.rejects(exportProfile(profile), /missing|incomplete|pending|recovery/u);
  await assertFailedStageIsPreserved(profile);
  assert.equal((await lstat(published.recordPath)).isFile(), true);
  await assert.rejects(lstat(published.payloadPath), { code: "ENOENT" });
});

test("owner export rejects a final record with only a pending image and preserves both", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("907");
  const interrupted = new HandoffPublisher(profile.workspaceRoot, {
    hook: async (phase) => {
      if (phase === "after-payload-fsync") throw new Error("synthetic publication interruption");
    },
  });
  await assert.rejects(interrupted.publish(key, rawIntakePublicId, MEDIA), /synthetic publication interruption/u);
  const recordPath = join(profile.handoffRoot, `${rawIntakePublicId}${RECORD_SUFFIX}`);
  const pendingPath = join(profile.handoffRoot, HANDOFF_PENDING_PAYLOAD);
  const recordBefore = await readFile(recordPath);
  const pendingBefore = await readFile(pendingPath);
  assert.deepEqual(pendingBefore, MEDIA.bytes);

  await assert.rejects(exportProfile(profile), /missing|incomplete|pending|recovery/u);
  await assertFailedStageIsPreserved(profile);
  assert.deepEqual(await readFile(recordPath), recordBefore);
  assert.deepEqual(await readFile(pendingPath), pendingBefore);
  await assert.rejects(lstat(join(profile.handoffRoot, `${rawIntakePublicId}.jpg`)), { code: "ENOENT" });
});

test("owner export rejects unknown handoff files and keeps its failed stage", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("905");
  await new HandoffPublisher(profile.workspaceRoot).publish(key, rawIntakePublicId, MEDIA);
  const unknownPath = join(profile.handoffRoot, "operator-note.txt");
  await writeFile(unknownPath, "synthetic unknown entry\n", { mode: 0o600 });

  await assert.rejects(exportProfile(profile), /unknown|unbound/u);
  await assertFailedStageIsPreserved(profile);
  assert.equal(await readFile(unknownPath, "utf8"), "synthetic unknown entry\n");
});

test("owner export rejects a record whose content hash was changed and keeps its failed stage", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("906");
  await new HandoffPublisher(profile.workspaceRoot).publish(key, rawIntakePublicId, MEDIA);
  const recordPath = join(profile.handoffRoot, `${rawIntakePublicId}${RECORD_SUFFIX}`);
  const record = JSON.parse(await readFile(recordPath, "utf8")) as Record<string, unknown>;
  record.content_hash = "0".repeat(64);
  await writeFile(recordPath, `${JSON.stringify(record)}\n`, { mode: 0o600 });

  await assert.rejects(exportProfile(profile), /hash|match|changed|content/u);
  await assertFailedStageIsPreserved(profile);
  assert.equal((JSON.parse(await readFile(recordPath, "utf8")) as Record<string, unknown>).content_hash,
    "0".repeat(64));
});

test("owner export rejects a payload changed after inventory but before freezing", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("908");
  const published = await new HandoffPublisher(profile.workspaceRoot).publish(
    key, rawIntakePublicId, MEDIA,
  );
  const changed = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x01, 0x02, 0x04]);
  await assert.rejects(withExclusiveBridgeCut({
    applicationSupportRoot: profile.applicationSupportRoot,
    profileId: profile.profileId,
    runtimeRoot: profile.runtimeRoot,
  }, async (cut, sink) => await new HandoffPublisher(cut.workspaceRoot, {
    hook: async (phase) => {
      if (phase === "after-export-inventory") await writeFile(published.payloadPath, changed);
    },
  }).exportFrozen(cut, sink)), /frozen original differs from its record/u);
  await assertFailedStageIsPreserved(profile);
  assert.deepEqual(await readFile(published.payloadPath), changed);
});

test("owner export rejects self-consistent record and payload rewrites with invalid image magic", { concurrency: false }, async (t) => {
  const profile = await syntheticProfile(t);
  const { key, rawIntakePublicId } = captureKey("912");
  const published = await new HandoffPublisher(profile.workspaceRoot).publish(
    key, rawIntakePublicId, MEDIA,
  );
  const changed = Buffer.from("not-jpg");
  await assert.rejects(exportProfileWithHook(profile, async (phase) => {
    if (phase !== "after-export-inventory") return;
    const record = JSON.parse(await readFile(published.recordPath, "utf8")) as Record<string, unknown>;
    record.content_hash = sha256(changed);
    record.byte_size = changed.byteLength;
    await writeFile(published.recordPath, `${JSON.stringify(record)}\n`, { mode: 0o600 });
    await writeFile(published.payloadPath, changed, { mode: 0o600 });
  }), /magic|image|mime|payload|changed|record/u);

  const preservedRecord = JSON.parse(await readFile(published.recordPath, "utf8")) as Record<string, unknown>;
  assert.equal(preservedRecord.content_hash, sha256(changed));
  assert.equal(preservedRecord.byte_size, changed.byteLength);
  assert.deepEqual(await readFile(published.payloadPath), changed);
  await assertFailedStageIsPreserved(profile);
});
