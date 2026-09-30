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
import { runManagedCoreSnapshot } from "../src/managed-cut-coordinator.js";
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

async function coreDistributionFixture(root: string, commit: string, overrideRoot?: string): Promise<{
  manifestSha256: string;
  wheelSha256: string;
  migrationLedgerDigest: string;
}> {
  const program = String.raw`
import base64,csv,hashlib,io,json,pathlib,sys,zipfile
source=pathlib.Path(sys.argv[1])/'finance_core'
root=pathlib.Path(sys.argv[2])
commit=sys.argv[3]
override_root=pathlib.Path(sys.argv[4]) if len(sys.argv)>4 else None
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
  const args = ["-c", program, REPOSITORY_ROOT, root, commit];
  if (overrideRoot !== undefined) args.push(overrideRoot);
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

async function makeProfile(applicationSupport: string): Promise<string> {
  const profileId = "synthetic";
  const profileRoot = join(applicationSupport, "Finance-Codex", "profiles", profileId);
  const runtimeRoot = join(profileRoot, "runtime");
  const workspaceRoot = join(profileRoot, "workspace");
  for (const path of [
    join(applicationSupport, "Finance-Codex"),
    join(applicationSupport, "Finance-Codex", "profiles"),
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
    "            pipeline = _run_b5_pipeline(connection, scratch, 's2b_coordinator')",
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
