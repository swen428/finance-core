import assert from "node:assert/strict";
import { execFile as execFileCallback } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmod,
  cp,
  mkdtemp,
  mkdir,
  readFile,
  realpath,
  rename,
  rm,
  symlink,
  truncate,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { promisify } from "node:util";

import type { OpenClawPluginApi } from "openclaw-sdk/plugin-sdk/plugin-entry";

import {
  canonicalProjectionSha256V2,
  type FinanceAgentConfigProjectionV2,
} from "../src/agent-profile-projection-v2.js";
import {
  computeBuildSourceIdentityV1,
  computeArtifactHashV1,
  computeObserverHashV1,
  resolveOpenClawArtifactRootV1,
} from "../src/artifact-hash-v1.js";
import { validatePluginConfig, type FinanceBridgeConfig } from "../src/config.js";
import { executeCompatibilityRegistrationV1 } from "../src/operator-cli-v1.js";
import {
  loadCompatibilityAssetsV1,
  operatorFailureEnvelopeV1,
  OperatorFailureV1,
  registerCompatibilityReceiptV1,
  runCompatibilityHarnessV1,
  verifyArtifactEvidenceV1,
  type CompatibilityLlmResultV1,
} from "../src/model-compatibility-operator-v1.js";
import {
  executeReceiptBoundCompatibilityV1,
  verifyPlatformArtifactReceiptV1,
} from "../src/platform-artifact-verifier-v1.js";
import { createBridgeRequest } from "../src/protocol.js";
import type { BridgeRequest, BridgeResponse, JsonObject } from "../src/protocol.js";
import { BridgeCliRunner } from "../src/subprocess.js";

const REPO_ROOT = resolve("../..");
const execFile = promisify(execFileCallback);
const ACCEPT_TEST_EXECUTABLE = async (): Promise<void> => undefined;
const FIXTURE_PATCH = "fixture patch\n";
const FIXTURE_STATIC_AUDIT = `${JSON.stringify({
  summary: { critical: 0, warn: 0, info: 1 },
  findings: [{ checkId: "summary.attack_surface", severity: "info" }],
  secretDiagnostics: [],
}, null, 2)}\n`;
const FIXTURE_NPM_AUDIT = `${JSON.stringify({
  auditReportVersion: 2,
  vulnerabilities: {},
  metadata: {
    vulnerabilities: { info: 0, low: 0, moderate: 0, high: 0, critical: 0, total: 0 },
    dependencies: { prod: 4, dev: 336, optional: 13, peer: 0, peerOptional: 0, total: 339 },
  },
}, null, 2)}\n`;

