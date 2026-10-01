import assert from "node:assert/strict";
import { chmod, mkdir, realpath, symlink } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

import { checkProfileAncestors, profileLocatorEnvironment, resolveProfileLayout } from "../src/profile-layout.js";
import { temporaryDirectory } from "./support.js";

test("trusted profile locators retain Mac layout and select exactly one Linux root", async () => {
  await using fixture = await temporaryDirectory();
  const root = await realpath(fixture.path);
  const mac = join(root, "Application Support");
  const linux = join(root, "finance-codex");
  await mkdir(mac, { mode: 0o700 });
  await mkdir(linux, { mode: 0o700 });
  const macLocator = { applicationSupportRoot: mac, profileId: "synthetic" };
  const linuxLocator = { linuxDataRoot: linux, profileId: "synthetic" };
  assert.equal(resolveProfileLayout(macLocator).profileRoot, join(mac, "Finance-Codex/profiles/synthetic"));
  assert.equal(resolveProfileLayout(linuxLocator).profileRoot, join(linux, "profiles/synthetic"));
  assert.deepEqual(profileLocatorEnvironment(linuxLocator), {
    FINANCE_CUT_LINUX_DATA_ROOT: linux,
    FINANCE_RUNTIME_ROOT: join(linux, "profiles/synthetic/runtime"),
  });
  assert.deepEqual(profileLocatorEnvironment(macLocator), {
    FINANCE_CUT_APPLICATION_SUPPORT: mac,
    FINANCE_RUNTIME_ROOT: join(mac, "Finance-Codex/profiles/synthetic/runtime"),
  });
  assert.throws(() => resolveProfileLayout({ ...macLocator, linuxDataRoot: linux }), /Exactly one/u);
  assert.throws(() => resolveProfileLayout({ profileId: "synthetic" }), /Exactly one/u);
  assert.throws(() => resolveProfileLayout({ ...linuxLocator, profileId: "../escape" }), /locator/u);
  assert.throws(() => resolveProfileLayout({ ...linuxLocator, linuxDataRoot: mac }), /fixed layout/u);
  const alias = join(root, "alias");
  await symlink(linux, alias);
  assert.throws(() => resolveProfileLayout({ ...linuxLocator, linuxDataRoot: alias }), /canonical/u);
  if (process.platform !== "linux") {
    assert.throws(() => checkProfileAncestors(linuxLocator), /requires Linux/u);
    return;
  }
  checkProfileAncestors(linuxLocator);
  await chmod(linux, 0o755);
  assert.throws(() => checkProfileAncestors(linuxLocator), /Unsafe/u);
  await chmod(linux, 0o700);
  await mkdir(join(root, ".git"));
  assert.throws(() => checkProfileAncestors(linuxLocator), /Repository/u);
});
