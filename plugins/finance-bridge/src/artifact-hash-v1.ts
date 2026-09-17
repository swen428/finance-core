import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open, opendir, readFile, realpath } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

export const ARTIFACT_HASH_POLICY_VERSION = "finance-runtime-artifact-tree-v1";
export const BUILD_SOURCE_POLICY_VERSION = "finance-plugin-build-source-v3";
export const OBSERVER_SOURCE_POLICY_VERSION = "finance-gate5-rehearsal-observer-source-v1";
export const OBSERVER_SOURCE_VERSION = "finance-gate5-rehearsal-observer-v1";

const MAX_FILES = 50_000;
const MAX_BYTES = 2 * 1024 * 1024 * 1024;
const MAX_IDENTITY_BYTES = 2 * 1024 * 1024;
const READ_BUFFER_BYTES = 64 * 1024;

export type ArtifactKind = "openclaw_package" | "finance_plugin_build";

export interface ArtifactHashEntryV1 {
  path: string;
  byte_count: number;
  mode: number;
  sha256: string;
}

export interface ArtifactHashResultV1 {
  policy_version: typeof ARTIFACT_HASH_POLICY_VERSION;
  artifact_kind: ArtifactKind;
  package_version: string;
  artifact_sha256: string;
  file_count: number;
  byte_count: number;
  source_identity_sha256: string | null;
  entries: ArtifactHashEntryV1[];
}

export interface BuildSourceIdentityV1 {
  policy_version: typeof BUILD_SOURCE_POLICY_VERSION;
  source_identity_sha256: string;
  file_count: number;
  byte_count: number;
  entries: ArtifactHashEntryV1[];
}

export interface ObserverHashResultV1 {
  policy_version: typeof OBSERVER_SOURCE_POLICY_VERSION;
  observer_version: typeof OBSERVER_SOURCE_VERSION;
  observer_sha256: string;
  file_count: number;
  byte_count: number;
  entries: ArtifactHashEntryV1[];
}

const BUILD_SOURCE_PATHS = [
  "binding.gyp",
  "native",
  "openclaw.plugin.json",
  "npm-shrinkwrap.json",
  "package.json",
  "scripts",
  "src",
  "types",
  "tsconfig.json",
] as const;
export const OBSERVER_SOURCE_PATHS = [
  "scripts/run-gate5-local-rehearsal.mjs",
  "scripts/gate5-loopback-only.sb",
] as const;
const OBSERVER_SOURCE_PATH_SET = new Set<string>(OBSERVER_SOURCE_PATHS);
const BUILD_PROVENANCE_PATH = "dist/build-provenance-v1.json";

const REQUIRED_PATHS: Record<ArtifactKind, readonly string[]> = {
  openclaw_package: [
    "package.json", "npm-shrinkwrap.json", "openclaw.mjs", "dist", "node_modules",
    "node_modules/@openclaw/ai/package.json",
    "node_modules/@openclaw/ai/npm-shrinkwrap.json",
    "node_modules/@openclaw/ai/dist/internal/runtime.mjs",
    "node_modules/@openclaw/codex/package.json",
    "node_modules/@openclaw/codex/npm-shrinkwrap.json",
    "node_modules/@openclaw/codex/openclaw.plugin.json",
    "node_modules/@openclaw/codex/dist/index.js",
  ],
  finance_plugin_build: [
    "package.json", "npm-shrinkwrap.json", "tsconfig.json", "openclaw.plugin.json", "dist/src",
    BUILD_PROVENANCE_PATH,
    "build/Release/finance_bridge_posix.node",
    "node_modules/fs-ext/build/Release/fs_ext.node",
    "node_modules",
  ],
};