function hashFixture(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function bridgeSuccess(request: BridgeRequest, result: JsonObject): BridgeResponse {
  return {
    envelopeVersion: "v1",
    requestId: request.request_id,
    operationId: "op_0123456789abcdef0123456789abcdef",
    status: "ok",
    result,
    idempotentReplay: false,
  };
}

function bridgeFailure(
  request: BridgeRequest,
  code: string,
  message: string,
  details?: JsonObject,
): BridgeResponse {
  return {
    envelopeVersion: "v1",
    requestId: request.request_id,
    operationId: null,
    status: "error",
    error: { code, message, retryable: false, ...(details === undefined ? {} : { details }) },
  };
}

async function write(path: string, body: string | Buffer): Promise<void> {
  await mkdir(resolve(path, ".."), { recursive: true });
  await writeFile(path, body);
}

async function artifact(
  kind: "openclaw_package" | "finance_plugin_build",
  openclawVersion = "1",
): Promise<string> {
  const root = await realpath(await mkdtemp(join(tmpdir(), `finance-artifact-${kind}-`)));
  if (kind === "openclaw_package") {
    await write(join(root, "package.json"), JSON.stringify({
      name: "openclaw",
      version: openclawVersion,
      dependencies: { "@openclaw/ai": "1.0.0" },
    }));
    await write(join(root, "npm-shrinkwrap.json"), JSON.stringify({
      name: "openclaw",
      version: openclawVersion,
      lockfileVersion: 3,
      packages: {
        "": {
          name: "openclaw",
          version: openclawVersion,
          dependencies: { "@openclaw/ai": "1.0.0" },
        },
        "node_modules/@openclaw/ai": {
          version: "1.0.0",
          integrity: `sha512-${"A".repeat(86)}==`,
        },
      },
    }));
    await write(join(root, "openclaw.mjs"), "export {};\n");
    await write(join(root, "dist/runtime.js"), "export const runtime = true;\n");
    await write(join(root, "node_modules/@openclaw/ai/package.json"), JSON.stringify({
      name: "@openclaw/ai",
      version: "1.0.0",
      dependencies: {},
      exports: {
        "./internal/*": {
          types: "./dist/internal/*.d.mts",
          import: "./dist/internal/*.mjs",
          default: "./dist/internal/*.mjs",
        },
      },
    }));
    await write(join(root, "node_modules/@openclaw/ai/npm-shrinkwrap.json"), JSON.stringify({
      name: "@openclaw/ai",
      version: "1.0.0",
      lockfileVersion: 3,
      packages: {
        "": {
          name: "@openclaw/ai",
          version: "1.0.0",
          dependencies: {},
        },
      },
    }));
    await write(
      join(root, "node_modules/@openclaw/ai/dist/internal/runtime.mjs"),
      "export {};\n",
    );
    await write(join(root, "node_modules/@openclaw/codex/package.json"), JSON.stringify({
      name: "@openclaw/codex",
      version: "1",
      dependencies: { zod: "4.4.3" },
      openclaw: { runtimeExtensions: ["./dist/index.js"] },
    }));
    await write(join(root, "node_modules/@openclaw/codex/npm-shrinkwrap.json"), JSON.stringify({
      name: "@openclaw/codex",
      version: "1",
      lockfileVersion: 3,
      packages: {
        "": {
          name: "@openclaw/codex",
          version: "1",
          dependencies: { zod: "4.4.3" },
        },
      },
    }));
    await write(join(root, "node_modules/@openclaw/codex/openclaw.plugin.json"), JSON.stringify({
      id: "codex",
      providers: ["codex"],
      contracts: { tools: ["codex_threads"] },
    }));
    await write(join(root, "node_modules/@openclaw/codex/dist/index.js"), "export {};\n");
  } else {
    await write(
      join(root, "package.json"),
      JSON.stringify({
        name: "@finance-codex/finance-bridge",
        version: "1",
        engines: { node: "24.15.0" },
        peerDependencies: { openclaw: "2026.7.1-2" },
        peerDependenciesMeta: { openclaw: { optional: true } },
        devDependencies: {
          "@openclaw/ai": "2026.7.2-beta.6",
          "openclaw-sdk": "npm:openclaw@2026.7.2-beta.6",
        },
      }),
    );
    await write(join(root, "npm-shrinkwrap.json"), "{}\n");
    await write(join(root, "openclaw.plugin.json"), "{}\n");
    await write(join(root, "binding.gyp"), "{}\n");
    await write(join(root, "native/addon.cc"), "// native source\n");
    await write(join(root, "scripts/build-native.mjs"), "export {};\n");
    await cp(
      join(REPO_ROOT, "plugins/finance-bridge/scripts/verify-core-distribution.py"),
      join(root, "scripts/verify-core-distribution.py"),
    );
    await write(join(root, "src/index.ts"), "export {};\n");
    await write(join(root, "types/openclaw-runtime-peer.d.ts"), "export {};\n");
    await write(join(root, "tsconfig.json"), "{}\n");
    await write(join(root, "platform/openclaw-2026.7.1-2-max-retries.patch"), FIXTURE_PATCH);
    await write(join(root, "platform/openclaw-2026.7.1-2-security-audit.json"), FIXTURE_STATIC_AUDIT);
    await write(join(root, "platform/finance-plugin-full-audit.json"), FIXTURE_NPM_AUDIT);
    await write(join(root, "platform/finance-plugin-production-audit.json"), FIXTURE_NPM_AUDIT);
    await write(join(root, "dist/src/index.js"), "export {};\n");
    await write(
      join(root, "dist/src/finance-agent-runtime-v2.js"),
      "export const getLoadedOpenAIPluginSourceV2 = true;\n",
    );
    await write(join(root, "build/Release/finance_bridge_posix.node"), Buffer.from([1, 2, 3]));
    await write(
      join(root, "node_modules/fs-ext/build/Release/fs_ext.node"),
      Buffer.from([4, 5, 6]),
    );
    const sourceIdentity = await computeBuildSourceIdentityV1(root);
    await write(join(root, "dist/build-provenance-v1.json"), `${JSON.stringify({
      policy_version: sourceIdentity.policy_version,
      source_identity_sha256: sourceIdentity.source_identity_sha256,
      file_count: sourceIdentity.file_count,
      byte_count: sourceIdentity.byte_count,
    })}\n`);
  }
  await write(
    join(root, "node_modules/runtime-dependency/package.json"),
    JSON.stringify({ name: "runtime-dependency", version: "1" }),
  );
  await write(join(root, "node_modules/runtime-dependency/index.js"), "export const value = 1;\n");
  return await realpath(root);
}

async function coreDistributionFixture(
  root: string,
  coreCommit: string,
): Promise<{
  manifestSha256: string;
  wheelSha256: string;
  migrationLedgerDigest: string;
}> {
  const program = String.raw`
import base64,csv,hashlib,io,json,pathlib,shutil,sys,zipfile
source=pathlib.Path(sys.argv[1])/'finance_core'
root=pathlib.Path(sys.argv[2])
commit=sys.argv[3]
allowed={'.json','.py','.sql','.txt'}
files={}
for path in source.rglob('*'):
    if path.is_file() and path.suffix in allowed:
        name=path.relative_to(source.parent).as_posix()
        files[name]=path.read_bytes()
        target=root/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(files[name])
wheel_name='finance_core-0.1.0-py3-none-any.whl'
wheel=root/wheel_name
metadata_root_name='finance_core-0.1.0.dist-info'
metadata_name=f'{metadata_root_name}/METADATA'
metadata=b'Metadata-Version: 2.4\nName: finance-core\nVersion: 0.1.0\n\n'
wheel_files={**files,metadata_name:metadata}
record_name=f'{metadata_root_name}/RECORD'
record_stream=io.StringIO(newline='')
writer=csv.writer(record_stream,lineterminator='\n')
for name,payload in sorted(wheel_files.items()):
    encoded=base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b'=').decode()
    writer.writerow((name,f'sha256={encoded}',str(len(payload))))
writer.writerow((record_name,'',''))
wheel_files[record_name]=record_stream.getvalue().encode()
with zipfile.ZipFile(wheel,'w') as archive:
    for name,payload in sorted(wheel_files.items()): archive.writestr(name,payload)
for name,payload in wheel_files.items():
    target=root/name
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(payload)
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
migrations={pathlib.PurePosixPath(name).name:body for name,body in files.items() if name.startswith('finance_core/resources/migrations/') and name.endswith('.sql')}
digest=hashlib.sha256()
for name in sorted(migrations):
    body=migrations[name]
    digest.update(name.encode()); digest.update(b'\0'); digest.update(len(body).to_bytes(8,'big')); digest.update(body)
ledger=digest.hexdigest()
artifacts=[
 {'filename':'finance-codex-finance-bridge-0.1.0.tgz','sha256':'1'*64,'size_bytes':1},
 {'filename':'finance_core-0.1.0.tar.gz','sha256':'2'*64,'size_bytes':1},
 {'filename':wheel_name,'sha256':sha(wheel),'size_bytes':wheel.stat().st_size},
]
manifest={'api_contract_version':'finance-core-api-v1','artifacts':artifacts,'bridge_version':'0.1.0','core_commit':commit,'core_version':'0.1.0','migration_ledger_digest':ledger,'schema':'finance-core-component-manifest-v1'}
manifest_path=root/'component-manifest-v1.json'
manifest_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
checks={entry['filename']:entry['sha256'] for entry in artifacts}; checks[manifest_path.name]=sha(manifest_path)
(root/'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name,value in sorted(checks.items())))
print(json.dumps({'manifestSha256':sha(manifest_path),'wheelSha256':sha(wheel),'migrationLedgerDigest':ledger}))
`;
  const result = await execFile("/usr/bin/python3", [
    "-c", program, REPO_ROOT, root, coreCommit,
  ]);
  return JSON.parse(result.stdout) as {
    manifestSha256: string;
    wheelSha256: string;
    migrationLedgerDigest: string;
  };
}

function platformReceipt(
  artifactValue: Awaited<ReturnType<typeof computeArtifactHashV1>>,
  openclawArtifact: Awaited<ReturnType<typeof computeArtifactHashV1>>,
): Record<string, unknown> {
  const entry = (path: string) => {
    const value = artifactValue.entries.find((candidate) => candidate.path === path);
    assert.notEqual(value, undefined);
    return value!;
  };
  const financeBinding = entry("build/Release/finance_bridge_posix.node");
  const fsExtBinding = entry("node_modules/fs-ext/build/Release/fs_ext.node");
  const compiledRuntime = entry("dist/src/finance-agent-runtime-v2.js");
  const buildProvenance = entry("dist/build-provenance-v1.json");
  const openclawEntry = (path: string) => {
    const value = openclawArtifact.entries.find((candidate) => candidate.path === path);
    assert.notEqual(value, undefined);
    return value!;
  };
  return {
    schema_version: "finance-openclaw-platform-patch-v1",
    upstream_tag: "v2026.7.1-2",
    npm_package_version: "2026.7.1-2",
    patch_file: "openclaw-2026.7.1-2-max-retries.patch",
    patch_sha256: hashFixture(FIXTURE_PATCH),
    verified_supply_chain: {
      node_version: "24.15.0",
      openclaw_static_security_audit: {
        evidence_scope: "reviewed_supply_chain_baseline_without_stage_a_config",
        critical: 0,
        warn: 0,
        info: 1,
        secret_diagnostics: 0,
        evidence_file: "openclaw-2026.7.1-2-security-audit.json",
        audit_sha256: hashFixture(FIXTURE_STATIC_AUDIT),
      },
      finance_plugin_artifact: {
        package_name: "@finance-codex/finance-bridge",
        package_version: "1",
        development_dependency_boundary: {
          runtime_peer_version: "2026.7.1-2",
          runtime_peer_optional: true,
          runtime_package_installed: false,
          compile_sdk_package: "openclaw-sdk",
          compile_sdk_version: "2026.7.2-beta.6",
          ai_test_package: "@openclaw/ai",
          ai_test_version: "2026.7.2-beta.6",
        },
        full_audit_evidence_file: "finance-plugin-full-audit.json",
        full_audit_sha256: hashFixture(FIXTURE_NPM_AUDIT),
        full_audit: {
          critical: 0, high: 0, moderate: 0, low: 0, total: 0,
          production_dependencies: 4, development_dependencies: 336,
          optional_dependencies: 13, total_dependencies: 339,
        },
        production_audit_evidence_file: "finance-plugin-production-audit.json",
        production_audit_sha256: hashFixture(FIXTURE_NPM_AUDIT),
        production_audit: {
          critical: 0, high: 0, moderate: 0, low: 0, total: 0,
          production_dependencies: 4, development_dependencies: 336,
          optional_dependencies: 13, total_dependencies: 339,
        },
        policy_version: artifactValue.policy_version,
        artifact_sha256: artifactValue.artifact_sha256,
        file_count: artifactValue.file_count,
        byte_count: artifactValue.byte_count,
        native_binding: {
          ...financeBinding,
          platform: "darwin-arm64",
          signature: "adhoc",
          codesign_verified: true,
          reproducible_build_runs: 2,
        },
        native_dependencies: [{
          package_name: "fs-ext",
          package_version: "2.1.1",
          ...fsExtBinding,
          platform: "darwin-arm64",
          signature: "adhoc",
          codesign_verified: true,
          reproducible_build_runs: 2,
        }],
        compiled_runtime: compiledRuntime,
        build_provenance: {
          ...buildProvenance,
          source_identity_sha256: artifactValue.source_identity_sha256,
        },
      },
      root_artifact: {
        package_version: openclawArtifact.package_version,
        package_json_sha256: openclawEntry("package.json").sha256,
        npm_shrinkwrap_sha256: openclawEntry("npm-shrinkwrap.json").sha256,
      },
      ai_artifact: {
        package_json_sha256: openclawEntry("node_modules/@openclaw/ai/package.json").sha256,
        npm_shrinkwrap_sha256: openclawEntry("node_modules/@openclaw/ai/npm-shrinkwrap.json").sha256,
      },
      codex_artifact: {
        package_json_sha256: openclawEntry("node_modules/@openclaw/codex/package.json").sha256,
        npm_shrinkwrap_sha256: openclawEntry("node_modules/@openclaw/codex/npm-shrinkwrap.json").sha256,
      },
      combined_openclaw_artifact: {
        policy_version: openclawArtifact.policy_version,
        artifact_sha256: openclawArtifact.artifact_sha256,
        file_count: openclawArtifact.file_count,
        byte_count: openclawArtifact.byte_count,
      },
    },
  };
}

function response(caseValue: Record<string, unknown>): string {
  const expected = caseValue.expected as Record<string, unknown>;
  const catalog = caseValue.catalog as Record<string, string>;
  const ref = Object.keys(catalog)[0]!;
  const moneyRef = caseValue.case_id === "clear_ocr" ? "e0003"
    : caseValue.case_id === "ocr_prompt_injection" ? "e0002" : ref;
  const fields = [
    "amount", "currency", "transaction_date", "merchant", "description", "account", "category",
  ];
  const values = Object.fromEntries(fields.map((field) => [field, expected[field]]));
  return JSON.stringify({
    schema_version: "finance-ai-facts-v2",
    intent_type: expected.intent_type,
    ...values,
    field_confidence_bps: Object.fromEntries(fields.map((field) => [
      field,
      expected[field] === null ? null : 9000,
    ])),
    field_conflicts: { amount: [], currency: [], transaction_date: [], merchant: [] },
    field_evidence_refs: Object.fromEntries(fields.map((field) => [
      field,
      expected[field] === null ? [] : [field === "amount" || field === "currency" ? moneyRef : ref],
    ])),
  });
}

function reviewedAdmissionHostFixture(pluginRoot: string, openclawRoot: string): {
  currentConfig: Record<string, unknown>;
  loadedSources: Record<string, Record<string, unknown>>;
} {
  const codexRoot = join(openclawRoot, "node_modules/@openclaw/codex");
  const openaiRoot = join(openclawRoot, "dist/extensions/openai");
  const denied = [
    "finance_propose", "finance_get_review", "finance_confirm", "finance_edit",
    "finance_reject", "finance_finalize", "finance_get_status", "finance_health",
    "codex_threads",
  ];
  const sourceEntrySha256 =
    "0462103550920ca01c9a510bdb3f62f69032860014fbaa633957a97b85436ed3";
  return {
    currentConfig: {
      plugins: {
        allow: ["telegram", "finance-bridge", "codex", "openai"],
        load: { paths: [pluginRoot, codexRoot] },
        entries: {
          codex: {
            enabled: true,
            config: {
              codexDynamicToolsLoading: "searchable",
              codexDynamicToolsExclude: [],
            },
          },
          openai: { enabled: true, config: { personality: "friendly" } },
          "finance-bridge": {
            enabled: true,
            llm: { allowModelOverride: true, allowAgentIdOverride: true },
          },
        },
      },
      tools: { deny: denied },
      agents: {
        list: [
          {
            id: "main",
            default: true,
            model: { primary: "openai/gpt-5.6-sol" },
            tools: { deny: denied },
          },
          {
            id: "finance",
            model: { primary: "openai/example-model", fallbacks: [] },
            models: { "openai/example-model": { alias: "Nomi cloud" } },
            tools: { deny: [...denied, "sessions_spawn"] },
            memorySearch: { enabled: false, experimental: { sessionMemory: false } },
            contextInjection: "never",
          },
        ],
      },
      bindings: [],
      hooks: { enabled: false },
    },
    loadedSources: {
      codex: {
        pluginId: "codex",
        packageName: "@openclaw/codex",
        source: join(codexRoot, "dist/index.js"),
        rootDir: codexRoot,
        origin: "config",
        status: "loaded",
        providerIds: ["codex"],
        sourceConfigEntrySha256: sourceEntrySha256,
      },
      openai: {
        pluginId: "openai",
        packageName: "@openclaw/openai-provider",
        source: join(openaiRoot, "index.js"),
        rootDir: openaiRoot,
        origin: "bundled",
        status: "loaded",
        providerIds: ["openai"],
        sourceConfigEntrySha256: sourceEntrySha256,
      },
    },
  };
}

async function projection(): Promise<FinanceAgentConfigProjectionV2> {
  const fixture = JSON.parse(await readFile(
    resolve("../../tests/fixtures/finance_ai/agent_projection_v2_golden.json"),
    "utf8",
  )) as { projection: FinanceAgentConfigProjectionV2 };
  return fixture.projection;
}

test("artifact tree hashes are deterministic, kind-separated, and symlink closed", async () => {
  const openclawRoot = await artifact("openclaw_package");
  const first = await computeArtifactHashV1("openclaw_package", openclawRoot);
  const second = await computeArtifactHashV1("openclaw_package", openclawRoot);
  assert.equal(first.artifact_sha256, second.artifact_sha256);
  assert.equal(first.file_count, 13);
  assert.deepEqual(first.entries.map((entry) => entry.path), [
    "dist/runtime.js", "node_modules/@openclaw/ai/dist/internal/runtime.mjs",
    "node_modules/@openclaw/ai/npm-shrinkwrap.json",
    "node_modules/@openclaw/ai/package.json",
    "node_modules/@openclaw/codex/dist/index.js",
    "node_modules/@openclaw/codex/npm-shrinkwrap.json",
    "node_modules/@openclaw/codex/openclaw.plugin.json",
    "node_modules/@openclaw/codex/package.json",
    "node_modules/runtime-dependency/index.js",
    "node_modules/runtime-dependency/package.json", "npm-shrinkwrap.json",
    "openclaw.mjs", "package.json",
  ]);
  assert.equal(await resolveOpenClawArtifactRootV1(join(openclawRoot, "openclaw.mjs")), openclawRoot);
  await write(join(openclawRoot, "node_modules/runtime-dependency/index.js"), "changed\n");
  const dependencyChanged = await computeArtifactHashV1("openclaw_package", openclawRoot);
  assert.notEqual(dependencyChanged.artifact_sha256, first.artifact_sha256);
  const aiPackagePath = join(openclawRoot, "node_modules/@openclaw/ai/package.json");
  const aiPackage = await readFile(aiPackagePath, "utf8");
  await write(aiPackagePath, JSON.stringify({ name: "@openclaw/ai", version: "1.0.1" }));
  await assert.rejects(
    computeArtifactHashV1("openclaw_package", openclawRoot),
    /OpenClaw AI package identity/u,
  );
  await write(aiPackagePath, aiPackage);
  const aiShrinkwrapPath = join(openclawRoot, "node_modules/@openclaw/ai/npm-shrinkwrap.json");
  const aiShrinkwrap = JSON.parse(await readFile(aiShrinkwrapPath, "utf8")) as Record<string, unknown>;
  aiShrinkwrap.version = "1.0.1";
  await write(aiShrinkwrapPath, JSON.stringify(aiShrinkwrap));
  await assert.rejects(
    computeArtifactHashV1("openclaw_package", openclawRoot),
    /OpenClaw AI package identity/u,
  );
  aiShrinkwrap.version = "1.0.0";
  await write(aiShrinkwrapPath, JSON.stringify(aiShrinkwrap));
  const rootPackagePath = join(openclawRoot, "package.json");
  const rootPackage = JSON.parse(await readFile(rootPackagePath, "utf8")) as Record<string, unknown>;
  delete rootPackage.dependencies;
  await write(rootPackagePath, JSON.stringify(rootPackage));
  await assert.rejects(
    computeArtifactHashV1("openclaw_package", openclawRoot),
    /OpenClaw AI package identity/u,
  );
  rootPackage.dependencies = { "@openclaw/ai": "1.0.0" };
  await write(rootPackagePath, JSON.stringify(rootPackage));
  await write(
    join(openclawRoot, "node_modules/@openclaw/codex/openclaw.plugin.json"),
    JSON.stringify({ id: "codex", providers: ["fallback"], contracts: { tools: [] } }),
  );
  await assert.rejects(
    computeArtifactHashV1("openclaw_package", openclawRoot),
    /Codex plugin package or manifest identity/u,
  );
  await write(join(openclawRoot, "node_modules/@openclaw/codex/openclaw.plugin.json"), JSON.stringify({
    id: "codex",
    providers: ["codex"],
    contracts: { tools: ["codex_threads"] },
  }));
  await rm(join(openclawRoot, "node_modules/@openclaw/codex/npm-shrinkwrap.json"));
  await assert.rejects(
    computeArtifactHashV1("openclaw_package", openclawRoot),
    /npm-shrinkwrap.json/u,
  );
  await write(join(openclawRoot, "node_modules/@openclaw/codex/npm-shrinkwrap.json"), JSON.stringify({
    name: "@openclaw/codex",
    version: "1",
    lockfileVersion: 3,
    packages: {
      "": {
        name: "@openclaw/codex",
        version: "1",
        dependencies: { zod: "4.4.2" },
      },
    },
  }));
  await assert.rejects(
    computeArtifactHashV1("openclaw_package", openclawRoot),
    /Codex plugin package or manifest identity/u,
  );
  await write(join(openclawRoot, "node_modules/@openclaw/codex/npm-shrinkwrap.json"), JSON.stringify({
    name: "@openclaw/codex",
    version: "1",
    lockfileVersion: 3,
    packages: {
      "": {
        name: "@openclaw/codex",
        version: "1",
        dependencies: { zod: "4.4.3" },
      },
    },
  }));
  await write(join(openclawRoot, "openclaw.mjs"), "");
  await assert.rejects(computeArtifactHashV1("openclaw_package", openclawRoot), /empty/u);

  const pluginRoot = await artifact("finance_plugin_build");
  const plugin = await computeArtifactHashV1("finance_plugin_build", pluginRoot);
  assert.notEqual(plugin.artifact_sha256, first.artifact_sha256);
  await write(join(pluginRoot, "src/index.ts"), "export const stale = true;\n");
  await assert.rejects(
    computeArtifactHashV1("finance_plugin_build", pluginRoot),
    /build provenance does not match current build inputs/u,
  );
  await write(join(pluginRoot, "src/index.ts"), "export {};\n");
  await write(
    join(pluginRoot, "types/openclaw-runtime-peer.d.ts"),
    "declare module \"openclaw/plugin-sdk/media-runtime\";\n",
  );
  await assert.rejects(
    computeArtifactHashV1("finance_plugin_build", pluginRoot),
    /build provenance does not match current build inputs/u,
  );
  await write(join(pluginRoot, "types/openclaw-runtime-peer.d.ts"), "export {};\n");
  await rm(join(pluginRoot, "node_modules/fs-ext/build/Release/fs_ext.node"));
  await assert.rejects(
    computeArtifactHashV1("finance_plugin_build", pluginRoot),
    /fs_ext\.node/u,
  );
  await write(
    join(pluginRoot, "node_modules/fs-ext/build/Release/fs_ext.node"),
    Buffer.from([4, 5, 6]),
  );
  await symlink(join(pluginRoot, "package.json"), join(pluginRoot, "dist/src/alias.js"));
  await assert.rejects(
    computeArtifactHashV1("finance_plugin_build", pluginRoot),
    /symlink/u,
  );
});

test("artifact identity replacement cannot escape the bytes bound by the hash", async () => {
  const openclawRoot = await artifact("openclaw_package");
  await truncate(join(openclawRoot, "dist/runtime.js"), 256 * 1024 * 1024);
  const shrinkwrapPath = join(
    openclawRoot,
    "node_modules/@openclaw/codex/npm-shrinkwrap.json",
  );
  const replacementDirectory = await mkdtemp(join(tmpdir(), "finance-artifact-replacement-"));
  const replacementPath = join(replacementDirectory, "npm-shrinkwrap.json");
  await write(replacementPath, JSON.stringify({
    name: "@openclaw/codex",
    version: "1",
    lockfileVersion: 3,
    packages: {
      "": {
        name: "@openclaw/codex",
        version: "1",
        dependencies: { zod: "0.0.0-unreviewed" },
      },
    },
  }));

  const pendingHash = computeArtifactHashV1("openclaw_package", openclawRoot);
  await delay(25);
  await rename(replacementPath, shrinkwrapPath);
  await assert.rejects(pendingHash, /Codex plugin package or manifest identity/u);
});

test("platform receipt verifier executes artifact, runtime, native, and signature checks", async () => {
  const pluginRoot = await artifact("finance_plugin_build");
  const openclawRoot = await artifact("openclaw_package", "2026.7.1");
  await chmod(join(pluginRoot, "build/Release/finance_bridge_posix.node"), 0o755);
  await chmod(join(pluginRoot, "node_modules/fs-ext/build/Release/fs_ext.node"), 0o755);
  const pluginArtifact = await computeArtifactHashV1("finance_plugin_build", pluginRoot);
  const openclawArtifact = await computeArtifactHashV1("openclaw_package", openclawRoot);
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(platformReceipt(pluginArtifact, openclawArtifact)),
  );
  const signatureChecks: string[] = [];
  const evidence = await verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature(path) { signatureChecks.push(path); },
    },
  });
  assert.equal(evidence.artifact_sha256, pluginArtifact.artifact_sha256);
  assert.deepEqual(signatureChecks.map((path) => path.slice(pluginRoot.length + 1)), [
    "build/Release/finance_bridge_posix.node",
    "node_modules/fs-ext/build/Release/fs_ext.node",
  ]);

  const tamperedCodexReceipt = platformReceipt(pluginArtifact, openclawArtifact);
  const tamperedCodexArtifact = (
    tamperedCodexReceipt.verified_supply_chain as Record<string, any>
  ).codex_artifact as Record<string, any>;
  tamperedCodexArtifact.npm_shrinkwrap_sha256 = "4".repeat(64);
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(tamperedCodexReceipt),
  );
  await assert.rejects(
    verifyPlatformArtifactReceiptV1({
      pluginRoot,
      artifact: pluginArtifact,
      openclawArtifact,
      environment: {
        nodeVersion: "v24.15.0",
        platform: "darwin",
        arch: "arm64",
        verifyCodeSignature() { throw new Error("signature verification must not start"); },
      },
    }),
    /OpenClaw Codex npm-shrinkwrap sha256 mismatch/u,
  );

  const unverifiableClaimReceipt = platformReceipt(pluginArtifact, openclawArtifact);
  const unverifiableRootArtifact = (
    unverifiableClaimReceipt.verified_supply_chain as Record<string, any>
  ).root_artifact as Record<string, any>;
  unverifiableRootArtifact.tar_sha256 = "5".repeat(64);
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(unverifiableClaimReceipt),
  );
  await assert.rejects(
    verifyPlatformArtifactReceiptV1({
      pluginRoot,
      artifact: pluginArtifact,
      openclawArtifact,
      environment: {
        nodeVersion: "v24.15.0",
        platform: "darwin",
        arch: "arm64",
        verifyCodeSignature() { throw new Error("signature verification must not start"); },
      },
    }),
    /OpenClaw root artifact receipt fields mismatch/u,
  );

  const unverifiableCombinedClaimReceipt = platformReceipt(pluginArtifact, openclawArtifact);
  const unverifiableCombinedArtifact = (
    unverifiableCombinedClaimReceipt.verified_supply_chain as Record<string, any>
  ).combined_openclaw_artifact as Record<string, any>;
  unverifiableCombinedArtifact.tar_sha256 = "6".repeat(64);
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(unverifiableCombinedClaimReceipt),
  );
  await assert.rejects(
    verifyPlatformArtifactReceiptV1({
      pluginRoot,
      artifact: pluginArtifact,
      openclawArtifact,
      environment: {
        nodeVersion: "v24.15.0",
        platform: "darwin",
        arch: "arm64",
        verifyCodeSignature() { throw new Error("signature verification must not start"); },
      },
    }),
    /combined OpenClaw artifact receipt fields mismatch/u,
  );

  const wrongNpmVersionReceipt = platformReceipt(pluginArtifact, openclawArtifact);
  wrongNpmVersionReceipt.npm_package_version = "2026.7.1";
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(wrongNpmVersionReceipt),
  );
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature verification must not start"); },
    },
  }), /OpenClaw npm package version mismatch/u);

  const vulnerableAuditReceipt = platformReceipt(pluginArtifact, openclawArtifact);
  const vulnerableArtifact = (vulnerableAuditReceipt.verified_supply_chain as Record<string, any>)
    .finance_plugin_artifact as Record<string, any>;
  vulnerableArtifact.full_audit.moderate = 1;
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(vulnerableAuditReceipt),
  );
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature verification must not start"); },
    },
  }), /Finance plugin full npm audit moderate mismatch/u);

  const unboundAuditReceipt = platformReceipt(pluginArtifact, openclawArtifact);
  const unboundAuditArtifact = (
    unboundAuditReceipt.verified_supply_chain as Record<string, any>
  ).finance_plugin_artifact as Record<string, any>;
  unboundAuditArtifact.full_audit_sha256 = "3".repeat(64);
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(unboundAuditReceipt),
  );
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature verification must not start"); },
    },
  }), /full npm audit evidence sha256 mismatch/u);

  for (const [relativePath, original, expectedError] of [
    [
      "platform/finance-plugin-full-audit.json",
      FIXTURE_NPM_AUDIT,
      /Finance plugin full npm audit evidence sha256 mismatch/u,
    ],
    [
      "platform/finance-plugin-production-audit.json",
      FIXTURE_NPM_AUDIT,
      /Finance plugin production npm audit evidence sha256 mismatch/u,
    ],
    [
      "platform/openclaw-2026.7.1-2-security-audit.json",
      FIXTURE_STATIC_AUDIT,
      /static audit evidence sha256 mismatch/u,
    ],
  ] as const) {
    await write(
      join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
      JSON.stringify(platformReceipt(pluginArtifact, openclawArtifact)),
    );
    await write(join(pluginRoot, relativePath), `${original} `);
    await assert.rejects(verifyPlatformArtifactReceiptV1({
      pluginRoot,
      artifact: pluginArtifact,
      openclawArtifact,
      environment: {
        nodeVersion: "v24.15.0",
        platform: "darwin",
        arch: "arm64",
        verifyCodeSignature() { throw new Error("signature verification must not start"); },
      },
    }), expectedError);
    await write(join(pluginRoot, relativePath), original);
  }

  const wrongRuntimeArtifact = structuredClone(openclawArtifact);
  wrongRuntimeArtifact.package_version = "2026.7.1-2";
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(platformReceipt(pluginArtifact, openclawArtifact)),
  );
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact: wrongRuntimeArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature verification must not start"); },
    },
  }), /OpenClaw runtime package version mismatch/u);

  const runtimeShadowArtifact = structuredClone(pluginArtifact);
  runtimeShadowArtifact.entries.push({
    path: "node_modules/openclaw/package.json",
    byte_count: 1,
    mode: 0o644,
    sha256: "3".repeat(64),
  });
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(platformReceipt(runtimeShadowArtifact, openclawArtifact)),
  );
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: runtimeShadowArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature verification must not start"); },
    },
  }), /must not install or bundle the runtime OpenClaw peer/u);

  const staleReceipt = platformReceipt(pluginArtifact, openclawArtifact);
  const staleArtifact = (staleReceipt.verified_supply_chain as Record<string, any>)
    .finance_plugin_artifact as Record<string, unknown>;
  staleArtifact.artifact_sha256 = "f".repeat(64);
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(staleReceipt),
  );
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature verification must not start"); },
    },
  }), /artifact artifact_sha256 mismatch/u);

  const wrongModeReceipt = platformReceipt(pluginArtifact, openclawArtifact);
  const wrongModeArtifact = (wrongModeReceipt.verified_supply_chain as Record<string, any>)
    .finance_plugin_artifact as Record<string, any>;
  wrongModeArtifact.native_binding.mode = 0o644;
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(wrongModeReceipt),
  );
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature verification must not start"); },
    },
  }), /Finance native binding mode mismatch/u);

  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(platformReceipt(pluginArtifact, openclawArtifact)),
  );
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature is invalid"); },
    },
  }), /signature is invalid/u);

  await rm(join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"));
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() {},
    },
  }), /ENOENT/u);
});

