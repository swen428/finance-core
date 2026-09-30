import assert from "node:assert/strict";
import { execFile as execFileCallback, spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
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

async function coreDistributionFixture(root: string, commit: string): Promise<{
  manifestSha256: string;
  wheelSha256: string;
  migrationLedgerDigest: string;
}> {
  const program = String.raw`
import base64,csv,hashlib,io,json,pathlib,sys,zipfile
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
  const result = await execFile("/usr/bin/python3", ["-c", program, REPOSITORY_ROOT, root, commit]);
  return JSON.parse(result.stdout) as {
    manifestSha256: string;
    wheelSha256: string;
    migrationLedgerDigest: string;
  };
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
  t.after(async () => rm(scratch, { recursive: true, force: true }));
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
  const distribution = await coreDistributionFixture(coreDistributionRoot, "a".repeat(40));

  const bootstrap = [
    "from finance_core.profile_paths import validate_profile_paths",
    "from finance_core.managed_staging_profile import bootstrap_registered_staging",
    "support, profile_id = __import__('sys').argv[1:]",
    "blank = validate_profile_paths(support, profile_id)",
    "try:",
    "    managed = bootstrap_registered_staging(blank)",
    "    managed.close()",
    "finally:",
    "    blank.close()",
  ].join("\n");
  await execFile(pythonExecutable, ["-c", bootstrap, applicationSupport, "synthetic"], {
    env: { ...process.env, FINANCE_RUNTIME_ROOT: join(profileRoot, "runtime"),
      PYTHONPATH: REPOSITORY_ROOT },
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
  const gatePath = join(profileRoot, ".profile-gate.v1.lock");
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

  const gate = openProfileGate(profileRoot);
  try {
    const shared = await gate.acquireShared(1_000);
    shared.close();
  } finally {
    gate.close();
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
});
