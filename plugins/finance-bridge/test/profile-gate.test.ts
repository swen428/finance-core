import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { constants, openSync, closeSync } from "node:fs";
import { chmod, link, mkdtemp, realpath, rename, rm, stat, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test, { type TestContext } from "node:test";
import { setTimeout as delay } from "node:timers/promises";

import {
  initializeProfileGate,
  isSharedProfileGateLease,
  openProfileGate,
  PROFILE_GATE_BASENAME,
} from "../src/profile-gate.js";
import { rejectAclGrants } from "../src/posix.js";

async function privateRoot(t: TestContext): Promise<string> {
  const root = await realpath(await mkdtemp(join(tmpdir(), "finance-profile-gate-")));
  t.after(async () => rm(root, { recursive: true, force: true }));
  return root;
}

const CHILD_LOCK_PROBE = [
  "const fs=require('node:fs');",
  "const {flock}=require('fs-ext');",
  "const fd=fs.openSync(process.argv[1], 'r+');",
  "flock(fd,process.argv[2],error=>{console.log(error?.code??'held');fs.closeSync(fd);});",
].join("");

function childTryLock(path: string, operation: "shnb" | "exnb"): string {
  const child = spawnSync(
    process.execPath,
    ["--input-type=commonjs", "-e", CHILD_LOCK_PROBE, path, operation],
    { cwd: resolve("."), encoding: "utf8", timeout: 3_000 },
  );
  assert.equal(child.status, 0, child.stderr);
  return child.stdout.trim();
}

test("profile gate requires explicit owner-only initialization and fixed identity", async (t) => {
  const root = await privateRoot(t);
  const lockPath = join(root, PROFILE_GATE_BASENAME);
  assert.throws(() => openProfileGate(root));
  initializeProfileGate(root);
  assert.equal((await stat(lockPath)).mode & 0o777, 0o600);
  assert.throws(() => initializeProfileGate(root), /exists/u);
  const gate = openProfileGate(root);
  gate.close();

  await chmod(lockPath, 0o644);
  assert.throws(() => openProfileGate(root), /private empty/u);
  await chmod(lockPath, 0o600);
  await link(lockPath, join(root, "alias"));
  assert.throws(() => openProfileGate(root), /single-link/u);
});

test("failed gate initialization preserves an unusable mode-000 lock", async (t) => {
  const root = await privateRoot(t);
  const program = [
    "import fs from 'node:fs';",
    "import {syncBuiltinESMExports} from 'node:module';",
    "import {pathToFileURL} from 'node:url';",
    "fs.fsyncSync=()=>{throw new Error('injected fsync failure');};",
    "syncBuiltinESMExports();",
    "const gate=await import(pathToFileURL(process.cwd()+'/dist/src/profile-gate.js').href);",
    "try { gate.initializeProfileGate(process.argv[1]); process.exitCode=2; }",
    "catch(error) { if(!String(error).includes('injected fsync failure')) process.exitCode=3; }",
  ].join("");
  const child = spawnSync(process.execPath, ["--input-type=module", "-e", program, root], {
    cwd: resolve("."), encoding: "utf8", timeout: 3_000,
  });
  assert.equal(child.status, 0, child.stderr);
  assert.equal((await stat(join(root, PROFILE_GATE_BASENAME))).mode & 0o777, 0);
  assert.throws(() => openProfileGate(root), /Permission denied|private empty/u);
});

test("profile gate rejects a symbolic lock and replacement of a pinned inode", async (t) => {
  const root = await privateRoot(t);
  initializeProfileGate(root);
  const gate = openProfileGate(root);
  const path = join(root, PROFILE_GATE_BASENAME);
  await rename(path, join(root, "old-lock"));
  const replacement = openSync(path, constants.O_CREAT | constants.O_EXCL | constants.O_RDWR, 0o600);
  closeSync(replacement);
  await assert.rejects(gate.acquireShared(100), /identity changed/u);
  gate.close();
  await rm(path);
  await symlink(join(root, "old-lock"), path);
  assert.throws(() => openProfileGate(root));
});

test("pinned native ACL probe rejects grants and accepts deny-only ACLs on macOS", async (t) => {
  const root = await privateRoot(t);
  initializeProfileGate(root);
  const path = join(root, PROFILE_GATE_BASENAME);
  const fd = openSync(path, constants.O_RDONLY);
  try {
    assert.doesNotThrow(() => rejectAclGrants(fd));
    assert.throws(() => rejectAclGrants(-1));
    if (process.platform !== "darwin") return;

    execFileSync("chmod", ["+a", "everyone allow read", path]);
    assert.throws(() => rejectAclGrants(fd), /ACL grant/u);
    assert.throws(() => openProfileGate(root), /ACL grant/u);
    execFileSync("chmod", ["-N", path]);
    execFileSync("chmod", ["+a", "everyone deny delete", path]);
    assert.doesNotThrow(() => rejectAclGrants(fd));
    openProfileGate(root).close();
    execFileSync("chmod", ["+a", "everyone allow read", root]);
    assert.throws(() => openProfileGate(root), /ACL grant/u);
    execFileSync("chmod", ["-N", root]);
  } finally {
    closeSync(fd);
    if (process.platform === "darwin") {
      execFileSync("chmod", ["-N", root]);
      execFileSync("chmod", ["-N", path]);
    }
  }
});

test("profile gate shared and exclusive locks contend across processes without upgrades", async (t) => {
  const root = await privateRoot(t);
  initializeProfileGate(root);
  const gate = openProfileGate(root);
  const path = join(root, PROFILE_GATE_BASENAME);
  try {
    const shared = await gate.acquireShared(1_000);
    assert.equal(isSharedProfileGateLease(shared), true);
    assert.equal(childTryLock(path, "shnb"), "held");
    assert.match(childTryLock(path, "exnb"), /EAGAIN|EWOULDBLOCK/u);
    await assert.rejects(gate.acquireExclusive(80), /deadline exceeded/u);
    assert.throws(() => gate.close(), /active leases/u);
    shared.close();

    const exclusive = await gate.acquireExclusive(1_000, 1_000);
    exclusive.assertValid();
    assert.match(childTryLock(path, "shnb"), /EAGAIN|EWOULDBLOCK/u);
    exclusive.close();
    assert.throws(() => exclusive.assertValid(), /closed/u);
    assert.equal(childTryLock(path, "exnb"), "held");
  } finally {
    gate.close();
  }
});

test("exclusive profile cut checks its cooperative bounded hold deadline", async (t) => {
  const root = await privateRoot(t);
  initializeProfileGate(root);
  const gate = openProfileGate(root);
  await assert.rejects(gate.acquireExclusive(100, 30_001), /hold must/u);
  const cut = await gate.acquireExclusive(1_000, 1);
  await delay(10);
  assert.throws(() => cut.assertValid(), /hold deadline exceeded/u);
  cut.close();
  gate.close();
});

test("caller-owned shared lease cannot close until the child is reaped", async (t) => {
  const root = await privateRoot(t);
  initializeProfileGate(root);
  const gate = openProfileGate(root);
  const lease = await gate.acquireShared(100);
  lease.bindChild();
  assert.throws(() => lease.close(), /child reap/u);
  assert.throws(() => lease.bindChild(), /already bound/u);
  lease.unbindChild();
  assert.match(childTryLock(join(root, PROFILE_GATE_BASENAME), "exnb"), /EAGAIN|EWOULDBLOCK/u);
  lease.close();
  assert.equal(isSharedProfileGateLease(lease), false);
  gate.close();
});
