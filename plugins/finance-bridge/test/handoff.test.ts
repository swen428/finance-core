import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { fstatSync } from "node:fs";
import { chmod, lstat, mkdir, open, readFile, realpath, rename, symlink, truncate, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { flock } from "fs-ext";

import {
  HANDOFF_PENDING_PAYLOAD,
  HANDOFF_PENDING_RECORD,
  HandoffPublisher,
  type HandoffHook,
} from "../src/handoff.js";
import type { ValidatedMedia } from "../src/media.js";
import { captureIdentities, canonicalCaptureKey } from "../src/protocol.js";
import { temporaryDirectory } from "./support.js";

const bytes = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);
const media: ValidatedMedia = {
  bytes,
  byteSize: bytes.length,
  contentHash: "474ebe266cd7f9ed28807fa3fdfe0c04cdb3cef9313cdda5c08b15910fcc8184",
  detectedMimeType: "image/jpeg",
  canonicalExtension: ".jpg",
  originalFilename: "receipt.jpg",
};

async function workspace(): Promise<AsyncDisposable & {path: string}> {
  const fixture = await temporaryDirectory();
  const path = join(fixture.path, "workspace");
  await mkdir(path, { mode: 0o700 });
  await chmod(path, 0o700);
  return { path: await realpath(path), async [Symbol.asyncDispose]() { await fixture[Symbol.asyncDispose](); } };
}

function flockPromise(fileDescriptor: number, operation: "ex" | "un"): Promise<void> {
  return new Promise((resolve, reject) => {
    flock(fileDescriptor, operation, (error) => error ? reject(error) : resolve());
  });
}

test("handoff publishes one retained 0600 record and payload and reuses exact replay", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "20");
  const identity = captureIdentities(key).rawIntakePublicId;
  const publisher = new HandoffPublisher(fixture.path);

  const first = await publisher.publish(key, identity, media);
  const replay = await publisher.publish(key, identity, media);

  assert.deepEqual(replay, first);
  assert.equal((await lstat(first.recordPath)).mode & 0o777, 0o600);
  assert.equal((await lstat(first.payloadPath)).mode & 0o777, 0o600);
  assert.equal((await readFile(first.payloadPath)).equals(bytes), true);
  const record = JSON.parse(await readFile(first.recordPath, "utf8"));
  assert.deepEqual(Object.keys(record).sort(), [
    "byte_size", "canonical_extension", "canonical_key_hash", "content_hash",
    "detected_mime_type", "payload_basename", "raw_intake_public_id", "schema_version",
  ]);
  assert.equal(record.raw_intake_public_id, identity);
  assert.equal(record.payload_basename, `${identity}.jpg`);
  assert.equal(record.canonical_key_hash.length, 64);
  await assert.rejects(
    publisher.publish(key, identity, {
      ...media,
      bytes: Buffer.from([0xff, 0xd8, 0xff, 9]),
      byteSize: 4,
      contentHash: "8684e6957e3048d61991356349440e439910e42b17658567ba158f4cd10b673d",
    }),
    /conflict/u,
  );
  await assert.rejects(
    publisher.publish(canonicalCaptureKey("111", "21"), identity, media),
    /identity/u,
  );
});

test("retained slot replay supplies a pinned payload without upstream media", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "41");
  const identity = captureIdentities(key).rawIntakePublicId;
  const publisher = new HandoffPublisher(fixture.path);
  await publisher.publish(key, identity, media);
  let callbackCalls = 0;
  const result = await publisher.withRetained(
    key,
    identity,
    async (published, payloadFd, retained) => {
      callbackCalls += 1;
      assert.equal(published.rawIntakePublicId, identity);
      assert.equal(fstatSync(payloadFd).isFile(), true);
      assert.equal(retained.bytes.equals(media.bytes), true);
      assert.equal(retained.contentHash, media.contentHash);
      return "replayed";
    },
  );
  assert.equal(result, "replayed");
  assert.equal(callbackCalls, 1);
});

