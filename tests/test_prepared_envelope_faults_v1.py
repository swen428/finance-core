"""Actual capsule publication, cold-process crashes, custody and syscall faults."""

from __future__ import annotations

import errno
import os
import pickle
import signal
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest
from prepared_envelope_test_support import native_rows, prepared_case
from test_native_age_v1 import real_toolchain

from finance_core._native_age_v1 import derive_recipient_v1, generate_identity_v1
from finance_core.instance_admission import acquire_production_owner
from finance_core.lifecycle_v2 import _prepared_envelope_store_v1 as store_module
from finance_core.lifecycle_v2.prepared_envelope_v1 import (
    PreparedEnvelopeErrorV1,
    PreparedEnvelopeLimitsV1,
    PreparedEnvelopeStoreV1,
    check_prepared_envelope_v1,
)
from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1
from finance_core.profile_layout import profile_root


@pytest.fixture(scope="module")
def keys():
    tools = real_toolchain()
    recovery_identity = generate_identity_v1(tools)
    return tools, recovery_identity, derive_recipient_v1(recovery_identity, tools)


def configured_store(root, case, keys, *, limits=None, epoch=1):
    if not root.exists():
        root.mkdir(mode=0o700)
    return PreparedEnvelopeStoreV1(
        root.resolve(),
        namespace="synthetic-prepared",
        recovery_key_id="recovery-test-v1",
        recovery_recipient=keys[2],
        key_epoch=epoch,
        authentication_key_id="envelope-test-v1",
        authentication_key=b"e" * 32,
        authenticator=case.authenticator,
        toolchain=keys[0],
        limits=limits,
    )


def seal(store, case):
    return store.seal(
        case.preparation,
        case.reference.operation_id,
        expected_intent_sha256=case.reference.intent_sha256,
    )


def test_actual_store_restart_read_retry_and_source_pending_unchanged(tmp_path, monkeypatch, keys):
    with prepared_case(tmp_path, monkeypatch) as case:
        before = native_rows(case.service)
        root = tmp_path / "capsules"
        store = configured_store(root, case, keys)
        ref = seal(store, case)
        envelope = store.read(ref)
        assert envelope[:8] == b"FCENV001"
        assert keys[1] not in b"".join(p.read_bytes() for p in root.rglob("final"))
        capsule = next(root.rglob("final")).read_bytes()
        assert capsule[:8] == b"FCLOC001" and b"AGE-SECRET-KEY-1" in capsule
        fresh = configured_store(root, case, keys)
        assert seal(fresh, case) == ref and fresh.read(ref) == envelope
        checked = check_prepared_envelope_v1(
            envelope,
            expected=ref,
            recovery_identity=keys[1],
            authentication_key=b"e" * 32,
            authenticator=case.authenticator,
            toolchain=keys[0],
        )
        assert checked.status == "CONTENT_VERIFIED_OFFLINE"
        assert checked.source_finalization == "NOT_ESTABLISHED"
        assert native_rows(case.service) == before
        assert case.preparation.inspect(case.reference.operation_id).state == "PREPARED_OFFLINE"
        with pytest.raises(PreparedEnvelopeErrorV1):
            seal(configured_store(root, case, keys, epoch=2), case)
        assert next(root.rglob("final")).read_bytes() == capsule


@pytest.mark.parametrize("outcome", ["apply_offline", "cancel_offline"])
def test_sealed_history_survives_source_outcome_but_first_seal_refuses(
    tmp_path, monkeypatch, keys, outcome
):
    with prepared_case(tmp_path, monkeypatch) as case:
        root = tmp_path / "capsules"
        store = configured_store(root, case, keys)
        ref = seal(store, case)
        envelope = store.read(ref)
        getattr(case.preparation, outcome)(
            case.reference.operation_id, expected_intent_sha256=case.reference.intent_sha256
        )
        assert seal(configured_store(root, case, keys), case) == ref
        assert store.read(ref) == envelope
        with pytest.raises(PreparedEnvelopeErrorV1):
            seal(configured_store(tmp_path / "late-capsules", case, keys), case)