const NONEMPTY_FILES: Record<ArtifactKind, ReadonlySet<string>> = {
  openclaw_package: new Set([
    "package.json", "npm-shrinkwrap.json", "openclaw.mjs",
    "node_modules/@openclaw/ai/package.json",
    "node_modules/@openclaw/ai/npm-shrinkwrap.json",
    "node_modules/@openclaw/ai/dist/internal/runtime.mjs",
    "node_modules/@openclaw/codex/package.json",
    "node_modules/@openclaw/codex/npm-shrinkwrap.json",
    "node_modules/@openclaw/codex/openclaw.plugin.json",
    "node_modules/@openclaw/codex/dist/index.js",
  ]),
  finance_plugin_build: new Set([
    "package.json", "npm-shrinkwrap.json", "tsconfig.json", "openclaw.plugin.json",
    BUILD_PROVENANCE_PATH,
    "build/Release/finance_bridge_posix.node",
    "node_modules/fs-ext/build/Release/fs_ext.node",
  ]),
};

const IDENTITY_FILES: Record<ArtifactKind, ReadonlySet<string>> = {
  openclaw_package: new Set([
    "package.json",
    "npm-shrinkwrap.json",
    "node_modules/@openclaw/ai/package.json",
    "node_modules/@openclaw/ai/npm-shrinkwrap.json",
    "node_modules/@openclaw/codex/package.json",
    "node_modules/@openclaw/codex/npm-shrinkwrap.json",
    "node_modules/@openclaw/codex/openclaw.plugin.json",
  ]),
  finance_plugin_build: new Set(["package.json", BUILD_PROVENANCE_PATH]),
};

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function slashPath(value: string): string {
  return value.split(sep).join("/");
}

function compareUtf8(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

function canonicalManifest(value: unknown): string {
  return JSON.stringify(value);
}

function isExecutableBinDirectory(relativePath: string): boolean {
  const parts = slashPath(relativePath).split("/");
  return parts.length >= 2 && parts.at(-1) === ".bin" && parts.at(-2) === "node_modules";
}

async function requireArtifactRoot(rootValue: string): Promise<string> {
  if (!isAbsolute(rootValue) || resolve(rootValue) !== rootValue) {
    throw new Error("Artifact root must be an absolute normalized path.");
  }
  const rootInfo = await lstat(rootValue);
  if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) {
    throw new Error("Artifact root must be a real directory, not a symlink.");
  }
  if (await realpath(rootValue) !== rootValue) {
    throw new Error("Artifact root must not traverse a symlink.");
  }
  return rootValue;
}

async function collectPath(root: string, relativePath: string, files: string[]): Promise<void> {
  const absolute = join(root, relativePath);
  const info = await lstat(absolute);
  if (info.isSymbolicLink()) throw new Error(`Artifact path is a symlink: ${relativePath}`);
  if (info.isFile()) {
    files.push(relativePath);
    if (files.length > MAX_FILES) throw new Error("Artifact file count exceeds the bounded policy.");
    return;
  }
  if (!info.isDirectory()) {
    throw new Error(`Artifact path is not a regular file or directory: ${relativePath}`);
  }
  const initialCount = files.length;
  const pending = [relativePath];
  while (pending.length > 0) {
    const directory = pending.pop()!;
    if (await realpath(join(root, directory)) !== join(root, directory)) {
      throw new Error(`Artifact directory traverses a symlink: ${slashPath(directory)}`);
    }
    const handle = await opendir(join(root, directory));
    for await (const child of handle) {
      const childRelative = join(directory, child.name);
      if (isExecutableBinDirectory(childRelative)) continue;
      const childInfo = await lstat(join(root, childRelative));
      if (childInfo.isSymbolicLink()) {
        throw new Error(`Artifact path is a symlink: ${slashPath(childRelative)}`);
      }
      if (childInfo.isDirectory()) pending.push(childRelative);
      else if (childInfo.isFile()) {
        files.push(childRelative);
        if (files.length > MAX_FILES) {
          throw new Error("Artifact file count exceeds the bounded policy.");
        }
      } else {
        throw new Error(`Artifact contains a non-regular entry: ${slashPath(childRelative)}`);
      }
    }
  }
  if (files.length === initialCount) throw new Error(`Artifact directory is empty: ${relativePath}`);
}

interface StableStatIdentity {
  dev: bigint;
  ino: bigint;
  size: bigint;
  mode: bigint;
  mtimeNs: bigint;
  ctimeNs: bigint;
}