test("platform receipt verifier refuses compiled-runtime drift after the tree hash", async () => {
  const pluginRoot = await artifact("finance_plugin_build");
  const openclawRoot = await artifact("openclaw_package", "2026.7.1");
  const pluginArtifact = await computeArtifactHashV1("finance_plugin_build", pluginRoot);
  const openclawArtifact = await computeArtifactHashV1("openclaw_package", openclawRoot);
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(platformReceipt(pluginArtifact, openclawArtifact)),
  );
  await write(join(pluginRoot, "dist/src/finance-agent-runtime-v2.js"), "export {};\n");
  await assert.rejects(verifyPlatformArtifactReceiptV1({
    pluginRoot,
    artifact: pluginArtifact,
    openclawArtifact,
    environment: {
      nodeVersion: "v24.15.0",
      platform: "darwin",
      arch: "arm64",
      verifyCodeSignature() { throw new Error("signature verification must not start"); },
    },
  }), /compiled runtime current/u);
});

test("receipt-bound sequencing performs zero provider calls and zero writes on preflight failure", async () => {
  let providerCalls = 0;
  let receiptWrites = 0;
  await assert.rejects(executeReceiptBoundCompatibilityV1({
    async verifyBeforeProvider() { throw new Error("platform receipt mismatch"); },
    async runProviderCases() { providerCalls += 1; return ["unexpected"]; },
    async verifyBeforeReceipt() { throw new Error("must not run"); },
    async writeReceipt() { receiptWrites += 1; return "unexpected"; },
  }), /platform receipt mismatch/u);
  assert.equal(providerCalls, 0);
  assert.equal(receiptWrites, 0);
});