@pytest.mark.parametrize("fault", ["short_write", "zero_write", "enospc", "file_sync", "dir_sync"])
def test_real_syscall_faults_withhold_result_and_never_replace(tmp_path, monkeypatch, keys, fault):
    with prepared_case(tmp_path, monkeypatch) as case:
        store = configured_store(tmp_path / "capsules", case, keys)
        actual_write, actual_sync = os.write, os.fsync
        with monkeypatch.context() as patch:
            if fault in {"short_write", "zero_write", "enospc"}:

                def write(fd, data):
                    # The native child uses separate short-lived FDs. Intercept
                    # only the actual managed capsule regular-file descriptor.
                    if os.fstat(fd).st_mode & 0o170000 == 0o100000:
                        if fault == "zero_write":
                            return 0
                        if fault == "enospc":
                            raise OSError(errno.ENOSPC, "synthetic ENOSPC")
                        return actual_write(fd, data[:17])
                    return actual_write(fd, data)

                patch.setattr(os, "write", write)
            else:

                def sync(fd):
                    info = os.fstat(fd)
                    is_dir = info.st_mode & 0o170000 == 0o040000
                    if (fault == "dir_sync" and is_dir) or (
                        fault == "file_sync" and not is_dir and info.st_size > 0
                    ):
                        raise OSError(errno.EIO, "synthetic fsync failure")
                    return actual_sync(fd)

                patch.setattr(os, "fsync", sync)
            if fault == "short_write":
                ref = seal(store, case)
            else:
                with pytest.raises(PreparedEnvelopeErrorV1):
                    seal(store, case)
        if fault == "short_write":
            assert store.read(ref).startswith(b"FCENV001")
        else:
            candidates = list(store.root.rglob("candidate"))
            if candidates and candidates[0].stat().st_size < 32:
                persisted = candidates[0].read_bytes()
                with pytest.raises(PreparedEnvelopeErrorV1):
                    seal(configured_store(store.root, case, keys), case)
                assert candidates[0].read_bytes() == persisted
        assert not any(row for row in native_rows(case.service)["facts"])


def test_partial_candidate_hardlink_permissions_and_quota_refuse(tmp_path, monkeypatch, keys):
    with prepared_case(tmp_path, monkeypatch) as case:
        root = tmp_path / "capsules"
        tiny = configured_store(
            root, case, keys, limits=PreparedEnvelopeLimitsV1(max_store_bytes=1)
        )
        with pytest.raises(PreparedEnvelopeErrorV1, match="quota"):
            seal(tiny, case)
        assert not list(root.rglob("candidate"))
        normal = configured_store(root, case, keys)
        ref = seal(normal, case)
        final = next(root.rglob("final"))
        data = final.read_bytes()
        final.chmod(0o644)
        with pytest.raises(PreparedEnvelopeErrorV1):
            normal.read(ref)
        final.chmod(0o600)
        link = tmp_path / "uncontrolled-link"
        os.link(final, link)
        with pytest.raises(PreparedEnvelopeErrorV1):
            normal.read(ref)
        link.unlink()
        final.write_bytes(data[:-1])
        with pytest.raises(PreparedEnvelopeErrorV1):
            normal.read(ref)
        assert final.read_bytes() == data[:-1]
        with pytest.raises(PreparedEnvelopeErrorV1):
            normal.read(replace(ref, context_id="0" * 64))


def _restart_fixture(case, path, keys, root):
    config = {
        name: getattr(case.config, name)
        for name in (
            "control_root",
            "locator_root",
            "profile_id",
            "layout",
            "instance_id",
            "config_identity",
            "core_identity",
        )
    }
    path.write_bytes(
        pickle.dumps(
            (config, case.port, case.service.binding, case.reference, keys[0], keys[2], root)
        )
    )
    path.chmod(0o600)