function sameIdentity(left: StableStatIdentity, right: StableStatIdentity): boolean {
  return left.dev === right.dev && left.ino === right.ino && left.size === right.size &&
    left.mode === right.mode && left.mtimeNs === right.mtimeNs && left.ctimeNs === right.ctimeNs;
}

async function hashStableFile(
  root: string,
  path: string,
  remainingBytes: number,
  requireNonempty: boolean,
  captureIdentityBytes: boolean,
): Promise<{ entry: ArtifactHashEntryV1; identityBytes?: Buffer }> {
  const absolute = join(root, path);
  const normalizedRelative = relative(root, absolute);
  if (normalizedRelative.startsWith(`..${sep}`) || normalizedRelative === "..") {
    throw new Error("Artifact path escapes its root.");
  }
  const handle = await open(absolute, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const before = await handle.stat({ bigint: true });
    if (!before.isFile() || before.size > BigInt(remainingBytes)) {
      throw new Error("Artifact file is not regular or exceeds the remaining byte budget.");
    }
    if (requireNonempty && before.size === 0n) {
      throw new Error(`Required artifact file is empty: ${slashPath(path)}`);
    }
    if (captureIdentityBytes && before.size > BigInt(MAX_IDENTITY_BYTES)) {
      throw new Error(`Artifact identity file exceeds its byte limit: ${slashPath(path)}`);
    }
    const digest = createHash("sha256");
    const identityChunks: Buffer[] | undefined = captureIdentityBytes ? [] : undefined;
    const buffer = Buffer.allocUnsafe(READ_BUFFER_BYTES);
    let position = 0;
    const size = Number(before.size);
    while (position < size) {
      const requested = Math.min(buffer.byteLength, size - position);
      const { bytesRead } = await handle.read(buffer, 0, requested, position);
      if (bytesRead <= 0) throw new Error("Artifact file changed while it was being hashed.");
      const chunk = buffer.subarray(0, bytesRead);
      digest.update(chunk);
      identityChunks?.push(Buffer.from(chunk));
      position += bytesRead;
    }
    const after = await handle.stat({ bigint: true });
    if (!sameIdentity(before, after)) {
      throw new Error("Artifact file changed while it was being hashed.");
    }
    return {
      entry: {
        path: slashPath(path),
        byte_count: size,
        mode: Number(before.mode & 0o777n),
        sha256: digest.digest("hex"),
      },
      identityBytes: identityChunks === undefined
        ? undefined
        : Buffer.concat(identityChunks, size),
    };
  } finally {
    await handle.close();
  }
}

function requireIdentityBytes(
  identityBytes: ReadonlyMap<string, Buffer>,
  path: string,
): Buffer {
  const bytes = identityBytes.get(path);
  if (bytes === undefined) throw new Error(`Artifact identity bytes are missing: ${path}`);
  return bytes;
}

function requirePackageIdentity(
  identityBytes: ReadonlyMap<string, Buffer>,
  kind: ArtifactKind,
): string {
  const parsed = JSON.parse(
    requireIdentityBytes(identityBytes, "package.json").toString("utf8"),
  ) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Artifact package.json must be an object.");
  }
  const manifest = parsed as Record<string, unknown>;
  const expectedName = kind === "openclaw_package"
    ? "openclaw"
    : "@finance-codex/finance-bridge";
  if (manifest.name !== expectedName || typeof manifest.version !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/u.test(manifest.version)) {
    throw new Error("Artifact package identity does not match its declared kind.");
  }
  return manifest.version;
}

export async function computeBuildSourceIdentityV1(
  rootValue: string,
): Promise<BuildSourceIdentityV1> {
  const root = await requireArtifactRoot(rootValue);
  const collected: string[] = [];
  for (const path of BUILD_SOURCE_PATHS) await collectPath(root, path, collected);
  const paths = [...new Set(collected)].filter((path) => !OBSERVER_SOURCE_PATH_SET.has(
    slashPath(path),
  )).sort((left, right) => (
    compareUtf8(slashPath(left), slashPath(right))
  ));
  const entries: ArtifactHashEntryV1[] = [];
  let byteCount = 0;
  for (const path of paths) {
    const { entry } = await hashStableFile(
      root,
      path,
      MAX_BYTES - byteCount,
      true,
      false,
    );
    byteCount += entry.byte_count;
    entries.push(entry);
  }
  const material: Pick<BuildSourceIdentityV1, "policy_version" | "entries"> = {
    policy_version: BUILD_SOURCE_POLICY_VERSION,
    entries,
  };
  return {
    ...material,
    source_identity_sha256: sha256(
      `${BUILD_SOURCE_POLICY_VERSION}\0${canonicalManifest(material)}`,
    ),
    file_count: entries.length,
    byte_count: byteCount,
  };
}

