"""Bounded D3 locators against synthetic temporary staging databases."""

from __future__ import annotations

from pathlib import Path

import openclaw_staging_bridge_support_v1 as support

from finance_core.openclaw_staging_bridge import commands
from finance_core.openclaw_staging_bridge.capture_discovery import (
    _HIGH_WATER_SQL,
    _PAGE_SQL,
)


def _context(workspace: support.BridgeWorkspace) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.workspace_path),
        "operator_actor_id": "111",
        "telegram_account_id": "finance-account",
        "telegram_conversation_id": "111",
        "conversation_binding_id": "binding-1",
    }


def _capture(
    workspace: support.BridgeWorkspace,
    message_id: int,
    *,
    account_id: str = "finance-account",
    binding_id: str = "binding-1",
) -> str:
    update = support.telegram_text_update(
        f"synthetic expense {message_id}", update_id=message_id, message_id=message_id
    )
    captured = support.run_cli(
        support.make_request(
            "capture",
            support.authenticated_text_capture_arguments(
                workspace, update, account_id=account_id, binding_id=binding_id
            ),
            idempotency_key=support.canonical_capture_key(message_id=message_id),
        )
    )
    assert captured.exit_code == 0, captured.response
    return str(captured.response["result"]["capture_job"]["public_id"])


def _list(workspace: support.BridgeWorkspace, **page: object) -> support.CliOutcome:
    return support.run_cli(
        support.make_request("list_capture_recovery_candidates", {**_context(workspace), **page})
    )


def test_discovery_pages_stable_job_ids_and_requires_rescan_for_old_changes(
    tmp_path: Path,
) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    first_id = _capture(workspace, 10)
    second_id = _capture(workspace, 11)
    first = _list(workspace, limit=1)
    assert first.exit_code == 0, first.response
    page = first.response["result"]
    assert [row["job_public_id"] for row in page["candidates"]] == [first_id]
    assert page["has_more"] is True
    through = page["through_job_public_id"]
    third_id = _capture(workspace, 12)
    second = _list(
        workspace,
        after_job_public_id=page["after_job_public_id"],
        through_job_public_id=through,
        limit=1,
    )
    assert second.exit_code == 0, second.response
    assert [row["job_public_id"] for row in second.response["result"]["candidates"]] == [second_id]
    assert second.response["result"]["has_more"] is False
    with support.open_database(workspace) as conn:
        conn.execute(
            "UPDATE finance_capture_jobs SET status = 'needs_attention' WHERE public_id = ?",
            (first_id,),
        )
        conn.commit()
    rescan = _list(workspace, limit=100)
    assert rescan.exit_code == 0, rescan.response
    assert [row["job_public_id"] for row in rescan.response["result"]["candidates"]] == [
        first_id,
        second_id,
        third_id,
    ]


def test_discovery_and_original_message_locator_isolate_every_context_field(
    tmp_path: Path,
) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    job_id = _capture(workspace, 10)
    for field in (
        "operator_actor_id",
        "telegram_account_id",
        "telegram_conversation_id",
        "conversation_binding_id",
    ):
        wrong = {**_context(workspace), field: "other"}
        listed = support.run_cli(
            support.make_request("list_capture_recovery_candidates", {**wrong, "limit": 10})
        )
        if listed.exit_code == 0:
            assert listed.response["result"]["candidates"] == []
        else:
            assert listed.response["error"]["code"] == "ACTOR_MISMATCH"
        located = support.run_cli(
            support.make_request(
                "get_capture_job_for_message", {**wrong, "telegram_message_id": 10}
            )
        )
        if located.exit_code == 0:
            assert located.response["result"]["candidate"] is None
        else:
            assert located.response["error"]["code"] == "ACTOR_MISMATCH"
    located = support.run_cli(
        support.make_request(
            "get_capture_job_for_message", {**_context(workspace), "telegram_message_id": 10}
        )
    )
    assert located.exit_code == 0, located.response
    assert located.response["result"]["candidate"]["job_public_id"] == job_id
    assert located.response["result"]["candidate"]["telegram_message_id"] == "10"