_CHILD = r"""
import os,pickle,signal,sys
from pathlib import Path
from finance_core.instance_admission import TrustedInstanceConfig,acquire_production_owner
from finance_core.lifecycle_v2 import LedgerService
from finance_core.lifecycle_v2.prepared_finalization_v1 import PreparedFinalizationServiceV1
from finance_core.lifecycle_v2.prepared_envelope_v1 import (
    PreparedEnvelopeStoreV1,PreparedEnvelopeErrorV1,
)
from finance_core.lifecycle_v2 import _prepared_envelope_store_v1 as module
from finance_core.profile_layout import profile_root
values,port,binding,ref,tools,recipient,root=pickle.loads(Path(sys.argv[1]).read_bytes())
os.environ['FINANCE_RUNTIME_ROOT']=str(profile_root(values['locator_root'],values['profile_id'],values['layout'])/'runtime')
cfg=TrustedInstanceConfig(**values,approval_verifier=lambda *args:True)
owner=acquire_production_owner(cfg)
service=LedgerService(owner,port,binding,clock=lambda:1792800000)
preparation=PreparedFinalizationServiceV1(service,archive_source=port,authenticator=port.authenticator)
store=PreparedEnvelopeStoreV1(root,namespace='synthetic-prepared',recovery_key_id='recovery-test-v1',recovery_recipient=recipient,key_epoch=1,authentication_key_id='envelope-test-v1',authentication_key=b'e'*32,authenticator=port.authenticator,toolchain=tools)
point=sys.argv[2]
if point in ('close_before','close_after'):
    original_cleanup=module._Custody.close
    original_close=os.close
    def cleanup(custody):
        targets=[fd for fd,role in custody.handles if role=='data']
        target=targets[-1]
        def close(fd):
            if fd==target:
                if point=='close_after': original_close(fd)
                raise OSError('synthetic close ambiguity')
            original_close(fd)
        os.close=close
        original_cleanup(custody)
    module._Custody.close=cleanup
elif point=='partial_write':
    original_write=os.write
    def write(fd,data):
        if os.fstat(fd).st_mode & 0o170000==0o100000:
            return original_write(fd,data[:17])
        return original_write(fd,data)
    os.write=write
def checkpoint(name):
    if name==point or (point=='partial_write' and name=='write'):
        os.kill(os.getpid(),signal.SIGKILL)
module._checkpoint=checkpoint
try:
    store.seal(preparation,ref.operation_id,expected_intent_sha256=ref.intent_sha256)
except PreparedEnvelopeErrorV1 as exc:
    assert point in ('close_before','close_after') and exc.code=='ARTIFACT_UNKNOWN'
    assert store._uncertain and store._ambiguous_close is not None and store._held_handles
    import fcntl
    lock=next(root.rglob('namespace.lock'))
    contender=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
    try:
        try: fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: pass
        else: raise AssertionError('Uncertain store released namespace custody')
    finally: original_close(contender)
    try: store.seal(preparation,ref.operation_id,expected_intent_sha256=ref.intent_sha256)
    except PreparedEnvelopeErrorV1: pass
    else: raise AssertionError('Poisoned store released a result')
    os._exit(0)
else:
    raise AssertionError('Crash/close injection did not execute')
"""