export async function computeObserverHashV1(
  rootValue: string,
): Promise<ObserverHashResultV1> {
  const root = await requireArtifactRoot(rootValue);
  const entries: ArtifactHashEntryV1[] = [];
  let byteCount = 0;
  for (const path of OBSERVER_SOURCE_PATHS) {
    const { entry } = await hashStableFile(
      root,
      path,
      MAX_BYTES - byteCount,
      true,
      false,
    );
    byteCount += entry.byte_count;
    entries.push(entry);
  }
  const material: Pick<ObserverHashResultV1,
    "policy_version" | "observer_version" | "entries"> = {
    policy_version: OBSERVER_SOURCE_POLICY_VERSION,
    observer_version: OBSERVER_SOURCE_VERSION,
    entries,
  };
  return {
    ...material,
    observer_sha256: sha256(
      `${OBSERVER_SOURCE_VERSION}\0${canonicalManifest(material)}`,
    ),
    file_count: entries.length,
    byte_count: byteCount,
  };
}

function requireBuildProvenance(
  identityBytes: ReadonlyMap<string, Buffer>,
  sourceIdentity: BuildSourceIdentityV1,
): string {
  const parsed = JSON.parse(
    requireIdentityBytes(identityBytes, BUILD_PROVENANCE_PATH).toString("utf8"),
  ) as unknown;
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Finance build provenance must be an object.");
  }
  const provenance = parsed as Record<string, unknown>;
  if (Object.keys(provenance).sort().join(",") !==
      ["byte_count", "file_count", "policy_version", "source_identity_sha256"].join(",") ||
      provenance.policy_version !== BUILD_SOURCE_POLICY_VERSION ||
      provenance.source_identity_sha256 !== sourceIdentity.source_identity_sha256 ||
      provenance.file_count !== sourceIdentity.file_count ||
      provenance.byte_count !== sourceIdentity.byte_count) {
    throw new Error("Finance build provenance does not match current build inputs.");
  }
  return sourceIdentity.source_identity_sha256;
}

