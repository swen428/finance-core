import assert from "node:assert/strict";
import { createRequire, syncBuiltinESMExports } from "node:module";
import { constants, openSync, closeSync } from "node:fs";
import { chmod, mkdir, mkdtemp, readFile, readdir, realpath, rename, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test, { type TestContext } from "node:test";

import { assertBridgeCut, withExclusiveBridgeCut, type BridgeCutContext, type PrivateBridgeStageSink } from "../src/bridge-export-boundary.js";
import { initializeProfileGate, openProfileGate, PROFILE_GATE_BASENAME } from "../src/profile-gate.js";
import { descriptorIdentitySync } from "../src/posix.js";

async function synthetic(t: TestContext): Promise<{
  applicationSupportRoot: string; profileId: string; runtimeRoot: string;
  profileRoot: string; handoffRoot: string;
}> {
  const scratch = await realpath(await mkdtemp(join(tmpdir(), "bridge-cut-")));
  t.after(async () => rm(scratch, { recursive: true, force: true }));
  const applicationSupportRoot = join(scratch, "Application Support");
  const profileId = "synthetic";
  const profileRoot = join(applicationSupportRoot, "Finance-Codex", "profiles", profileId);
  const runtimeRoot = join(profileRoot, "runtime");
  const workspaceRoot = join(profileRoot, "workspace");
  const handoffRoot = join(workspaceRoot, "handoff");
  for (const path of [applicationSupportRoot, join(applicationSupportRoot, "Finance-Codex"),
    join(applicationSupportRoot, "Finance-Codex", "profiles"), profileRoot, runtimeRoot,
    join(runtimeRoot, "database"), workspaceRoot, join(workspaceRoot, "database"),
    handoffRoot, join(profileRoot, "backups"), join(profileRoot, "work"), join(profileRoot, "restore")]) {
    await mkdir(path, { mode: 0o700 });
  }
  await writeFile(join(profileRoot, "profile.json"), JSON.stringify({
    profile_id: profileId, runtime_root: runtimeRoot, workspace_root: workspaceRoot,
  }), { mode: 0o600 });
  initializeProfileGate(profileRoot);
  return { applicationSupportRoot, profileId, runtimeRoot, profileRoot, handoffRoot };
}

test("owner cut creates a new private stage and rejects forged or closed capabilities", async (t) => {
  const profile = await synthetic(t);
  let expiredContext: BridgeCutContext | undefined;
  let expiredSink: PrivateBridgeStageSink | undefined;
  const result = await withExclusiveBridgeCut(profile, async (context, sink) => {
    expiredContext = context; expiredSink = sink;
    assert.equal(context.profileId, profile.profileId);
    assert.equal(context.handoffRoot, profile.handoffRoot);
    assert.equal(context.stagePath, sink.stagePath);
    assert.match(context.stageRelativeName, /^owner-export-[0-9a-f]{32}$/u);
    assertBridgeCut(context, sink);
    assert.throws(() => assertBridgeCut({ ...context }, sink), /live matched/u);
    assert.throws(() => assertBridgeCut(context, { ...sink }), /live matched/u);
    const entry = await sink.writeValidated("snapshot.bin", Buffer.from("synthetic"));
    assert.equal(entry.relativeName, "snapshot.bin");
    assert.equal(entry.byteSize, 9);
    assert.equal(entry.sha256.length, 64);
    assert.equal((await stat(context.stagePath)).mode & 0o777, 0o700);
    assert.equal((await stat(join(context.stagePath, "snapshot.bin"))).mode & 0o777, 0o600);
    assert.equal((await readFile(join(context.stagePath, "snapshot.bin"))).toString(), "synthetic");
    await assert.rejects(sink.writeValidated("snapshot.bin", Buffer.from("duplicate")));
    await assert.rejects(sink.writeValidated("../escape", Buffer.from("unsafe")));
    return "done";
  });
  assert.equal(result, "done");
  assert.throws(() => assertBridgeCut(expiredContext!, expiredSink!), /live matched/u);
  await assert.rejects(expiredSink!.writeValidated("later", Buffer.from("x")), /live matched/u);
});

test("cut binds handoff directory FD and rejects a changed source identity", async (t) => {
  const profile = await synthetic(t);
  await withExclusiveBridgeCut(profile, async (context, sink) => {
    const fd = openSync(profile.handoffRoot, constants.O_RDONLY | constants.O_DIRECTORY);
    try {
      const identity = descriptorIdentitySync(fd);
      assertBridgeCut(context, sink, { fd, identity });
      assert.throws(() => assertBridgeCut(context, sink, { fd, identity: { ...identity, ino: identity.ino + 1n } }), /source descriptor/u);
    } finally { closeSync(fd); }
  });
});

test("cut snapshots the explicit locator before waiting, even if the caller selects another profile", async (t) => {
  const selected = await synthetic(t);
  const alternate = await synthetic(t);
  const locator = {
    applicationSupportRoot: selected.applicationSupportRoot,
    profileId: selected.profileId,
    runtimeRoot: selected.runtimeRoot,
  };
  const alternateLocator = {
    applicationSupportRoot: alternate.applicationSupportRoot,
    profileId: alternate.profileId,
    runtimeRoot: alternate.runtimeRoot,
  };
  const gate = openProfileGate(selected.profileRoot);
  const held = await gate.acquireExclusive(1_000, 10_000);
  try {
    const pendingCut = withExclusiveBridgeCut(locator, async (context, sink) => {
      assert.equal(context.profileId, selected.profileId);
      assert.equal(context.handoffRoot, selected.handoffRoot);
      assert.equal(context.stagePath.startsWith(join(selected.profileRoot, "work") + "/"), true);
      await sink.writeValidated("selected-profile.bin", Buffer.from("selected"));
      return { profileId: context.profileId, stagePath: context.stagePath };
    }, { waitMs: 2_000, maxHoldMs: 10_000 });

    Object.assign(locator, alternateLocator);
    held.close();
    const result = await pendingCut;
    assert.equal(result.profileId, selected.profileId);
    assert.equal(result.stagePath.startsWith(join(selected.profileRoot, "work") + "/"), true);
    assert.deepEqual(await readdir(join(alternate.profileRoot, "work")), []);
  } finally {
    held.close();
    gate.close();
  }
});

test("cut keeps explicit runtime and profile-manifest bindings authoritative", async (t) => {
  const profile = await synthetic(t);
  const previousRuntimeRoot = process.env.FINANCE_RUNTIME_ROOT;
  process.env.FINANCE_RUNTIME_ROOT = join(profile.profileRoot, "unselected-runtime");
  try {
    const validLocator = {
      applicationSupportRoot: profile.applicationSupportRoot,
      profileId: profile.profileId,
      runtimeRoot: profile.runtimeRoot,
    };
    await withExclusiveBridgeCut(validLocator, async (context) => context.profileId);

    await assert.rejects(withExclusiveBridgeCut({
      ...validLocator,
      runtimeRoot: join(profile.profileRoot, "unselected-runtime"),
    }, async () => undefined), /Runtime root does not match the selected profile/u);

    await writeFile(join(profile.profileRoot, "profile.json"), JSON.stringify({
      profile_id: profile.profileId,
      runtime_root: join(profile.profileRoot, "unselected-runtime"),
      workspace_root: join(profile.profileRoot, "workspace"),
    }), { mode: 0o600 });
    await assert.rejects(withExclusiveBridgeCut(validLocator, async () => undefined), /profile.json does not bind/u);
  } finally {
    if (previousRuntimeRoot === undefined) delete process.env.FINANCE_RUNTIME_ROOT;
    else process.env.FINANCE_RUNTIME_ROOT = previousRuntimeRoot;
  }
});

test("finalization rejects changed earlier outputs and unexpected stage entries", async (t) => {
  for (const change of ["mutate", "replace", "extra"] as const) {
    await t.test(change, async (caseContext) => {
      const profile = await synthetic(caseContext);
      let stagePath: string | undefined;
      await assert.rejects(withExclusiveBridgeCut(profile, async (context, sink) => {
        stagePath = context.stagePath;
        await sink.writeValidated("first.bin", Buffer.from("original"));
        await sink.writeValidated("second.bin", Buffer.from("later"));
        const first = join(context.stagePath, "first.bin");
        if (change === "mutate") await writeFile(first, Buffer.from("modified"));
        else if (change === "replace") {
          await rm(first);
          await writeFile(first, Buffer.from("original"), { mode: 0o600 });
        } else await writeFile(join(context.stagePath, "unlisted.bin"), Buffer.from("extra"), { mode: 0o600 });
        return "must not be accepted";
      }), /Bridge stage (output|inventory)/u);
      assert.ok(stagePath);
      assert.equal((await readFile(join(stagePath, "second.bin"))).toString(), "later");
      const gate = openProfileGate(profile.profileRoot);
      try {
        const lease = await gate.acquireExclusive(1_000, 1_000);
        lease.close();
      } finally { gate.close(); }
    });
  }
});

test("cut rejects unsafe profile manifest, source replacement, and gate replacement", async (t) => {
  const profile = await synthetic(t);
  const manifest = join(profile.profileRoot, "profile.json");
  await writeFile(manifest, '{"profile_id":"synthetic","profile_id":"synthetic"}');
  await assert.rejects(withExclusiveBridgeCut(profile, async () => undefined), /duplicate/u);
  await writeFile(manifest, JSON.stringify({
    profile_id: profile.profileId, runtime_root: profile.runtimeRoot,
    workspace_root: join(profile.profileRoot, "workspace"),
  }));
  await chmod(manifest, 0o644);
  await assert.rejects(withExclusiveBridgeCut(profile, async () => undefined), /Unsafe/u);
  await chmod(manifest, 0o600);
  await withExclusiveBridgeCut(profile, async (context, sink) => {
    await rename(profile.handoffRoot, `${profile.handoffRoot}.old`);
    await mkdir(profile.handoffRoot, { mode: 0o700 });
    assert.throws(() => assertBridgeCut(context, sink), /identity changed/u);
  }).then(() => assert.fail("cut should fail"), () => undefined);
  await rm(profile.handoffRoot, { recursive: true });
  await rename(`${profile.handoffRoot}.old`, profile.handoffRoot);
  const gatePath = join(profile.profileRoot, PROFILE_GATE_BASENAME);
  await rename(gatePath, `${gatePath}.old`);
  await symlink(`${gatePath}.old`, gatePath);
  await assert.rejects(withExclusiveBridgeCut(profile, async () => undefined));
});

test("a stage output descriptor close failure still releases the exclusive gate and rejects success", async (t) => {
  const profile = await synthetic(t);
  const fsModule = createRequire(import.meta.url)("node:fs") as typeof import("node:fs");
  const originalClose = fsModule.closeSync;
  let injected = false;
  try {
    await assert.rejects(withExclusiveBridgeCut(profile, async (_context, sink) => {
      await sink.writeValidated("output.bin", Buffer.from("synthetic"));
      fsModule.closeSync = (fd: number): void => {
        const status = fsModule.fstatSync(fd);
        originalClose(fd);
        if (!injected && status.isFile() && status.size === 9) {
          injected = true;
          throw new Error("injected output close failure");
        }
      };
      syncBuiltinESMExports();
    }), /(?:Bridge cut cleanup failed|injected output close failure)/u);
  } finally {
    fsModule.closeSync = originalClose;
    syncBuiltinESMExports();
  }
  assert.equal(injected, true);
  const gate = openProfileGate(profile.profileRoot);
  try {
    const lease = await gate.acquireExclusive(1_000, 1_000);
    lease.close();
  } finally { gate.close(); }
});