test("receipt-bound sequencing performs zero writes on post-provider evidence drift", async () => {
  let providerCalls = 0;
  let receiptWrites = 0;
  await assert.rejects(executeReceiptBoundCompatibilityV1({
    async verifyBeforeProvider() {},
    async runProviderCases() { providerCalls += 4; return ["four-cases"]; },
    async verifyBeforeReceipt() { throw new Error("platform receipt drift"); },
    async writeReceipt() { receiptWrites += 1; return "unexpected"; },
  }), /platform receipt drift/u);
  assert.equal(providerCalls, 4);
  assert.equal(receiptWrites, 0);
});

test("receipt-bound sequencing never reruns provider cases after an uncertain receipt write", async () => {
  let providerRuns = 0;
  let receiptWrites = 0;
  await assert.rejects(executeReceiptBoundCompatibilityV1({
    async verifyBeforeProvider() {},
    async runProviderCases() { providerRuns += 1; return ["four-cases"]; },
    async verifyBeforeReceipt() {},
    async writeReceipt() {
      receiptWrites += 1;
      throw new Error("receipt outcome unknown");
    },
  }), /receipt outcome unknown/u);
  assert.equal(providerRuns, 1);
  assert.equal(receiptWrites, 1);
});

test("artifact evidence binds production runtime version and refuses an untracked file", async () => {
  const repoRoot = await realpath(await mkdtemp(join(tmpdir(), "finance-evidence-repo-")));
  const coreDistributionRoot = await realpath(
    await mkdtemp(join(tmpdir(), "finance-evidence-core-")),
  );
  const workspaceRoot = await realpath(
    await mkdtemp(join(tmpdir(), "finance-evidence-workspace-")),
  );
  const pluginRoot = await artifact("finance_plugin_build");
  await write(join(repoRoot, "runtime.txt"), "private runtime checkout\n");
  await execFile("git", ["init", "--quiet"], { cwd: repoRoot });
  await execFile("git", ["add", "."], { cwd: repoRoot });
  await execFile("git", [
    "-c", "user.name=Finance Test", "-c", "user.email=finance-test@example.invalid",
    "commit", "--quiet", "-m", "fixture",
  ], { cwd: repoRoot });

  const openclawRoot = await artifact("openclaw_package", "2026.7.1");
  const openclaw = await computeArtifactHashV1("openclaw_package", openclawRoot);
  const plugin = await computeArtifactHashV1("finance_plugin_build", pluginRoot);
  const head = (await execFile("git", ["rev-parse", "HEAD"], { cwd: repoRoot })).stdout.trim();
  const coreCommit = "f".repeat(40);
  const core = await coreDistributionFixture(coreDistributionRoot, coreCommit);
  const config = await validatePluginConfig({
    repoRoot,
    coreDistributionRoot,
    pythonExecutable: "/usr/bin/python3",
    workspaceRoot,
    agentProfileV2: {
      openclawPackageSha256: openclaw.artifact_sha256,
      financeCommit: coreCommit,
      coreVersion: "0.1.0",
      coreManifestSha256: core.manifestSha256,
      coreWheelSha256: core.wheelSha256,
      coreApiContractVersion: "finance-core-api-v1",
      coreMigrationLedgerDigest: core.migrationLedgerDigest,
      pluginBuildSha256: plugin.artifact_sha256,
      executionClass: "cloud_projection",
    },
  });
  const evidence = await verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  });
  assert.equal(evidence.finance_commit, coreCommit);
  assert.equal(evidence.core.manifest_sha256, core.manifestSha256);
  assert.equal(evidence.runtime_commit, head);
  assert.notEqual(evidence.finance_commit, evidence.runtime_commit);
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1-2",
  }), /Configured Agent profile hashes do not match/u);
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "package.json"),
    expectedOpenclawVersion: "2026.7.1",
  }), /Loaded plugin source/u);
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2",
  }), /runtime artifacts/u);

  const installedInit = join(coreDistributionRoot, "finance_core/__init__.py");
  const originalInit = await readFile(installedInit);
  await writeFile(installedInit, Buffer.concat([originalInit, Buffer.from("# tampered\n")]));
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  }), /installed Finance Core distribution/u);
  await writeFile(installedInit, originalInit);

  const installedMetadata = join(
    coreDistributionRoot,
    "finance_core-0.1.0.dist-info/METADATA",
  );
  const originalMetadata = await readFile(installedMetadata);
  await writeFile(
    installedMetadata,
    Buffer.concat([originalMetadata, Buffer.from("Project-URL: Internal, https://invalid.example\n")]),
  );
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  }), /installed Finance Core distribution/u);
  await writeFile(installedMetadata, originalMetadata);

  const installedEntryPoint = join(
    coreDistributionRoot,
    "finance_core-0.1.0.dist-info/entry_points.txt",
  );
  await writeFile(installedEntryPoint, "[console_scripts]\nunsafe = finance_core:unsafe\n");
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  }), /distribution inventory does not match/u);
  await rm(installedEntryPoint);

  const installedPackage = join(coreDistributionRoot, "finance_core");
  await chmod(installedPackage, 0o777);
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  }), /Core distribution root entry is unsafe/u);
  await chmod(installedPackage, 0o700);

  const unexpectedCoreFile = join(coreDistributionRoot, "finance_core/private-runtime-secret.py");
  await writeFile(unexpectedCoreFile, "SECRET = 'must-not-load'\n");
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  }), /installed Finance Core distribution/u);
  await rm(unexpectedCoreFile);

  const shadowModule = join(coreDistributionRoot, "decimal.py");
  await writeFile(shadowModule, "raise SystemExit('must-not-shadow')\n");
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  }), /Core distribution root inventory is not exact/u);
  await rm(shadowModule);

  const manifestPath = join(coreDistributionRoot, "component-manifest-v1.json");
  const originalManifest = await readFile(manifestPath);
  await writeFile(manifestPath, Buffer.concat([originalManifest, Buffer.from(" \n")]));
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  }), /component manifest digest does not match/u);
  await writeFile(manifestPath, originalManifest);

  await write(join(repoRoot, "untracked.py"), "raise SystemExit('must not run')\n");
  await assert.rejects(verifyArtifactEvidenceV1({
    config,
    openclawArtifactRoot: openclawRoot,
    loadedPluginRoot: pluginRoot,
    loadedPluginSource: join(pluginRoot, "dist/src/index.js"),
    expectedOpenclawVersion: "2026.7.1",
  }), /clean/u);
});

