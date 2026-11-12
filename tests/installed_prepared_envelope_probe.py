"""Required real-age acceptance of exact wheel and separately installed sdist.

This finite synthetic probe never builds the input wheel, downloads dependencies,
or imports Core from the checkout. Each artifact gets a fresh offline venv;
runtime children use -I -S and explicit installed/dependency/helper roots only.
Recovery identities stay in memory and anonymous pipes, outside the writer's
inputs and the public acceptance receipt. No standalone identity file is made.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = "finance-prepared-envelope-acceptance-v1"
RELEASE = "v1.3.2"
SOURCE_COMMIT = "b74dce4cdbe35b5e5f66c06d9612b72f89028758"
SOURCE_PINS = {
    "darwin-arm64": (
        "e2020b073c44f692685a24d6abc378817eb81ffaaf49fd0531ef8565f767f2f5",
        "26e3fcc371f19e35c7d37500baf8e82c4386538dc6f47e66f4e0fefec50531e4",
    ),
    "linux-x86_64": (
        "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10",
        "98402fa59b4e433426156b4a7e74a32e44902e0434c61158f7de18ffc84f1f97",
    ),
}
NATIVE_CHECKS = (
    "age_version",
    "age_keygen_version",
    "two_recipient_encrypt",
    "recovery_decrypt",
    "temporary_decrypt",
    "wrong_key_refused",
    "corrupt_tail_refused",
)
ARTIFACT_CHECKS = (
    "installed_origin",
    "checkout_invisible",
    "protected_prepare_export",
    "large_frame_over_8mib",
    "seal",
    "restart_same_bytes",
    "detached_source_free",
)
HELPERS = (
    "prepared_envelope_test_support.py",
    "lifecycle_recovery_material_support_v1.py",
    "instance_admission_support_v1.py",
)
LOCKED_DEPENDENCIES = {"pypdf": "6.21.4", "typing-extensions": "4.16.0"}
MAX_SECONDS = 900


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    )


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_identity(path):
    require(path.is_file() and not path.is_symlink(), "nonregular acceptance input")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return {"size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def load(path, maximum=8 * 1024 * 1024):
    raw = path.read_bytes()
    require(len(raw) <= maximum, "acceptance JSON exceeds bound")
    return json.loads(raw)


def private_write(path, data):
    with path.open("xb") as output:
        path.chmod(0o600)
        output.write(data)


def counts(checks):
    require(checks and all(value is True for value in checks.values()), "required check failed")
    return {"required": len(checks), "passed": len(checks), "skipped": 0, "failed": 0, "errors": 0}


def _run(command, *, cwd, deadline, stage, environment=None, input_bytes=None):
    remaining = deadline - time.monotonic()
    require(remaining > 0, "acceptance deadline expired")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        input=input_bytes,
        capture_output=True,
        timeout=min(remaining, 360),
    )
    require(
        len(result.stdout) <= 128 * 1024 and len(result.stderr) <= 128 * 1024, stage + " output"
    )
    if result.returncode:
        # Child diagnostics deliberately contain no body, keys or native stderr.
        detail = ""
        try:
            failure = json.loads(result.stdout)
            detail = ":" + failure.get("failure_type", "") + ":" + failure.get("code", "")
        except (ValueError, AttributeError, TypeError):
            pass
        raise ValueError(stage + " failed" + detail)
    return result.stdout


def _copy_distribution(name, version, destination):
    distribution = importlib.metadata.distribution(name)
    require(distribution.version == version, "installed dependency differs from validation lock")
    copied = []
    for entry in distribution.files or ():
        relative = Path(str(entry))
        # Entry-point scripts are irrelevant to the isolated import roots.
        if relative.is_absolute() or ".." in relative.parts or relative.suffix == ".pyc":
            continue
        source = Path(distribution.locate_file(entry))
        require(source.is_file() and not source.is_symlink(), "unsafe locked dependency member")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.append({"path": relative.as_posix(), **file_identity(target)})
    require(copied, "locked dependency has no copied files")
    return {"version": version, "files_sha256": sha(encoded(copied))}


def _tool_qualification(path, tool_directory):
    value = load(path)
    host = sys.platform + "-" + platform.machine().lower().replace("aarch64", "arm64")
    require(host in SOURCE_PINS, "unsupported native platform")
    require(
        value["release"] == RELEASE and value["source_commit"] == SOURCE_COMMIT, "wrong release"
    )
    if "platforms" in value:
        selected = value["platforms"][host]
        require(
            selected["source_authentication"] == "VERIFIED" and selected["proof_exit_code"] == 0,
            "prior actual Sigsum proof is missing",
        )
        asset_platform = "linux-amd64" if host == "linux-x86_64" else host
        asset_name = "age-" + RELEASE + "-" + asset_platform + ".tar.gz"
        archive_path = tool_directory.parent / asset_name
        proof_path = tool_directory.parent / (asset_name + ".proof")
        archive = {"filename": asset_name, **file_identity(archive_path)}
        proof = {"filename": asset_name + ".proof", **file_identity(proof_path)}
        supplied = {
            "age": selected["tools"]["age"],
            "age_keygen": selected["tools"]["age-keygen"],
        }
    else:
        require(
            value["schema"] == "finance-age-toolchain-qualification-v1"
            and value["status"] == "PINNED_LINUX_TOOLS_READY"
            and value["platform"] == host
            and value["source_authentication"]["status"] == "PREQUALIFIED_SIGSUM_PROOF_REUSED",
            "prior source qualification is missing",
        )
        archive, proof, supplied = value["archive"], value["proof"], value["tools"]
    require((archive["sha256"], proof["sha256"]) == SOURCE_PINS[host], "source proof hashes differ")
    require(tool_directory.resolve() == tool_directory, "tool directory must be canonical")
    tools = {}
    for name, filename in (("age", "age"), ("age_keygen", "age-keygen")):
        tool = tool_directory / filename
        actual = file_identity(tool)
        require(actual["sha256"] == supplied[name]["sha256"], "qualified executable differs")
        expected_size = supplied[name].get("size_bytes", supplied[name].get("bytes"))
        require(actual["size_bytes"] == expected_size, "qualified executable size differs")
        tools[name] = {"path": str(tool), **actual, "version": RELEASE}
    return {
        "release": RELEASE,
        "source_commit": SOURCE_COMMIT,
        "source_authentication": {
            "status": "PREQUALIFIED_SIGSUM_PROOF_REUSED",
            "source_commit": SOURCE_COMMIT,
            "archive_sha256": archive["sha256"],
            "proof_sha256": proof["sha256"],
            "policy": "sigsum-generic-2025-1",
            "verifier_version": "sigsum-verify (sigsum-go module) v0.13.1",
        },
        "archive": archive,
        "proof": proof,
        "tools": tools,
        "platform": host,
    }


def _package_inputs(identity_path, package_dir, run_id, run_attempt):
    identity = load(identity_path)
    require(
        identity["schema"] == "finance-candidate-identity-v1"
        and identity["run_id"] == run_id
        and identity["run_attempt"] == run_attempt,
        "candidate run identity mismatch",
    )
    for key in ("base_sha", "head_sha", "tested_sha", "tested_tree", "event_sha", "workflow_sha"):
        require(re.fullmatch(r"[0-9a-f]{40}", identity[key]), "invalid candidate commit identity")
    require(identity["tested_sha"] == identity["head_sha"], "tested candidate is not exact head")
    receipt_path = package_dir / "package-receipt.json"
    receipt = load(receipt_path)
    require(
        receipt["schema"] == "finance-candidate-package-v1"
        and receipt["status"] == "BUILT_NOT_VALIDATED"
        and encoded(receipt["identity"]) == encoded(identity),
        "immutable package and candidate identities disagree",
    )
    inventory_path = package_dir / "source-inventory.json"
    inventory = load(inventory_path)
    require(
        sha(inventory_path.read_bytes()) == receipt["source_inventory_sha256"]
        and encoded(inventory["identity"]) == encoded(identity)
        and sha(encoded(receipt["recipe"])) == receipt["recipe_sha256"],
        "immutable package evidence changed",
    )
    names = tuple(receipt["artifacts"])
    wheels = [name for name in names if name.endswith(".whl")]
    sdists = [name for name in names if name.endswith(".tar.gz")]
    require(len(wheels) == len(sdists) == 1 and len(names) == 2, "exact two artifacts required")
    require(
        {item.name for item in package_dir.iterdir()}
        == {*names, "package-receipt.json", "source-inventory.json"},
        "unexpected candidate package member",
    )
    artifacts = {}
    for kind, name in (("wheel", wheels[0]), ("sdist", sdists[0])):
        require(Path(name).name == name, "unsafe artifact filename")
        actual = file_identity(package_dir / name)
        require(actual == receipt["artifacts"][name], "candidate artifact bytes changed")
        artifacts[kind] = {"filename": name, **actual}
    return (
        identity,
        inventory,
        {
            "receipt_sha256": sha(receipt_path.read_bytes()),
            "source_inventory_sha256": receipt["source_inventory_sha256"],
            "artifacts": artifacts,
        },
    )


def _activate(config):
    install, dependencies, helpers, checkout = (
        Path(config[key]).resolve() for key in ("install", "dependencies", "helpers", "checkout")
    )
    require(
        all(not item.is_relative_to(checkout) for item in (install, dependencies, helpers)),
        "source root",
    )
    require(
        not any(Path(item).resolve().is_relative_to(checkout) for item in sys.path if item),
        "checkout visible",
    )
    sys.path[:0] = [str(install), str(dependencies), str(helpers)]
    import finance_core

    require(
        Path(finance_core.__file__).resolve().is_relative_to(install), "Core import escaped install"
    )
    require(
        not any(Path(item).resolve().is_relative_to(checkout) for item in sys.path if item),
        "checkout visible",
    )
    from finance_core._native_age_v1 import NativeAgeToolchainV1

    toolchain = NativeAgeToolchainV1(
        Path(config["age"]), Path(config["age_keygen"]), config["platform"]
    )
    return toolchain, str(Path(finance_core.__file__).resolve())


def _reference(value):
    from finance_core.lifecycle_v2 import RuntimeBinding
    from finance_core.lifecycle_v2._prepared_content_v1 import (
        PreparedContentAnchorV1,
        PreparedPackageIdentityV1,
    )
    from finance_core.lifecycle_v2.prepared_envelope_v1 import (
        PreparedEnvelopeKeyContextV1,
        PreparedEnvelopeRefV1,
    )
    from finance_core.lifecycle_v2.recovery_material_v2 import ClosureFrontierV2, ClosureIdentityV2

    anchor = dict(value["content_anchor"])
    identity = dict(anchor["source_identity"])
    identity["source_binding"] = RuntimeBinding(**identity["source_binding"])
    anchor["source_identity"] = ClosureIdentityV2(**identity)
    anchor["projected_frontier"] = ClosureFrontierV2(**anchor["projected_frontier"])
    anchor["packages"] = tuple(PreparedPackageIdentityV1(**item) for item in anchor["packages"])
    return PreparedEnvelopeRefV1(
        **{
            **value,
            "content_anchor": PreparedContentAnchorV1(**anchor),
            "key_context": PreparedEnvelopeKeyContextV1(**value["key_context"]),
        }
    )


def _store(config, toolchain, authenticator, secrets):
    from finance_core.lifecycle_v2.prepared_envelope_v1 import PreparedEnvelopeStoreV1

    return PreparedEnvelopeStoreV1(
        Path(config["store"]),
        namespace="installed-probe",
        recovery_key_id="disposable-recovery-v1",
        recovery_recipient=config["recovery_recipient"],
        key_epoch=1,
        authentication_key_id="disposable-authentication-v1",
        authentication_key=bytes.fromhex(secrets["authentication_key_hex"]),
        authenticator=authenticator,
        toolchain=toolchain,
    )


def _native(config, toolchain):
    from finance_core._native_age_v1 import (
        NativeAgeError,
        decrypt_v1,
        derive_recipient_v1,
        encrypt_v1,
        generate_identity_v1,
    )

    checks = {}
    for key in ("age", "age_keygen"):
        output = subprocess.run([config[key], "--version"], capture_output=True, timeout=10)
        require(
            output.returncode == 0 and output.stdout.strip() == RELEASE.encode(), "tool version"
        )
        checks[key + "_version"] = True
    recovery, temporary, wrong = (generate_identity_v1(toolchain) for _ in range(3))
    recipients = tuple(derive_recipient_v1(item, toolchain) for item in (recovery, temporary))
    plaintext = b"installed native two-recipient acceptance\n"
    ciphertext = encrypt_v1(plaintext, recipients, toolchain, maximum_output=1024 * 1024)
    checks["two_recipient_encrypt"] = True
    for name, identity in (("recovery", recovery), ("temporary", temporary)):
        require(
            decrypt_v1(ciphertext, identity, toolchain, maximum_output=1024) == plaintext, "decrypt"
        )
        checks[name + "_decrypt"] = True
    for name, payload, identity in (
        ("wrong_key_refused", ciphertext, wrong),
        ("corrupt_tail_refused", ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), recovery),
    ):
        try:
            decrypt_v1(payload, identity, toolchain, maximum_output=1024)
        except NativeAgeError:
            checks[name] = True
        else:
            raise AssertionError("native rejection failed")
    # This child stdout is an anonymous pipe captured by the parent, never a
    # diagnostic stream or acceptance receipt. The parent removes this field.
    return {
        "checks": checks,
        "recovery_recipient": recipients[0],
        "recovery_identity_hex": recovery.hex(),
        **counts(checks),
    }


def _writer(config, toolchain, module_file, secrets):
    from dataclasses import asdict

    from prepared_envelope_test_support import native_rows, prepared_case

    class Environment:
        def setenv(self, key, value):
            os.environ[key] = value

    require(set(secrets) == {"authentication_key_hex"}, "writer recovery identity exposure")
    with prepared_case(
        Path(config["source"]), Environment(), envelope_padding=b"x" * (2200 * 1024)
    ) as case:
        before = native_rows(case.service)
        content = case.preparation.export_content_v1(
            case.reference.operation_id, expected_intent_sha256=case.reference.intent_sha256
        )
        require(content.observed_local_state == "PREPARED_OFFLINE", "not pending")
        store = _store(config, toolchain, case.authenticator, secrets)
        reference = store.seal(
            case.preparation,
            case.reference.operation_id,
            expected_intent_sha256=case.reference.intent_sha256,
        )
        envelope = store.read(reference)
        require(native_rows(case.service) == before and not before["facts"], "source facts changed")
        require(reference.content_anchor == content.anchor, "source anchor changed")
        metadata_size = struct.unpack(">Q", envelope[8:16])[0]
        manifest = json.loads(envelope[24 : 24 + metadata_size])
        require(manifest["frame_size_bytes"] > 8 * 1024 * 1024, "large legal frame was not run")
        private_write(Path(config["envelope_output"]), envelope)
        return {
            "module_file": module_file,
            "reference": asdict(reference),
            "frame_size_bytes": manifest["frame_size_bytes"],
            "envelope_size_bytes": len(envelope),
            "envelope_sha256": sha(envelope),
            "ciphertext_sha256": reference.ciphertext_sha256,
            "intent_sha256": case.reference.intent_sha256,
            "source_native_facts": 0,
            "prepared_fact_sha256": case.reference.fact_sha256,
            "prepared_record_id": case.reference.record_id,
            "evidence_root": case.authenticator._secret.hex(),
        }


def _restart(config, toolchain, secrets):
    from lifecycle_recovery_material_support_v1 import SyntheticAuthenticatorV1

    reference = _reference(load(Path(config["reference"])))
    authenticator = SyntheticAuthenticatorV1(bytes.fromhex(secrets["evidence_root_hex"]))
    envelope = _store(config, toolchain, authenticator, secrets).read(reference)
    require(sha(envelope) == config["envelope_sha256"], "restart bytes changed")
    return {"restart_same_bytes": True, "envelope_sha256": sha(envelope)}


def _reader(config, toolchain, secrets):
    from lifecycle_recovery_material_support_v1 import SyntheticAuthenticatorV1

    from finance_core.lifecycle_v2.prepared_envelope_v1 import check_prepared_envelope_v1

    require(all(not Path(path).exists() for path in config["destroyed"]), "source still accessible")
    reference = _reference(load(Path(config["reference"])))
    checked = check_prepared_envelope_v1(
        Path(config["envelope"]).read_bytes(),
        expected=reference,
        recovery_identity=bytes.fromhex(secrets["recovery_identity_hex"]),
        authentication_key=bytes.fromhex(secrets["authentication_key_hex"]),
        authenticator=SyntheticAuthenticatorV1(bytes.fromhex(secrets["evidence_root_hex"])),
        toolchain=toolchain,
    )
    require(
        checked.status == "CONTENT_VERIFIED_OFFLINE"
        and checked.content_kind == "PREPARED_PROJECTION"
        and checked.source_finalization == "NOT_ESTABLISHED",
        "content result acquired authority",
    )
    require(
        len(checked.current_values) == 1
        and checked.current_values[0][0] == config["prepared_record_id"]
        and checked.current_values[0][3] == config["prepared_fact_sha256"]
        and checked.intent_sha256 == config["intent_sha256"],
        "detached projected content differs",
    )
    return {
        "status": checked.status,
        "content_kind": checked.content_kind,
        "source_finalization": checked.source_finalization,
        "inventory_rows_sha256": checked.inventory.rows_sha256,
        "inventory_evidence_sha256": checked.inventory.evidence_sha256,
        "fact_version_count": checked.inventory.fact_version_count,
        "detached_source_free": True,
    }


def child(phase, config_path):
    config = load(config_path)
    secret_input = sys.stdin.buffer.read(8193)
    require(len(secret_input) <= 8192, "secret pipe exceeds bound")
    secrets = json.loads(secret_input)
    toolchain, module_file = _activate(config)
    if phase == "native":
        result = _native(config, toolchain)
    elif phase == "writer":
        result = _writer(config, toolchain, module_file, secrets)
    elif phase == "restart":
        result = _restart(config, toolchain, secrets)
    else:
        require(phase == "reader", "unknown child phase")
        result = _reader(config, toolchain, secrets)
    print(encoded(result).decode(), end="")


def acceptance(args):
    started = time.monotonic()
    deadline = started + MAX_SECONDS
    checkout = Path(__file__).resolve().parents[1]
    identity, inventory, package = _package_inputs(
        args.candidate_identity, args.package_dir, args.run_id, args.run_attempt
    )
    qualification = _tool_qualification(args.qualification_result, args.tool_directory.resolve())
    inventory_by_path = {item["path"]: item for item in inventory["files"]}
    result = {
        "schema": SCHEMA,
        "status": "RUNNING",
        "identity": identity,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "platform": qualification["platform"],
        "qualification": qualification,
        "package": package,
        "artifacts": {},
    }
    with tempfile.TemporaryDirectory(prefix="finance-prepared-installed-") as temporary:
        outside = Path(temporary).resolve()
        outside.chmod(0o700)
        helpers = outside / "helpers"
        helpers.mkdir(mode=0o700)
        for filename in (*HELPERS, Path(__file__).name):
            source = checkout / "tests" / filename
            expected = inventory_by_path["tests/" + filename]
            require(
                file_identity(source) == {key: expected[key] for key in ("sha256", "size_bytes")},
                "probe/helper differs from immutable candidate source",
            )
            shutil.copyfile(source, helpers / filename)
        dependencies = outside / "runtime-dependencies"
        dependencies.mkdir(mode=0o700)
        dependency_identity = {
            name: _copy_distribution(name, version, dependencies)
            for name, version in LOCKED_DEPENDENCIES.items()
        }
        result["runtime_dependencies"] = dependency_identity
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("FINANCE_") and key not in {"PYTHONPATH", "PYTHONHOME"}
        }
        environment.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
        native = None
        for kind in ("wheel", "sdist"):
            artifact_root = outside / kind
            artifact_root.mkdir(mode=0o700)
            virtual = artifact_root / "venv"
            _run(
                [sys.executable, "-I", "-m", "venv", str(virtual)],
                cwd=artifact_root,
                deadline=deadline,
                stage=kind + " fresh offline venv",
                environment=environment,
            )
            python = virtual / "bin/python"
            site = (
                virtual
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            if kind == "sdist":
                result["sdist_build_backend"] = _copy_distribution("setuptools", "84.1.0", site)
            installed = artifact_root / "installed"
            package_path = args.package_dir / package["artifacts"][kind]["filename"]
            command = [str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps"]
            if kind == "sdist":
                command.append("--no-build-isolation")
            _run(
                [*command, "--target", str(installed), str(package_path)],
                cwd=artifact_root,
                deadline=deadline,
                stage=kind + " offline installation",
                environment=environment,
            )
            common = {
                "install": str(installed),
                "dependencies": str(dependencies),
                "helpers": str(helpers),
                "checkout": str(checkout),
                "age": qualification["tools"]["age"]["path"],
                "age_keygen": qualification["tools"]["age_keygen"]["path"],
                "platform": qualification["platform"],
            }

            def run_phase(phase, values, secret_values=None):
                config_path = artifact_root / (phase + ".json")
                private_write(config_path, encoded({**common, **values}))
                return json.loads(
                    _run(
                        [
                            str(python),
                            "-I",
                            "-S",
                            "-B",
                            str(helpers / Path(__file__).name),
                            "--phase",
                            phase,
                            "--phase-config",
                            str(config_path),
                        ],
                        cwd=artifact_root,
                        deadline=deadline,
                        stage=kind + " " + phase,
                        environment=environment,
                        input_bytes=encoded(secret_values or {}),
                    )
                )

            if native is None:
                native = run_phase("native", {})
                require(set(native["checks"]) == set(NATIVE_CHECKS), "native coverage incomplete")
                result["native"] = {"checks": native["checks"], **counts(native["checks"])}
            else:
                # A distinct native process in each artifact rechecks installed
                # registry and tool bytes; only the first receipt counts globally.
                artifact_native = run_phase("native", {})
                require(artifact_native["checks"] == native["checks"], "sdist native coverage")
                native = artifact_native
            recovery_identity_hex = native.pop("recovery_identity_hex")
            authentication = {"authentication_key_hex": os.urandom(32).hex()}
            source = artifact_root / "source"
            store = artifact_root / "capsule-store"
            source.mkdir(mode=0o700)
            store.mkdir(mode=0o700)
            envelope = artifact_root / "public-envelope.bin"
            writer_config = {
                "source": str(source),
                "store": str(store),
                "recovery_recipient": native["recovery_recipient"],
                "envelope_output": str(envelope),
            }
            writer = run_phase("writer", writer_config, authentication)
            # The parent independently retains this exact ref before source loss.
            reference = artifact_root / "independently-retained-reference.json"
            private_write(reference, encoded(writer.pop("reference")))
            roots = {**authentication, "evidence_root_hex": writer.pop("evidence_root")}
            restart = run_phase(
                "restart",
                {
                    **writer_config,
                    "reference": str(reference),
                    "envelope_sha256": writer["envelope_sha256"],
                },
                roots,
            )
            # No source database/control/port or capsule/T exists in the cold
            # reader's process. Only public bytes, external ref and roots remain.
            shutil.rmtree(source)
            shutil.rmtree(store)
            reader = run_phase(
                "reader",
                {
                    "reference": str(reference),
                    "envelope": str(envelope),
                    "destroyed": [str(source), str(store)],
                    "prepared_record_id": writer["prepared_record_id"],
                    "prepared_fact_sha256": writer["prepared_fact_sha256"],
                    "intent_sha256": writer["intent_sha256"],
                },
                {**roots, "recovery_identity_hex": recovery_identity_hex},
            )
            checks = {
                "installed_origin": Path(writer["module_file"]).is_relative_to(installed),
                "checkout_invisible": True,
                "protected_prepare_export": writer["source_native_facts"] == 0,
                "large_frame_over_8mib": writer["frame_size_bytes"] > 8 * 1024 * 1024,
                "seal": writer["envelope_sha256"] == sha(envelope.read_bytes()),
                "restart_same_bytes": restart["restart_same_bytes"],
                "detached_source_free": reader["detached_source_free"],
            }
            require(set(checks) == set(ARTIFACT_CHECKS), "artifact coverage incomplete")
            result["artifacts"][kind] = {
                "checks": checks,
                **counts(checks),
                **writer,
                **reader,
                "content_status": reader["status"],
                "status": "PASS",
                "native_checks": native["checks"],
                "source_checkout_visible": False,
                "isolation": (
                    "fresh per-artifact venv; -I -S; explicit locked dependency/helper roots"
                ),
            }
        # Re-read immutable input objects after both installed executions.
        require(
            _package_inputs(
                args.candidate_identity, args.package_dir, args.run_id, args.run_attempt
            )[2]
            == package,
            "package changed during acceptance",
        )
    total = len(NATIVE_CHECKS) + 2 * len(ARTIFACT_CHECKS)
    result.update(
        status="ACCEPTED",
        totals={"required": total, "passed": total, "skipped": 0, "failed": 0, "errors": 0},
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("native", "writer", "restart", "reader"))
    parser.add_argument("--phase-config", type=Path)
    parser.add_argument("--candidate-identity", type=Path)
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--qualification-result", type=Path)
    parser.add_argument("--tool-directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--run-attempt", type=int)
    args = parser.parse_args()
    try:
        if args.phase:
            require(args.phase_config is not None, "child config required")
            child(args.phase, args.phase_config)
            return
        require(
            all(
                getattr(args, key) is not None
                for key in (
                    "candidate_identity",
                    "package_dir",
                    "qualification_result",
                    "tool_directory",
                    "output",
                    "run_id",
                    "run_attempt",
                )
            ),
            "all acceptance arguments are required",
        )
        result = acceptance(args)
        args.output.write_bytes(encoded(result))
        print(
            encoded(
                {"schema": SCHEMA, "status": result["status"], "totals": result["totals"]}
            ).decode(),
            end="",
        )
    except Exception as error:
        failure = {
            "schema": SCHEMA,
            "status": "REFUSED",
            "failure_type": type(error).__name__,
            "code": getattr(error, "code", ""),
            "reason": str(error)[:256] if type(error) is ValueError else "",
        }
        if args.output is not None:
            args.output.write_bytes(encoded(failure))
        print(encoded(failure).decode(), end="")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
