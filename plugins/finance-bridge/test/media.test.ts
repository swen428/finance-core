import assert from "node:assert/strict";
import { chmod, mkdir, stat, symlink, truncate, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

import { ReceiptMediaAdapter } from "../src/media.js";
import { temporaryDirectory } from "./support.js";

const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x01, 0x02]);
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x01]);

function hostFields(path: string, type: string) {
  return {
    mediaPath: path, mediaUrl: path, mediaPaths: [path], mediaUrls: [path],
    mediaType: type, mediaTypes: [type],
  };
}

test("trusted host six-field 0644 JPEG/PNG path reads originals without changing permissions", async () => {
  await using fixture = await mediaFixture();
  const adapter = new ReceiptMediaAdapter(() => fixture.root);
  for (const [name, bytes, type] of [
    ["host-photo.jpg", JPEG, "image/jpeg"], ["host-photo.png", PNG, "image/png"],
  ] as const) {
    const path = join(fixture.inbound, name);
    await writeFile(path, bytes, { mode: 0o644 });
    await chmod(path, 0o644);
    const media = await adapter.acquireTrustedInbound(hostFields(path, type));
    assert.equal(media.detectedMimeType, type);
    assert.equal(media.bytes.equals(bytes), true);
    assert.equal((await stat(path)).mode & 0o777, 0o644);
    await assert.rejects(adapter.acquire(hostFields(path, type)), /Direct media paths/u);
    if (path.startsWith("/var/")) {
      const alias = `/private${path}`;
      assert.equal((await adapter.acquireTrustedInbound(hostFields(alias, type))).bytes.equals(bytes), true);
    } else if (path.startsWith("/private/var/")) {
      const alias = path.slice("/private".length);
      assert.equal((await adapter.acquireTrustedInbound(hostFields(alias, type))).bytes.equals(bytes), true);
    }
  }
});

test("trusted host path reader rejects external, alias-conflicting, plural, and pending media", async () => {
  await using fixture = await mediaFixture();
  const adapter = new ReceiptMediaAdapter(() => fixture.root);
  const path = join(fixture.inbound, "host-photo.jpg");
  await writeFile(path, JPEG, { mode: 0o644 });
  await chmod(path, 0o644);
  const outside = join(fixture.root, "outside.jpg");
  await writeFile(outside, JPEG, { mode: 0o644 });
  for (const metadata of [
    hostFields(outside, "image/jpeg"),
    hostFields(`${fixture.inbound}/../outside.jpg`, "image/jpeg"),
    { ...hostFields(path, "image/jpeg"), mediaUrl: outside },
    { ...hostFields(path, "image/jpeg"), mediaPaths: [path, path] },
    { ...hostFields(path, "image/jpeg"), mediaUrls: [] },
    { ...hostFields(path, "image/jpeg"), mediaTypes: ["image/png"] },
    { ...hostFields(path, "image/jpeg"), mediaStagingPending: true },
    { ...hostFields(path, "image/jpeg"), mediaPath: undefined },
  ]) {
    await assert.rejects(adapter.acquireTrustedInbound(metadata), /trusted receipt|Trusted receipt/iu);
  }
});

test("trusted host reader rejects leaf symlink, unsafe mode, directory drift, and MIME magic mismatch", async () => {
  await using fixture = await mediaFixture();
  const adapter = new ReceiptMediaAdapter(() => fixture.root);
  const valid = join(fixture.inbound, "valid.jpg");
  await writeFile(valid, JPEG, { mode: 0o644 });
  await chmod(valid, 0o644);
  const link = join(fixture.inbound, "link.jpg");
  await symlink("valid.jpg", link);
  await assert.rejects(adapter.acquireTrustedInbound(hostFields(link, "image/jpeg")), /openat|symbolic/iu);
  for (const mode of [0o664, 0o755, 0o640]) {
    await chmod(valid, mode);
    await assert.rejects(adapter.acquireTrustedInbound(hostFields(valid, "image/jpeg")), /owner-controlled/u);
  }
  await chmod(valid, 0o644);
  await chmod(fixture.inbound, 0o755);
  await assert.rejects(adapter.acquireTrustedInbound(hostFields(valid, "image/jpeg")), /0700/u);
  await chmod(fixture.inbound, 0o700);
  await assert.rejects(adapter.acquireTrustedInbound(hostFields(valid, "image/png")), /MIME/u);
});

async function mediaFixture(): Promise<AsyncDisposable & {root: string; inbound: string}> {
  const fixture = await temporaryDirectory();
  const root = join(fixture.path, "media");
  const inbound = join(root, "inbound");
  await mkdir(inbound, { recursive: true, mode: 0o700 });
  await chmod(root, 0o700);
  await chmod(inbound, 0o700);
  return {
    root,
    inbound,
    async [Symbol.asyncDispose]() { await fixture[Symbol.asyncDispose](); },
  };
}