test("retained replay refuses a foreign incomplete slot before invoking the callback", async () => {
  await using fixture = await workspace();
  const publisher = new HandoffPublisher(fixture.path);
  const key = canonicalCaptureKey("111", "41");
  const identity = captureIdentities(key).rawIntakePublicId;
  await publisher.publish(key, identity, media);
  const foreignKey = canonicalCaptureKey("111", "42");
  const foreignIdentity = captureIdentities(foreignKey).rawIntakePublicId;
  const foreign = await publisher.publish(foreignKey, foreignIdentity, media);
  await unlink(foreign.payloadPath);
  let callbackCalls = 0;
  await assert.rejects(
    publisher.withRetained(key, identity, async () => {
      callbackCalls += 1;
      return "unreachable";
    }),
    /Incomplete handoff residue/u,
  );
  assert.equal(callbackCalls, 0);
});

test("publication failures retain fixed residue and exact restart resumes without deletion", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "21");
  const identity = captureIdentities(key).rawIntakePublicId;
  let injected = false;
  const hook: HandoffHook = async (phase) => {
    if (!injected && phase === "after-payload-fsync") {
      injected = true;
      throw new Error("synthetic crash");
    }
  };
  await assert.rejects(new HandoffPublisher(fixture.path, { hook }).publish(key, identity, media));
  const handoff = join(fixture.path, "handoff");
  assert.equal((await lstat(join(handoff, HANDOFF_PENDING_PAYLOAD))).isFile(), true);
  const recovered = await new HandoffPublisher(fixture.path).publish(key, identity, media);
  assert.equal((await readFile(recovered.payloadPath)).equals(bytes), true);
  await assert.rejects(lstat(join(handoff, HANDOFF_PENDING_PAYLOAD)), /ENOENT/u);
  await assert.rejects(lstat(join(handoff, HANDOFF_PENDING_RECORD)), /ENOENT/u);
});

test("exact pending resume does not reserve the handoff payload twice", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "36");
  const identity = captureIdentities(key).rawIntakePublicId;
  const hook: HandoffHook = async (phase) => {
    if (phase === "after-payload-fsync") throw new Error("synthetic crash");
  };
  await assert.rejects(new HandoffPublisher(fixture.path, { hook }).publish(key, identity, media));
  const resumed = await new HandoffPublisher(fixture.path, {
    freeBytes: async () => media.byteSize + 16 * 1024 * 1024,
  }).publish(key, identity, media);
  assert.equal((await readFile(resumed.payloadPath)).equals(media.bytes), true);
});

test("handoff lock contention is bounded and lock inode replacement is refused", async () => {
  await using fixture = await workspace();
  const firstKey = canonicalCaptureKey("111", "24");
  const firstIdentity = captureIdentities(firstKey).rawIntakePublicId;
  await new HandoffPublisher(fixture.path).publish(firstKey, firstIdentity, media);
  const lockPath = join(fixture.path, "handoff", ".finance-bridge.lock.v1");
  const held = await open(lockPath, "r+");
  await flockPromise(held.fd, "ex");
  try {
    const secondKey = canonicalCaptureKey("111", "25");
    const secondIdentity = captureIdentities(secondKey).rawIntakePublicId;
    await assert.rejects(
      new HandoffPublisher(fixture.path, { lockTimeoutMs: 20 })
        .publish(secondKey, secondIdentity, media),
      /lock deadline/u,
    );
  } finally {
    await flockPromise(held.fd, "un");
    await held.close();
  }

  await assert.rejects(
    new HandoffPublisher(fixture.path).withPublished(
      firstKey,
      firstIdentity,
      media,
      async (published) => {
        await unlink(lockPath);
        await writeFile(lockPath, "replacement", { mode: 0o600 });
        return published;
      },
    ),
    /lock identity/u,
  );

  await using replacementFixture = await workspace();
  const replacementLock = join(
    replacementFixture.path,
    "handoff",
    ".finance-bridge.lock.v1",
  );
  let callbackCalls = 0;
  const replacingPublisher = new HandoffPublisher(replacementFixture.path, {
    hook: async (phase) => {
      if (phase !== "after-payload-publish") return;
      await unlink(replacementLock);
      await writeFile(replacementLock, "replacement", { mode: 0o600 });
    },
  });
  await assert.rejects(
    replacingPublisher.withPublished(
      firstKey,
      firstIdentity,
      media,
      async () => { callbackCalls += 1; },
    ),
    /lock identity/u,
  );
  assert.equal(callbackCalls, 0);
});

