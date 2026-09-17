import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { cp, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

const pluginRoot = await realpath(resolve(import.meta.dirname, ".."));
const packageJson = JSON.parse(await readFile(resolve(pluginRoot, "package.json"), "utf8"));
const expectedNodeVersion = `v${packageJson.engines?.node ?? ""}`;
if (process.version !== expectedNodeVersion) {
  throw new Error(
    `Finance bridge reproducible build requires Node ${expectedNodeVersion}; got ${process.version}.`,
  );
}

function runRequired(root, script, args = []) {
  const result = spawnSync(process.execPath, [script, ...args], {
    cwd: root,
    env: process.env,
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`Required build command failed with status ${result.status ?? 1}: ${script}`);
  }
}

async function build(root) {
  await rm(join(root, "dist"), { force: true, recursive: true });
  runRequired(root, join(root, "scripts/build-native.mjs"));
  runRequired(root, join(root, "node_modules/typescript/bin/tsc"), [
    "-p", join(root, "tsconfig.json"),
  ]);
  const { computeBuildSourceIdentityV1 } = await import(
    pathToFileURL(join(root, "dist/src/artifact-hash-v1.js")).href
  );
  const sourceIdentity = await computeBuildSourceIdentityV1(root);
  await writeFile(
    join(root, "dist/build-provenance-v1.json"),
    `${JSON.stringify({
      policy_version: sourceIdentity.policy_version,
      source_identity_sha256: sourceIdentity.source_identity_sha256,
      file_count: sourceIdentity.file_count,
      byte_count: sourceIdentity.byte_count,
    })}\n`,
    { encoding: "utf8", mode: 0o644 },
  );
  const bindings = [
    join(root, "build/Release/finance_bridge_posix.node"),
    join(root, "node_modules/fs-ext/build/Release/fs_ext.node"),
  ];
  return await Promise.all(bindings.map(async (binding) => (
    createHash("sha256").update(await readFile(binding)).digest("hex")
  )));
}

const stagingRoot = await realpath(
  await mkdtemp(join(tmpdir(), "finance-bridge-cross-path-")),
);
const secondRoot = join(stagingRoot, "finance-bridge");
try {
  await cp(pluginRoot, secondRoot, {
    recursive: true,
    preserveTimestamps: true,
    filter(source) {
      const path = relative(pluginRoot, source);
      return path !== "dist" && !path.startsWith(`dist${sep}`) &&
        path !== "build" && !path.startsWith(`build${sep}`) &&
        path !== join("node_modules", "fs-ext", "build") &&
        !path.startsWith(`${join("node_modules", "fs-ext", "build")}${sep}`) &&
        path !== join("node_modules", ".bin") &&
        !path.startsWith(`${join("node_modules", ".bin")}${sep}`);
    },
  });
  const firstNative = await build(pluginRoot);
  const secondNative = await build(secondRoot);
  for (const [index, label] of ["finance_bridge_posix.node", "fs_ext.node"].entries()) {
    if (firstNative[index] !== secondNative[index]) {
      throw new Error(
        `Cross-path native build is not reproducible for ${label}: ` +
        `${firstNative[index]} != ${secondNative[index]}`,
      );
    }
  }
  const { computeArtifactHashV1 } = await import(
    new URL("../dist/src/artifact-hash-v1.js", import.meta.url)
  );
  const firstArtifact = await computeArtifactHashV1("finance_plugin_build", pluginRoot);
  const secondArtifact = await computeArtifactHashV1("finance_plugin_build", secondRoot);
  if (firstArtifact.artifact_sha256 !== secondArtifact.artifact_sha256) {
    throw new Error(
      `Cross-path plugin artifact is not reproducible: ` +
      `${firstArtifact.artifact_sha256} != ${secondArtifact.artifact_sha256}`,
    );
  }
  process.stdout.write(
    `cross-path plugin build reproducible ` +
    `artifact_sha256=${firstArtifact.artifact_sha256} ` +
    `native_sha256=${firstNative.join(",")}\n`,
  );
} finally {
  await rm(stagingRoot, { force: true, recursive: true });
}
