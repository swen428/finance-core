import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

const pluginRoot = resolve(import.meta.dirname, "..");
const packageJson = JSON.parse(await readFile(resolve(pluginRoot, "package.json"), "utf8"));
const expectedNodeVersion = `v${packageJson.engines?.node ?? ""}`;
if (process.version !== expectedNodeVersion) {
  throw new Error(
    `Finance bridge native build requires Node ${expectedNodeVersion}; got ${process.version}.`,
  );
}

const npmCli = process.env.npm_execpath;
if (!npmCli) throw new Error("npm_execpath is required to locate the pinned node-gyp runtime.");
const nodeGyp = resolve(dirname(npmCli), "../node_modules/node-gyp/bin/node-gyp.js");
const nodeRoot = resolve(dirname(process.execPath), "..");

function runRequired(command, args) {
  const commandResult = spawnSync(command, args, { stdio: "inherit" });
  if (commandResult.error) throw commandResult.error;
  if (commandResult.status !== 0) process.exit(commandResult.status ?? 1);
}

function rebuild(root) {
  const result = spawnSync(process.execPath, [nodeGyp, "rebuild", `--nodedir=${nodeRoot}`], {
    cwd: root,
    env: process.env,
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

async function normalizeDarwinBinding(binding) {
  if (process.platform !== "darwin") return;
  runRequired("/usr/bin/strip", ["-S", binding]);
  runRequired("/usr/bin/codesign", ["--remove-signature", binding]);
  const bytes = await readFile(binding);
  if (bytes.readUInt32LE(0) !== 0xfeedfacf) {
    throw new Error("Expected a 64-bit little-endian Mach-O native binding.");
  }
  const commandCount = bytes.readUInt32LE(16);
  let offset = 32;
  let uuidOffset;
  for (let index = 0; index < commandCount; index += 1) {
    if (offset + 8 > bytes.length) throw new Error("Mach-O load commands are truncated.");
    const command = bytes.readUInt32LE(offset);
    const commandSize = bytes.readUInt32LE(offset + 4);
    if (commandSize < 8 || offset + commandSize > bytes.length) {
      throw new Error("Mach-O load command size is invalid.");
    }
    if (command === 0x1b) {
      if (commandSize !== 24 || uuidOffset !== undefined) {
        throw new Error("Mach-O LC_UUID command is invalid or duplicated.");
      }
      uuidOffset = offset + 8;
      bytes.fill(0, uuidOffset, uuidOffset + 16);
    }
    offset += commandSize;
  }
  if (uuidOffset === undefined) throw new Error("Mach-O LC_UUID command is missing.");
  const uuid = createHash("sha256").update(bytes).digest().subarray(0, 16);
  uuid[6] = (uuid[6] & 0x0f) | 0x80;
  uuid[8] = (uuid[8] & 0x3f) | 0x80;
  uuid.copy(bytes, uuidOffset);
  await writeFile(binding, bytes);
  runRequired("/usr/bin/codesign", ["--force", "--sign", "-", binding]);
}

async function retainOnlyRuntimeBinding(buildRoot, binding) {
  const bytes = await readFile(binding);
  await rm(buildRoot, { force: true, recursive: true });
  await mkdir(dirname(binding), { mode: 0o755, recursive: true });
  await writeFile(binding, bytes, { mode: 0o755 });
  await chmod(binding, 0o755);
}

const fsExtRoot = resolve(pluginRoot, "node_modules/fs-ext");
const fsExtBuildRoot = resolve(fsExtRoot, "build");
const fsExtBinding = resolve(fsExtBuildRoot, "Release/fs_ext.node");
rebuild(fsExtRoot);
await normalizeDarwinBinding(fsExtBinding);
await retainOnlyRuntimeBinding(fsExtBuildRoot, fsExtBinding);

const financeBinding = resolve(pluginRoot, "build/Release/finance_bridge_posix.node");
rebuild(pluginRoot);
await normalizeDarwinBinding(financeBinding);
