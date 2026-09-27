import assert from "node:assert/strict";
import { lstat, mkdir, readFile, rename, symlink, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

import {
  chmodDescriptor,
  closeDescriptor,
  constants,
  descriptorIdentitySync,
  openDirectory,
  openFileAt,
  openPrivateDirectoryAt,
  readDescriptor,
  renameNoReplaceAt,
  syncDescriptor,
  unlinkAtIfIdentity,
  writeDescriptor,
} from "../src/posix.js";
import { temporaryDirectory } from "./support.js";

test("native POSIX boundary publishes by descriptor-relative atomic no-replace rename", async () => {
  await using fixture = await temporaryDirectory();
  const workspace = join(fixture.path, "workspace");
  await mkdir(workspace, { mode: 0o700 });
  const workspaceFd = openDirectory(workspace);
  const directoryFd = openPrivateDirectoryAt(workspaceFd, "handoff");
  await closeDescriptor(workspaceFd);
  const pendingFd = openFileAt(
    directoryFd,
    ".pending",
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
    0o600,
  );
  await writeDescriptor(pendingFd, Buffer.from("evidence"));
  await chmodDescriptor(pendingFd, 0o600);
  await syncDescriptor(pendingFd);
  await closeDescriptor(pendingFd);

  renameNoReplaceAt(directoryFd, ".pending", "final");
  await syncDescriptor(directoryFd);
  await assert.rejects(lstat(join(workspace, "handoff", ".pending")), /ENOENT/u);
  assert.equal(await readFile(join(workspace, "handoff", "final"), "utf8"), "evidence");

  const secondFd = openFileAt(
    directoryFd,
    ".second",
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
    0o600,
  );
  await writeDescriptor(secondFd, Buffer.from("other"));
  await closeDescriptor(secondFd);
  assert.throws(() => renameNoReplaceAt(directoryFd, ".second", "final"), /exists/iu);
  assert.throws(
    () => openFileAt(directoryFd, "../escape", constants.O_RDONLY),
    /directory-entry/u,
  );
  await closeDescriptor(directoryFd);
});

test("descriptor read refuses same-size in-place mutation after identity capture", async () => {
  await using fixture = await temporaryDirectory();
  const directoryFd = openDirectory(fixture.path);
  const writerFd = openFileAt(
    directoryFd,
    "evidence",
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
    0o600,
  );
  await writeDescriptor(writerFd, Buffer.from("original"));
  await syncDescriptor(writerFd);
  await closeDescriptor(writerFd);
  const readerFd = openFileAt(directoryFd, "evidence", constants.O_RDONLY);
  try {
    await assert.rejects(readDescriptor(readerFd, 100, async () => {
      const mutatorFd = openFileAt(directoryFd, "evidence", constants.O_WRONLY);
      await writeDescriptor(mutatorFd, Buffer.from("mutated!"));
      await syncDescriptor(mutatorFd);
      await closeDescriptor(mutatorFd);
    }), /changed during read/u);
  } finally {
    await closeDescriptor(readerFd);
    await closeDescriptor(directoryFd);
  }
});

test("identity-bound unlink removes the expected regular file relative to its pinned directory", async () => {
  await using fixture = await temporaryDirectory();
  const directoryFd = openDirectory(fixture.path);
  const writerFd = openFileAt(
    directoryFd,
    "evidence",
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
    0o600,
  );
  let identity;
  try {
    await writeDescriptor(writerFd, Buffer.from("evidence"));
    await syncDescriptor(writerFd);
    identity = descriptorIdentitySync(writerFd);
  } finally {
    await closeDescriptor(writerFd);
  }
  try {
    unlinkAtIfIdentity(directoryFd, "evidence", identity);
    await assert.rejects(lstat(join(fixture.path, "evidence")), /ENOENT/u);
  } finally {
    await closeDescriptor(directoryFd);
  }
});

test("identity-bound unlink refuses a replaced entry and preserves the replacement", async () => {
  await using fixture = await temporaryDirectory();
  const directoryFd = openDirectory(fixture.path);
  const originalFd = openFileAt(
    directoryFd,
    "evidence",
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
    0o600,
  );
  try {
    await writeDescriptor(originalFd, Buffer.from("original"));
    await syncDescriptor(originalFd);
    const expectedIdentity = descriptorIdentitySync(originalFd);
    const replacementFd = openFileAt(
      directoryFd,
      "replacement",
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
      0o600,
    );
    await writeDescriptor(replacementFd, Buffer.from("replacement"));
    await syncDescriptor(replacementFd);
    await closeDescriptor(replacementFd);
    await unlink(join(fixture.path, "evidence"));
    await rename(join(fixture.path, "replacement"), join(fixture.path, "evidence"));

    assert.throws(
      () => unlinkAtIfIdentity(directoryFd, "evidence", expectedIdentity),
      /identity changed/u,
    );
    assert.equal(await readFile(join(fixture.path, "evidence"), "utf8"), "replacement");
  } finally {
    await closeDescriptor(originalFd);
    await closeDescriptor(directoryFd);
  }
});

test("identity-bound unlink refuses symlinks without touching their targets", async () => {
  await using fixture = await temporaryDirectory();
  const target = join(fixture.path, "target");
  const link = join(fixture.path, "link");
  await writeFile(target, "keep", { mode: 0o600 });
  await symlink("target", link);
  const linkStatus = await lstat(link, { bigint: true });
  const expectedIdentity = {
    dev: linkStatus.dev,
    ino: linkStatus.ino,
    uid: Number(linkStatus.uid),
    mode: Number(linkStatus.mode),
    size: Number(linkStatus.size),
    ctimeNs: linkStatus.ctimeNs,
    mtimeNs: linkStatus.mtimeNs,
  };
  const directoryFd = openDirectory(fixture.path);
  try {
    assert.throws(
      () => unlinkAtIfIdentity(directoryFd, "link", expectedIdentity),
      /symlink/u,
    );
    assert.equal((await lstat(link)).isSymbolicLink(), true);
    assert.equal(await readFile(target, "utf8"), "keep");
  } finally {
    await closeDescriptor(directoryFd);
  }
});

test("identity-bound unlink rejects special basenames before touching entries", async () => {
  await using fixture = await temporaryDirectory();
  await writeFile(join(fixture.path, "evidence"), "keep", { mode: 0o600 });
  const directoryFd = openDirectory(fixture.path);
  const fileFd = openFileAt(directoryFd, "evidence", constants.O_RDONLY);
  try {
    const identity = descriptorIdentitySync(fileFd);
    for (const name of ["", ".", "..", "../outside", "nested/entry", "bad\0name"]) {
      assert.throws(() => unlinkAtIfIdentity(directoryFd, name, identity), /basename/u);
    }
    assert.equal(await readFile(join(fixture.path, "evidence"), "utf8"), "keep");
  } finally {
    await closeDescriptor(fileFd);
    await closeDescriptor(directoryFd);
  }
});

test("identity-bound unlink cannot reach a file outside the pinned directory", async () => {
  await using fixture = await temporaryDirectory();
  const outside = join(fixture.path, "outside");
  const nested = join(fixture.path, "private");
  await writeFile(outside, "keep", { mode: 0o600 });
  await mkdir(nested, { mode: 0o700 });
  const parentFd = openDirectory(fixture.path);
  const outsideFd = openFileAt(parentFd, "outside", constants.O_RDONLY);
  const nestedFd = openPrivateDirectoryAt(parentFd, "private");
  try {
    const outsideIdentity = descriptorIdentitySync(outsideFd);
    assert.throws(
      () => unlinkAtIfIdentity(nestedFd, "outside", outsideIdentity),
      (error: unknown) => (error as NodeJS.ErrnoException).code === "ENOENT",
    );
    assert.equal(await readFile(outside, "utf8"), "keep");
  } finally {
    await closeDescriptor(outsideFd);
    await closeDescriptor(nestedFd);
    await closeDescriptor(parentFd);
  }
});
