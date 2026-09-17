import assert from "node:assert/strict";
import { chmod, mkdir, realpath, rename, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";

import {
  revalidatePythonExecutableForSpawn,
  validatePluginConfig,
} from "../src/config.js";
import { ACTION_NOT_ENABLED, TOOL_NAMES, createDisabledTools } from "../src/tools.js";
import { temporaryDirectory } from "./support.js";

const AGENT_PROFILE_V2 = {
  openclawPackageSha256: "1".repeat(64),
  financeCommit: "2".repeat(40),
  coreVersion: "0.1.0",
  coreManifestSha256: "4".repeat(64),
  coreWheelSha256: "5".repeat(64),
  coreApiContractVersion: "finance-core-api-v1",
  coreMigrationLedgerDigest: "6".repeat(64),
  pluginBuildSha256: "3".repeat(64),
  executionClass: "cloud_projection" as const,
};

test("strict plugin config accepts only canonical external staging paths", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const pythonExecutable = join(repoRoot, ".venv", "bin", "python");
  await mkdir(join(repoRoot, ".venv", "bin"), { recursive: true, mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await writeFile(pythonExecutable, "python", { mode: 0o700 });
  await chmod(pythonExecutable, 0o700);
  const canonicalRepoRoot = await realpath(repoRoot);
  const canonicalCoreDistributionRoot = await realpath(coreDistributionRoot);
  const canonicalWorkspaceRoot = await realpath(workspaceRoot);
  const canonicalPythonExecutable = join(canonicalRepoRoot, ".venv", "bin", "python");

  const validated = await validatePluginConfig({
    repoRoot: canonicalRepoRoot,
    coreDistributionRoot: canonicalCoreDistributionRoot,
    pythonExecutable: canonicalPythonExecutable,
    workspaceRoot: canonicalWorkspaceRoot,
    agentProfileV2: AGENT_PROFILE_V2,
  });

  assert.deepEqual(validated, {
    repoRoot: canonicalRepoRoot,
    coreDistributionRoot: canonicalCoreDistributionRoot,
    pythonExecutable: canonicalPythonExecutable,
    workspaceRoot: canonicalWorkspaceRoot,
    agentProfileV2: AGENT_PROFILE_V2,
  });
});

test("strict plugin config refuses unknown, relative, symlinked, and in-repo paths", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const pythonExecutable = join(repoRoot, ".venv", "bin", "python");
  await mkdir(join(repoRoot, ".venv", "bin"), { recursive: true, mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await writeFile(pythonExecutable, "python", { mode: 0o700 });
  await mkdir(join(repoRoot, "runtime"), { mode: 0o700 });
  const symlinkedWorkspace = join(fixture.path, "workspace-link");
  const symlinkedCoreDistribution = join(fixture.path, "core-distribution-link");
  await symlink(workspaceRoot, symlinkedWorkspace);
  await symlink(coreDistributionRoot, symlinkedCoreDistribution);

  const canonicalRepoRoot = await realpath(repoRoot);
  const valid = {
    repoRoot: canonicalRepoRoot,
    coreDistributionRoot: await realpath(coreDistributionRoot),
    pythonExecutable: join(canonicalRepoRoot, ".venv", "bin", "python"),
    workspaceRoot: await realpath(workspaceRoot),
    agentProfileV2: AGENT_PROFILE_V2,
  };
  await assert.rejects(validatePluginConfig({ ...valid, token: "secret" }), /unknown field/u);
  await assert.rejects(validatePluginConfig({ ...valid, repoRoot: "relative" }), /absolute/u);
  await assert.rejects(
    validatePluginConfig({ ...valid, workspaceRoot: symlinkedWorkspace }),
    /symbolic link/u,
  );
  await assert.rejects(
    validatePluginConfig({ ...valid, coreDistributionRoot: symlinkedCoreDistribution }),
    /symbolic link/u,
  );
  await assert.rejects(
    validatePluginConfig({ ...valid, workspaceRoot: await realpath(join(repoRoot, "runtime")) }),
    /outside the runtime and core distribution roots/u,
  );
  await assert.rejects(
    validatePluginConfig({ ...valid, coreDistributionRoot: canonicalRepoRoot }),
    /independent from repoRoot/u,
  );
  await chmod(coreDistributionRoot, 0o777);
  await assert.rejects(validatePluginConfig(valid), /coreDistributionRoot.*writable/u);
  await chmod(coreDistributionRoot, 0o700);
  const unsafeCoreAncestor = join(fixture.path, "unsafe-core-ancestor");
  const unsafeCoreDistributionRoot = join(unsafeCoreAncestor, "core-distribution");
  await mkdir(unsafeCoreDistributionRoot, { recursive: true, mode: 0o700 });
  await chmod(unsafeCoreAncestor, 0o777);
  await assert.rejects(
    validatePluginConfig({
      ...valid,
      coreDistributionRoot: await realpath(unsafeCoreDistributionRoot),
    }),
    /coreDistributionRoot parent directory.*owner-controlled/u,
  );
  await chmod(workspaceRoot, 0o755);
  await assert.rejects(validatePluginConfig(valid), /0700/u);
  await chmod(workspaceRoot, 0o700);
  await chmod(pythonExecutable, 0o722);
  await assert.rejects(validatePluginConfig(valid), /writable/u);
  await chmod(pythonExecutable, 0o700);
  await chmod(repoRoot, 0o777);
  await assert.rejects(validatePluginConfig(valid), /repoRoot.*writable/u);
});

test("strict plugin config accepts a venv Python symlink but returns the alias", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const binDirectory = join(repoRoot, ".venv", "bin");
  const target = join(binDirectory, "python3.12");
  const pythonExecutable = join(binDirectory, "python");
  await mkdir(binDirectory, { recursive: true, mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await writeFile(target, "python", { mode: 0o700 });
  await chmod(target, 0o700);
  await symlink("python3.12", pythonExecutable);

  const canonicalRepoRoot = await realpath(repoRoot);
  const canonicalAlias = join(canonicalRepoRoot, ".venv", "bin", "python");
  const validated = await validatePluginConfig({
    repoRoot: canonicalRepoRoot,
    coreDistributionRoot: await realpath(coreDistributionRoot),
    pythonExecutable: canonicalAlias,
    workspaceRoot: await realpath(workspaceRoot),
    agentProfileV2: AGENT_PROFILE_V2,
  });
  assert.equal(validated.pythonExecutable, canonicalAlias);
  assert.notEqual(validated.pythonExecutable, await realpath(pythonExecutable));
});

test("strict plugin config accepts an owner-controlled Python environment outside repoRoot", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const externalBin = join(fixture.path, "python-environment", "bin");
  await mkdir(repoRoot, { mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await mkdir(externalBin, { recursive: true, mode: 0o700 });
  const canonicalExternalBin = await realpath(externalBin);
  const target = join(canonicalExternalBin, "python3.12");
  const pythonExecutable = join(canonicalExternalBin, "python");
  await writeFile(target, "python", { mode: 0o700 });
  await chmod(target, 0o700);
  await symlink("python3.12", pythonExecutable);

  const validated = await validatePluginConfig({
    repoRoot: await realpath(repoRoot),
    coreDistributionRoot: await realpath(coreDistributionRoot),
    pythonExecutable,
    workspaceRoot: await realpath(workspaceRoot),
    agentProfileV2: AGENT_PROFILE_V2,
  });

  assert.equal(validated.pythonExecutable, pythonExecutable);
  assert.notEqual(validated.pythonExecutable, await realpath(pythonExecutable));
});

test("strict plugin config rejects an unsafe external Python environment", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const externalBin = join(fixture.path, "python-environment", "bin");
  await mkdir(repoRoot, { mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await mkdir(externalBin, { recursive: true, mode: 0o700 });
  const canonicalExternalBin = await realpath(externalBin);
  const pythonExecutable = join(canonicalExternalBin, "python");
  await writeFile(pythonExecutable, "python", { mode: 0o700 });
  const valid = {
    repoRoot: await realpath(repoRoot),
    coreDistributionRoot: await realpath(coreDistributionRoot),
    pythonExecutable,
    workspaceRoot: await realpath(workspaceRoot),
    agentProfileV2: AGENT_PROFILE_V2,
  };

  await chmod(canonicalExternalBin, 0o777);
  await assert.rejects(validatePluginConfig(valid), /parent directory.*owner-controlled/u);
  await chmod(canonicalExternalBin, 0o700);
  await chmod(pythonExecutable, 0o722);
  await assert.rejects(validatePluginConfig(valid), /target.*writable/u);
  await assert.rejects(
    validatePluginConfig({ ...valid, pythonExecutable: "python" }),
    /non-empty absolute path/u,
  );
});

test("strict plugin config rejects an unsafe Python ancestor directory", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const unsafeAncestor = join(fixture.path, "unsafe-ancestor");
  const externalBin = join(unsafeAncestor, "environment", "bin");
  await mkdir(repoRoot, { mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await mkdir(externalBin, { recursive: true, mode: 0o700 });
  const pythonExecutable = join(await realpath(externalBin), "python");
  await writeFile(pythonExecutable, "python", { mode: 0o700 });
  await chmod(unsafeAncestor, 0o777);

  await assert.rejects(validatePluginConfig({
    repoRoot: await realpath(repoRoot),
    coreDistributionRoot: await realpath(coreDistributionRoot),
    pythonExecutable,
    workspaceRoot: await realpath(workspaceRoot),
    agentProfileV2: AGENT_PROFILE_V2,
  }), /ancestor directory chain.*owner-controlled/u);
});

test("Python executable revalidation detects alias directory replacement", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const externalBin = join(fixture.path, "python-environment", "bin");
  await mkdir(repoRoot, { mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await mkdir(externalBin, { recursive: true, mode: 0o700 });
  const canonicalExternalBin = await realpath(externalBin);
  const pythonExecutable = join(canonicalExternalBin, "python");
  await writeFile(pythonExecutable, "python", { mode: 0o700 });
  const validated = await validatePluginConfig({
    repoRoot: await realpath(repoRoot),
    coreDistributionRoot: await realpath(coreDistributionRoot),
    pythonExecutable,
    workspaceRoot: await realpath(workspaceRoot),
    agentProfileV2: AGENT_PROFILE_V2,
  });

  await rename(canonicalExternalBin, `${canonicalExternalBin}-old`);
  await mkdir(canonicalExternalBin, { mode: 0o700 });
  await writeFile(pythonExecutable, "replacement", { mode: 0o700 });

  await assert.rejects(
    revalidatePythonExecutableForSpawn(validated),
    /identity changed after configuration validation/u,
  );
});

test("Python executable revalidation detects target directory replacement", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const externalBin = join(fixture.path, "python-environment", "bin");
  const targetDirectory = join(fixture.path, "python-target");
  await mkdir(repoRoot, { mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await mkdir(externalBin, { recursive: true, mode: 0o700 });
  await mkdir(targetDirectory, { mode: 0o700 });
  const canonicalExternalBin = await realpath(externalBin);
  const canonicalTargetDirectory = await realpath(targetDirectory);
  const target = join(canonicalTargetDirectory, "python3.12");
  const pythonExecutable = join(canonicalExternalBin, "python");
  await writeFile(target, "python", { mode: 0o700 });
  await symlink(target, pythonExecutable);
  const validated = await validatePluginConfig({
    repoRoot: await realpath(repoRoot),
    coreDistributionRoot: await realpath(coreDistributionRoot),
    pythonExecutable,
    workspaceRoot: await realpath(workspaceRoot),
    agentProfileV2: AGENT_PROFILE_V2,
  });

  await rename(canonicalTargetDirectory, `${canonicalTargetDirectory}-old`);
  await mkdir(canonicalTargetDirectory, { mode: 0o700 });
  await writeFile(target, "replacement", { mode: 0o700 });

  await assert.rejects(
    revalidatePythonExecutableForSpawn(validated),
    /identity changed after configuration validation/u,
  );
});

test("strict plugin config refuses model names and malformed v2 evidence", async () => {
  await using fixture = await temporaryDirectory();
  const repoRoot = join(fixture.path, "repo");
  const coreDistributionRoot = join(fixture.path, "core-distribution");
  const workspaceRoot = join(fixture.path, "workspace");
  const pythonExecutable = join(repoRoot, ".venv", "bin", "python");
  await mkdir(join(repoRoot, ".venv", "bin"), { recursive: true, mode: 0o700 });
  await mkdir(coreDistributionRoot, { mode: 0o700 });
  await mkdir(workspaceRoot, { mode: 0o700 });
  await writeFile(pythonExecutable, "python", { mode: 0o700 });
  const valid = {
    repoRoot: await realpath(repoRoot),
    coreDistributionRoot: await realpath(coreDistributionRoot),
    pythonExecutable: await realpath(pythonExecutable),
    workspaceRoot: await realpath(workspaceRoot),
    agentProfileV2: AGENT_PROFILE_V2,
  };
  await assert.rejects(
    validatePluginConfig({
      ...valid,
      agentProfileV2: { ...AGENT_PROFILE_V2, model: "openai/example" },
    }),
    /exact public-safe build evidence/u,
  );
  await assert.rejects(
    validatePluginConfig({
      ...valid,
      agentProfileV2: { ...AGENT_PROFILE_V2, financeCommit: "main" },
    }),
    /exact public-safe build evidence/u,
  );
});

test("all eight optional Agent tools fail before any private controller call", async () => {
  let privateCalls = 0;
  const tools = createDisabledTools(() => {
    privateCalls += 1;
  });

  assert.equal(tools.length, 8);
  assert.deepEqual(
    tools.map((tool) => tool.name),
    [...TOOL_NAMES],
  );
  for (const tool of tools) {
    const result = await tool.execute("call", {}, undefined, undefined);
    assert.deepEqual(result.details, { code: ACTION_NOT_ENABLED });
    assert.equal(result.content[0]?.type, "text");
    assert.match(
      result.content[0]?.type === "text" ? result.content[0].text : "",
      new RegExp(ACTION_NOT_ENABLED, "u"),
    );
  }
  assert.equal(privateCalls, 0);
});