function requireOpenClawAiIdentity(identityBytes: ReadonlyMap<string, Buffer>): void {
  const rootManifest = JSON.parse(
    requireIdentityBytes(identityBytes, "package.json").toString("utf8"),
  ) as unknown;
  const rootShrinkwrap = JSON.parse(
    requireIdentityBytes(identityBytes, "npm-shrinkwrap.json").toString("utf8"),
  ) as unknown;
  const aiManifest = JSON.parse(
    requireIdentityBytes(
      identityBytes,
      "node_modules/@openclaw/ai/package.json",
    ).toString("utf8"),
  ) as unknown;
  const aiShrinkwrap = JSON.parse(
    requireIdentityBytes(
      identityBytes,
      "node_modules/@openclaw/ai/npm-shrinkwrap.json",
    ).toString("utf8"),
  ) as unknown;
  if (
    typeof rootManifest !== "object" || rootManifest === null || Array.isArray(rootManifest) ||
    typeof rootShrinkwrap !== "object" || rootShrinkwrap === null || Array.isArray(rootShrinkwrap) ||
    typeof aiManifest !== "object" || aiManifest === null || Array.isArray(aiManifest) ||
    typeof aiShrinkwrap !== "object" || aiShrinkwrap === null || Array.isArray(aiShrinkwrap)
  ) {
    throw new Error("OpenClaw AI package identity is invalid.");
  }
  const rootRecord = rootManifest as Record<string, unknown>;
  const shrinkwrapRecord = rootShrinkwrap as Record<string, unknown>;
  const aiRecord = aiManifest as Record<string, unknown>;
  const aiShrinkwrapRecord = aiShrinkwrap as Record<string, unknown>;
  const rootDependencies = rootRecord.dependencies;
  const packages = shrinkwrapRecord.packages;
  const shrinkwrapRoot = typeof packages === "object" && packages !== null && !Array.isArray(packages)
    ? (packages as Record<string, unknown>)[""]
    : undefined;
  const shrinkwrapAi = typeof packages === "object" && packages !== null && !Array.isArray(packages)
    ? (packages as Record<string, unknown>)["node_modules/@openclaw/ai"]
    : undefined;
  const rootDependencyRecord = typeof rootDependencies === "object" && rootDependencies !== null &&
    !Array.isArray(rootDependencies)
    ? rootDependencies as Record<string, unknown>
    : undefined;
  const shrinkwrapRootRecord = typeof shrinkwrapRoot === "object" && shrinkwrapRoot !== null &&
    !Array.isArray(shrinkwrapRoot)
    ? shrinkwrapRoot as Record<string, unknown>
    : undefined;
  const shrinkwrapRootDependencies = shrinkwrapRootRecord?.dependencies;
  const shrinkwrapRootDependencyRecord = typeof shrinkwrapRootDependencies === "object" &&
    shrinkwrapRootDependencies !== null && !Array.isArray(shrinkwrapRootDependencies)
    ? shrinkwrapRootDependencies as Record<string, unknown>
    : undefined;
  const shrinkwrapAiRecord = typeof shrinkwrapAi === "object" && shrinkwrapAi !== null &&
    !Array.isArray(shrinkwrapAi)
    ? shrinkwrapAi as Record<string, unknown>
    : undefined;
  const version = rootDependencyRecord?.["@openclaw/ai"];
  const exportsValue = aiRecord.exports;
  const exportsRecord = typeof exportsValue === "object" && exportsValue !== null &&
    !Array.isArray(exportsValue)
    ? exportsValue as Record<string, unknown>
    : undefined;
  const internalExports = exportsRecord?.["./internal/*"];
  const internalExportRecord = typeof internalExports === "object" && internalExports !== null &&
    !Array.isArray(internalExports)
    ? internalExports as Record<string, unknown>
    : undefined;
  const aiPackages = aiShrinkwrapRecord.packages;
  const aiShrinkwrapRoot = typeof aiPackages === "object" && aiPackages !== null &&
    !Array.isArray(aiPackages)
    ? (aiPackages as Record<string, unknown>)[""]
    : undefined;
  const aiShrinkwrapRootRecord = typeof aiShrinkwrapRoot === "object" &&
    aiShrinkwrapRoot !== null && !Array.isArray(aiShrinkwrapRoot)
    ? aiShrinkwrapRoot as Record<string, unknown>
    : undefined;
  const normalizedDependencyMap = (value: unknown): [string, string][] | undefined => {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
    const entries = Object.entries(value);
    if (entries.some(([, spec]) => typeof spec !== "string")) return undefined;
    return (entries as [string, string][]).toSorted(([left], [right]) => left.localeCompare(right));
  };
  if (
    shrinkwrapRecord.lockfileVersion !== 3 ||
    typeof version !== "string" ||
    !/^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$/u.test(version) ||
    shrinkwrapRootDependencyRecord?.["@openclaw/ai"] !== version ||
    shrinkwrapAiRecord?.version !== version ||
    typeof shrinkwrapAiRecord?.integrity !== "string" ||
    !shrinkwrapAiRecord.integrity.startsWith("sha512-") ||
    aiRecord.name !== "@openclaw/ai" ||
    aiRecord.version !== version ||
    aiShrinkwrapRecord.lockfileVersion !== 3 ||
    aiShrinkwrapRecord.name !== aiRecord.name ||
    aiShrinkwrapRecord.version !== version ||
    aiShrinkwrapRootRecord?.name !== aiRecord.name ||
    aiShrinkwrapRootRecord?.version !== version ||
    JSON.stringify(normalizedDependencyMap(aiShrinkwrapRootRecord?.dependencies)) !==
      JSON.stringify(normalizedDependencyMap(aiRecord.dependencies)) ||
    internalExportRecord?.import !== "./dist/internal/*.mjs" ||
    internalExportRecord?.default !== "./dist/internal/*.mjs"
  ) {
    throw new Error("OpenClaw AI package identity is invalid.");
  }
}