test("handoff ancestor replacement after descriptor pinning refuses before publication", async () => {
  await using fixture = await workspace();
  const handoff = join(fixture.path, "handoff");
  const displaced = join(fixture.path, "handoff-displaced");
  const key = canonicalCaptureKey("111", "38");
  const identity = captureIdentities(key).rawIntakePublicId;
  let callbackCalls = 0;
  const hook: HandoffHook = async (phase) => {
    if (phase !== "after-lock") return;
    await rename(handoff, displaced);
    await mkdir(handoff, { mode: 0o700 });
  };
  await assert.rejects(
    new HandoffPublisher(fixture.path, { hook }).withPublished(
      key,
      identity,
      media,
      async () => { callbackCalls += 1; },
    ),
    /directory identity changed/u,
  );
  assert.equal(callbackCalls, 0);
  for (const directory of [handoff, displaced]) {
    await assert.rejects(lstat(join(directory, `${identity}.handoff.json`)), /ENOENT/u);
    await assert.rejects(lstat(join(directory, `${identity}.jpg`)), /ENOENT/u);
  }
});

test("foreign pending created after atomic publication is preserved and refused", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "27");
  const identity = captureIdentities(key).rawIntakePublicId;
  const hook: HandoffHook = async (phase) => {
    if (phase !== "after-payload-publish") return;
    const pending = join(fixture.path, "handoff", HANDOFF_PENDING_PAYLOAD);
    await writeFile(pending, "foreign", { mode: 0o600 });
  };
  await assert.rejects(
    new HandoffPublisher(fixture.path, { hook }).publish(key, identity, media),
    /unexpected pending/u,
  );
  assert.equal(await readFile(
    join(fixture.path, "handoff", HANDOFF_PENDING_PAYLOAD),
    "utf8",
  ), "foreign");
});

test("pending content mutation after publication is refused and retained", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "33");
  const identity = captureIdentities(key).rawIntakePublicId;
  const hook: HandoffHook = async (phase) => {
    if (phase !== "after-payload-fsync") return;
    await writeFile(
      join(fixture.path, "handoff", HANDOFF_PENDING_PAYLOAD),
      Buffer.from([0xff, 0xd8, 0xff, 9, 9, 9, 9]),
      { mode: 0o600 },
    );
  };
  await assert.rejects(
    new HandoffPublisher(fixture.path, { hook }).publish(key, identity, media),
    /content changed/u,
  );
  assert.equal((await lstat(
    join(fixture.path, "handoff", HANDOFF_PENDING_PAYLOAD),
  )).isFile(), true);
});

test("final payload mutation before capture refuses without invoking the callback", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "34");
  const identity = captureIdentities(key).rawIntakePublicId;
  let callbackCalls = 0;
  const hook: HandoffHook = async (phase) => {
    if (phase !== "after-payload-publish") return;
    await writeFile(
      join(fixture.path, "handoff", `${identity}.jpg`),
      Buffer.from([0xff, 0xd8, 0xff, 9, 9, 9, 9]),
      { mode: 0o600 },
    );
  };
  await assert.rejects(
    new HandoffPublisher(fixture.path, { hook }).withPublished(
      key,
      identity,
      media,
      async () => { callbackCalls += 1; },
    ),
    /content changed/u,
  );
  assert.equal(callbackCalls, 0);
});