@pytest.mark.parametrize(
    "point",
    [
        "create",
        "partial_write",
        "full_write",
        "file_sync",
        "readback",
        "link",
        "unlink",
        "directory_sync",
        "close_before_reply",
        "close_before",
        "close_after",
    ],
)
def test_real_cold_process_killpoints_and_close_custody(tmp_path, monkeypatch, keys, point):
    with prepared_case(tmp_path, monkeypatch) as case:
        root = tmp_path / "capsules"
        configured_store(root, case, keys)
        fixture = tmp_path / "synthetic-process-fixture.pickle"
        _restart_fixture(case, fixture, keys, root.resolve())
        case.service.owner.close()
        checkout = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(checkout / "tests"), str(checkout))))
        child = subprocess.run(
            [sys.executable, "-c", _CHILD, str(fixture), point],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert child.returncode == (
            0 if point.startswith("close_") and point != "close_before_reply" else -signal.SIGKILL
        ), child.stdout + child.stderr
        case.service.owner = acquire_production_owner(case.config)
        before = {p.name: p.read_bytes() for p in root.rglob("candidate")}
        before.update({p.name: p.read_bytes() for p in root.rglob("final")})
        fresh = configured_store(root, case, keys)
        if point in {"create", "partial_write"}:
            with pytest.raises(PreparedEnvelopeErrorV1):
                seal(fresh, case)
            assert next(root.rglob("candidate")).read_bytes() == before["candidate"]
            assert not list(root.rglob("final"))
        else:
            ref = seal(fresh, case)
            original = before.get("final", before.get("candidate"))
            assert next(root.rglob("final")).read_bytes() == original
            assert fresh.read(ref).startswith(b"FCENV001")
            assert not list(root.rglob("candidate"))
        assert case.preparation.inspect(case.reference.operation_id).state == "PREPARED_OFFLINE"
        assert not native_rows(case.service)["facts"]


@pytest.mark.parametrize(
    "fault", ["directory_swap", "directory_mode", "lock_swap", "existing_final"]
)
def test_actual_path_and_no_replace_faults_refuse_success(tmp_path, monkeypatch, keys, fault):
    with prepared_case(tmp_path, monkeypatch) as case:
        store = configured_store(tmp_path / "capsules", case, keys)
        moved = tmp_path / "retained-slot"

        def checkpoint(name):
            if name != "readback":
                return
            candidate = next(store.root.rglob("candidate"))
            directory = candidate.parent
            if fault == "directory_swap":
                directory.rename(moved)
                directory.mkdir(mode=0o700)
            elif fault == "directory_mode":
                directory.chmod(0o755)
            elif fault == "lock_swap":
                lock = directory / "operation.lock"
                lock.rename(directory / "unrecognized.lock")
                lock.write_bytes(b"")
                lock.chmod(0o600)
            else:
                final = directory / "final"
                final.write_bytes(b"pre-existing object must never be replaced")
                final.chmod(0o600)

        with monkeypatch.context() as patch:
            patch.setattr(store_module, "_checkpoint", checkpoint)
            with pytest.raises(PreparedEnvelopeErrorV1):
                seal(store, case)
        if fault == "directory_swap":
            assert (moved / "candidate").exists()
        elif fault == "existing_final":
            assert next(store.root.rglob("final")).read_bytes() == (
                b"pre-existing object must never be replaced"
            )
        else:
            assert not list(store.root.rglob("final"))
        assert not native_rows(case.service)["facts"]


def test_actual_namespace_quota_competing_processes(tmp_path, monkeypatch, keys):
    with ExitStack() as stack:
        cases = []
        for index in range(2):
            source = tmp_path / f"source-{index}"
            source.mkdir(mode=0o700)
            environment = stack.enter_context(monkeypatch.context())
            cases.append(
                stack.enter_context(
                    prepared_case(
                        source,
                        environment,
                        key=f"expense-{index}",
                        operation_id=f"operation-{index}",
                    )
                )
            )
        root = tmp_path / "capsules"
        fixtures = []
        for index, case in enumerate(cases):
            configured_store(root, case, keys)
            fixture = tmp_path / f"synthetic-quota-{index}.pickle"
            _restart_fixture(case, fixture, keys, root.resolve())
            fixtures.append(fixture)
            case.service.owner.close()
        script = (
            _CHILD.split("point=sys.argv[2]")[0]
            + r"""
import time
from finance_core.lifecycle_v2.prepared_envelope_v1 import PreparedEnvelopeLimitsV1
store.limits=PreparedEnvelopeLimitsV1(max_slots=1)
ready=Path(sys.argv[1]+'.ready'); ready.touch()
other=Path(sys.argv[2]+'.ready')
deadline=time.monotonic()+15
while not other.exists():
    if time.monotonic()>deadline: raise AssertionError('Test concurrency barrier expired')
    time.sleep(.01)
try:
    result=store.seal(preparation,ref.operation_id,expected_intent_sha256=ref.intent_sha256)
except PreparedEnvelopeErrorV1:
    print('REFUSED')
else:
    print('SEALED:'+result.envelope_sha256)
owner.close(); cfg.close()
"""
        )
        checkout = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(checkout / "tests"), str(checkout))))
        children = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(fixtures[i]), str(fixtures[1 - i])],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(2)
        ]
        outcomes = []
        for child in children:
            out, err = child.communicate(timeout=90)
            assert child.returncode == 0, out + err
            outcomes.append(out.strip())
        assert sum(value.startswith("SEALED:") for value in outcomes) == 1
        assert outcomes.count("REFUSED") == 1
        assert len(list(root.rglob("final"))) == 1
        namespace = next(path for path in root.iterdir() if path.is_dir())
        assert sum(path.is_dir() for path in namespace.iterdir()) == 1
        original = next(root.rglob("final")).read_bytes()
        for index, case in enumerate(cases):
            with monkeypatch.context() as current:
                current.setenv(
                    "FINANCE_RUNTIME_ROOT",
                    str(
                        profile_root(
                            case.config.locator_root, case.config.profile_id, case.config.layout
                        )
                        / "runtime"
                    ),
                )
                case.service.owner = acquire_production_owner(case.config)
                store = configured_store(
                    root, case, keys, limits=PreparedEnvelopeLimitsV1(max_slots=1)
                )
                if outcomes[index].startswith("SEALED:"):
                    assert seal(store, case).envelope_sha256 == outcomes[index].split(":")[1]
                else:
                    with pytest.raises(PreparedEnvelopeErrorV1):
                        seal(store, case)
                assert not native_rows(case.service)["facts"]
        assert next(root.rglob("final")).read_bytes() == original