test("default runner can verify and import the same installed Core twice without bytecode drift", async () => {
  const repoRoot = await realpath(await mkdtemp(join(tmpdir(), "finance-bytecode-repo-")));
  const coreDistributionRoot = await realpath(
    await mkdtemp(join(tmpdir(), "finance-bytecode-core-")),
  );
  const workspaceRoot = await realpath(
    await mkdtemp(join(tmpdir(), "finance-bytecode-workspace-")),
  );
  await mkdir(join(repoRoot, "database"), { recursive: true });
  const coreCommit = "f".repeat(40);
  const core = await coreDistributionFixture(coreDistributionRoot, coreCommit);
  const configuredPython = process.env.PYTHON_EXECUTABLE ?? "/usr/bin/python3";
  const pythonSource = await realpath(configuredPython);
  const pythonExecutable = join(dirname(configuredPython), `finance-test-python-${process.pid}`);
  await cp(pythonSource, pythonExecutable);
  await chmod(pythonExecutable, 0o700);
  const config = await validatePluginConfig({
    repoRoot,
    coreDistributionRoot,
    pythonExecutable,
    workspaceRoot,
    agentProfileV2: {
      openclawPackageSha256: "1".repeat(64),
      financeCommit: coreCommit,
      coreVersion: "0.1.0",
      coreManifestSha256: core.manifestSha256,
      coreWheelSha256: core.wheelSha256,
      coreApiContractVersion: "finance-core-api-v1",
      coreMigrationLedgerDigest: core.migrationLedgerDigest,
      pluginBuildSha256: "2".repeat(64),
      executionClass: "cloud_projection",
    },
  });
  const runner = new BridgeCliRunner(config);

  for (let iteration = 0; iteration < 2; iteration += 1) {
    const response = await runner.run(createBridgeRequest("health", {
      workspace_path: workspaceRoot,
    }), 5_000);
    assert.equal(response.status, "error");
    if (response.status === "error") assert.equal(response.error.code, "WORKSPACE_REFUSED");
  }

  const cacheSearch = await execFile("find", [
    coreDistributionRoot,
    "-name",
    "__pycache__",
    "-print",
  ]);
  assert.equal(cacheSearch.stdout, "");
  await rm(pythonExecutable, { force: true });
});

test("observer hash is independent from production build-source identity", async () => {
  const root = await realpath(await mkdtemp(join(tmpdir(), "finance-observer-identity-")));
  for (const [relativePath, body] of [
    ["binding.gyp", "{}\n"],
    ["openclaw.plugin.json", "{}\n"],
    ["npm-shrinkwrap.json", "{}\n"],
    ["package.json", "{}\n"],
    ["tsconfig.json", "{}\n"],
    ["native/addon.cc", "native\n"],
    ["src/index.ts", "source\n"],
    ["types/runtime.d.ts", "types\n"],
    ["scripts/build-native.mjs", "build-one\n"],
    ["scripts/run-gate5-local-rehearsal.mjs", "observer-one\n"],
    ["scripts/gate5-loopback-only.sb", "(version 1)\n"],
  ] as const) await write(join(root, relativePath), body);

  const sourceBefore = await computeBuildSourceIdentityV1(root);
  const observerBefore = await computeObserverHashV1(root);
  await write(join(root, "scripts/run-gate5-local-rehearsal.mjs"), "observer-two\n");
  const sourceAfterObserver = await computeBuildSourceIdentityV1(root);
  const observerAfter = await computeObserverHashV1(root);
  assert.equal(sourceAfterObserver.source_identity_sha256, sourceBefore.source_identity_sha256);
  assert.notEqual(observerAfter.observer_sha256, observerBefore.observer_sha256);
  assert.deepEqual(observerAfter.entries.map((entry) => entry.path), [
    "scripts/run-gate5-local-rehearsal.mjs",
    "scripts/gate5-loopback-only.sb",
  ]);

  await write(join(root, "scripts/build-native.mjs"), "build-two\n");
  const sourceAfterBuild = await computeBuildSourceIdentityV1(root);
  assert.notEqual(sourceAfterBuild.source_identity_sha256, sourceAfterObserver.source_identity_sha256);
});

test("published plugin includes every file required to recompute source identity", async () => {
  const cacheRoot = await realpath(await mkdtemp(join(tmpdir(), "finance-pack-cache-")));
  try {
    const packed = JSON.parse((await execFile("npm", [
      "pack", "--dry-run", "--json", "--ignore-scripts", "--cache", cacheRoot,
    ], { cwd: join(REPO_ROOT, "plugins/finance-bridge") })).stdout) as Array<{
      files: Array<{ path: string }>;
    }>;
    const files = new Set(packed[0]?.files.map((entry) => entry.path));
    assert.equal(files.has("npm-shrinkwrap.json"), true);
    assert.equal(files.has("tsconfig.json"), true);
  } finally {
    await rm(cacheRoot, { recursive: true, force: true });
  }
});