test("final record identity mutation before capture refuses without invoking callback", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "37");
  const identity = captureIdentities(key).rawIntakePublicId;
  let callbackCalls = 0;
  const hook: HandoffHook = async (phase) => {
    if (phase !== "after-payload-publish") return;
    const recordPath = join(fixture.path, "handoff", `${identity}.handoff.json`);
    const record = JSON.parse(await readFile(recordPath, "utf8"));
    record.canonical_key_hash = "0".repeat(64);
    await writeFile(recordPath, `${JSON.stringify(record)}\n`, { mode: 0o600 });
  };
  await assert.rejects(
    new HandoffPublisher(fixture.path, { hook }).withPublished(
      key,
      identity,
      media,
      async () => { callbackCalls += 1; },
    ),
    /record changed/u,
  );
  assert.equal(callbackCalls, 0);
});

test("publication identities reject byte-identical replacement before fresh or retained callbacks", async () => {
  for (const mode of ["fresh", "retained"] as const) {
    for (const target of ["record", "payload"] as const) {
      await using fixture = await workspace();
      const messageId = mode === "fresh"
        ? (target === "record" ? "45" : "46")
        : (target === "record" ? "47" : "48");
      const key = canonicalCaptureKey("111", messageId);
      const identity = captureIdentities(key).rawIntakePublicId;
      const original = new HandoffPublisher(fixture.path);
      if (mode === "retained") await original.publish(key, identity, media);
      let callbackCalls = 0;
      const replaceTarget = async () => {
        const name = target === "record" ? `${identity}.handoff.json` : `${identity}.jpg`;
        const path = join(fixture.path, "handoff", name);
        const content = await readFile(path);
        await unlink(path);
        await writeFile(path, content, { mode: 0o600 });
      };
      const publisher = new HandoffPublisher(fixture.path, {
        hook: async (phase) => {
          const freshPhase = target === "record" ? "after-record-pin" : "after-payload-pin";
          if ((mode === "fresh" && phase === freshPhase) ||
              (mode === "retained" && phase === "before-callback")) {
            await replaceTarget();
          }
        },
      });
      const operation = mode === "fresh"
        ? publisher.withPublished(key, identity, media, async () => { callbackCalls += 1; })
        : publisher.withRetained(key, identity, async () => { callbackCalls += 1; });
      await assert.rejects(operation, /inode identity changed/u);
      assert.equal(callbackCalls, 0);
    }
  }
});

test("callback-window payload mutation is refused before successful handoff return", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "39");
  const identity = captureIdentities(key).rawIntakePublicId;
  await assert.rejects(
    new HandoffPublisher(fixture.path).withPublished(
      key,
      identity,
      media,
      async (published) => {
        await writeFile(
          published.payloadPath,
          Buffer.from([0xff, 0xd8, 0xff, 9, 9, 9, 9]),
          { mode: 0o600 },
        );
        return "durable-child-result";
      },
    ),
    /changed before capture/u,
  );
});

test("callback-window record mutation is refused before successful handoff return", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "40");
  const identity = captureIdentities(key).rawIntakePublicId;
  await assert.rejects(
    new HandoffPublisher(fixture.path).withPublished(
      key,
      identity,
      media,
      async (published) => {
        const record = JSON.parse(await readFile(published.recordPath, "utf8"));
        record.canonical_key_hash = "0".repeat(64);
        await writeFile(published.recordPath, `${JSON.stringify(record)}\n`, { mode: 0o600 });
        return "durable-child-result";
      },
    ),
    /record changed/u,
  );
});

