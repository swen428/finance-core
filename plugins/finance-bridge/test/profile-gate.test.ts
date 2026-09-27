import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { constants, openSync, closeSync } from "node:fs";
import {
  chmod,
  link,
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rename,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
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

const REPOSITORY_ROOT = resolve("../..");

async function privateRoot(t: TestContext): Promise<string> {
  const root = await realpath(await mkdtemp(join(tmpdir(), "finance-profile-gate-")));
  t.after(async () => rm(root, { recursive: true, force: true }));
  return root;
}

async function syntheticProfile(t: TestContext): Promise<{
  applicationSupport: string;
  profileId: string;
  profileRoot: string;
  runtimeRoot: string;
}> {
  const applicationSupport = join(await privateRoot(t), "Application Support");
  const profileId = "synthetic";
  const profileRoot = join(applicationSupport, "Finance-Codex", "profiles", profileId);
  const runtimeRoot = join(profileRoot, "runtime");
  const workspaceRoot = join(profileRoot, "workspace");
  for (const directory of [
    applicationSupport,
    join(applicationSupport, "Finance-Codex"),
    join(applicationSupport, "Finance-Codex", "profiles"),
    profileRoot,
    runtimeRoot,
    join(runtimeRoot, "database"),
    workspaceRoot,
    join(workspaceRoot, "database"),
    join(profileRoot, "backups"),
    join(profileRoot, "work"),
    join(profileRoot, "restore"),
  ]) {
    await mkdir(directory, { mode: 0o700 });
  }
  await writeFile(
    join(profileRoot, "profile.json"),
    JSON.stringify({
      profile_id: profileId,
      runtime_root: runtimeRoot,
      workspace_root: workspaceRoot,
    }),
    { encoding: "utf8", mode: 0o600 },
  );
  return { applicationSupport, profileId, profileRoot, runtimeRoot };
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

test("exclusive acquisition counts validation after flock and releases an expired lock", async (t) => {
  const root = await privateRoot(t);
  initializeProfileGate(root);
  const gate = openProfileGate(root);
  const originalNow = performance.now;
  let readings = 0;
  try {
    // The fourth timestamp is after post-flock validation. Simulate that
    // validation taking longer than the one-millisecond hold bound.
    performance.now = () => ++readings >= 4 ? 2 : 0;
    await assert.rejects(gate.acquireExclusive(1_000, 1), /hold deadline exceeded/u);
    assert.equal(childTryLock(join(root, PROFILE_GATE_BASENAME), "exnb"), "held");
  } finally {
    performance.now = originalNow;
    gate.close();
  }
});

test("exclusive assertValid counts validation time and closes an expired lease", async (t) => {
  const root = await privateRoot(t);
  initializeProfileGate(root);
  const gate = openProfileGate(root);
  const originalNow = performance.now;
  let cut: Awaited<ReturnType<typeof gate.acquireExclusive>> | undefined;
  try {
    performance.now = () => 0;
    cut = await gate.acquireExclusive(1_000, 1);
    let readings = 0;
    performance.now = () => ++readings === 1 ? 0 : 2;
    assert.throws(() => cut!.assertValid(), /hold deadline exceeded/u);
    assert.throws(() => cut!.assertValid(), /closed/u);
    assert.equal(childTryLock(join(root, PROFILE_GATE_BASENAME), "exnb"), "held");
  } finally {
    performance.now = originalNow;
    cut?.close();
    gate.close();
  }
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

test("Node shared lease reaches Python through FD4 and remains held through child reap", async (t) => {
  const { applicationSupport, profileId, profileRoot, runtimeRoot } = await syntheticProfile(t);
  const python = process.env.PYTHON_EXECUTABLE ?? join(REPOSITORY_ROOT, ".venv", "bin", "python");
  assert.equal(resolve(python), python, "PYTHON_EXECUTABLE must be normalized");
  const pythonEnvironment: NodeJS.ProcessEnv = {
    FINANCE_RUNTIME_ROOT: runtimeRoot,
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONUTF8: "1",
  };
  const pythonVersion = execFileSync(
    python,
    ["-B", "-c", "import json,sys; print(json.dumps(list(sys.version_info[:2])))"],
    { cwd: REPOSITORY_ROOT, encoding: "utf8", env: pythonEnvironment, timeout: 5_000 },
  );
  assert.deepEqual(JSON.parse(pythonVersion.trim()), [3, 12]);

  initializeProfileGate(profileRoot);
  const gate = openProfileGate(profileRoot);
  const lease = await gate.acquireShared(1_000);
  let leaseBound = false;
  let leaseClosed = false;
  try {
    lease.bindChild();
    leaseBound = true;
    assert.throws(() => lease.close(), /child reap/u);

    const childProgram = `
import os
import sys
from finance_core.profile_gate import writer_gate_from_parent
from finance_core.profile_paths import validate_profile_paths

with validate_profile_paths(sys.argv[1], sys.argv[2]) as profile:
    writer = writer_gate_from_parent(profile, inherited_fd=4)
    try:
        profile.revalidate()
        marker = profile.work / "node-fd4-writer.synthetic"
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, b"synthetic FD4 writer gate integration\\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        writer.close()
print("CHILD_WRITER_GATE_OK")
`;
    const child = spawnSync(
      python,
      ["-B", "-c", childProgram, applicationSupport, profileId],
      {
        cwd: REPOSITORY_ROOT,
        encoding: "utf8",
        env: pythonEnvironment,
        stdio: ["ignore", "pipe", "pipe", "ignore", lease.fdForChild()],
        timeout: 5_000,
      },
    );
    assert.equal(child.error, undefined, child.error?.message ?? child.stderr);
    assert.equal(child.signal, null, child.stderr);
    assert.equal(child.status, 0, child.stderr);
    assert.equal(child.stdout.trim(), "CHILD_WRITER_GATE_OK");
    assert.equal(
      await readFile(join(profileRoot, "work", "node-fd4-writer.synthetic"), "utf8"),
      "synthetic FD4 writer gate integration\n",
    );

    // spawnSync returns only after the Python child has exited and been reaped.
    // The parent's bound shared lease must still exclude a cut at that point.
    await assert.rejects(gate.acquireExclusive(80), /deadline exceeded/u);
    lease.unbindChild();
    leaseBound = false;
    lease.close();
    leaseClosed = true;

    const cut = await gate.acquireExclusive(1_000);
    cut.assertValid();
    cut.close();
  } finally {
    if (leaseBound) lease.unbindChild();
    if (!leaseClosed) lease.close();
    gate.close();
  }
});