async function requireCodexPluginIdentity(
  root: string,
  identityBytes: ReadonlyMap<string, Buffer>,
): Promise<void> {
  const codexRoot = join(root, "node_modules", "@openclaw", "codex");
  if (await realpath(codexRoot) !== codexRoot) {
    throw new Error("Codex plugin root must be inside the real OpenClaw artifact tree.");
  }
  const packageJson = JSON.parse(requireIdentityBytes(
    identityBytes,
    "node_modules/@openclaw/codex/package.json",
  ).toString("utf8")) as unknown;
  const shrinkwrap = JSON.parse(
    requireIdentityBytes(
      identityBytes,
      "node_modules/@openclaw/codex/npm-shrinkwrap.json",
    ).toString("utf8"),
  ) as unknown;
  const manifest = JSON.parse(
    requireIdentityBytes(
      identityBytes,
      "node_modules/@openclaw/codex/openclaw.plugin.json",
    ).toString("utf8"),
  ) as unknown;
  if (typeof packageJson !== "object" || packageJson === null || Array.isArray(packageJson) ||
      typeof shrinkwrap !== "object" || shrinkwrap === null || Array.isArray(shrinkwrap) ||
      typeof manifest !== "object" || manifest === null || Array.isArray(manifest)) {
    throw new Error("Codex plugin package or manifest identity is invalid.");
  }
  const packageRecord = packageJson as Record<string, unknown>;
  const shrinkwrapRecord = shrinkwrap as Record<string, unknown>;
  const manifestRecord = manifest as Record<string, unknown>;
  const packageOpenClawValue = packageRecord.openclaw;
  const contractsValue = manifestRecord.contracts;
  const packageOpenClaw = typeof packageOpenClawValue === "object" &&
    packageOpenClawValue !== null && !Array.isArray(packageOpenClawValue)
    ? packageOpenClawValue as Record<string, unknown>
    : undefined;
  const contracts = typeof contractsValue === "object" &&
    contractsValue !== null && !Array.isArray(contractsValue)
    ? contractsValue as Record<string, unknown>
    : undefined;
  const shrinkwrapPackages = shrinkwrapRecord.packages;
  const shrinkwrapRoot = typeof shrinkwrapPackages === "object" &&
    shrinkwrapPackages !== null && !Array.isArray(shrinkwrapPackages) &&
    typeof (shrinkwrapPackages as Record<string, unknown>)[""] === "object" &&
    (shrinkwrapPackages as Record<string, unknown>)[""] !== null &&
    !Array.isArray((shrinkwrapPackages as Record<string, unknown>)[""])
    ? (shrinkwrapPackages as Record<string, Record<string, unknown>>)[""]
    : undefined;
  const normalizedDependencies = (value: unknown, label: string): Record<string, string> => {
    if (value === undefined) return {};
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new Error(`Codex plugin ${label} dependencies are invalid.`);
    }
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.some((entry) => typeof entry[1] !== "string")) {
      throw new Error(`Codex plugin ${label} dependencies are invalid.`);
    }
    return Object.fromEntries(
      entries.sort(([left], [right]) => left.localeCompare(right)),
    ) as Record<string, string>;
  };
  if (packageRecord.name !== "@openclaw/codex" ||
      typeof packageRecord.version !== "string" || packageRecord.version.length === 0 ||
      shrinkwrapRecord.lockfileVersion !== 3 ||
      shrinkwrapRecord.name !== packageRecord.name ||
      shrinkwrapRecord.version !== packageRecord.version ||
      shrinkwrapRoot?.name !== packageRecord.name ||
      shrinkwrapRoot.version !== packageRecord.version ||
      JSON.stringify(normalizedDependencies(shrinkwrapRoot.dependencies, "shrinkwrap root")) !==
        JSON.stringify(normalizedDependencies(packageRecord.dependencies, "package")) ||
      JSON.stringify(normalizedDependencies(shrinkwrapRoot.optionalDependencies, "shrinkwrap root")) !==
        JSON.stringify(normalizedDependencies(packageRecord.optionalDependencies, "package")) ||
      packageOpenClaw === undefined ||
      JSON.stringify(packageOpenClaw.runtimeExtensions) !== JSON.stringify(["./dist/index.js"]) ||
      manifestRecord.id !== "codex" ||
      JSON.stringify(manifestRecord.providers) !== JSON.stringify(["codex"]) ||
      contracts === undefined ||
      JSON.stringify(contracts.tools) !== JSON.stringify(["codex_threads"])) {
    throw new Error("Codex plugin package or manifest identity is invalid.");
  }
}