test("callback-window inventory and byte-identical inode replacement are refused", async () => {
  await using freshFixture = await workspace();
  const freshKey = canonicalCaptureKey("111", "43");
  const freshIdentity = captureIdentities(freshKey).rawIntakePublicId;
  await assert.rejects(
    new HandoffPublisher(freshFixture.path).withPublished(
      freshKey,
      freshIdentity,
      media,
      async () => {
        await writeFile(join(freshFixture.path, "handoff", "foreign"), "residue", {
          mode: 0o600,
        });
        return "durable-child-result";
      },
    ),
    /inventory changed/u,
  );

  await using retainedFixture = await workspace();
  const retainedKey = canonicalCaptureKey("111", "44");
  const retainedIdentity = captureIdentities(retainedKey).rawIntakePublicId;
  const retainedPublisher = new HandoffPublisher(retainedFixture.path);
  const retained = await retainedPublisher.publish(retainedKey, retainedIdentity, media);
  await assert.rejects(
    retainedPublisher.withRetained(
      retainedKey,
      retainedIdentity,
      async () => {
        const payload = await readFile(retained.payloadPath);
        await unlink(retained.payloadPath);
        await writeFile(retained.payloadPath, payload, { mode: 0o600 });
        return "durable-child-result";
      },
    ),
    /inode identity changed/u,
  );
});

test("replay refuses non-private final record or payload without repairing it", async () => {
  for (const target of ["recordPath", "payloadPath"] as const) {
    await using fixture = await workspace();
    const key = canonicalCaptureKey("111", target === "recordPath" ? "28" : "29");
    const identity = captureIdentities(key).rawIntakePublicId;
    const published = await new HandoffPublisher(fixture.path).publish(key, identity, media);
    await chmod(published[target], 0o644);
    await assert.rejects(
      new HandoffPublisher(fixture.path).publish(key, identity, media),
      /private/u,
    );
    assert.equal((await lstat(published[target])).mode & 0o777, 0o644);
  }
});

test("unknown entries, symlinks, and foreign residue block before publication", async () => {
  for (const setup of [
    async (handoff: string) => await writeFile(join(handoff, "unknown"), "x", { mode: 0o600 }),
    async (handoff: string) => await symlink("elsewhere", join(handoff, "unknown-link")),
    async (handoff: string) => await writeFile(join(handoff, HANDOFF_PENDING_RECORD), "foreign", { mode: 0o600 }),
  ]) {
    await using fixture = await workspace();
    const handoff = join(fixture.path, "handoff");
    await mkdir(handoff, { mode: 0o700 });
    await setup(handoff);
    const key = canonicalCaptureKey("111", "22");
    const identity = captureIdentities(key).rawIntakePublicId;
    await assert.rejects(new HandoffPublisher(fixture.path).publish(key, identity, media), /unknown|symlink|residue/u);
  }
});

test("foreign payload pending blocks before publishing a slot record", async () => {
  await using fixture = await workspace();
  const handoff = join(fixture.path, "handoff");
  await mkdir(handoff, { mode: 0o700 });
  await writeFile(join(handoff, HANDOFF_PENDING_PAYLOAD), "foreign", { mode: 0o600 });
  const key = canonicalCaptureKey("111", "26");
  const identity = captureIdentities(key).rawIntakePublicId;
  await assert.rejects(
    new HandoffPublisher(fixture.path).publish(key, identity, media),
    /residue/u,
  );
  await assert.rejects(
    lstat(join(handoff, `${identity}.handoff.json`)),
    /ENOENT/u,
  );
});

test("orphan exact payload pending blocks before publishing a slot record", async () => {
  await using fixture = await workspace();
  const handoff = join(fixture.path, "handoff");
  await mkdir(handoff, { mode: 0o700 });
  await writeFile(join(handoff, HANDOFF_PENDING_PAYLOAD), media.bytes, { mode: 0o600 });
  const key = canonicalCaptureKey("111", "35");
  const identity = captureIdentities(key).rawIntakePublicId;
  await assert.rejects(
    new HandoffPublisher(fixture.path).publish(key, identity, media),
    /orphan.*residue/iu,
  );
  await assert.rejects(
    lstat(join(handoff, `${identity}.handoff.json`)),
    /ENOENT/u,
  );
});