test("public operator completes real production-shaped pre-provider admission", async () => {
  const repoRoot = await realpath(await mkdtemp(join(tmpdir(), "finance-runtime-repo-")));
  const coreDistributionRoot = await realpath(
    await mkdtemp(join(tmpdir(), "finance-core-distribution-")),
  );
  const workspaceRoot = await realpath(
    await mkdtemp(join(tmpdir(), "finance-core-workspace-")),
  );
  const pluginRoot = await realpath(await artifact("finance_plugin_build"));

  const openclawRoot = await artifact("openclaw_package", "2026.7.1");
  const codexRoot = join(openclawRoot, "node_modules/@openclaw/codex");
  const openaiRoot = join(openclawRoot, "dist/extensions/openai");
  await write(join(openaiRoot, "index.js"), "export {};\n");
  const openclaw = await computeArtifactHashV1("openclaw_package", openclawRoot);
  const plugin = await computeArtifactHashV1("finance_plugin_build", pluginRoot);
  await write(
    join(pluginRoot, "platform/openclaw-2026.7.1-2-max-retries.json"),
    JSON.stringify(platformReceipt(plugin, openclaw)),
  );
  await execFile("git", ["init", "--quiet"], { cwd: repoRoot });
  await write(join(repoRoot, "runtime.txt"), "private runtime checkout\n");
  await execFile("git", ["add", "."], { cwd: repoRoot });
  await execFile("git", [
    "-c", "user.name=Finance Test", "-c", "user.email=finance-test@example.invalid",
    "commit", "--quiet", "-m", "production-shaped admission fixture",
  ], { cwd: repoRoot });
  const head = (await execFile("git", ["rev-parse", "HEAD"], { cwd: repoRoot })).stdout.trim();
  const coreCommit = "f".repeat(40);
  const core = await coreDistributionFixture(coreDistributionRoot, coreCommit);
  const config = await validatePluginConfig({
    repoRoot,
    coreDistributionRoot,
    pythonExecutable: "/usr/bin/python3",
    workspaceRoot,
    agentProfileV2: {
      openclawPackageSha256: openclaw.artifact_sha256,
      financeCommit: coreCommit,
      coreVersion: "0.1.0",
      coreManifestSha256: core.manifestSha256,
      coreWheelSha256: core.wheelSha256,
      coreApiContractVersion: "finance-core-api-v1",
      coreMigrationLedgerDigest: core.migrationLedgerDigest,
      pluginBuildSha256: plugin.artifact_sha256,
      executionClass: "cloud_projection",
    },
  });
  assert.notEqual(head, config.agentProfileV2.financeCommit);
  const denied = [
    "finance_propose", "finance_get_review", "finance_confirm", "finance_edit",
    "finance_reject", "finance_finalize", "finance_get_status", "finance_health",
    "codex_threads",
  ];
  const currentConfig = {
    plugins: {
      allow: ["telegram", "finance-bridge", "codex", "openai"],
      load: { paths: [pluginRoot, codexRoot] },
      entries: {
        codex: {
          enabled: true,
          config: {
            codexDynamicToolsLoading: "searchable",
            codexDynamicToolsExclude: [],
          },
        },
        openai: { enabled: true, config: { personality: "friendly" } },
        "finance-bridge": {
          enabled: true,
          llm: { allowModelOverride: true, allowAgentIdOverride: true },
        },
      },
    },
    tools: { deny: denied },
    agents: {
      list: [
        {
          id: "main",
          default: true,
          model: { primary: "openai/gpt-5.6-sol" },
          tools: { deny: denied },
        },
        {
          id: "finance",
          model: { primary: "openai/example-model", fallbacks: [] },
          models: { "openai/example-model": { alias: "Nomi cloud" } },
          tools: { deny: [...denied, "sessions_spawn"] },
          memorySearch: { enabled: false, experimental: { sessionMemory: false } },
          contextInjection: "never",
        },
      ],
    },
    bindings: [],
    hooks: { enabled: false },
  };
  const sourceEntrySha256 =
    "0462103550920ca01c9a510bdb3f62f69032860014fbaa633957a97b85436ed3";
  const loadedSources = {
    codex: {
      pluginId: "codex",
      packageName: "@openclaw/codex",
      source: join(codexRoot, "dist/index.js"),
      rootDir: codexRoot,
      origin: "config",
      status: "loaded",
      providerIds: ["codex"],
      sourceConfigEntrySha256: sourceEntrySha256,
    },
    openai: {
      pluginId: "openai",
      packageName: "@openclaw/openai-provider",
      source: join(openaiRoot, "index.js"),
      rootDir: openaiRoot,
      origin: "bundled",
      status: "loaded",
      providerIds: ["openai"],
      sourceConfigEntrySha256: sourceEntrySha256,
    },
  };
  let completionCount = 0;
  const api = {
    rootDir: pluginRoot,
    source: join(pluginRoot, "dist/src/index.js"),
    pluginConfig: config,
    runtime: {
      version: "2026.7.1",
      config: { current: () => currentConfig },
      pluginSources: {
        getLoaded(pluginId: string) {
          return loadedSources[pluginId as keyof typeof loadedSources];
        },
      },
      llm: {
        capabilities: { maxRetries: true },
        async complete() {
          completionCount += 1;
          throw new Error("provider completion must not start");
        },
      },
    },
  } as unknown as OpenClawPluginApi;

  let artifactVerificationCount = 0;
  let platformVerificationCount = 0;
  let harnessBoundaryCount = 0;
  let runnerCreateCount = 0;
  let runnerHealthCount = 0;
  await assert.rejects(executeCompatibilityRegistrationV1({
    api,
    dependencies: {
      async validateConfig() { return config; },
      createRunner() {
        runnerCreateCount += 1;
        return {
          async run(request: BridgeRequest): Promise<BridgeResponse> {
            assert.equal(request.command, "health");
            runnerHealthCount += 1;
            return bridgeSuccess(request, {
              workspace_verified: true,
              database_verified: true,
              callback_key_status: "present",
            });
          },
        };
      },
      async resolveOpenClawRoot() { return openclawRoot; },
      async verifyArtifactEvidence(params) {
        artifactVerificationCount += 1;
        return await verifyArtifactEvidenceV1(params);
      },
      async verifyPlatformArtifact(params) {
        platformVerificationCount += 1;
        return await verifyPlatformArtifactReceiptV1({
          ...params,
          environment: {
            nodeVersion: "v24.15.0",
            platform: "darwin",
            arch: "arm64",
            verifyCodeSignature() {},
          },
        });
      },
      async runCompatibilityHarness() {
        harnessBoundaryCount += 1;
        throw new Error("provider boundary sentinel");
      },
      writeOutput() { throw new Error("output must not be written"); },
    },
  }), /provider boundary sentinel/u);
  assert.equal(openclaw.package_version, "2026.7.1");
  assert.notEqual(openclaw.package_version, "2026.7.1-2");
  assert.equal(artifactVerificationCount, 2);
  assert.equal(platformVerificationCount, 1);
  assert.equal(harnessBoundaryCount, 1);
  assert.equal(completionCount, 0);
  assert.equal(runnerCreateCount, 1);
  assert.equal(runnerHealthCount, 1);
});

test("public operator safely refuses initial plugin configuration failure", async () => {
  let runnerCreateCount = 0;
  let providerBoundaryCount = 0;
  const api = {
    pluginConfig: {},
    runtime: {
      llm: {
        async complete() {
          providerBoundaryCount += 1;
          throw new Error("provider must not start");
        },
      },
    },
  } as unknown as OpenClawPluginApi;

  await assert.rejects(executeCompatibilityRegistrationV1({
    api,
    dependencies: {
      async validateConfig() {
        throw new Error("missing /private/path with sk-live-config-secret");
      },
      createRunner() {
        runnerCreateCount += 1;
        throw new Error("runner must not be created");
      },
      async resolveOpenClawRoot() {
        throw new Error("artifact resolution must not start");
      },
      writeOutput() {
        throw new Error("output must not be written");
      },
    },
  }), (error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    assert.equal(message, "Finance bridge configuration refused: CONFIG_REFUSED");
    assert.doesNotMatch(message, /private|sk-live|missing/u);
    return true;
  });
  assert.equal(runnerCreateCount, 0);
  assert.equal(providerBoundaryCount, 0);
});

test("public operator refuses pre-provider health failure before provider or harness", async () => {
  const repoRoot = "/tmp/finance-health-repo";
  const pluginRoot = "/tmp/finance-health-plugin";
  const openclawRoot = "/tmp/finance-health-openclaw";
  const { currentConfig, loadedSources } = reviewedAdmissionHostFixture(pluginRoot, openclawRoot);
  const config = {
    repoRoot,
    coreDistributionRoot: "/tmp/finance-health-core",
    pythonExecutable: join(repoRoot, ".venv/bin/python"),
    workspaceRoot: "/tmp/finance-health-workspace",
    agentProfileV2: {
      openclawPackageSha256: "a".repeat(64),
      financeCommit: "b".repeat(40),
      coreVersion: "0.1.0",
      coreManifestSha256: "d".repeat(64),
      coreWheelSha256: "e".repeat(64),
      coreApiContractVersion: "finance-core-api-v1",
      coreMigrationLedgerDigest: "f".repeat(64),
      pluginBuildSha256: "c".repeat(64),
      executionClass: "cloud_projection",
    },
  } satisfies FinanceBridgeConfig;
  let providerCalls = 0;
  let harnessCalls = 0;
  let healthCalls = 0;
  let artifactCalls = 0;
  let platformCalls = 0;
  const fakeArtifacts = {} as unknown as Awaited<ReturnType<typeof verifyArtifactEvidenceV1>>;
  const api = {
    rootDir: pluginRoot,
    source: join(pluginRoot, "dist/src/index.js"),
    pluginConfig: {},
    runtime: {
      version: "2026.7.1",
      config: { current: () => currentConfig },
      pluginSources: {
        getLoaded(pluginId: string) {
          return loadedSources[pluginId];
        },
      },
      llm: {
        capabilities: { maxRetries: true },
        async complete() {
          providerCalls += 1;
          throw new Error("provider must not start");
        },
      },
    },
  } as unknown as OpenClawPluginApi;

  const assertHealthRefusal = async (
    label: string,
    candidateConfig: FinanceBridgeConfig,
    createRunner: () => { run(request: BridgeRequest, deadlineMs: number): Promise<BridgeResponse> },
  ): Promise<void> => {
    providerCalls = 0;
    harnessCalls = 0;
    healthCalls = 0;
    artifactCalls = 0;
    platformCalls = 0;
    await assert.rejects(executeCompatibilityRegistrationV1({
      api,
      dependencies: {
        async validateConfig() { return candidateConfig; },
        createRunner,
        async resolveOpenClawRoot() { return openclawRoot; },
        async verifyArtifactEvidence() {
          artifactCalls += 1;
          return fakeArtifacts;
        },
        async verifyPlatformArtifact() {
          platformCalls += 1;
          throw new Error("platform verification must not start");
        },
        async runCompatibilityHarness() {
          harnessCalls += 1;
          throw new Error("harness must not start");
        },
        writeOutput() { throw new Error("output must not be written"); },
      },
    }), (error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      assert.match(message, /Finance bridge pre-provider health refused/u);
      assert.doesNotMatch(message, /sk-live-health-secret/u);
      if (label === "error") assert.match(message, /HEALTH_REFUSED/u);
      if (label === "malformed") assert.match(message, /INVALID_HEALTH_RESULT/u);
      if (label === "unavailable") assert.match(message, /BRIDGE_UNAVAILABLE/u);
      if (label === "missing-executable") assert.match(message, /BRIDGE_PROCESS_STARTUP_FAILED/u);
      if (label === "missing-dependency") {
        assert.match(message, /BRIDGE_PROCESS_NONZERO_UNVERIFIED/u);
      }
      if (label === "missing-key") assert.match(message, /INVALID_HEALTH_RESULT/u);
      if (label === "unsafe-key") assert.match(message, /INVALID_HEALTH_RESULT/u);
      return true;
    });
    assert.equal(healthCalls, 1);
    assert.equal(artifactCalls, 1);
    assert.equal(platformCalls, 0);
    assert.equal(providerCalls, 0);
    assert.equal(harnessCalls, 0);
  };

  for (const [label, healthResponse] of [
    ["error", (request: BridgeRequest) => bridgeFailure(
      request,
      "HEALTH_REFUSED",
      "health response contained sk-live-health-secret",
    )],
    ["malformed", (request: BridgeRequest) => bridgeSuccess(request, {
      workspace_verified: true,
    })],
    ["missing-key", (request: BridgeRequest) => bridgeSuccess(request, {
      workspace_verified: true,
      database_verified: true,
      callback_key_status: "missing",
    })],
    ["unsafe-key", (request: BridgeRequest) => bridgeSuccess(request, {
      workspace_verified: true,
      database_verified: true,
      callback_key_status: "unsafe",
    })],
    ["unavailable", (_request: BridgeRequest) => {
      throw new Error("spawn failure included sk-live-health-secret");
    }],
  ] as const) {
    await assertHealthRefusal(label, config, () => ({
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        assert.equal(request.command, "health");
        healthCalls += 1;
        return healthResponse(request);
      },
    }));
  }

  const failureRoot = await realpath(await mkdtemp(join(tmpdir(), "finance-health-python-")));
  const realPython = (await execFile(
    "python3",
    ["-c", "import sys; print(sys.executable)"],
  )).stdout.trim();
  try {
    for (const [label, candidateConfig] of [
      ["missing-executable", {
        ...config,
        pythonExecutable: join(failureRoot, "python-does-not-exist"),
      }],
      ["missing-dependency", {
        ...config,
        repoRoot: failureRoot,
        coreDistributionRoot: failureRoot,
        pythonExecutable: realPython,
      }],
    ] as const) {
      await assertHealthRefusal(label, candidateConfig, () => {
        const runner = new BridgeCliRunner(
          candidateConfig,
          undefined,
          undefined,
          undefined,
          ACCEPT_TEST_EXECUTABLE,
        );
        return {
          async run(request: BridgeRequest, deadlineMs: number): Promise<BridgeResponse> {
            healthCalls += 1;
            return await runner.run(request, deadlineMs);
          },
        };
      });
    }
  } finally {
    await rm(failureRoot, { recursive: true, force: true });
  }
});