def test_discovery_high_water_is_scoped_to_visible_binding(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    first_id = _capture(workspace, 10)
    _capture(workspace, 20, account_id="other-account")
    _capture(workspace, 30, binding_id="binding-2")
    second_id = _capture(workspace, 40)
    first_page = _list(workspace, limit=1)
    assert first_page.exit_code == 0, first_page.response
    first = first_page.response["result"]
    assert first["through_job_public_id"] == second_id
    assert first["after_job_public_id"] == first_id
    assert [row["job_public_id"] for row in first["candidates"]] == [first_id]
    assert not {"job_id", "after_job_id", "through_job_id"}.intersection(first)
    assert "job_id" not in first["candidates"][0]
    second_page = _list(
        workspace,
        after_job_public_id=first["after_job_public_id"],
        through_job_public_id=first["through_job_public_id"],
        limit=1,
    )
    assert second_page.exit_code == 0, second_page.response
    assert [row["job_public_id"] for row in second_page.response["result"]["candidates"]] == [
        second_id
    ]
    for hidden in (
        {**_context(workspace), "telegram_account_id": "missing-account"},
        {**_context(workspace), "conversation_binding_id": "missing-binding"},
    ):
        empty = support.run_cli(
            support.make_request("list_capture_recovery_candidates", {**hidden, "limit": 1})
        )
        assert empty.exit_code == 0, empty.response
        assert empty.response["result"]["candidates"] == []
        assert empty.response["result"]["through_job_public_id"] is None
        assert empty.response["result"]["after_job_public_id"] is None
        assert empty.response["result"]["has_more"] is False

    foreign = support.run_cli(
        support.make_request(
            "list_capture_recovery_candidates",
            {
                **_context(workspace),
                "after_job_public_id": first_id,
                "through_job_public_id": second_id,
                "telegram_account_id": "other-account",
                "limit": 1,
            },
        )
    )
    assert foreign.exit_code != 0
    assert foreign.response["error"]["code"] == "LIFECYCLE_CONFLICT"


def test_discovery_rejects_unbounded_or_invalid_page(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    _capture(workspace, 10)
    for page in (
        {"limit": 101},
        {"after_job_public_id": "fcj_unknown", "limit": 1},
        {"through_job_public_id": "fcj_unknown", "limit": 1},
        {"after_job_public_id": True, "through_job_public_id": "fcj_unknown", "limit": 1},
    ):
        refused = _list(workspace, **page)
        assert refused.exit_code != 0


def test_discovery_queries_use_binding_index_and_job_keyset(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    for offset in range(36):
        _capture(
            workspace,
            1000 + offset,
            account_id="finance-account" if offset % 3 == 0 else "other-account",
            binding_id="binding-1" if offset % 2 == 0 else "binding-2",
        )
    with support.open_database(workspace) as conn:
        upper_plan = [
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + _HIGH_WATER_SQL,
                ("missing-account", "111", "111", "binding-1"),
            )
        ]
        page_plan = [
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + _PAGE_SQL,
                (0, 2**63 - 1, "111", "finance-account", "111", "binding-1", 101),
            )
        ]
    assert any("SEARCH source USING INDEX" in item for item in upper_plan)
    assert all("SCAN job" not in item for item in upper_plan)
    assert any("SEARCH job USING INTEGER PRIMARY KEY" in item for item in page_plan)
    assert all("TEMP B-TREE" not in item for item in page_plan)
    empty = support.run_cli(
        support.make_request(
            "list_capture_recovery_candidates",
            {**_context(workspace), "telegram_account_id": "missing-account", "limit": 1},
        )
    )
    assert empty.exit_code == 0, empty.response
    assert empty.response["result"]["through_job_public_id"] is None
    assert empty.response["result"]["candidates"] == []


def test_notice_observation_token_changes_before_any_host_enqueue(tmp_path: Path) -> None:
    workspace = support.create_bridge_workspace(tmp_path)
    job_id = _capture(workspace, 10)
    context = {**_context(workspace), "job_public_id": job_id}
    observed = support.run_cli(support.make_request("get_capture_recovery", context))
    assert observed.exit_code == 0, observed.response
    old_token = observed.response["result"]["recovery_step_token"]
    with support.open_database(workspace) as conn:
        conn.execute(
            "UPDATE finance_capture_jobs SET status = 'needs_attention', "
            "last_error = 'synthetic_failure' WHERE public_id = ?",
            (job_id,),
        )
        conn.commit()
    current = support.run_cli(support.make_request("get_capture_recovery", context))
    assert current.exit_code == 0, current.response
    assert current.response["result"]["recovery_step_token"] != old_token
    stale = support.run_cli(
        support.make_request(
            "resume_capture_recovery",
            {**context, "recovery_step_token": old_token},
            idempotency_key=commands._capture_recovery_key(job_id, old_token),
        )
    )
    assert stale.exit_code == 0, stale.response
    assert stale.response["result"]["stale_recovery_step"] is True
    assert stale.response["result"]["performed_action"] == "none"
