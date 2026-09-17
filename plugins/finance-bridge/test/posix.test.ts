import assert from "node:assert/strict";
import { lstat, mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

import {
  chmodDescriptor,
  closeDescriptor,
  constants,
  openDirectory,
  openFileAt,
  openPrivateDirectoryAt,
  readDescriptor,
  renameNoReplaceAt,
  syncDescriptor,
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