test("operator harness makes exactly four direct zero-retry completions", async () => {
  const assets = await loadCompatibilityAssetsV1(REPO_ROOT);
  const pinned = await projection();
  const calls: Array<Record<string, unknown>> = [];
  const verificationRequests: BridgeRequest[] = [];
  const events: string[] = [];
  let currentCase = 0;
  let clock = 0;
  const outcomes = await runCompatibilityHarnessV1({
    projection: pinned,
    assets,
    now: () => {
      const value = clock;
      clock += 100;
      return value;
    },
    runner: {
      async run(request: BridgeRequest): Promise<BridgeResponse> {
        events.push(request.command);
        assert.equal(request.command, "verify_ai_model_compatibility_case_v2");
        verificationRequests.push(request);
        const outcome = request.arguments.harness_outcome as Record<string, unknown>;
        const responseBase64 = outcome.response_utf8_b64;
        assert.equal(typeof responseBase64, "string");
        return bridgeSuccess(request, {
          case_id: outcome.case_id as string,
          verified: true,
          response_sha256: hashFixture(
            Buffer.from(responseBase64 as string, "base64").toString("utf8"),
          ),
        });
      },
    },
    runtime: {
      capabilities: { maxRetries: true },
      async complete(params): Promise<CompatibilityLlmResultV1> {
        events.push("provider");
        calls.push(params as unknown as Record<string, unknown>);
        const text = response(assets.fixtures.cases[currentCase++] as unknown as Record<string, unknown>);
        return {
          text,
          provider: "openai",
          model: "example-model",
          agentId: "finance",
          usage: {},
          audit: {
            caller: { kind: "plugin", id: "finance-bridge" },
            purpose: "finance-bridge.ai-proposal-v2",
          },
        };
      },
    },
  });
  assert.equal(calls.length, 4);
  assert.deepEqual(assets.fixtures.cases.map((item) => item.expected.acceptance_policy), [
    "exact-v1", "exact-v1", "conservative-abstention-v1", "exact-v1",
  ]);
  assert.ok(calls.every((call) => !JSON.stringify(call).includes("acceptance_policy")));
  assert.ok(calls.every((call) => !JSON.stringify(call).includes("conservative-abstention")));
  assert.deepEqual(outcomes.map((outcome) => outcome.case_id), [
    "clear_text", "clear_ocr", "ambiguous_amount_currency", "ocr_prompt_injection",
  ]);
  assert.ok(calls.every((call) => call.maxRetries === 0));
  assert.ok(calls.every((call) => call.agentId === "finance"));
  assert.ok(calls.every((call) => call.purpose === "finance-bridge.ai-proposal-v2"));
  assert.ok(outcomes.every((outcome) => (
    outcome.ordinary_agent_turn_count === 0 && outcome.isolated_completion_count === 1 &&
    outcome.provider_dispatch_count === 1 && outcome.effective_max_retries === 0 &&
    outcome.elapsed_ms === 100
  )));
  assert.deepEqual(events, [
    "provider", "verify_ai_model_compatibility_case_v2",
    "provider", "verify_ai_model_compatibility_case_v2",
    "provider", "verify_ai_model_compatibility_case_v2",
    "provider", "verify_ai_model_compatibility_case_v2",
  ]);
  assert.equal(verificationRequests.length, 4);
  assert.deepEqual(
    verificationRequests.map((request) => (
      (request.arguments.harness_outcome as Record<string, unknown>).case_id
    )),
    outcomes.map((outcome) => outcome.case_id),
  );
});

test("operator stops after each Python verification refusal without starting the next case", async () => {
  const assets = await loadCompatibilityAssetsV1(REPO_ROOT);
  const pinned = await projection();
  for (const [refusalIndex, expectedProviderCalls] of [[0, 1], [1, 2]] as const) {
    let providerCalls = 0;
    let verificationCalls = 0;
    let currentCase = 0;
    let rejection: unknown;
    try {
      await runCompatibilityHarnessV1({
        projection: pinned,
        assets,
        runner: {
          async run(request: BridgeRequest): Promise<BridgeResponse> {
            assert.equal(request.command, "verify_ai_model_compatibility_case_v2");
            const outcome = request.arguments.harness_outcome as Record<string, unknown>;
            const verificationIndex = verificationCalls++;
            if (verificationIndex === refusalIndex) {
              return bridgeFailure(request, "AI_MODEL_EVAL_REFUSED", "safe refusal", {
                verification_reason: "FIELD_MISMATCH",
                verification_field: "amount",
                ignored_untrusted_detail: "sk-live-compat-secret",
              });
            }
            const responseBase64 = outcome.response_utf8_b64;
            assert.equal(typeof responseBase64, "string");
            return bridgeSuccess(request, {
              case_id: outcome.case_id as string,
              verified: true,
              response_sha256: hashFixture(
                Buffer.from(responseBase64 as string, "base64").toString("utf8"),
              ),
            });
          },
        },
        runtime: {
          capabilities: { maxRetries: true },
          async complete(): Promise<CompatibilityLlmResultV1> {
            providerCalls += 1;
            const text = response(
              assets.fixtures.cases[currentCase++] as unknown as Record<string, unknown>,
            );
            return {
              text,
              provider: "openai",
              model: "example-model",
              agentId: "finance",
              usage: {},
              audit: {
                caller: { kind: "plugin", id: "finance-bridge" },
                purpose: "finance-bridge.ai-proposal-v2",
              },
            };
          },
        },
      });
      assert.fail("verification refusal must stop the harness");
    } catch (error) {
      rejection = error;
    }
    const message = rejection instanceof Error ? rejection.message : String(rejection);
    assert.match(message, /AI_MODEL_EVAL_REFUSED/u);
    assert.doesNotMatch(message, /sk-live-compat-secret/u);
    assert.ok(rejection instanceof OperatorFailureV1);
    assert.deepEqual(operatorFailureEnvelopeV1(rejection), {
      schema_version: "finance-compatibility-failure-envelope-v1",
      category: "AI_MODEL_EVAL_REFUSED",
      phase: "python_verification",
      timer_layer: "none",
      elapsed_ms: 0,
      timeout_triggered: false,
      verification_reason: "FIELD_MISMATCH",
      verification_field: "amount",
    });
    assert.equal(providerCalls, expectedProviderCalls);
    assert.equal(verificationCalls, refusalIndex + 1);
  }
});

for (const [mode, expectedCategory] of [
  ["nonzero", "BRIDGE_PROCESS_NONZERO_UNVERIFIED"],
  ["bad-json", "BRIDGE_PROCESS_PROTOCOL_INVALID"],
  ["timeout", "BRIDGE_PROCESS_TIMEOUT"],
] as const) {
  test(`real runner passes health then stops after one provider on verifier ${mode}`, async () => {
    const fixtureRoot = await realpath(await mkdtemp(join(tmpdir(), "finance-verifier-process-")));
    const packageRoot = join(fixtureRoot, "finance_core/openclaw_staging_bridge");
    await mkdir(packageRoot, { recursive: true });
    await writeFile(join(fixtureRoot, "finance_core/__init__.py"), "", { mode: 0o600 });
    await writeFile(join(packageRoot, "__init__.py"), "", { mode: 0o600 });
    await writeFile(join(packageRoot, "cli.py"), `
import hashlib
import json
import sys
import time

request = json.loads(sys.stdin.read())
arguments = json.dumps(request["arguments"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
operation_id = "op_" + hashlib.sha256(
    "\\x00".join(("operation", request["command"], request.get("idempotency_key") or "", arguments)).encode("utf-8")
).hexdigest()[:32]
if request["command"] != "health":
    sys.stderr.write("credential-shaped-verifier-process-diagnostic\\n")
    mode = ${JSON.stringify(mode)}
    if mode == "bad-json":
        sys.stdout.write("{not-json}\\n")
        raise SystemExit(5)
    if mode == "timeout":
        time.sleep(0.2)
        raise SystemExit(1)
    raise SystemExit(1)
response = {
    "envelope_version": "v1",
    "request_id": request["request_id"],
    "operation_id": operation_id,
    "status": "ok",
    "result": {
        "workspace_verified": True,
        "database_verified": True,
        "callback_key_status": "present",
    },
    "idempotent_replay": False,
}
sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\\n")
`, { mode: 0o600 });
    const python = (await execFile("python3", ["-c", "import sys; print(sys.executable)"])).stdout.trim();
    const runner = new BridgeCliRunner(
      {
        repoRoot: fixtureRoot,
        coreDistributionRoot: fixtureRoot,
        pythonExecutable: python,
        workspaceRoot: join(fixtureRoot, "workspace"),
        agentProfileV2: {
          openclawPackageSha256: "1".repeat(64),
          financeCommit: "2".repeat(40),
          coreVersion: "0.1.0",
          coreManifestSha256: "4".repeat(64),
          coreWheelSha256: "5".repeat(64),
          coreApiContractVersion: "finance-core-api-v1",
          coreMigrationLedgerDigest: "6".repeat(64),
          pluginBuildSha256: "3".repeat(64),
          executionClass: "cloud_projection",
        },
      },
      undefined,
      undefined,
      undefined,
      ACCEPT_TEST_EXECUTABLE,
    );
    try {
      const healthRequest = createBridgeRequest("health", {
        workspace_path: join(fixtureRoot, "workspace"),
      });
      const health = await runner.run(healthRequest, 1_000);
      assert.equal(health.status, "ok");

      const assets = await loadCompatibilityAssetsV1(REPO_ROOT);
      const pinned = await projection();
      let providerCalls = 0;
      let currentCase = 0;
      const deadlineRunner = {
        run(request: BridgeRequest, deadlineMs: number): Promise<BridgeResponse> {
          return runner.run(request, mode === "timeout" ? 50 : deadlineMs);
        },
      };
      await assert.rejects(runCompatibilityHarnessV1({
        projection: pinned,
        assets,
        runner: deadlineRunner,
        runtime: {
          capabilities: { maxRetries: true },
          async complete(): Promise<CompatibilityLlmResultV1> {
            providerCalls += 1;
            const text = response(
              assets.fixtures.cases[currentCase++] as unknown as Record<string, unknown>,
            );
            return {
              text,
              provider: "openai",
              model: "example-model",
              agentId: "finance",
              usage: {},
              audit: {
                caller: { kind: "plugin", id: "finance-bridge" },
                purpose: "finance-bridge.ai-proposal-v2",
              },
            };
          },
        },
      }), (error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        assert.equal(
          message,
          "Compatibility case clear_text Python verification was unavailable: " +
            `${expectedCategory}; stop without retry.`,
        );
        assert.doesNotMatch(message, /credential-shaped-verifier-process-diagnostic/u);
        return true;
      });
      assert.equal(providerCalls, 1);
    } finally {
      await rm(fixtureRoot, { recursive: true, force: true });
    }
  });
}

