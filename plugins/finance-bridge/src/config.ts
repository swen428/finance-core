import { constants, type BigIntStats } from "node:fs";
import { access, lstat, realpath } from "node:fs/promises";
import { dirname, isAbsolute, relative } from "node:path";

export interface FinanceBridgeConfig {
  repoRoot: string;
  coreDistributionRoot: string;
  pythonExecutable: string;
  workspaceRoot: string;
  agentProfileV2: FinanceAgentProfileEvidenceV2;
}

export interface FinanceAgentProfileEvidenceV2 {
  openclawPackageSha256: string;
  financeCommit: string;
  coreVersion: string;
  coreManifestSha256: string;
  coreWheelSha256: string;
  coreApiContractVersion: string;
  coreMigrationLedgerDigest: string;
  pluginBuildSha256: string;
  executionClass: "local_model" | "cloud_projection";
}

const CONFIG_FIELDS = [
  "repoRoot", "coreDistributionRoot", "pythonExecutable", "workspaceRoot", "agentProfileV2",
] as const;
const HASH = /^[0-9a-f]{64}$/u;
const COMMIT = /^[0-9a-f]{40}$/u;
const VERSION = /^[0-9]+\.[0-9]+\.[0-9]+$/u;
const CONTRACT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/u;

interface PathIdentity {
  path: string;
  dev: bigint;
  ino: bigint;
  uid: bigint;
  mode: bigint;
}

interface PythonExecutableProof {
  configuredPath: string;
  resolvedPath: string;
  alias: PathIdentity;
  target: PathIdentity;
  directories: readonly PathIdentity[];
}

const PYTHON_EXECUTABLE_PROOFS = new WeakMap<FinanceBridgeConfig, PythonExecutableProof>();

function requireObject(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Finance bridge config must be an object.");
  }
  return value as Record<string, unknown>;
}

async function canonicalPath(
  value: unknown,
  field: (typeof CONFIG_FIELDS)[number],
  kind: "directory" | "file",
): Promise<string> {
  if (typeof value !== "string" || value.length === 0 || !isAbsolute(value)) {
    throw new Error(`${field} must be a non-empty absolute path.`);
  }
  const stat = await lstat(value);
  if (stat.isSymbolicLink()) {
    throw new Error(`${field} must not be a symbolic link.`);
  }
  if (kind === "directory" ? !stat.isDirectory() : !stat.isFile()) {
    throw new Error(`${field} must name an existing ${kind}.`);
  }
  const canonical = await realpath(value);
  if (canonical !== value) {
    throw new Error(`${field} must be canonical and contain no symbolic link.`);
  }
  return canonical;
}

function hasTrustedOwner(uid: bigint): boolean {
  return typeof process.getuid !== "function" || uid === BigInt(process.getuid()) || uid === 0n;
}

function pathIdentity(path: string, status: BigIntStats): PathIdentity {
  return { path, dev: status.dev, ino: status.ino, uid: status.uid, mode: status.mode };
}

function directoryPaths(start: string): string[] {
  const paths: string[] = [];
  let current = start;
  for (;;) {
    paths.push(current);
    const parent = dirname(current);
    if (parent === current) return paths;
    current = parent;
  }
}

async function validateDirectoryChain(
  start: string,
  boundary: "pythonExecutable" | "pythonExecutable target" | "coreDistributionRoot",
): Promise<PathIdentity[]> {
  const paths = directoryPaths(start);
  const identities: PathIdentity[] = [];
  for (const [index, path] of paths.entries()) {
    const status = await lstat(path, { bigint: true });
    const canonical = await realpath(path);
    const writable = (status.mode & 0o022n) !== 0n;
    const trustedStickyDirectory = status.uid === 0n && (status.mode & 0o1000n) !== 0n;
    if (!status.isDirectory() || canonical !== path || !hasTrustedOwner(status.uid) ||
        (writable && !trustedStickyDirectory)) {
      const location = index === 0 ? "parent directory" : "ancestor directory chain";
      throw new Error(
        `${boundary} ${location} must be canonical, owner-controlled, and not ` +
        "group- or world-writable except for a root-owned sticky directory.",
      );
    }
    identities.push(pathIdentity(path, status));
  }
  return identities;
}

