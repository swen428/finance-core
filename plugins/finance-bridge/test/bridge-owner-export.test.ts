import assert from "node:assert/strict";
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
import {
  HANDOFF_PENDING_RECORD,
  HANDOFF_PENDING_PAYLOAD,
  HandoffPublisher,
  type ReclaimClaim,
} from "../src/handoff.js";
import { initializeProfileGate } from "../src/profile-gate.js";
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
  const previousRuntimeRoot = process.env.FINANCE_RUNTIME_ROOT;
  t.after(async () => {
    if (previousRuntimeRoot === undefined) delete process.env.FINANCE_RUNTIME_ROOT;
    else process.env.FINANCE_RUNTIME_ROOT = previousRuntimeRoot;
    await rm(root, { recursive: true, force: true });
  });

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
  process.env.FINANCE_RUNTIME_ROOT = runtimeRoot;
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