test("inventory rejects an existing payload hash tamper before any new slot write", async () => {
  await using fixture = await workspace();
  const oldKey = canonicalCaptureKey("111", "30");
  const oldIdentity = captureIdentities(oldKey).rawIntakePublicId;
  const old = await new HandoffPublisher(fixture.path).publish(oldKey, oldIdentity, media);
  await writeFile(old.payloadPath, Buffer.from([0xff, 0xd8, 0xff, 9, 9, 9, 9]), { mode: 0o600 });

  const newKey = canonicalCaptureKey("111", "31");
  const newIdentity = captureIdentities(newKey).rawIntakePublicId;
  await assert.rejects(
    new HandoffPublisher(fixture.path).publish(newKey, newIdentity, media),
    /does not match/u,
  );
  await assert.rejects(
    lstat(join(fixture.path, "handoff", `${newIdentity}.handoff.json`)),
    /ENOENT/u,
  );
});

test("complete replay still refuses an unknown workspace entry", async () => {
  await using fixture = await workspace();
  const key = canonicalCaptureKey("111", "32");
  const identity = captureIdentities(key).rawIntakePublicId;
  const publisher = new HandoffPublisher(fixture.path);
  await publisher.publish(key, identity, media);
  await writeFile(join(fixture.path, "handoff", "unknown"), "x", { mode: 0o600 });
  await assert.rejects(publisher.publish(key, identity, media), /unknown/u);
});

test("record-count, payload-byte, tree-byte, and free-space quotas fail closed", async () => {
  await using fixture = await workspace();
  const handoff = join(fixture.path, "handoff");
  await mkdir(handoff, { mode: 0o700 });
  for (let index = 0; index < 32; index += 1) {
    const id = `raw_intake_bridge_${index.toString(16).padStart(32, "0")}`;
    await writeFile(join(handoff, `${id}.handoff.json`), JSON.stringify({
      schema_version: "finance-bridge-handoff-v1",
      raw_intake_public_id: id,
      canonical_key_hash: "1".repeat(64),
      content_hash: createHash("sha256").update(Buffer.from([0xff, 0xd8, 0xff])).digest("hex"),
      byte_size: 3,
      detected_mime_type: "image/jpeg",
      canonical_extension: ".jpg",
      payload_basename: `${id}.jpg`,
    }), { mode: 0o600 });
    await writeFile(join(handoff, `${id}.jpg`), Buffer.from([0xff, 0xd8, 0xff]), { mode: 0o600 });
  }
  const key = canonicalCaptureKey("111", "23");
  const identity = captureIdentities(key).rawIntakePublicId;
  await assert.rejects(new HandoffPublisher(fixture.path).publish(key, identity, media), /32/u);

  await using payloadFixture = await workspace();
  const payloadHandoff = join(payloadFixture.path, "handoff");
  await mkdir(payloadHandoff, { mode: 0o700 });
  const quotaId = "raw_intake_bridge_00000000000000000000000000000000";
  await writeFile(join(payloadHandoff, `${quotaId}.handoff.json`), JSON.stringify({
    schema_version: "finance-bridge-handoff-v1",
    raw_intake_public_id: quotaId,
    canonical_key_hash: "1".repeat(64),
    content_hash: "2".repeat(64),
    byte_size: 320_000_000,
    detected_mime_type: "image/jpeg",
    canonical_extension: ".jpg",
    payload_basename: `${quotaId}.jpg`,
  }), { mode: 0o600 });
  await writeFile(join(payloadHandoff, `${quotaId}.jpg`), "", { mode: 0o600 });
  await truncate(join(payloadHandoff, `${quotaId}.jpg`), 320_000_000);
  await assert.rejects(
    new HandoffPublisher(payloadFixture.path).publish(key, identity, media),
    /receipt byte limit/u,
  );

  await using freeFixture = await workspace();
  await assert.rejects(new HandoffPublisher(freeFixture.path, {
    freeBytes: async () => 2 * media.byteSize + 16 * 1024 * 1024 - 1,
  }).publish(key, identity, media), /free space/u);
});