async function inspectPythonExecutable(
  value: unknown,
): Promise<{ path: string; proof: PythonExecutableProof }> {
  if (typeof value !== "string" || value.length === 0 || !isAbsolute(value)) {
    throw new Error("pythonExecutable must be a non-empty absolute path.");
  }
  const binDirectory = dirname(value);
  const aliasDirectories = await validateDirectoryChain(binDirectory, "pythonExecutable");
  const aliasStatus = await lstat(value, { bigint: true });
  if (!aliasStatus.isFile() && !aliasStatus.isSymbolicLink()) {
    throw new Error("pythonExecutable must name a regular file or interpreter alias.");
  }
  const target = await realpath(value);
  const targetDirectories = await validateDirectoryChain(
    dirname(target),
    "pythonExecutable target",
  );
  const targetStatus = await lstat(target, { bigint: true });
  if (!targetStatus.isFile() || (targetStatus.mode & 0o022n) !== 0n ||
      !hasTrustedOwner(targetStatus.uid)) {
    throw new Error(
      "pythonExecutable target must be a trusted, non-writable regular file.",
    );
  }
  await access(value, constants.X_OK);
  return {
    path: value,
    proof: {
      configuredPath: value,
      resolvedPath: target,
      alias: pathIdentity(value, aliasStatus),
      target: pathIdentity(target, targetStatus),
      directories: [...aliasDirectories, ...targetDirectories],
    },
  };
}

function sameIdentity(left: PathIdentity, right: PathIdentity): boolean {
  return left.path === right.path && left.dev === right.dev && left.ino === right.ino &&
    left.uid === right.uid && left.mode === right.mode;
}

function sameProof(left: PythonExecutableProof, right: PythonExecutableProof): boolean {
  return left.configuredPath === right.configuredPath &&
    left.resolvedPath === right.resolvedPath &&
    sameIdentity(left.alias, right.alias) && sameIdentity(left.target, right.target) &&
    left.directories.length === right.directories.length &&
    left.directories.every((identity, index) =>
      sameIdentity(identity, right.directories[index]!),
    );
}

export async function revalidatePythonExecutableForSpawn(
  config: FinanceBridgeConfig,
): Promise<void> {
  const expected = PYTHON_EXECUTABLE_PROOFS.get(config);
  if (expected === undefined) {
    throw new Error("Finance bridge config has no validated Python executable identity.");
  }
  let current: PythonExecutableProof;
  try {
    current = (await inspectPythonExecutable(config.pythonExecutable)).proof;
  } catch {
    throw new Error("pythonExecutable identity changed after configuration validation.");
  }
  if (!sameProof(expected, current)) {
    throw new Error("pythonExecutable identity changed after configuration validation.");
  }
}

function isWithin(parent: string, candidate: string): boolean {
  const child = relative(parent, candidate);
  return child === "" || (!child.startsWith("..") && !isAbsolute(child));
}

