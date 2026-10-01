import assert from "node:assert/strict";
import { execFile as execFileCallback, spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, mkdir, mkdtemp, readFile, readdir, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { promisify } from "node:util";

import { validatePluginConfig } from "../src/config.js";
import {
  runManagedCoreSnapshot,
  runManagedCoreSnapshotBundle,
} from "../src/managed-cut-coordinator.js";
import { openProfileGate } from "../src/profile-gate.js";

const execFile = promisify(execFileCallback);
const REPOSITORY_ROOT = resolve(process.env.FINANCE_CORE_TEST_REPO_ROOT ?? "../..");
const PYTHON_EXECUTABLE = process.env.PYTHON_EXECUTABLE;
const SHARED_LOCK_PROBE = [
  "const fs=require('node:fs');const{flock}=require('fs-ext');",
  "const fd=fs.openSync(process.argv[1],'r+');",
  "flock(fd,'shnb',error=>{console.log(error?.code??'held');fs.closeSync(fd);});",
].join("");

function trySharedLock(path: string): string {
  const result = spawnSync(process.execPath, ["--input-type=commonjs", "-e", SHARED_LOCK_PROBE, path], {
    cwd: process.cwd(),
    encoding: "utf8",
    timeout: 2_000,
  });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

async function waitForLine(child: ReturnType<typeof spawn>, expected: string): Promise<void> {
  await new Promise<void>((resolvePromise, reject) => {
    let output = "";
    child.stdout!.setEncoding("utf8");
    child.stdout!.on("data", (chunk: string) => {
      output += chunk;
      if (output.includes(expected)) resolvePromise();
    });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (!output.includes(expected)) reject(new Error(`Lock holder exited early: ${code}`));
    });
  });
}

async function coreDistributionFixture(
  root: string,
  commit: string,
  overrideRoot?: string,
  coreVersion = "0.1.0",
): Promise<{
  manifestSha256: string;
  wheelSha256: string;
  migrationLedgerDigest: string;
}> {
  const program = String.raw`
import base64,csv,hashlib,io,json,pathlib,sys,zipfile
source=pathlib.Path(sys.argv[1])/'finance_core'
root=pathlib.Path(sys.argv[2])
commit=sys.argv[3]
override_root=pathlib.Path(sys.argv[4]) if len(sys.argv)>4 and sys.argv[4] else None
core_version=sys.argv[5]
allowed={'.json','.py','.sql','.txt'}
files={}
for path in source.rglob('*'):
    if path.is_file() and path.suffix in allowed:
        name=path.relative_to(source.parent).as_posix()
        files[name]=path.read_bytes()
if override_root is not None:
    for path in override_root.rglob('*'):
        if path.is_file() and path.suffix in allowed:
            name=path.relative_to(override_root).as_posix()
            if name not in files:
                raise SystemExit(f'override is not a packaged Core file: {name}')
            files[name]=path.read_bytes()
        target=root/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(files[name])
wheel_name=f'finance_core-{core_version}-py3-none-any.whl'
wheel=root/wheel_name
metadata_root_name=f'finance_core-{core_version}.dist-info'
metadata_name=f'{metadata_root_name}/METADATA'
metadata=f'Metadata-Version: 2.4\nName: finance-core\nVersion: {core_version}\n\n'.encode()
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
 {'filename':f'finance-codex-finance-bridge-{core_version}.tgz','sha256':'1'*64,'size_bytes':1},
 {'filename':f'finance_core-{core_version}.tar.gz','sha256':'2'*64,'size_bytes':1},
 {'filename':wheel_name,'sha256':sha(wheel),'size_bytes':wheel.stat().st_size},
]
manifest={'api_contract_version':'finance-core-api-v1','artifacts':artifacts,'bridge_version':core_version,'core_commit':commit,'core_version':core_version,'migration_ledger_digest':ledger,'schema':'finance-core-component-manifest-v1'}
manifest_path=root/'component-manifest-v1.json'
manifest_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
checks={entry['filename']:entry['sha256'] for entry in artifacts}; checks[manifest_path.name]=sha(manifest_path)
(root/'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name,value in sorted(checks.items())))
print(json.dumps({'manifestSha256':sha(manifest_path),'wheelSha256':sha(wheel),'migrationLedgerDigest':ledger}))
`;
  const args = ["-c", program, REPOSITORY_ROOT, root, commit, overrideRoot ?? "", coreVersion];
  const result = await execFile("/usr/bin/python3", args);
  return JSON.parse(result.stdout) as {
    manifestSha256: string;
    wheelSha256: string;
    migrationLedgerDigest: string;
  };
}

async function waitUntil(description: string, timeoutMs: number, predicate: () => Promise<boolean>): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await delay(25);
  }
  assert.fail(`Timed out waiting for ${description}.`);
}