export async function resolveOpenClawArtifactRootV1(argv1Value: string): Promise<string> {
  if (!isAbsolute(argv1Value)) throw new Error("OpenClaw argv[1] must be an absolute path.");
  let current = dirname(await realpath(argv1Value));
  for (let depth = 0; depth < 12; depth += 1) {
    try {
      const parsed = JSON.parse(await readFile(join(current, "package.json"), "utf8")) as unknown;
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) &&
          (parsed as Record<string, unknown>).name === "openclaw") {
        return await requireArtifactRoot(current);
      }
    } catch {
      // Continue only through ancestors of the real CLI entrypoint.
    }
    const parent = dirname(current);
    if (parent === current || current.endsWith(`${sep}node_modules`)) break;
    current = parent;
  }
  throw new Error("The running CLI entrypoint is not inside an OpenClaw package root.");
}

export async function computeArtifactHashV1(
  kind: ArtifactKind,
  rootValue: string,
): Promise<ArtifactHashResultV1> {
  if (!(kind in REQUIRED_PATHS)) throw new Error("Artifact kind is invalid.");
  const root = await requireArtifactRoot(rootValue);
  const collected: string[] = [];
  for (const path of REQUIRED_PATHS[kind]) await collectPath(root, path, collected);
  const paths = [...new Set(collected)].sort((left, right) => (
    compareUtf8(slashPath(left), slashPath(right))
  ));
  if (paths.length === 0 || paths.length > MAX_FILES) {
    throw new Error("Artifact file count is outside the bounded policy.");
  }
  const entries: ArtifactHashEntryV1[] = [];
  const identityBytes = new Map<string, Buffer>();
  let byteCount = 0;
  for (const path of paths) {
    const normalizedPath = slashPath(path);
    const { entry, identityBytes: capturedBytes } = await hashStableFile(
      root,
      path,
      MAX_BYTES - byteCount,
      NONEMPTY_FILES[kind].has(normalizedPath),
      IDENTITY_FILES[kind].has(normalizedPath),
    );
    if (capturedBytes !== undefined) identityBytes.set(normalizedPath, capturedBytes);
    byteCount += entry.byte_count;
    entries.push(entry);
  }
  const packageVersion = requirePackageIdentity(identityBytes, kind);
  let sourceIdentitySha256: string | null = null;
  if (kind === "openclaw_package") {
    requireOpenClawAiIdentity(identityBytes);
    await requireCodexPluginIdentity(root, identityBytes);
  } else {
    sourceIdentitySha256 = requireBuildProvenance(
      identityBytes,
      await computeBuildSourceIdentityV1(root),
    );
  }
  const material: Pick<
    ArtifactHashResultV1,
    "policy_version" | "artifact_kind" | "entries"
  > = {
    policy_version: ARTIFACT_HASH_POLICY_VERSION,
    artifact_kind: kind,
    entries,
  };
  return {
    ...material,
    package_version: packageVersion,
    artifact_sha256: sha256(
      `finance-runtime-artifact-tree-v1\0${canonicalManifest(material)}`,
    ),
    file_count: entries.length,
    byte_count: byteCount,
    source_identity_sha256: sourceIdentitySha256,
  };
}