test("media adapter reads one safe opaque media id through pinned directory descriptors", async () => {
  await using fixture = await mediaFixture();
  await writeFile(join(fixture.inbound, "receipt_123"), JPEG, { mode: 0o600 });
  const adapter = new ReceiptMediaAdapter(() => fixture.root);
  const media = await adapter.acquire({
    mediaUrl: "media://inbound/receipt_123",
    mediaType: "image/jpeg",
    originalFilename: "receipt.jpg",
  });
  assert.equal(media.detectedMimeType, "image/jpeg");
  assert.equal(media.canonicalExtension, ".jpg");
  assert.equal(media.bytes.equals(JPEG), true);
  assert.match(media.contentHash, /^[0-9a-f]{64}$/u);
});

test("media adapter accepts PNG and refuses URL, path, traversal, multiplicity, and mismatch", async () => {
  await using fixture = await mediaFixture();
  await writeFile(join(fixture.inbound, "png"), PNG, { mode: 0o600 });
  await writeFile(join(fixture.inbound, "jpeg"), JPEG, { mode: 0o600 });
  const adapter = new ReceiptMediaAdapter(() => fixture.root);
  const png = await adapter.acquire({
    mediaUrl: "media://inbound/png",
    mediaType: "image/png",
    originalFilename: "receipt.png",
  });
  assert.equal(png.detectedMimeType, "image/png");

  for (const metadata of [
    {},
    { mediaUrl: "https://example.test/receipt.jpg", mediaType: "image/jpeg" },
    { mediaPath: "/tmp/receipt.jpg", mediaType: "image/jpeg" },
    { mediaUrl: "media://inbound/../receipt", mediaType: "image/jpeg" },
    { mediaUrl: "media://inbound/a%2Fb", mediaType: "image/jpeg" },
    { mediaUrls: ["media://inbound/a", "media://inbound/b"], mediaTypes: ["image/jpeg", "image/jpeg"] },
    { mediaUrl: "media://inbound/a", mediaUrls: ["media://inbound/a"], mediaType: "image/jpeg" },
    { mediaUrl: "media://inbound/a", mediaType: "image/jpeg", mediaTypes: ["image/jpeg"] },
    { mediaUrl: 1, mediaType: "image/jpeg" },
    { mediaUrls: "media://inbound/a", mediaType: "image/jpeg" },
    { mediaUrl: "media://inbound/a", mediaTypes: [1] },
    { mediaUrl: "media://inbound/a", mediaType: "image/jpeg", originalFilename: 1 },
    { mediaUrl: "media://inbound/jpeg", mediaType: "image/png", originalFilename: "receipt.png" },
    { mediaUrl: "media://inbound/jpeg", mediaType: "image/jpeg", originalFilename: "receipt.png" },
    { mediaStagingPending: true, mediaUrl: "media://inbound/jpeg", mediaType: "image/jpeg" },
  ]) {
    await assert.rejects(adapter.acquire(metadata), /media|attachment|MIME|extension|pending|filename/iu);
  }
});

test("media adapter refuses malformed or pending staging state before reading media", async () => {
  await using fixture = await mediaFixture();
  const adapter = new ReceiptMediaAdapter(() => fixture.root);
  for (const mediaStagingPending of ["true", 1, null, true]) {
    await assert.rejects(
      adapter.acquire({
        mediaUrl: "media://inbound/jpeg",
        mediaType: "image/jpeg",
        mediaStagingPending,
      }),
      /staging state|still pending/u,
    );
  }
});

test("media adapter refuses invalid or overlong filenames before reading media", async () => {
  await using fixture = await mediaFixture();
  await writeFile(join(fixture.inbound, "jpeg"), JPEG, { mode: 0o600 });
  const adapter = new ReceiptMediaAdapter(() => fixture.root);
  for (const originalFilename of [
    `receipt\ud800.jpg`,
    `${"a".repeat(254)}😀.jpg`,
    `${"a".repeat(256)}.jpg`,
  ]) {
    await assert.rejects(
      adapter.acquire({
        mediaUrl: "media://inbound/jpeg",
        mediaType: "image/jpeg",
        originalFilename,
      }),
      /Unicode scalar|bounded limit/u,
    );
  }
});

test("media adapter refuses symlink, non-private, and oversized entries without fallback", async () => {
  await using fixture = await mediaFixture();
  const adapter = new ReceiptMediaAdapter(() => fixture.root);
  await symlink("outside", join(fixture.inbound, "link"));
  await writeFile(join(fixture.inbound, "public"), JPEG, { mode: 0o644 });
  await writeFile(join(fixture.inbound, "large"), JPEG, { mode: 0o600 });
  await truncate(join(fixture.inbound, "large"), 10_000_001);
  for (const id of ["link", "public", "large"]) {
    await assert.rejects(
      adapter.acquire({ mediaUrl: `media://inbound/${id}`, mediaType: "image/jpeg" }),
      /openat|private|bounded|symbolic/u,
    );
  }
});
