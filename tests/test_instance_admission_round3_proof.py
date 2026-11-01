"""Operation-local installation proof cannot escape its issued witness."""

from __future__ import annotations

import asyncio
import contextvars
import os
import subprocess
import sys
from pathlib import Path

import pytest
from instance_admission_support_v1 import synthetic_config

from finance_core.instance_admission import (
    InstanceAdmissionError,
    acquire_production_owner,
    admit_production_instance,
    authority,
    prepare_production_profile,
    production_read_operation,
    sessions,
)


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    config = synthetic_config(tmp_path, monkeypatch)
    prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
    admit_production_instance(config, operation_id="admit", approval_ref="approved")
    owner = acquire_production_owner(config)
    yield config, owner
    if owner._lease.fd >= 0:
        owner.close()
    config.close()


def test_copied_context_cannot_lend_exited_lifecycle_proof(admitted, monkeypatch):
    config, owner = admitted
    state = authority._state(config)
    assert all("recovery_context" not in entry for entry in state["history"])
    with sessions.lifecycle_read_operation(owner) as conn:
        witness = sessions._witness_for(conn)
        assert witness is not None
        copied = contextvars.copy_context()
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert witness.closed and not witness.active

    def open_legacy_read():
        with production_read_operation(owner):
            raise AssertionError("Legacy read borrowed an old lifecycle proof")

    with monkeypatch.context() as drift:
        drift.setattr(authority, "_migration_contract_digest", lambda: "0" * 64)
        with pytest.raises(InstanceAdmissionError, match="Installed migration identity"):
            copied.run(config.assert_valid)
        with pytest.raises(InstanceAdmissionError, match="Installed migration identity"):
            copied.run(open_legacy_read)
        with pytest.raises(InstanceAdmissionError, match="outside its operation"):
            config.assert_valid(_proof=witness)


def test_async_child_cannot_reuse_parent_lifecycle_proof(admitted, monkeypatch):
    config, owner = admitted

    async def scenario():
        release = asyncio.Event()

        async def child():
            await release.wait()
            with pytest.raises(InstanceAdmissionError, match="Installed migration identity"):
                config.assert_valid()
            with pytest.raises(InstanceAdmissionError, match="Installed migration identity"):
                with production_read_operation(owner):
                    raise AssertionError("Async child borrowed exited lifecycle proof")

        with sessions.lifecycle_read_operation(owner) as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            task = asyncio.create_task(child())
            await asyncio.sleep(0)
        with monkeypatch.context() as drift:
            drift.setattr(authority, "_migration_contract_digest", lambda: "0" * 64)
            release.set()
            await task

    asyncio.run(scenario())


def test_active_proof_is_only_for_its_config_and_normal_other_owner(
    admitted,
    tmp_path,
    monkeypatch,
):
    first, owner = admitted
    first_runtime = os.environ["FINANCE_RUNTIME_ROOT"]
    second_root = tmp_path / "second-owner"
    second_root.mkdir(mode=0o700)
    second = synthetic_config(second_root, monkeypatch, instance_id="second-owner")
    prepare_production_profile(
        second, operation_id="prepare-second", approval_ref="approved"
    ).close()
    admit_production_instance(second, operation_id="admit-second", approval_ref="approved")
    other = acquire_production_owner(second)
    monkeypatch.setenv("FINANCE_RUNTIME_ROOT", first_runtime)
    try:
        with sessions.lifecycle_read_operation(owner) as conn:
            witness = sessions._witness_for(conn)
            assert witness is not None
            first.assert_valid(_proof=witness)
            with pytest.raises(InstanceAdmissionError, match="outside its operation"):
                second.assert_valid(_proof=witness)
            copied = contextvars.copy_context()

            def open_other_read():
                with production_read_operation(other):
                    raise AssertionError("Other owner borrowed the lifecycle proof")

            with monkeypatch.context() as drift:
                drift.setattr(authority, "_migration_contract_digest", lambda: "0" * 64)
                # The active lifecycle witness still owns its own read; copied
                # ambient context cannot authorize a second owner's legacy read.
                assert conn.execute("SELECT 1").fetchone()[0] == 1
                with pytest.raises(InstanceAdmissionError, match="Installed migration identity"):
                    copied.run(open_other_read)
    finally:
        other.close()
        second.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork proof requires POSIX")
def test_fork_inherited_witness_is_not_a_proof(admitted):
    config, owner = admitted
    with sessions.lifecycle_read_operation(owner) as conn:
        witness = sessions._witness_for(conn)
        assert witness is not None
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                config.assert_valid(_proof=witness)
            except InstanceAdmissionError:
                os.write(write_fd, b"REJECTED")
            else:
                os.write(write_fd, b"ACCEPTED")
            os._exit(0)
        os.close(write_fd)
        result = os.read(read_fd, 16)
        os.close(read_fd)
        _, status = os.waitpid(child, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        assert result == b"REJECTED"
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_close_uncertain_revokes_proof_without_releasing_owner(tmp_path):
    child_root = tmp_path / "close-uncertain"
    script = r"""
import os
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() / "tests"))
from instance_admission_support_v1 import synthetic_config
from finance_core.instance_admission import (
    InstanceAdmissionError, ProductionCloseUncertain,
    acquire_production_owner, admit_production_instance, prepare_production_profile,
)
from finance_core.instance_admission import sessions
class Environment:
    def setenv(self, key, value):
        os.environ[key] = value
root = Path(sys.argv[1])
root.mkdir(mode=0o700)
config = synthetic_config(root, Environment())
prepare_production_profile(config, operation_id="prepare", approval_ref="approved").close()
admit_production_instance(config, operation_id="admit", approval_ref="approved")
owner = acquire_production_owner(config)
try:
    with patch.object(sessions, "_close_sqlite_handle", side_effect=OSError("synthetic close")):
        with sessions.lifecycle_read_operation(owner) as conn:
            witness = sessions._witness_for(conn)
            assert witness is not None
            assert conn.execute("SELECT 1").fetchone()[0] == 1
except ProductionCloseUncertain:
    pass
else:
    raise AssertionError("Close uncertainty did not fail")
assert witness.closing and owner._uncertain
try:
    config.assert_valid(_proof=witness)
except InstanceAdmissionError:
    pass
else:
    raise AssertionError("Uncertain witness remained valid")
print("UNCERTAIN_PROOF_REVOKED")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(child_root)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNCERTAIN_PROOF_REVOKED" in result.stdout
