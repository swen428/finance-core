import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { lstat, readFile, realpath } from "node:fs/promises";
import { join, resolve, sep } from "node:path";
const PLATFORM_RECEIPT = "platform/openclaw-2026.7.1-2-max-retries.json";
const FINANCE_BINDING = "build/Release/finance_bridge_posix.node";
const FS_EXT_BINDING = "node_modules/fs-ext/build/Release/fs_ext.node";
const COMPILED_RUNTIME = "dist/src/finance-agent-runtime-v2.js";
const BUILD_PROVENANCE = "dist/build-provenance-v1.json";
const PLATFORM_PATCH = "platform/openclaw-2026.7.1-2-max-retries.patch";
const STATIC_AUDIT = "platform/openclaw-2026.7.1-2-security-audit.json";
const FULL_NPM_AUDIT = "platform/finance-plugin-full-audit.json";
const PRODUCTION_NPM_AUDIT = "platform/finance-plugin-production-audit.json";
const STATIC_AUDIT_SCOPE = "reviewed_supply_chain_baseline_without_stage_a_config";
function record(value, label) {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
        throw new Error(`${label} must be an object.`);
    }
    return value;
}
function requireEqual(actual, expected, label) {
    if (actual !== expected) {
        throw new Error(`${label} mismatch: expected ${String(expected)}, got ${String(actual)}.`);
    }
}
function requireExactKeys(value, expectedKeys, label) {
    const actual = Object.keys(value).toSorted();
    const expected = [...expectedKeys].toSorted();
    requireEqual(JSON.stringify(actual), JSON.stringify(expected), `${label} fields`);
}
function requireZeroNpmAudit(value, label) {
    const audit = record(value, label);
    for (const severity of ["critical", "high", "moderate", "low", "total"]) {
        requireEqual(audit[severity], 0, `${label} ${severity}`);
    }
}
function sha256(bytes) {
    return createHash("sha256").update(bytes).digest("hex");
}
async function verifyNpmAuditEvidence(params) {
    const label = `Finance plugin ${params.receiptPrefix} npm audit`;
    const expectedFileName = params.expectedFile.slice("platform/".length);
    requireEqual(params.expectedArtifact[`${params.receiptPrefix}_audit_evidence_file`], expectedFileName, `${label} evidence file`);
    const expectedHash = params.expectedArtifact[`${params.receiptPrefix}_audit_sha256`];
    if (typeof expectedHash !== "string" || !/^[0-9a-f]{64}$/u.test(expectedHash)) {
        throw new Error(`${label} sha256 is invalid.`);
    }
    const evidenceBytes = (await readRegularFile(params.pluginRoot, params.expectedFile)).bytes;
    requireEqual(sha256(evidenceBytes), expectedHash, `${label} evidence sha256`);
    const payload = record(JSON.parse(evidenceBytes.toString("utf8")), `${label} evidence`);
    requireEqual(payload.auditReportVersion, 2, `${label} report version`);
    const vulnerabilities = record(payload.vulnerabilities, `${label} vulnerabilities`);
    requireEqual(Object.keys(vulnerabilities).length, 0, `${label} vulnerability entries`);
    const metadata = record(payload.metadata, `${label} metadata`);
    const actualVulnerabilities = record(metadata.vulnerabilities, `${label} vulnerability summary`);
    const actualDependencies = record(metadata.dependencies, `${label} dependency summary`);
    const expectedSummary = record(params.expectedArtifact[`${params.receiptPrefix}_audit`], `${label} receipt summary`);
    for (const severity of ["critical", "high", "moderate", "low", "total"]) {
        requireEqual(actualVulnerabilities[severity], 0, `${label} ${severity}`);
        requireEqual(expectedSummary[severity], actualVulnerabilities[severity], `${label} ${severity}`);
    }
    for (const [receiptField, auditField] of [
        ["production_dependencies", "prod"],
        ["development_dependencies", "dev"],
        ["optional_dependencies", "optional"],
        ["total_dependencies", "total"],
    ]) {
        requireEqual(expectedSummary[receiptField], actualDependencies[auditField], `${label} ${receiptField}`);
    }
    requireEqual(actualDependencies.peer, 0, `${label} peer dependencies`);
    requireEqual(actualDependencies.peerOptional, 0, `${label} optional peer dependencies`);
}
function defaultCodeSignatureVerifier(path) {
    const result = spawnSync("/usr/bin/codesign", ["--verify", "--strict", path], {
        stdio: "pipe",
    });
    if (result.error)
        throw result.error;
    if (result.status !== 0) {
        throw new Error(`Native binding signature verification failed: ${path}`);
    }
}
async function readRegularFile(root, relativePath) {
    const absolutePath = resolve(root, relativePath);
    if (absolutePath !== join(root, relativePath) ||
        (!absolutePath.startsWith(`${root}${sep}`) && absolutePath !== root)) {
        throw new Error(`Platform receipt path escapes the plugin root: ${relativePath}`);
    }
    const info = await lstat(absolutePath);
    if (!info.isFile() || info.isSymbolicLink() || await realpath(absolutePath) !== absolutePath) {
        throw new Error(`Platform receipt path is not one real regular file: ${relativePath}`);
    }
    return {
        bytes: await readFile(absolutePath),
        mode: info.mode & 0o777,
        absolutePath,
    };
}
async function verifyBoundEntry(params) {
    const expected = record(params.expectedValue, params.label);
    requireEqual(expected.path, params.expectedPath, `${params.label} path`);
    const entry = params.artifactEntries.get(params.expectedPath);
    if (entry === undefined)
        throw new Error(`${params.label} is absent from the artifact.`);
    for (const field of ["sha256", "byte_count", "mode"]) {
        requireEqual(entry[field], expected[field], `${params.label} ${field}`);
    }
    const current = await readRegularFile(params.pluginRoot, params.expectedPath);
    requireEqual(current.bytes.byteLength, expected.byte_count, `${params.label} current byte_count`);
    requireEqual(current.mode, expected.mode, `${params.label} current mode`);
    requireEqual(createHash("sha256").update(current.bytes).digest("hex"), expected.sha256, `${params.label} current sha256`);
    return current.absolutePath;
}
export async function verifyPlatformArtifactReceiptV1(params) {
    const pluginRoot = resolve(params.pluginRoot);
    if (pluginRoot !== params.pluginRoot || await realpath(pluginRoot) !== pluginRoot) {
        throw new Error("Platform verifier requires an absolute real plugin root.");
    }
    const packageBytes = (await readRegularFile(pluginRoot, "package.json")).bytes;
    const packageJson = record(JSON.parse(packageBytes.toString("utf8")), "Finance plugin package");
    const engines = record(packageJson.engines, "Finance plugin engines");
    const peerDependencies = record(packageJson.peerDependencies, "Finance plugin peer dependencies");
    const peerDependenciesMeta = record(packageJson.peerDependenciesMeta, "Finance plugin peer dependency metadata");
    const openClawPeerMeta = record(peerDependenciesMeta.openclaw, "Finance plugin OpenClaw peer metadata");
    const devDependencies = record(packageJson.devDependencies, "Finance plugin development dependencies");
    const expectedNodeVersion = `v${String(engines.node ?? "")}`;
    const environment = {
        nodeVersion: params.environment?.nodeVersion ?? process.version,
        platform: params.environment?.platform ?? process.platform,
        arch: params.environment?.arch ?? process.arch,
        verifyCodeSignature: params.environment?.verifyCodeSignature ?? defaultCodeSignatureVerifier,
    };
    requireEqual(environment.nodeVersion, expectedNodeVersion, "Node version");
    requireEqual(environment.platform, "darwin", "artifact platform");
    requireEqual(environment.arch, "arm64", "artifact architecture");
    requireEqual(params.artifact.artifact_kind, "finance_plugin_build", "artifact kind");
    requireEqual(params.artifact.package_version, packageJson.version, "artifact package version");
    const receiptBytes = (await readRegularFile(pluginRoot, PLATFORM_RECEIPT)).bytes;
    const receipt = record(JSON.parse(receiptBytes.toString("utf8")), "platform receipt");
    requireEqual(receipt.schema_version, "finance-openclaw-platform-patch-v1", "platform schema");
    requireEqual(receipt.npm_package_version, peerDependencies.openclaw, "OpenClaw npm package version");
    requireEqual(openClawPeerMeta.optional, true, "OpenClaw peer optional boundary");
    requireEqual(devDependencies.openclaw, undefined, "development OpenClaw host dependency");
    requireEqual(receipt.upstream_tag, `v${String(peerDependencies.openclaw)}`, "upstream tag");
    requireEqual(receipt.patch_file, PLATFORM_PATCH.slice("platform/".length), "platform patch file");
    const patchHash = receipt.patch_sha256;
    if (typeof patchHash !== "string" || !/^[0-9a-f]{64}$/u.test(patchHash)) {
        throw new Error("Platform patch sha256 is invalid.");
    }
    requireEqual(sha256((await readRegularFile(pluginRoot, PLATFORM_PATCH)).bytes), patchHash, "platform patch sha256");
    const supply = record(receipt.verified_supply_chain, "verified supply chain");
    requireEqual(supply.node_version, engines.node, "receipt Node version");
    const staticAudit = record(supply.openclaw_static_security_audit, "OpenClaw static security audit");
    requireEqual(staticAudit.evidence_scope, STATIC_AUDIT_SCOPE, "static audit evidence scope");
    requireEqual(staticAudit.evidence_file, STATIC_AUDIT.slice("platform/".length), "static audit evidence file");
    const staticAuditHash = staticAudit.audit_sha256;
    if (typeof staticAuditHash !== "string" || !/^[0-9a-f]{64}$/u.test(staticAuditHash)) {
        throw new Error("Static audit sha256 is invalid.");
    }
    const staticAuditBytes = (await readRegularFile(pluginRoot, STATIC_AUDIT)).bytes;
    requireEqual(sha256(staticAuditBytes), staticAuditHash, "static audit evidence sha256");
    const staticAuditPayload = record(JSON.parse(staticAuditBytes.toString("utf8")), "static audit evidence");
    const staticAuditSummary = record(staticAuditPayload.summary, "static audit evidence summary");
    for (const severity of ["critical", "warn", "info"]) {
        requireEqual(staticAuditSummary[severity], staticAudit[severity], `static audit ${severity}`);
    }
    requireEqual(staticAuditSummary.critical, 0, "static audit critical");
    requireEqual(staticAuditSummary.warn, 0, "static audit warn");
    if (!Array.isArray(staticAuditPayload.secretDiagnostics)) {
        throw new Error("Static audit sensitive diagnostics must be an array.");
    }
    requireEqual(staticAuditPayload.secretDiagnostics.length, staticAudit.secret_diagnostics, "static audit sensitive diagnostics");
    requireEqual(staticAudit.secret_diagnostics, 0, "static audit sensitive diagnostics receipt");
    const expectedArtifact = record(supply.finance_plugin_artifact, "Finance plugin artifact receipt");
    requireEqual(expectedArtifact.package_name, packageJson.name, "Finance plugin package name");
    requireEqual(expectedArtifact.package_version, packageJson.version, "Finance plugin package version");
    const dependencyBoundary = record(expectedArtifact.development_dependency_boundary, "Finance plugin development dependency boundary");
    requireEqual(dependencyBoundary.runtime_peer_version, peerDependencies.openclaw, "runtime peer version");
    requireEqual(dependencyBoundary.runtime_peer_optional, true, "runtime peer optional receipt");
    requireEqual(dependencyBoundary.runtime_package_installed, false, "runtime package installation receipt");
    requireEqual(dependencyBoundary.compile_sdk_package, "openclaw-sdk", "compile SDK package");
    requireEqual(dependencyBoundary.compile_sdk_version, "2026.7.2-beta.6", "compile SDK version");
    requireEqual(devDependencies["openclaw-sdk"], "npm:openclaw@2026.7.2-beta.6", "compile SDK alias");
    requireEqual(dependencyBoundary.ai_test_package, "@openclaw/ai", "AI test package");
    requireEqual(dependencyBoundary.ai_test_version, "2026.7.2-beta.6", "AI test version");
    requireEqual(devDependencies["@openclaw/ai"], "2026.7.2-beta.6", "AI test dependency");
    requireZeroNpmAudit(expectedArtifact.full_audit, "Finance plugin full npm audit");
    requireZeroNpmAudit(expectedArtifact.production_audit, "Finance plugin production npm audit");
    await verifyNpmAuditEvidence({
        pluginRoot,
        expectedArtifact,
        receiptPrefix: "full",
        expectedFile: FULL_NPM_AUDIT,
    });
    await verifyNpmAuditEvidence({
        pluginRoot,
        expectedArtifact,
        receiptPrefix: "production",
        expectedFile: PRODUCTION_NPM_AUDIT,
    });
    for (const field of [
        "policy_version", "package_version", "artifact_sha256", "file_count", "byte_count",
    ]) {
        requireEqual(params.artifact[field], expectedArtifact[field], `Finance plugin artifact ${field}`);
    }
    const artifactEntries = new Map(params.artifact.entries.map((entry) => [entry.path, entry]));
    if (params.artifact.entries.some((entry) => entry.path === "node_modules/openclaw" || entry.path.startsWith("node_modules/openclaw/"))) {
        throw new Error("Finance plugin artifact must not install or bundle the runtime OpenClaw peer.");
    }
    requireEqual(params.openclawArtifact.artifact_kind, "openclaw_package", "OpenClaw artifact kind");
    const rootArtifact = record(supply.root_artifact, "OpenClaw root artifact receipt");
    const aiArtifact = record(supply.ai_artifact, "OpenClaw AI artifact receipt");
    const codexArtifact = record(supply.codex_artifact, "OpenClaw Codex artifact receipt");
    requireExactKeys(rootArtifact, ["package_version", "package_json_sha256", "npm_shrinkwrap_sha256"], "OpenClaw root artifact receipt");
    requireExactKeys(aiArtifact, ["package_json_sha256", "npm_shrinkwrap_sha256"], "OpenClaw AI artifact receipt");
    requireExactKeys(codexArtifact, ["package_json_sha256", "npm_shrinkwrap_sha256"], "OpenClaw Codex artifact receipt");
    const combinedOpenClawArtifact = record(supply.combined_openclaw_artifact, "combined OpenClaw artifact receipt");
    requireExactKeys(combinedOpenClawArtifact, ["policy_version", "artifact_sha256", "file_count", "byte_count"], "combined OpenClaw artifact receipt");
    requireEqual(params.openclawArtifact.package_version, rootArtifact.package_version, "OpenClaw runtime package version");
    for (const field of ["policy_version", "artifact_sha256", "file_count", "byte_count"]) {
        requireEqual(params.openclawArtifact[field], combinedOpenClawArtifact[field], `combined OpenClaw artifact ${field}`);
    }
    const openclawEntries = new Map(params.openclawArtifact.entries.map((entry) => [entry.path, entry]));
    requireEqual(openclawEntries.get("package.json")?.sha256, rootArtifact.package_json_sha256, "OpenClaw root package.json sha256");
    requireEqual(openclawEntries.get("npm-shrinkwrap.json")?.sha256, rootArtifact.npm_shrinkwrap_sha256, "OpenClaw root npm-shrinkwrap sha256");
    for (const [label, artifactReceipt, prefix] of [
        ["OpenClaw AI", aiArtifact, "node_modules/@openclaw/ai"],
        ["OpenClaw Codex", codexArtifact, "node_modules/@openclaw/codex"],
    ]) {
        requireEqual(openclawEntries.get(`${prefix}/package.json`)?.sha256, artifactReceipt.package_json_sha256, `${label} package.json sha256`);
        requireEqual(openclawEntries.get(`${prefix}/npm-shrinkwrap.json`)?.sha256, artifactReceipt.npm_shrinkwrap_sha256, `${label} npm-shrinkwrap sha256`);
    }
    const financeBinding = record(expectedArtifact.native_binding, "Finance native binding");
    requireEqual(financeBinding.platform, "darwin-arm64", "Finance native binding platform");
    requireEqual(financeBinding.signature, "adhoc", "Finance native binding signature");
    requireEqual(financeBinding.codesign_verified, true, "Finance native binding codesign receipt");
    requireEqual(financeBinding.reproducible_build_runs, 2, "Finance native reproducibility runs");
    const financeBindingPath = await verifyBoundEntry({
        pluginRoot,
        artifactEntries,
        expectedValue: financeBinding,
        expectedPath: FINANCE_BINDING,
        label: "Finance native binding",
    });
    const nativeDependencies = expectedArtifact.native_dependencies;
    if (!Array.isArray(nativeDependencies) || nativeDependencies.length !== 1) {
        throw new Error("Finance plugin artifact must bind exactly one native dependency.");
    }
    const fsExt = record(nativeDependencies[0], "fs-ext native binding");
    requireEqual(fsExt.package_name, "fs-ext", "native dependency package");
    requireEqual(fsExt.package_version, "2.1.1", "native dependency version");
    requireEqual(fsExt.platform, "darwin-arm64", "fs-ext native binding platform");
    requireEqual(fsExt.signature, "adhoc", "fs-ext native binding signature");
    requireEqual(fsExt.codesign_verified, true, "fs-ext native binding codesign receipt");
    requireEqual(fsExt.reproducible_build_runs, 2, "fs-ext reproducibility runs");
    const fsExtPath = await verifyBoundEntry({
        pluginRoot,
        artifactEntries,
        expectedValue: fsExt,
        expectedPath: FS_EXT_BINDING,
        label: "fs-ext native binding",
    });
    const compiledRuntime = record(expectedArtifact.compiled_runtime, "compiled runtime receipt");
    const compiledRuntimePath = await verifyBoundEntry({
        pluginRoot,
        artifactEntries,
        expectedValue: compiledRuntime,
        expectedPath: COMPILED_RUNTIME,
        label: "compiled runtime",
    });
    const compiledRuntimeBytes = await readFile(compiledRuntimePath);
    if (!compiledRuntimeBytes.toString("utf8").includes("getLoadedOpenAIPluginSourceV2")) {
        throw new Error("Compiled runtime does not contain the reviewed OpenAI source binding.");
    }
    const buildProvenance = record(expectedArtifact.build_provenance, "build provenance receipt");
    await verifyBoundEntry({
        pluginRoot,
        artifactEntries,
        expectedValue: buildProvenance,
        expectedPath: BUILD_PROVENANCE,
        label: "build provenance",
    });
    requireEqual(params.artifact.source_identity_sha256, buildProvenance.source_identity_sha256, "build provenance source identity");
    if (typeof params.artifact.source_identity_sha256 !== "string") {
        throw new Error("Finance artifact source identity is missing.");
    }
    environment.verifyCodeSignature(financeBindingPath);
    environment.verifyCodeSignature(fsExtPath);
    return {
        artifact_sha256: params.artifact.artifact_sha256,
        file_count: params.artifact.file_count,
        byte_count: params.artifact.byte_count,
        compiled_runtime_sha256: String(compiledRuntime.sha256),
        source_identity_sha256: params.artifact.source_identity_sha256,
    };
}
export async function executeReceiptBoundCompatibilityV1(params) {
    await params.verifyBeforeProvider();
    const outcomes = await params.runProviderCases();
    await params.verifyBeforeReceipt();
    return { outcomes, receipt: await params.writeReceipt(outcomes) };
}