test("operator stops without receipt registration on attribution mismatch", async () => {
  const assets = await loadCompatibilityAssetsV1(REPO_ROOT);
  const pinned = await projection();
  let providerCalls = 0;
  let verificationCalls = 0;
  await assert.rejects(runCompatibilityHarnessV1({
    projection: pinned,
    assets,
    runner: {
      async run(): Promise<BridgeResponse> {
        verificationCalls += 1;
        throw new Error("verification must not run after attribution refusal");
      },
    },
    runtime: {
      capabilities: { maxRetries: true },
      async complete() {
        providerCalls += 1;
        return {
          text: "{}",
          provider: "wrong-provider",
          model: "example-model",
          agentId: "finance",
          usage: {},
          audit: {
            caller: { kind: "plugin", id: "finance-bridge" },
            purpose: "finance-bridge.ai-proposal-v2",
          },
        };
      },
    },
  }), /attribution/u);
  assert.equal(providerCalls, 1);
  assert.equal(verificationCalls, 0);
});

test("operator deadline settles a host completion that never returns", async (t) => {
  const assets = await loadCompatibilityAssetsV1(REPO_ROOT);
  const pinned = await projection();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let observedNow = 0;
  const signals: AbortSignal[] = [];
  let calls = 0;
  let verificationCalls = 0;
  const failure = runCompatibilityHarnessV1({
    projection: pinned,
    assets,
    deadlineMs: 10,
    now: () => observedNow,
    runner: {
      async run(): Promise<BridgeResponse> {
        verificationCalls += 1;
        throw new Error("verification must not run after timeout");
      },
    },
    runtime: {
      capabilities: { maxRetries: true },
      async complete(request) {
        signals.push(request.signal);
        calls += 1;
        return await new Promise<never>(() => undefined);
      },
    },
  });
  assert.equal(calls, 1);
  assert.equal(signals[0]?.aborted, false);
  t.mock.timers.tick(9);
  assert.equal(signals[0]?.aborted, false);
  // Timer scheduling and the measured clock need not align to an integer millisecond.
  // A fired deadline must abort even when its observed duration floors below 10.
  observedNow = 9.8;
  t.mock.timers.tick(1);
  await assert.rejects(failure, (error: unknown) => {
    assert.ok(error instanceof OperatorFailureV1);
    const envelope = operatorFailureEnvelopeV1(error);
    assert.deepEqual({
      schema_version: envelope.schema_version,
      category: envelope.category,
      phase: envelope.phase,
      timer_layer: envelope.timer_layer,
      timeout_triggered: envelope.timeout_triggered,
    }, {
      schema_version: "finance-compatibility-failure-envelope-v1",
      category: "COMPATIBILITY_REFUSED",
      phase: "compatibility_case",
      timer_layer: "compatibility_case",
      timeout_triggered: true,
    });
    assert.equal(envelope.elapsed_ms, 9);
    assert.match(error.message, /stop without retry/u);
    return true;
  });
  assert.equal(calls, 1);
  assert.equal(verificationCalls, 0);
  assert.equal(signals[0]?.aborted, true);
});

test("operator classifies a completion observed after the deadline as an inner timeout", async () => {
  const assets = await loadCompatibilityAssetsV1(REPO_ROOT);
  const pinned = await projection();
  const observations = [0, 11];
  let verificationCalls = 0;
  await assert.rejects(runCompatibilityHarnessV1({
    projection: pinned,
    assets,
    deadlineMs: 10,
    now: () => observations.shift() ?? 11,
    runner: {
      async run(): Promise<BridgeResponse> {
        verificationCalls += 1;
        throw new Error("verification must not run after a late completion");
      },
    },
    runtime: {
      capabilities: { maxRetries: true },
      async complete() {
        return {
          text: "{}",
          provider: pinned.canonical_provider,
          model: pinned.canonical_model,
          agentId: "finance",
          usage: {},
          audit: {
            caller: { kind: "plugin", id: "finance-bridge" },
            purpose: "finance-bridge.ai-proposal-v2",
          },
        };
      },
    },
  }), (error: unknown) => {
    assert.ok(error instanceof OperatorFailureV1);
    const envelope = operatorFailureEnvelopeV1(error);
    assert.equal(envelope.timer_layer, "compatibility_case");
    assert.equal(envelope.timeout_triggered, true);
    assert.equal(envelope.elapsed_ms, 11);
    return true;
  });
  assert.equal(verificationCalls, 0);
});

test("direct receipt registration replay stays on the Python bridge without a model boundary", async () => {
  const pinned = await projection();
  const requests: BridgeRequest[] = [];
  const runner = {
    async run(candidate: BridgeRequest): Promise<BridgeResponse> {
      requests.push(candidate);
      return {
        envelopeVersion: "v1",
        requestId: candidate.request_id,
        operationId: "op_0123456789abcdef0123456789abcdef",
        status: "ok",
        result: {
          receipt_public_id: `aimr_${"a".repeat(64)}`,
          receipt_material_hash: "a".repeat(64),
          config_projection_hash: canonicalProjectionSha256V2(pinned),
          canonical_model: "openai/example-model",
          idempotent_replay: requests.length > 1,
        },
        idempotentReplay: requests.length > 1,
      };
    },
  };
  const first = await registerCompatibilityReceiptV1({
    runner,
    workspaceRoot: "/tmp/workspace",
    projection: pinned,
    outcomes: [],
  });
  const second = await registerCompatibilityReceiptV1({
    runner,
    workspaceRoot: "/tmp/workspace",
    projection: pinned,
    outcomes: [],
  });
  const expectedKey = `bridge-register-ai-model-receipt-v2:${canonicalProjectionSha256V2(pinned)}`;
  assert.equal(first.config_projection_hash, canonicalProjectionSha256V2(pinned));
  assert.equal(first.idempotent_replay, false);
  assert.equal(second.receipt_public_id, first.receipt_public_id);
  assert.equal(second.idempotent_replay, true);
  assert.deepEqual(requests.map((request) => request.command), [
    "register_ai_model_compatibility_receipt_v2",
    "register_ai_model_compatibility_receipt_v2",
  ]);
  assert.ok(requests.every((request) => request.idempotency_key === expectedKey));
});

test("receipt registration preserves safe real-runner failure categories", async () => {
  const pinned = await projection();
  for (const [mode, expectedCategory] of [
    ["startup", "BRIDGE_PROCESS_STARTUP_FAILED"],
    ["timeout", "BRIDGE_PROCESS_TIMEOUT"],
    ["bad-json", "BRIDGE_PROCESS_PROTOCOL_INVALID"],
    ["nonzero", "BRIDGE_PROCESS_NONZERO_UNVERIFIED"],
  ] as const) {
    const fixtureRoot = await realpath(await mkdtemp(join(tmpdir(), "finance-register-process-")));
    const packageRoot = join(fixtureRoot, "finance_core/openclaw_staging_bridge");
    await mkdir(packageRoot, { recursive: true });
    await writeFile(join(fixtureRoot, "finance_core/__init__.py"), "", { mode: 0o600 });
    await writeFile(join(packageRoot, "__init__.py"), "", { mode: 0o600 });
    await writeFile(join(packageRoot, "cli.py"), `
import sys
import time

sys.stdin.read()
sys.stderr.write("credential-shaped-registration-diagnostic\\n")
mode = ${JSON.stringify(mode)}
if mode == "timeout":
    time.sleep(0.2)
elif mode == "bad-json":
    sys.stdout.write("{not-json}\\n")
raise SystemExit(5)
`, { mode: 0o600 });
    const python = mode === "startup"
      ? join(fixtureRoot, "missing-python")
      : (await execFile("python3", ["-c", "import sys; print(sys.executable)"])).stdout.trim();
    const realRunner = new BridgeCliRunner(
      {
        repoRoot: fixtureRoot,
        coreDistributionRoot: fixtureRoot,
        pythonExecutable: python,
        workspaceRoot: join(fixtureRoot, "workspace"),
        agentProfileV2: {
          openclawPackageSha256: "1".repeat(64),
          financeCommit: "2".repeat(40),
          coreVersion: "0.1.0",
          coreManifestSha256: "4".repeat(64),
          coreWheelSha256: "5".repeat(64),
          coreApiContractVersion: "finance-core-api-v1",
          coreMigrationLedgerDigest: "6".repeat(64),
          pluginBuildSha256: "3".repeat(64),
          executionClass: "cloud_projection",
        },
      },
      undefined,
      undefined,
      undefined,
      ACCEPT_TEST_EXECUTABLE,
    );
    const runner = {
      run(request: BridgeRequest, deadlineMs: number): Promise<BridgeResponse> {
        return realRunner.run(request, mode === "timeout" ? 50 : deadlineMs);
      },
    };
    try {
      await assert.rejects(registerCompatibilityReceiptV1({
        runner,
        workspaceRoot: join(fixtureRoot, "workspace"),
        projection: pinned,
        outcomes: [],
      }), (error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        assert.equal(
          message,
          `Compatibility receipt registration refused: ${expectedCategory}`,
        );
        assert.doesNotMatch(message, /credential-shaped-registration-diagnostic/u);
        return true;
      });
    } finally {
      await rm(fixtureRoot, { recursive: true, force: true });
    }
  }
});

test("receipt registration keeps unknown runner errors generic", async () => {
  const pinned = await projection();
  await assert.rejects(registerCompatibilityReceiptV1({
    runner: {
      async run(): Promise<BridgeResponse> {
        throw new Error("credential-shaped-unknown-runner-error");
      },
    },
    workspaceRoot: "/tmp/workspace",
    projection: pinned,
    outcomes: [],
  }), (error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    assert.equal(message, "Compatibility receipt registration refused: BRIDGE_UNAVAILABLE");
    assert.doesNotMatch(message, /credential-shaped-unknown-runner-error/u);
    return true;
  });
});