export async function validatePluginConfig(value: unknown): Promise<FinanceBridgeConfig> {
  const object = requireObject(value);
  const unknown = Object.keys(object).filter(
    (key) => !(CONFIG_FIELDS as readonly string[]).includes(key),
  );
  if (unknown.length > 0) {
    throw new Error(`Finance bridge config contains unknown field: ${unknown[0]}.`);
  }
  for (const field of CONFIG_FIELDS) {
    if (!(field in object)) {
      throw new Error(`Finance bridge config is missing ${field}.`);
    }
  }

  const repoRoot = await canonicalPath(object.repoRoot, "repoRoot", "directory");
  const repoStatus = await lstat(repoRoot);
  if ((repoStatus.mode & 0o022) !== 0 ||
      (typeof process.getuid === "function" && repoStatus.uid !== process.getuid())) {
    throw new Error("repoRoot must be owner-controlled and not group- or world-writable.");
  }
  const coreDistributionRoot = await canonicalPath(
    object.coreDistributionRoot,
    "coreDistributionRoot",
    "directory",
  );
  const coreDistributionStatus = await lstat(coreDistributionRoot);
  if ((coreDistributionStatus.mode & 0o022) !== 0 ||
      (typeof process.getuid === "function" &&
       coreDistributionStatus.uid !== process.getuid())) {
    throw new Error(
      "coreDistributionRoot must be owner-controlled and not group- or world-writable.",
    );
  }
  await validateDirectoryChain(dirname(coreDistributionRoot), "coreDistributionRoot");
  const pythonExecutable = await inspectPythonExecutable(object.pythonExecutable);
  const workspaceRoot = await canonicalPath(object.workspaceRoot, "workspaceRoot", "directory");
  const profile = requireObject(object.agentProfileV2);
  const profileFields = [
    "openclawPackageSha256", "financeCommit", "coreVersion", "coreManifestSha256",
    "coreWheelSha256", "coreApiContractVersion", "coreMigrationLedgerDigest",
    "pluginBuildSha256", "executionClass",
  ];
  if (Object.keys(profile).sort().join(",") !== profileFields.sort().join(",") ||
      typeof profile.openclawPackageSha256 !== "string" ||
      !HASH.test(profile.openclawPackageSha256) ||
      typeof profile.financeCommit !== "string" || !COMMIT.test(profile.financeCommit) ||
      typeof profile.coreVersion !== "string" || !VERSION.test(profile.coreVersion) ||
      typeof profile.coreManifestSha256 !== "string" ||
      !HASH.test(profile.coreManifestSha256) ||
      typeof profile.coreWheelSha256 !== "string" || !HASH.test(profile.coreWheelSha256) ||
      typeof profile.coreApiContractVersion !== "string" ||
      !CONTRACT.test(profile.coreApiContractVersion) ||
      typeof profile.coreMigrationLedgerDigest !== "string" ||
      !HASH.test(profile.coreMigrationLedgerDigest) ||
      typeof profile.pluginBuildSha256 !== "string" || !HASH.test(profile.pluginBuildSha256) ||
      (profile.executionClass !== "local_model" &&
       profile.executionClass !== "cloud_projection")) {
    throw new Error("agentProfileV2 must contain exact public-safe build evidence.");
  }
  if (isWithin(repoRoot, coreDistributionRoot) || isWithin(coreDistributionRoot, repoRoot)) {
    throw new Error("coreDistributionRoot must be independent from repoRoot.");
  }
  if (isWithin(repoRoot, workspaceRoot) || isWithin(coreDistributionRoot, workspaceRoot)) {
    throw new Error("workspaceRoot must be outside the runtime and core distribution roots.");
  }
  const workspaceStatus = await lstat(workspaceRoot);
  if ((workspaceStatus.mode & 0o777) !== 0o700 ||
      (typeof process.getuid === "function" && workspaceStatus.uid !== process.getuid())) {
    throw new Error("workspaceRoot must be an owner-controlled 0700 directory.");
  }

  const config: FinanceBridgeConfig = {
    repoRoot,
    coreDistributionRoot,
    pythonExecutable: pythonExecutable.path,
    workspaceRoot,
    agentProfileV2: {
      openclawPackageSha256: profile.openclawPackageSha256,
      financeCommit: profile.financeCommit,
      coreVersion: profile.coreVersion,
      coreManifestSha256: profile.coreManifestSha256,
      coreWheelSha256: profile.coreWheelSha256,
      coreApiContractVersion: profile.coreApiContractVersion,
      coreMigrationLedgerDigest: profile.coreMigrationLedgerDigest,
      pluginBuildSha256: profile.pluginBuildSha256,
      executionClass: profile.executionClass,
    },
  };
  PYTHON_EXECUTABLE_PROOFS.set(config, pythonExecutable.proof);
  return config;
}