async function stageWithMarker(workRoot: string, markerName: string): Promise<string | undefined> {
  for (const name of await readdir(workRoot)) {
    if (!name.startsWith("core-cut-")) continue;
    const stagePath = join(workRoot, name);
    try {
      await stat(join(stagePath, markerName));
      return stagePath;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  return undefined;
}

async function makeProfile(applicationSupport: string, linux = false): Promise<string> {
  const profileId = "synthetic";
  const productRoot = linux ? applicationSupport : join(applicationSupport, "Finance-Codex");
  const profileRoot = join(productRoot, "profiles", profileId);
  const runtimeRoot = join(profileRoot, "runtime");
  const workspaceRoot = join(profileRoot, "workspace");
  for (const path of [
    ...(linux ? [] : [productRoot]),
    join(productRoot, "profiles"),
    profileRoot,
    runtimeRoot,
    join(runtimeRoot, "database"),
    workspaceRoot,
    join(workspaceRoot, "database"),
    join(profileRoot, "backups"),
    join(profileRoot, "work"),
    join(profileRoot, "restore"),
  ]) {
    await mkdir(path, { mode: 0o700 });
    await chmod(path, 0o700);
  }
  await writeFile(join(profileRoot, "profile.json"), JSON.stringify({
    profile_id: profileId,
    runtime_root: runtimeRoot,
    workspace_root: workspaceRoot,
  }), { mode: 0o600 });
  return profileRoot;
}

async function makePrivatePythonEnvironment(scratch: string): Promise<string> {
  assert.ok(PYTHON_EXECUTABLE, "PYTHON_EXECUTABLE must name the pinned Python 3.12 interpreter");
  const privatePythonEnv = join(scratch, "private-python-env");
  await execFile(PYTHON_EXECUTABLE, ["-m", "venv", "--copies", privatePythonEnv]);
  await chmod(privatePythonEnv, 0o700);
  await chmod(join(privatePythonEnv, "bin"), 0o700);
  const pythonExecutable = join(privatePythonEnv, "bin", "python");
  await chmod(pythonExecutable, 0o500);
  const sitePackages = (await execFile(PYTHON_EXECUTABLE, [
    "-c", "import sysconfig; print(sysconfig.get_path('purelib'))",
  ])).stdout.trim();
  const privateSitePackages = join(privatePythonEnv, "lib", "python3.12", "site-packages");
  await rm(privateSitePackages, { recursive: true, force: true });
  await symlink(sitePackages, privateSitePackages, "dir");
  return pythonExecutable;
}

async function bootstrapBundleCapture(
  pythonExecutable: string,
  applicationSupport: string,
  scratch: string,
  linux = false,
): Promise<{
  stagingDatabase: string;
  markerPublicId: string;
  walBytesAtCommit: number;
  walBytesAfterExit: number;
}> {
  const script = [
    "import hashlib, json, os, sqlite3, sys",
    "from pathlib import Path",
    "from finance_core.managed_staging_profile import bootstrap_registered_staging, _managed_staging_connection",
    linux ? "from finance_core.profile_paths import validate_linux_profile_paths as validate_profile_paths"
      : "from finance_core.profile_paths import validate_profile_paths",
    "from finance_core.openclaw_staging_bridge import envelope",
    "from finance_core.receipt_staging_runner.workspace import generate_callback_signing_key",
    "import openclaw_staging_bridge_support_v1 as support",
    "from tests import test_s1c_a_managed_bridge_commands as managed_commands",
    "from tests import test_s3a_managed_capture_publication as capture_tests",
    "application_support, profile_id, scratch_root = sys.argv[1:]",
    linux ? "profile_base = Path(application_support) / 'profiles' / profile_id"
      : "profile_base = Path(application_support) / 'Finance-Codex' / 'profiles' / profile_id",
    "blank = validate_profile_paths(application_support, profile_id)",
    "try:",
    "    managed = bootstrap_registered_staging(blank)",
    "    try:",
    "        workspace_path = managed.workspace",
    "        (workspace_path / 'attachments').mkdir(mode=0o700, exist_ok=True)",
    "        (workspace_path / 'runtime').mkdir(mode=0o700, exist_ok=True)",
    "        (workspace_path / 'evidence').mkdir(mode=0o700, exist_ok=True)",
    "        (workspace_path / 'handoff').mkdir(mode=0o700, exist_ok=True)",
    "        generate_callback_signing_key(str(workspace_path / 'runtime'))",
    "        with _managed_staging_connection(managed, purpose='reopen') as connection:",
    "            connection.execute('PRAGMA journal_mode=WAL')",
    "            connection.execute('PRAGMA wal_autocheckpoint=0')",
    "            connection.commit()",
    "        workspace = managed_commands.ManagedBridgeWorkspace(",
    "            profile_base, workspace_path, managed, []",
    "        )",
    "        handoff = capture_tests._write_managed_handoff(",
    "            workspace, 'bundle-main.jpg', support.JPEG_BYTES",
    "        )",
    "        image_outcome = support.run_cli(capture_tests._receipt_request(",
    "            workspace, message_id=9301, filename=handoff.name",
    "        ))",
    "        if image_outcome.exit_code != 0:",
    "            raise RuntimeError(f'synthetic managed receipt capture failed: {image_outcome.response!r}; stderr={image_outcome.stderr!r}')",
    "        update = support.telegram_text_update(",
    "            'synthetic bundle text capture', update_id=19302, message_id=9302",
    "        )",
    "        arguments = support.authenticated_text_capture_arguments(",
    "            workspace, update, account_id=managed_commands.ACCOUNT,",
    "            binding_id=managed_commands.BINDING,",
    "            payload_sha256=hashlib.sha256(b'synthetic bundle text').hexdigest(),",
    "        )",
    "        arguments.pop('kind')",
    "        text_request = managed_commands._request(",
    "            workspace, envelope.COMMAND_CAPTURE_INTERACTION, arguments,",
    "            idempotency_key=support.canonical_capture_key(message_id=9302),",
    "        )",
    "        text_outcome = support.run_cli(text_request)",
    "        if text_outcome.exit_code != 0:",
    "            raise RuntimeError(f'synthetic managed text capture failed: {text_outcome.response!r}; stderr={text_outcome.stderr!r}')",
    "        unused_bytes = b'\\xff\\xd8\\xffsynthetic-unused-bundle-member'",
    "        unused_hash = hashlib.sha256(unused_bytes).hexdigest()",
    "        unused_path = workspace_path / 'attachments' / unused_hash[:2] / (unused_hash + '.jpg')",
    "        unused_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)",
    "        unused_path.write_bytes(unused_bytes)",
    "        unused_path.chmod(0o400)",
    "        with _managed_staging_connection(managed, purpose='reopen') as connection:",
    "            connection.execute(",
    "                'INSERT INTO attachments (public_id, attachment_type, file_path, original_filename, mime_type, file_hash, source_channel) VALUES (?, ?, ?, ?, ?, ?, ?)',",
    "                ('att_bundle_unused', 'telegram_attachment', str(unused_path), 'unused.jpg', 'image/jpeg', unused_hash, 'telegram'),",
    "            )",
    "            connection.commit()",
    "        marker_public_id = 'bundle_wal_only_marker'",
    "        with _managed_staging_connection(managed, purpose='reopen') as connection:",
    "            journal_mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower()",
    "            if journal_mode != 'wal':",
    "                raise RuntimeError(f'synthetic managed database did not enter WAL mode: {journal_mode!r}')",
    "            connection.execute('PRAGMA wal_autocheckpoint=0')",
    "            connection.execute(\"INSERT INTO raw_intake_records (public_id, source_type, source_channel, raw_input, received_at) VALUES (?, 'manual_entry', 'manual', ?, '2026-10-01T00:00:00Z')\",",
    "                (marker_public_id, 'synthetic WAL-only bundle marker'),",
    "            )",
    "            connection.commit()",
    "            marker_count = connection.execute(",
    "                'SELECT count(*) FROM raw_intake_records WHERE public_id=?',",
    "                (marker_public_id,),",
    "            ).fetchone()[0]",
    "            if marker_count != 1:",
    "                raise RuntimeError('managed connection cannot read its committed WAL marker')",
    "            main_connection = sqlite3.connect(",
    "                Path(managed.staging_database).as_uri() + '?mode=ro&immutable=1', uri=True",
    "            )",
    "            try:",
    "                main_marker_count = main_connection.execute(",
    "                    'SELECT count(*) FROM raw_intake_records WHERE public_id=?',",
    "                    (marker_public_id,),",
    "                ).fetchone()[0]",
    "            finally:",
    "                main_connection.close()",
    "            if main_marker_count != 0:",
    "                raise RuntimeError('WAL-only marker was unexpectedly checkpointed into the main database')",
    "            wal_path = Path(str(managed.staging_database) + '-wal')",
    "            wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0",
    "            if wal_bytes <= 32:",
    "                raise RuntimeError('committed synthetic marker did not produce a nonempty WAL')",
    "            os.write(1, json.dumps({",
    "                'staging_database': str(managed.staging_database),",
    "                'marker_public_id': marker_public_id,",
    "                'main_marker_count': main_marker_count,",
    "                'wal_bytes_at_commit': wal_bytes,",
    "            }).encode() + b'\\n')",
    "            os._exit(0)",
    "    finally:",
    "        managed.close()",
    "finally:",
    "    blank.close()",
  ].join("\n");
  const result = await execFile(pythonExecutable, ["-c", script, applicationSupport, "synthetic", scratch], {
    env: {
      ...process.env,
      FINANCE_RUNTIME_ROOT: join(applicationSupport, ...(linux ? [] : ["Finance-Codex"]), "profiles", "synthetic", "runtime"),
      PYTHONPATH: `${REPOSITORY_ROOT}/tests:${REPOSITORY_ROOT}`,
    },
  });
  const committed = JSON.parse(result.stdout) as {
    staging_database: string;
    marker_public_id: string;
    main_marker_count: number;
    wal_bytes_at_commit: number;
  };
  assert.equal(committed.main_marker_count, 0, "WAL marker must be absent from the main database before child exit");
  assert.ok(committed.wal_bytes_at_commit > 32);
  const walBytesAfterExit = (await stat(`${committed.staging_database}-wal`)).size;
  assert.ok(walBytesAfterExit > 32, "committed WAL must survive actual child-process exit");
  return {
    stagingDatabase: committed.staging_database,
    markerPublicId: committed.marker_public_id,
    walBytesAtCommit: committed.wal_bytes_at_commit,
    walBytesAfterExit,
  };
}

async function createBundleScenario(scratch: string, linux = false): Promise<{
  applicationSupport: string;
  profileRoot: string;
  workRoot: string;
  gatePath: string;
  workspaceRoot: string;
  pythonExecutable: string;
  coreVersion: string;
  walEvidence: Awaited<ReturnType<typeof bootstrapBundleCapture>>;
  config: Awaited<ReturnType<typeof validatePluginConfig>>;
}> {
  const applicationSupport = join(scratch, linux ? "finance-codex" : "Application Support");
  const repoRoot = join(scratch, "runtime-repo");
  const coreDistributionRoot = join(scratch, "core-distribution");
  const workspaceRoot = join(scratch, "bridge-workspace");
  for (const path of [applicationSupport, repoRoot, coreDistributionRoot, workspaceRoot]) {
    await mkdir(path, { mode: 0o700 });
    await chmod(path, 0o700);
  }
  const profileRoot = await makeProfile(applicationSupport, linux);
  const workRoot = join(profileRoot, "work");
  const gatePath = join(profileRoot, ".profile-gate.v1.lock");
  const pythonExecutable = await makePrivatePythonEnvironment(scratch);
  const coreVersion = (await execFile(pythonExecutable, [
    "-I", "-c",
    "import pathlib,sys,tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text())['project']['version'])",
    join(REPOSITORY_ROOT, "pyproject.toml"),
  ])).stdout.trim();
  const distribution = await coreDistributionFixture(
    coreDistributionRoot, "d".repeat(40), undefined, coreVersion,
  );
  const walEvidence = await bootstrapBundleCapture(pythonExecutable, applicationSupport, scratch, linux);
  const config = await validatePluginConfig({
    repoRoot,
    coreDistributionRoot,
    pythonExecutable,
    workspaceRoot,
    agentProfileV2: {
      openclawPackageSha256: "b".repeat(64),
      financeCommit: "d".repeat(40),
      coreVersion,
      coreManifestSha256: distribution.manifestSha256,
      coreWheelSha256: distribution.wheelSha256,
      coreApiContractVersion: "finance-core-api-v1",
      coreMigrationLedgerDigest: distribution.migrationLedgerDigest,
      pluginBuildSha256: "c".repeat(64),
      executionClass: "local_model",
    },
  });
  return {
    applicationSupport,
    profileRoot,
    workRoot,
    gatePath,
    workspaceRoot,
    pythonExecutable,
    coreVersion,
    walEvidence,
    config,
  };
}

test("Linux managed bundle runs fixed real children against an enrolled synthetic profile", {
  skip: process.platform !== "linux",
}, async (t) => {
  const scratch = await realpath(await mkdtemp(join(tmpdir(), "finance-linux-managed-bundle-")));
  t.after(async () => rm(scratch, { recursive: true, force: true }));
  const scenario = await createBundleScenario(scratch, true);
  const receipt = await runManagedCoreSnapshotBundle({
    config: scenario.config, linuxDataRoot: scenario.applicationSupport,
    profileId: "synthetic", waitMs: 5_000, maxHoldMs: 20_000,
  });
  assert.equal(receipt.status, "snapshot_verified");
  assert.equal(receipt.scope, "core_committed_snapshot");
  assert.ok(receipt.memberCount >= 2);
  assert.ok(receipt.referenceCount > 0);
  assert.equal(trySharedLock(scenario.gatePath), "held");
  const manifest = await readFile(join(receipt.stagePath, "manifest.json"));
  assert.equal(createHash("sha256").update(manifest).digest("hex"), receipt.manifestSha256);
  const stages = await readdir(scenario.workRoot);
  await assert.rejects(runManagedCoreSnapshotBundle({
    config: scenario.config, linuxDataRoot: scenario.applicationSupport,
    applicationSupportRoot: scenario.applicationSupport, profileId: "synthetic",
  }), /Exactly one/u);
  assert.deepEqual(await readdir(scenario.workRoot), stages);
});

async function createDelayedReaderConfig(
  scenario: Awaited<ReturnType<typeof createBundleScenario>>,
  scratch: string,
  markerPath: string,
  releasePath: string,
): Promise<Awaited<ReturnType<typeof validatePluginConfig>>> {
  const overrideRoot = join(scratch, "delayed-reader-overlay");
  const overridePackage = join(overrideRoot, "finance_core");
  const distributionRoot = join(scratch, "delayed-reader-distribution");
  await mkdir(overridePackage, { recursive: true, mode: 0o700 });
  await chmod(overridePackage, 0o700);
  await mkdir(distributionRoot, { mode: 0o700 });
  await chmod(distributionRoot, 0o700);

  const readerPath = join(REPOSITORY_ROOT, "finance_core", "managed_snapshot_reader.py");
  const readerSource = await readFile(readerPath, "utf8");
  const footer = 'if __name__ == "__main__":\n    raise SystemExit(main())\n';
  assert.ok(readerSource.endsWith(footer), "reader fixture footer must match the fixed entry point");
  const delayedFooter = [
    'if __name__ == "__main__":',
    "    from pathlib import Path",
    "    import time",
    "    result = main()",
    `    Path(${JSON.stringify(markerPath)}).write_text("verified\\n", encoding="ascii")`,
    `    release = Path(${JSON.stringify(releasePath)})`,
    "    while not release.is_file():",
    "        time.sleep(0.005)",
    "    raise SystemExit(result)",
    "",
  ].join("\n");
  await writeFile(join(overridePackage, "managed_snapshot_reader.py"),
    readerSource.slice(0, -footer.length) + delayedFooter, { mode: 0o600 });

  const distribution = await coreDistributionFixture(
    distributionRoot, "d".repeat(40), overrideRoot, scenario.coreVersion,
  );
  return await validatePluginConfig({
    repoRoot: scenario.config.repoRoot,
    coreDistributionRoot: distributionRoot,
    pythonExecutable: scenario.pythonExecutable,
    workspaceRoot: scenario.workspaceRoot,
    agentProfileV2: {
      ...scenario.config.agentProfileV2,
      coreManifestSha256: distribution.manifestSha256,
      coreWheelSha256: distribution.wheelSha256,
      coreMigrationLedgerDigest: distribution.migrationLedgerDigest,
    },
  });
}

test("managed Core bundle closes over real pending captures and waits for the receipt publisher gate", async (t) => {
  assert.ok(PYTHON_EXECUTABLE, "PYTHON_EXECUTABLE must name the pinned Python 3.12 interpreter");
  const scratch = await realpath(await mkdtemp(join(tmpdir(), "finance-managed-bundle-e2e-")));
  let releasePublisher: string | undefined;
  let publisher: ReturnType<typeof spawn> | undefined;
  let publisherExit: Promise<number | null> | undefined;
  t.after(async () => {
    if (releasePublisher !== undefined) {
      await writeFile(releasePublisher, "release\n", { mode: 0o600 }).catch(() => undefined);
    }
    if (publisherExit !== undefined) {
      await Promise.race([publisherExit, delay(8_000)]);
    }
    await rm(scratch, { recursive: true, force: true });
  });

  const scenario = await createBundleScenario(scratch);
  assert.equal(scenario.walEvidence.markerPublicId, "bundle_wal_only_marker");
  assert.ok(scenario.walEvidence.walBytesAfterExit > 32);
  const publisherReady = join(scratch, "receipt-publisher-paused.ready");
  releasePublisher = join(scratch, "receipt-publisher-paused.release");
  const publisherScript = [
    "import sys, time",
    "from pathlib import Path",
    "from types import SimpleNamespace",
    "import openclaw_staging_bridge_support_v1 as support",
    "from finance_core.openclaw_staging_bridge import receipt_handoff",
    "from tests import test_s3a_managed_capture_publication as capture_tests",
    "application_support, profile_id, ready_path, release_path = sys.argv[1:]",
    "profile_base = Path(application_support) / 'Finance-Codex' / 'profiles' / profile_id",
    "workspace = SimpleNamespace(workspace_path=profile_base / 'workspace')",
    "original_persist = receipt_handoff.persist_attachment_evidence",
    "def pause_after_publish(*args, **kwargs):",
    "    Path(ready_path).write_text('published\\n', encoding='ascii')",
    "    while not Path(release_path).exists():",
    "        time.sleep(0.005)",
    "    return original_persist(*args, **kwargs)",
    "receipt_handoff.persist_attachment_evidence = pause_after_publish",
    "capture_tests._write_managed_handoff(workspace, 'bundle-concurrent.jpg', support.JPEG_BYTES)",
    "outcome = support.run_cli(capture_tests._receipt_request(",
    "    workspace, message_id=9303, filename='bundle-concurrent.jpg'",
    "))",
    "if outcome.exit_code != 0:",
    "    raise RuntimeError('concurrent managed receipt capture failed')",
  ].join("\n");
  publisher = spawn(
    scenario.pythonExecutable,
    ["-c", publisherScript, scenario.applicationSupport, "synthetic", publisherReady, releasePublisher],
    {
      cwd: REPOSITORY_ROOT,
      env: {
        ...process.env,
        FINANCE_RUNTIME_ROOT: join(scenario.profileRoot, "runtime"),
        PYTHONPATH: `${REPOSITORY_ROOT}/tests:${REPOSITORY_ROOT}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  publisherExit = new Promise<number | null>((resolvePromise) => {
    publisher!.once("exit", resolvePromise);
  });
  const stderrChunks: string[] = [];
  publisher.stderr?.setEncoding("utf8");
  publisher.stderr?.on("data", (chunk: string) => stderrChunks.push(chunk));

  try {
    await waitUntil("receipt publication reaching the shared-lock handoff", 8_000, async () => {
      try {
        await stat(publisherReady);
        return true;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        if (publisher!.exitCode !== null) {
          assert.fail(`Receipt publisher exited before its gate pause: ${stderrChunks.join("")}`);
        }
        return false;
      }
    });
    const canonicalBeforeCommit = await readdir(join(scenario.profileRoot, "workspace", "attachments"));
    assert.ok(canonicalBeforeCommit.length > 0, "publisher must have installed canonical bytes before DB commit");

    const stagesBeforeCut = new Set(await readdir(scenario.workRoot));
    let bundleSettled = false;
    const bundlePromise = runManagedCoreSnapshotBundle({
      config: scenario.config,
      applicationSupportRoot: scenario.applicationSupport,
      profileId: "synthetic",
      waitMs: 5_000,
      maxHoldMs: 20_000,
    }).then((receipt) => {
      bundleSettled = true;
      return receipt;
    }, (error: unknown) => {
      bundleSettled = true;
      throw error;
    });
    await delay(150);
    assert.equal(bundleSettled, false,
      "bundle cut must wait while the real receipt publisher holds its managed shared gate");
    assert.deepEqual(await readdir(scenario.workRoot), [...stagesBeforeCut],
      "a cut waiting on the publisher must not create a bundle stage");

    await writeFile(releasePublisher, "release\n", { mode: 0o600 });
    const receipt = await bundlePromise;
    assert.equal(await publisherExit, 0, stderrChunks.join(""));
    assert.equal(receipt.version, "core-snapshot-bundle-receipt-v1");
    assert.equal(receipt.scope, "core_committed_snapshot");
    assert.equal(receipt.status, "snapshot_verified");
    assert.equal(receipt.memberCount, 3,
      "the package must contain DB, one deduplicated capture image, and the unused declared attachment");
    assert.ok(receipt.referenceCount > 1,
      "one canonical capture image should retain multiple database reference facts");

    const manifestBytes = await readFile(join(receipt.stagePath, "manifest.json"));
    const manifest = JSON.parse(manifestBytes.toString("utf8")) as {
      format: string;
      scope: string;
      reference_registry_version: string;
      limits_version: string;
      cut_id: string;
      members: { path: string; bytes: number; sha256: string }[];
      reference_count: number;
      reference_sha256: string;
      member_count: number;
      database: { sha256: string; bytes: number };
    };
    assert.equal(createHash("sha256").update(manifestBytes).digest("hex"), receipt.manifestSha256);
    assert.equal(manifest.format, "core-committed-snapshot-manifest-v1");
    assert.equal(manifest.scope, receipt.scope);
    assert.equal(manifest.reference_registry_version, "core-attachment-reference-registry-v1");
    assert.equal(manifest.limits_version, "core-snapshot-bundle-limits-v1");
    assert.equal(manifest.cut_id, receipt.cutId);
    assert.equal(manifest.reference_count, receipt.referenceCount);
    assert.equal(manifest.reference_sha256, receipt.referenceDigest);
    assert.equal(manifest.member_count, receipt.memberCount);
    assert.equal(manifest.database.sha256, receipt.dbSha256);
    assert.equal(manifest.database.bytes, receipt.dbByteLength);
    assert.deepEqual(manifest.members.map((member) => member.path),
      [...manifest.members.map((member) => member.path)].sort());
    assert.equal(manifest.members.filter((member) => member.path.startsWith("attachments/")).length, 2);
    assert.ok(manifest.members.some((member) => member.path === "db/core.sqlite"));

    const snapshotCheck = String.raw`
import json, sqlite3, sys
connection = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
connection.row_factory = sqlite3.Row
try:
    result = {
        'journal_mode': connection.execute('PRAGMA journal_mode').fetchone()[0],
        'raw_intake_count': connection.execute('SELECT count(*) FROM raw_intake_records').fetchone()[0],
        'wal_marker_count': connection.execute(
            'SELECT count(*) FROM raw_intake_records WHERE public_id=?',
            ('bundle_wal_only_marker',)).fetchone()[0],
        'capture_jobs': [dict(row) for row in connection.execute(
            'SELECT capture_kind, status, ai_status FROM finance_capture_jobs ORDER BY id')],
        'source_types': sorted(row[0] for row in connection.execute(
            'SELECT DISTINCT source_type FROM raw_intake_records')),
        'attachments_count': connection.execute('SELECT count(*) FROM attachments').fetchone()[0],
    }
    print(json.dumps(result))
finally:
    connection.close()
`;
    const inspected = await execFile(scenario.pythonExecutable, [
      "-c", snapshotCheck, join(receipt.stagePath, "db", "core.sqlite"),
    ], { env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT } });
    const snapshot = JSON.parse(inspected.stdout) as {
      journal_mode: string;
      raw_intake_count: number;
      wal_marker_count: number;
      capture_jobs: { capture_kind: string; status: string; ai_status: string }[];
      source_types: string[];
      attachments_count: number;
    };
    assert.equal(snapshot.journal_mode, "delete");
    assert.equal(snapshot.raw_intake_count, 4);
    assert.equal(snapshot.wal_marker_count, 1, "bundle must include the commit that existed only in WAL at child exit");
    assert.equal(snapshot.attachments_count, 2);
    assert.deepEqual(snapshot.capture_jobs.map((job) => job.capture_kind), [
      "receipt_image", "text", "receipt_image",
    ]);
    assert.ok(snapshot.capture_jobs.every((job) => job.status === "captured" && job.ai_status === "not_started"));
    assert.ok(snapshot.source_types.includes("telegram_image"));
    assert.ok(snapshot.source_types.includes("telegram_text"));
  } finally {
    await writeFile(releasePublisher, "release\n", { mode: 0o600 }).catch(() => undefined);
    if (publisherExit !== undefined) await Promise.race([publisherExit, delay(8_000)]);
  }
});

test("managed Core bundle cancellation waits for reader close and final tree changes are rejected", async (t) => {
  assert.ok(PYTHON_EXECUTABLE, "PYTHON_EXECUTABLE must name the pinned Python 3.12 interpreter");
  const scratch = await realpath(await mkdtemp(join(tmpdir(), "finance-managed-bundle-final-tree-")));
  const markerPath = join(scratch, "reader-verified.ready");
  const releasePath = join(scratch, "reader-verified.release");
  let activeAbort: AbortController | undefined;
  t.after(async () => {
    activeAbort?.abort();
    await writeFile(releasePath, "release\n", { mode: 0o600 }).catch(() => undefined);
    await rm(scratch, { recursive: true, force: true });
  });

  const scenario = await createBundleScenario(scratch);
  const config = await createDelayedReaderConfig(scenario, scratch, markerPath, releasePath);
  const stagesBeforeCancellation = new Set(await readdir(scenario.workRoot));
  const cancellation = new AbortController();
  activeAbort = cancellation;
  let cancellationFinished = false;
  const cancellationOutcome = runManagedCoreSnapshotBundle({
    config,
    applicationSupportRoot: scenario.applicationSupport,
    profileId: "synthetic",
    waitMs: 5_000,
    maxHoldMs: 20_000,
    signal: cancellation.signal,
  }).then(
    (receipt) => { cancellationFinished = true; return { kind: "resolved" as const, receipt }; },
    (error: unknown) => { cancellationFinished = true; return { kind: "rejected" as const,
      error: error instanceof Error ? error : new Error(String(error)) }; },
  );

  await waitUntil("reader terminal frame before actual child close", 12_000, async () => {
    if (cancellationFinished) {
      const outcome = await cancellationOutcome;
      if (outcome.kind === "rejected") throw outcome.error;
      throw new Error("bundle completed before the delayed reader reached its terminal frame");
    }
    try {
      await stat(markerPath);
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      return false;
    }
  });
  assert.match(trySharedLock(scenario.gatePath), /EAGAIN|EWOULDBLOCK/u,
    "the reader's inherited descriptor must retain EX while it is alive after its terminal frame");
  cancellation.abort();
  const cancelled = await cancellationOutcome;
  assert.equal(cancelled.kind, "rejected");
  if (cancelled.kind === "rejected") assert.match(cancelled.error.message, /cancelled/u);
  assert.equal(trySharedLock(scenario.gatePath), "held",
    "cancellation returns only after actual reader close releases the inherited EX");
  const stagesAfterCancellation = await readdir(scenario.workRoot);
  assert.equal(stagesAfterCancellation.filter((name) => !stagesBeforeCancellation.has(name)).length, 1,
    "the cancelled attempt retains its private failed stage for diagnosis");

  await rm(markerPath, { force: true });
  await rm(releasePath, { force: true });
  activeAbort = undefined;
  const stagesBeforeIdentityCheck = new Set(await readdir(scenario.workRoot));
  let identityFinished = false;
  const identityOutcome = runManagedCoreSnapshotBundle({
    config,
    applicationSupportRoot: scenario.applicationSupport,
    profileId: "synthetic",
    waitMs: 5_000,
    maxHoldMs: 20_000,
  }).then(
    (receipt) => { identityFinished = true; return { kind: "resolved" as const, receipt }; },
    (error: unknown) => { identityFinished = true; return { kind: "rejected" as const,
      error: error instanceof Error ? error : new Error(String(error)) }; },
  );
  await waitUntil("second fresh reader terminal frame", 12_000, async () => {
    if (identityFinished) {
      const outcome = await identityOutcome;
      if (outcome.kind === "rejected") throw outcome.error;
      throw new Error("bundle completed before the delayed reader reached its terminal frame");
    }
    try {
      await stat(markerPath);
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      return false;
    }
  });
  assert.match(trySharedLock(scenario.gatePath), /EAGAIN|EWOULDBLOCK/u);
  const newStages = (await readdir(scenario.workRoot))
    .filter((name) => !stagesBeforeIdentityCheck.has(name));
  assert.equal(newStages.length, 1);
  const stagePath = join(scenario.workRoot, newStages[0]!);
  const manifest = JSON.parse(await readFile(join(stagePath, "manifest.json"), "utf8")) as {
    members: { path: string; role: string }[];
  };
  const attachment = manifest.members.find((member) => member.role === "attachment");
  assert.ok(attachment, "the final tree mutation must target a copied attachment member");
  const attachmentPath = join(stagePath, attachment.path);
  await chmod(attachmentPath, 0o600);
  await writeFile(attachmentPath, Buffer.concat([await readFile(attachmentPath), Buffer.from("late-change")]));
  await chmod(attachmentPath, 0o400);
  await writeFile(releasePath, "release\n", { mode: 0o600 });

  const changed = await identityOutcome;
  assert.equal(changed.kind, "rejected");
  if (changed.kind === "rejected") {
    assert.match(changed.error.message,
      /Managed bundle manifest does not match actual members\.|Managed bundle changed after independent readback\./u);
  }
  assert.equal(trySharedLock(scenario.gatePath), "held",
    "final identity rejection still waits for the real reader close before releasing EX");
  assert.ok((await readdir(scenario.workRoot)).includes(newStages[0]!));
});

test("managed Core snapshot runs real children, excludes SH contention, and bounds EX wait", async (t) => {
  assert.ok(PYTHON_EXECUTABLE, "PYTHON_EXECUTABLE must name the pinned Python 3.12 interpreter");
  const scratch = await realpath(await mkdtemp(join(tmpdir(), "finance-managed-cut-e2e-")));
  let forcedReleasePath: string | undefined;
  let forcedGatePath: string | undefined;
  t.after(async () => {
    if (forcedReleasePath !== undefined && forcedGatePath !== undefined) {
      await writeFile(forcedReleasePath, "release\n", { mode: 0o600 }).catch(() => undefined);
      await waitUntil("test worker's late close during cleanup", 8_000, async () =>
        trySharedLock(forcedGatePath!) === "held");
    }
    await rm(scratch, { recursive: true, force: true });
  });
  const applicationSupport = join(scratch, "Application Support");
  const repoRoot = join(scratch, "runtime-repo");
  const coreDistributionRoot = join(scratch, "core-distribution");
  const workspaceRoot = join(scratch, "bridge-workspace");
  for (const path of [applicationSupport, repoRoot, coreDistributionRoot, workspaceRoot]) {
    await mkdir(path, { mode: 0o700 });
    await chmod(path, 0o700);
  }
  const privatePythonEnv = join(scratch, "private-python-env");
  await execFile(PYTHON_EXECUTABLE, ["-m", "venv", "--copies", privatePythonEnv]);
  await chmod(privatePythonEnv, 0o700);
  await chmod(join(privatePythonEnv, "bin"), 0o700);
  const pythonExecutable = join(privatePythonEnv, "bin", "python");
  await chmod(pythonExecutable, 0o500);
  const sitePackages = (await execFile(PYTHON_EXECUTABLE, [
    "-c", "import sysconfig; print(sysconfig.get_path('purelib'))",
  ])).stdout.trim();
  const privateSitePackages = join(privatePythonEnv, "lib", "python3.12", "site-packages");
  await rm(privateSitePackages, { recursive: true, force: true });
  await symlink(sitePackages, privateSitePackages, "dir");
  const profileRoot = await makeProfile(applicationSupport);
  const workRoot = join(profileRoot, "work");
  const gatePath = join(profileRoot, ".profile-gate.v1.lock");
  forcedGatePath = gatePath;
  const distribution = await coreDistributionFixture(coreDistributionRoot, "a".repeat(40));

  const bootstrap = [
    "from finance_core.profile_paths import validate_profile_paths",
    "from finance_core.managed_staging_profile import bootstrap_registered_staging, _managed_staging_connection",
    "from finance_core.parser_proposals.receipt_item_allocation_facts import supersede_receipt_item_allocation_facts",
    "from finance_core.receipt_finalization import authorize_receipt_finalization, finalize_prepared_receipt, prepare_receipt_calculation",
    "from pathlib import Path",
    "from tests.test_receipt_b5_staging_e2e_v1 import _run_b5_pipeline",
    "from tests.test_receipt_item_allocation_facts_supersession_v1 import correction_command, replacement_items",
    "support, profile_id, scratch_root = __import__('sys').argv[1:]",
    "blank = validate_profile_paths(support, profile_id)",
    "try:",
    "    managed = bootstrap_registered_staging(blank)",
    "    try:",
    "        (managed.workspace / 'attachments').mkdir(mode=0o700)",
    "        with _managed_staging_connection(managed, purpose='reopen') as connection:",
    "            connection.execute('PRAGMA journal_mode=WAL')",
    "            connection.execute('PRAGMA wal_autocheckpoint=0')",
    "            rowids = []",
    "            for label in ('before', 'hole', 'after'):",
    "                cursor = connection.execute(\"INSERT INTO raw_intake_records (public_id, source_type, source_channel, raw_input, received_at) VALUES (?, 'manual_entry', 'manual', ?, '2026-01-01T00:00:00Z')\", ('synthetic-rowid-' + label, 'synthetic ' + label))",
    "                rowids.append(cursor.lastrowid)",
    "            connection.execute('DELETE FROM raw_intake_records WHERE id=?', (rowids[1],))",
    "            connection.executemany(\"INSERT INTO raw_intake_records (public_id, source_type, source_channel, raw_input, received_at) VALUES (?, 'manual_entry', 'manual', ?, '2026-01-01T00:00:00Z')\", ((f'synthetic-cut-payload-{index}', 'x' * 24576) for index in range(512)))",
    "            scratch = Path(scratch_root)",
    "            scratch.mkdir(mode=0o700, exist_ok=True)",
    "            pipeline = _run_b5_pipeline(connection, scratch, 's2b_coordinator', managed_publication_workspace=managed.workspace)",
    "            v2 = supersede_receipt_item_allocation_facts(connection, correction_command(pipeline.suffix, pipeline.ctx, pipeline.iaf_result))",
    "            v3 = supersede_receipt_item_allocation_facts(connection, correction_command(pipeline.suffix + '_v3', pipeline.ctx, v2, items=replacement_items('S2-B synthetic correction'))) ",
    "            prepared = prepare_receipt_calculation(connection, pipeline.conversion.receipt_public_id)",
    "            authorization = authorize_receipt_finalization(connection, prepared, actor_id='owner')",
    "            assert finalize_prepared_receipt(connection, authorization).status == 'finalized'",
    "            connection.commit()",
    "            assert connection.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'",
    "            assert Path(str(managed.staging_database) + '-wal').stat().st_size > 32",
    "    finally:",
    "        managed.close()",
    "finally:",
    "    blank.close()",
  ].join("\n");
  await execFile(pythonExecutable, ["-c", bootstrap, applicationSupport, "synthetic", scratch], {
    env: { ...process.env, FINANCE_RUNTIME_ROOT: join(profileRoot, "runtime"),
      PYTHONPATH: `${REPOSITORY_ROOT}/tests:${REPOSITORY_ROOT}` },
  });

  const config = await validatePluginConfig({
    repoRoot,
    coreDistributionRoot,
    pythonExecutable,
    workspaceRoot,
    agentProfileV2: {
      openclawPackageSha256: "b".repeat(64),
      financeCommit: "a".repeat(40),
      coreVersion: "0.1.0",
      coreManifestSha256: distribution.manifestSha256,
      coreWheelSha256: distribution.wheelSha256,
      coreApiContractVersion: "finance-core-api-v1",
      coreMigrationLedgerDigest: distribution.migrationLedgerDigest,
      pluginBuildSha256: "c".repeat(64),
      executionClass: "local_model",
    },
  });

  const snapshotPromise = runManagedCoreSnapshot({
    config,
    applicationSupportRoot: applicationSupport,
    profileId: "synthetic",
    limits: {
      maxCoreDbBytes: 32 * 1024 * 1024,
      maxStageBytes: 64 * 1024 * 1024,
      minFreeBytes: 1024 * 1024,
      backupPagesPerStep: 128,
    },
    waitMs: 2_000,
    maxHoldMs: 20_000,
  });
  const contentionDeadline = Date.now() + 5_000;
  let sharedWasExcluded = false;
  while (Date.now() < contentionDeadline) {
    if (/EAGAIN|EWOULDBLOCK/u.test(trySharedLock(gatePath))) {
      sharedWasExcluded = true;
      break;
    }
    await delay(20);
  }
  assert.equal(sharedWasExcluded, true, "an independent SH process should contend with the EX cut");
  const receipt = await snapshotPromise;

  const staged = await readFile(join(receipt.stagePath, "core.sqlite"));
  assert.equal(staged.byteLength, receipt.byteLength);
  assert.equal(createHash("sha256").update(staged).digest("hex"), receipt.sha256);
  assert.equal(receipt.journalMode, "delete");
  assert.ok(receipt.pageCount > 0);
  assert.ok(receipt.schemaObjectCount > 0);
  assert.equal(receipt.stagePath.startsWith(join(profileRoot, "work", "core-cut-")), true);

  const snapshotCheck = [
    "import json, sqlite3, sys",
    "from finance_core.financial_audit import verify_financial_audit_chain",
    "connection = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)",
    "connection.row_factory = sqlite3.Row",
    "rowids = connection.execute(\"SELECT public_id, id FROM raw_intake_records WHERE public_id LIKE 'synthetic-rowid-%' ORDER BY id\").fetchall()",
    "payload_count = connection.execute(\"SELECT count(*) FROM raw_intake_records WHERE public_id LIKE 'synthetic-cut-payload-%'\").fetchone()[0]",
    "fact_sets = connection.execute('SELECT version, fact_set_public_id, supersedes_fact_set_public_id, superseded_by_fact_set_public_id FROM receipt_item_allocation_fact_sets ORDER BY version').fetchall()",
    "aggregates = connection.execute('SELECT DISTINCT aggregate_type, aggregate_public_id FROM financial_audit_events').fetchall()",
    "audit_valid = bool(aggregates) and all(verify_financial_audit_chain(connection, aggregate_type=row['aggregate_type'], aggregate_public_id=row['aggregate_public_id']).valid for row in aggregates)",
    "summary = {'rowids': [dict(row) for row in rowids], 'payload_count': payload_count, 'journal_mode': connection.execute('PRAGMA journal_mode').fetchone()[0], 'fact_versions': [row['version'] for row in fact_sets], 'fact_sets': [dict(row) for row in fact_sets], 'audit_count': connection.execute('SELECT count(*) FROM financial_audit_events').fetchone()[0], 'audit_valid': audit_valid, 'finalization_audit_count': connection.execute('SELECT count(*) FROM receipt_finalization_audit').fetchone()[0], 'transactions_count': connection.execute('SELECT count(*) FROM transactions').fetchone()[0]}",
    "print(json.dumps(summary))",
    "connection.close()",
  ].join("\n");
  const snapshotCheckOutput = await execFile(pythonExecutable, ["-c", snapshotCheck,
    join(receipt.stagePath, "core.sqlite")], {
      env: { ...process.env, PYTHONPATH: REPOSITORY_ROOT },
    });
  const snapshotFacts = JSON.parse(snapshotCheckOutput.stdout) as {
    rowids: { public_id: string; id: number }[];
    payload_count: number;
    journal_mode: string;
    fact_versions: number[];
    fact_sets: {
      fact_set_public_id: string;
      supersedes_fact_set_public_id: string | null;
      superseded_by_fact_set_public_id: string | null;
    }[];
    audit_count: number;
    audit_valid: boolean;
    finalization_audit_count: number;
    transactions_count: number;
  };
  assert.deepEqual(snapshotFacts.rowids.map((row) => row.public_id), [
    "synthetic-rowid-before", "synthetic-rowid-after",
  ]);
  assert.ok(snapshotFacts.rowids[1]!.id - snapshotFacts.rowids[0]!.id > 1,
    "closed coordinator output must preserve committed rowid gaps");
  assert.equal(snapshotFacts.payload_count, 512,
    "closed coordinator output must include committed synthetic WAL rows");
  assert.equal(snapshotFacts.journal_mode, "delete");
  assert.deepEqual(snapshotFacts.fact_versions, [1, 2, 3]);
  assert.equal(snapshotFacts.fact_sets[0]!.superseded_by_fact_set_public_id,
    snapshotFacts.fact_sets[1]!.fact_set_public_id);
  assert.equal(snapshotFacts.fact_sets[1]!.supersedes_fact_set_public_id,
    snapshotFacts.fact_sets[0]!.fact_set_public_id);
  assert.equal(snapshotFacts.fact_sets[1]!.superseded_by_fact_set_public_id,
    snapshotFacts.fact_sets[2]!.fact_set_public_id);
  assert.equal(snapshotFacts.fact_sets[2]!.supersedes_fact_set_public_id,
    snapshotFacts.fact_sets[1]!.fact_set_public_id);
  assert.ok(snapshotFacts.audit_count > 0);
  assert.equal(snapshotFacts.audit_valid, true);
  assert.equal(snapshotFacts.finalization_audit_count, 1);
  assert.equal(snapshotFacts.transactions_count, 1);

  const gate = openProfileGate(profileRoot);
  try {
    const shared = await gate.acquireShared(1_000);
    shared.close();
  } finally {
    gate.close();
  }

  const stagesBeforePreAbort = await readdir(workRoot);
  const preAborted = new AbortController();
  preAborted.abort();
  await assert.rejects(runManagedCoreSnapshot({
    config,
    applicationSupportRoot: applicationSupport,
    profileId: "synthetic",
    limits: {
      maxCoreDbBytes: 32 * 1024 * 1024,
      maxStageBytes: 64 * 1024 * 1024,
      minFreeBytes: 1024 * 1024,
      backupPagesPerStep: 128,
    },
    signal: preAborted.signal,
  }), /cancelled/u);
  assert.deepEqual(await readdir(workRoot), stagesBeforePreAbort,
    "an already-aborted cut must not create a stage");

  const existingStages = new Set(await readdir(workRoot));
  const cancellation = new AbortController();
  const cancellationStarted = Date.now();
  const cancellationPromise = runManagedCoreSnapshot({
    config,
    applicationSupportRoot: applicationSupport,
    profileId: "synthetic",
    limits: {
      maxCoreDbBytes: 32 * 1024 * 1024,
      maxStageBytes: 64 * 1024 * 1024,
      minFreeBytes: 1024 * 1024,
      backupPagesPerStep: 1,
    },
    waitMs: 2_000,
    maxHoldMs: 20_000,
    signal: cancellation.signal,
  });
  const stageDeadline = Date.now() + 5_000;
  let workerOutputObserved = false;
  while (Date.now() < stageDeadline) {
    for (const name of await readdir(workRoot)) {
      if (!name.startsWith("core-cut-") || existingStages.has(name)) continue;
      try {
        await stat(join(workRoot, name, "core.sqlite"));
        workerOutputObserved = true;
        break;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
    }
    if (workerOutputObserved) break;
    await delay(2);
  }
  assert.equal(workerOutputObserved, true,
    "fixed worker should create its output file before in-flight cancellation");
  cancellation.abort();
  await assert.rejects(cancellationPromise, /cancelled/u);
  assert.ok(Date.now() - cancellationStarted < 5_000, "cancellation must not wait for the full hold deadline");

  const afterCancellationGate = openProfileGate(profileRoot);
  try {
    const shared = await afterCancellationGate.acquireShared(1_000);
    shared.close();
  } finally {
    afterCancellationGate.close();
  }

  const holder = spawn(process.execPath, ["--input-type=commonjs", "-e", [
    "const fs=require('node:fs');const{flockSync}=require('fs-ext');",
    "const fd=fs.openSync(process.argv[1],'r+');flockSync(fd,'exnb');",
    "console.log('locked');setTimeout(()=>{fs.closeSync(fd);process.exit(0);},500);",
  ].join(""), gatePath], { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] });
  const holderExit = new Promise<number | null>((resolvePromise) => {
    holder.once("exit", (code) => resolvePromise(code));
  });
  await waitForLine(holder, "locked");
  await assert.rejects(runManagedCoreSnapshot({
    config,
    applicationSupportRoot: applicationSupport,
    profileId: "synthetic",
    limits: {
      maxCoreDbBytes: 32 * 1024 * 1024,
      maxStageBytes: 64 * 1024 * 1024,
      minFreeBytes: 1024 * 1024,
      backupPagesPerStep: 128,
    },
    waitMs: 80,
    maxHoldMs: 2_000,
  }), /deadline exceeded/u);
  assert.equal(await holderExit, 0);

  const cancelHolderRelease = join(scratch, "cancel-ex-holder.release");
  const cancelHolder = spawn(process.execPath, ["--input-type=commonjs", "-e", [
    "const fs=require('node:fs');const{flockSync}=require('fs-ext');",
    "const fd=fs.openSync(process.argv[1],'r+');flockSync(fd,'exnb');console.log('locked');",
    "const release=process.argv[2];const deadline=Date.now()+10000;",
    "const timer=setInterval(()=>{if(fs.existsSync(release)){clearInterval(timer);fs.closeSync(fd);process.exit(0)}",
    "if(Date.now()>=deadline){clearInterval(timer);fs.closeSync(fd);process.exit(4)}},10);",
  ].join(""), gatePath, cancelHolderRelease], {
    cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"],
  });
  const cancelHolderExit = new Promise<number | null>((resolvePromise) => {
    cancelHolder.once("exit", (code) => resolvePromise(code));
  });
  const cancelStagesBeforeWait = await readdir(workRoot);
  let gateCancellation: AbortController | undefined;
  let waitingCut: Promise<Awaited<ReturnType<typeof runManagedCoreSnapshot>>> | undefined;
  try {
    await waitForLine(cancelHolder, "locked");
    gateCancellation = new AbortController();
    waitingCut = runManagedCoreSnapshot({
      config,
      applicationSupportRoot: applicationSupport,
      profileId: "synthetic",
      limits: {
        maxCoreDbBytes: 32 * 1024 * 1024,
        maxStageBytes: 64 * 1024 * 1024,
        minFreeBytes: 1024 * 1024,
        backupPagesPerStep: 128,
      },
      waitMs: 2_000,
      maxHoldMs: 5_000,
      signal: gateCancellation.signal,
    });
    const reachedAbortPoint = await Promise.race([
      waitingCut.then(() => true, () => true),
      delay(200).then(() => false),
    ]);
    assert.equal(reachedAbortPoint, false,
      "a cut should remain pending while another process owns EX");
    assert.deepEqual(await readdir(workRoot), cancelStagesBeforeWait,
      "a cut waiting for EX must not create a stage");
    assert.match(trySharedLock(gatePath), /EAGAIN|EWOULDBLOCK/u,
      "the independent holder must still own EX before cancellation");
    gateCancellation.abort();
    await assert.rejects(waitingCut, /cancelled/u);
    assert.deepEqual(await readdir(workRoot), cancelStagesBeforeWait,
      "cancellation while EX is held must not leave a stage or late lease");
  } finally {
    gateCancellation?.abort();
    await waitingCut?.catch(() => undefined);
    await writeFile(cancelHolderRelease, "release\n", { mode: 0o600 }).catch(() => undefined);
    const holderCode = await Promise.race([
      cancelHolderExit,
      delay(2_000).then(() => undefined),
    ]);
    if (holderCode === undefined) {
      cancelHolder.kill("SIGKILL");
      await cancelHolderExit;
    }
  }
  assert.equal(await cancelHolderExit, 0);
  const stagesBeforePostCancelCut = new Set(await readdir(workRoot));
  const postCancelReceipt = await runManagedCoreSnapshot({
    config,
    applicationSupportRoot: applicationSupport,
    profileId: "synthetic",
    limits: {
      maxCoreDbBytes: 32 * 1024 * 1024,
      maxStageBytes: 64 * 1024 * 1024,
      minFreeBytes: 1024 * 1024,
      backupPagesPerStep: 128,
    },
    waitMs: 2_000,
    maxHoldMs: 5_000,
  });
  const stagesAfterPostCancelCut = await readdir(workRoot);
  assert.deepEqual(stagesAfterPostCancelCut.filter((name) => !stagesBeforePostCancelCut.has(name)),
    [basename(postCancelReceipt.stagePath)],
    "a normal cut after gate-wait cancellation must acquire the released EX without a late stage");

  const releasePath = join(scratch, "unknown-close.release");
  forcedReleasePath = releasePath;
  const overlayRoot = join(scratch, "unknown-close-worker-overlay");
  const overlayPackage = join(overlayRoot, "finance_core");
  const unknownCloseDistributionRoot = join(scratch, "unknown-close-core-distribution");
  await mkdir(overlayPackage, { recursive: true, mode: 0o700 });
  await chmod(overlayPackage, 0o700);
  await mkdir(unknownCloseDistributionRoot, { mode: 0o700 });
  await chmod(unknownCloseDistributionRoot, 0o700);
  const rogueWorker = String.raw`
import json
import os
from pathlib import Path
import signal
import time

control = os.fdopen(3, "r+b", buffering=0)
request = json.loads(control.readline(8193))
if request.get("version") != "delegated-cut-worker-v1" or request.get("operation") != "core_snapshot":
    raise SystemExit(2)
def send(frame):
    control.write((json.dumps(frame, sort_keys=True, separators=(",", ":")) + "\n").encode())
send({"version": "delegated-cut-worker-v1", "type": "ready",
      "cut_id": request["cut_id"], "worker_id": request["worker_id"]})
go = json.loads(control.readline(8193))
if go.get("type") != "go" or go.get("cut_id") != request["cut_id"] or go.get("worker_id") != request["worker_id"]:
    raise SystemExit(3)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
descendant = os.fork()
if descendant == 0:
    for fd in (5, 6):
        try:
            os.close(fd)
        except OSError:
            pass
    stage = Path(os.environ["FINANCE_CUT_STAGE_PATH"])
    stage.joinpath("fd-holder-ready").write_text("ready\n", encoding="ascii")
    release = Path(os.environ["FINANCE_CUT_APPLICATION_SUPPORT"]).parent / "unknown-close.release"
    end = time.monotonic() + 15
    while time.monotonic() < end and not release.exists():
        time.sleep(0.01)
    for fd in (3, 4):
        try:
            os.close(fd)
        except OSError:
            pass
    os._exit(0)
while True:
    time.sleep(1)
`;
  await writeFile(join(overlayPackage, "managed_cut_worker.py"), rogueWorker, { mode: 0o600 });
  const unknownCloseDistribution = await coreDistributionFixture(
    unknownCloseDistributionRoot, "a".repeat(40), overlayRoot);
  const unknownCloseConfig = await validatePluginConfig({
    repoRoot,
    coreDistributionRoot: unknownCloseDistributionRoot,
    pythonExecutable,
    workspaceRoot,
    agentProfileV2: {
      ...config.agentProfileV2,
      coreManifestSha256: unknownCloseDistribution.manifestSha256,
      coreWheelSha256: unknownCloseDistribution.wheelSha256,
      coreMigrationLedgerDigest: unknownCloseDistribution.migrationLedgerDigest,
    },
  });
  const beforeUnknownClose = new Set(await readdir(workRoot));
  const unknownCloseOutcome = runManagedCoreSnapshot({
    config: unknownCloseConfig,
    applicationSupportRoot: applicationSupport,
    profileId: "synthetic",
    limits: {
      maxCoreDbBytes: 32 * 1024 * 1024,
      maxStageBytes: 64 * 1024 * 1024,
      minFreeBytes: 1024 * 1024,
      backupPagesPerStep: 128,
    },
    waitMs: 2_000,
    maxHoldMs: 2_000,
  }).then(
    (receipt) => ({ kind: "resolved" as const, receipt }),
    (error: unknown) => ({ kind: "rejected" as const,
      error: error instanceof Error ? error : new Error(String(error)) }),
  );
  let rogueStagePath: string | undefined;
  try {
    const markerWait = waitUntil("forked worker holding FD3 and FD4", 5_000, async () => {
      rogueStagePath = await stageWithMarker(workRoot, "fd-holder-ready");
      return rogueStagePath !== undefined;
    });
    const firstObserved = await Promise.race([
      markerWait.then(() => ({ kind: "marker" as const })),
      unknownCloseOutcome.then((outcome) => ({ kind: "outcome" as const, outcome })),
    ]);
    if (firstObserved.kind === "outcome") {
      assert.equal(firstObserved.outcome.kind, "rejected",
        "the synthetic worker must not return a success receipt");
      assert.match(firstObserved.outcome.error.message, /child close\/reap is unknown/u,
        "the actual coordinator close outcome must be unknown-close");
      assert.fail("the coordinator reported unknown close before the forked FD holder marked readiness");
    }
    const unknownCloseResult = await unknownCloseOutcome;
    assert.equal(unknownCloseResult.kind, "rejected");
    assert.match(unknownCloseResult.error.message, /child close\/reap is unknown/u);
    assert.ok(rogueStagePath);
    assert.match(trySharedLock(gatePath), /EAGAIN|EWOULDBLOCK/u,
      "the inherited EX descriptor must remain held after TERM, KILL, and unknown close");
    const stagesBeforeUnhealthyCheck = await readdir(workRoot);
    await assert.rejects(runManagedCoreSnapshot({
      config: unknownCloseConfig,
      applicationSupportRoot: applicationSupport,
      profileId: "synthetic",
      limits: {
        maxCoreDbBytes: 32 * 1024 * 1024,
        maxStageBytes: 64 * 1024 * 1024,
        minFreeBytes: 1024 * 1024,
        backupPagesPerStep: 128,
      },
    }), /unhealthy after uncertain child closure/u);
    assert.deepEqual(await readdir(workRoot), stagesBeforeUnhealthyCheck,
      "an uncertain coordinator must refuse a later cut before creating a stage");
  } finally {
    await writeFile(releasePath, "release\n", { mode: 0o600 }).catch(() => undefined);
    await waitUntil("late close releasing the retained profile gate", 8_000, async () =>
      trySharedLock(gatePath) === "held");
  }
  assert.ok(rogueStagePath);
  assert.ok((await readdir(workRoot)).includes(basename(rogueStagePath)));
  assert.notDeepEqual(await readdir(workRoot), [...beforeUnknownClose],
    "the injected child must have entered a real staged cut before timing out");
});