def test_source_substitute_and_root_symlink_or_shared_mode_refused(tmp_path, monkeypatch, keys):
    with prepared_case(tmp_path, monkeypatch) as case:
        root = tmp_path / "capsules"
        store = configured_store(root, case, keys)

        class Substitute(PreparedFinalizationServiceV1):
            def export_content_v1(self, *args, **kwargs):
                raise AssertionError("Substitute must never be invoked")

        for source in (object(), object.__new__(Substitute)):
            with pytest.raises(PreparedEnvelopeErrorV1):
                store.seal(source, "finalize", expected_intent_sha256=case.reference.intent_sha256)
        assert not list(root.iterdir())
        root.chmod(0o755)
        with pytest.raises(PreparedEnvelopeErrorV1):
            configured_store(root, case, keys)
        root.chmod(0o700)
        alias = tmp_path / "capsules-alias"
        alias.symlink_to(root, target_is_directory=True)
        with pytest.raises(PreparedEnvelopeErrorV1):
            PreparedEnvelopeStoreV1(
                alias,
                namespace="synthetic-prepared",
                recovery_key_id="recovery-test-v1",
                recovery_recipient=keys[2],
                key_epoch=1,
                authentication_key_id="envelope-test-v1",
                authentication_key=b"e" * 32,
                authenticator=case.authenticator,
                toolchain=keys[0],
            )


@pytest.mark.parametrize("after_effect", [False, True])
def test_postpublication_directory_fsync_failure_keeps_original_capsule(
    tmp_path, monkeypatch, keys, after_effect
):
    with prepared_case(tmp_path, monkeypatch) as case:
        store = configured_store(tmp_path / "capsules", case, keys)
        actual_sync = os.fsync
        published = False

        def checkpoint(name):
            nonlocal published
            if name == "link":
                published = True

        def sync(fd):
            if published and os.fstat(fd).st_mode & 0o170000 == 0o040000:
                if after_effect:
                    actual_sync(fd)
                raise OSError(errno.EIO, "synthetic postpublication directory sync")
            return actual_sync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(store_module, "_checkpoint", checkpoint)
            patch.setattr(os, "fsync", sync)
            with pytest.raises(PreparedEnvelopeErrorV1) as caught:
                seal(store, case)
            assert caught.value.code == "ARTIFACT_UNKNOWN"
        original = next(store.root.rglob("final")).read_bytes()
        fresh = configured_store(store.root, case, keys)
        ref = seal(fresh, case)
        assert fresh.read(ref).startswith(b"FCENV001")
        assert next(store.root.rglob("final")).read_bytes() == original
        assert not native_rows(case.service)["facts"]
