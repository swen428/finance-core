"""B5.1a/B5.1b/B5.1c staging runner CLI — staged lifecycle, resume, finalize.

Usage::

    python -m finance_core.receipt_staging_runner.cli init \
        --workspace /path --manifest /path/manifest.json

    python -m finance_core.receipt_staging_runner.cli recover \
        --workspace /path --manifest /path/manifest.json

    python -m finance_core.receipt_staging_runner.cli intake \
        --workspace /path --manifest /path/manifest.json \
        --source-image /path/receipt.jpg \
        --public-id-prefix run1 \
        --ocr-helper /path/helper --ocr-version 1.0.0

    python -m finance_core.receipt_staging_runner.cli inspect \
        --workspace /path --manifest /path/manifest.json \
        --proposal-public-id prop_run1

    python -m finance_core.receipt_staging_runner.cli confirm \
        --workspace /path --manifest /path/manifest.json \
        --proposal-public-id prop_run1 \
        --expected-content-hash <hash> --actor owner

    python -m finance_core.receipt_staging_runner.cli convert \
        --workspace /path --manifest /path/manifest.json \
        --conversion-command /path/conversion.json

    python -m finance_core.receipt_staging_runner.cli fact-set \
        --workspace /path --manifest /path/manifest.json \
        --fact-set-command /path/fact_set.json

    python -m finance_core.receipt_staging_runner.cli prepare \
        --workspace /path --manifest /path/manifest.json \
        --receipt-public-id rcpt_...

    python -m finance_core.receipt_staging_runner.cli authorize \
        --workspace /path --manifest /path/manifest.json \
        --receipt-public-id rcpt_... \
        --expected-calculation-snapshot-hash <hash> --actor owner

    python -m finance_core.receipt_staging_runner.cli resume \
        --workspace /path --manifest /path/manifest.json \
        --authorization-id authz_...

    python -m finance_core.receipt_staging_runner.cli finalize \
        --workspace /path --manifest /path/manifest.json \
        --authorization-id authz_... --actor owner

Output: one machine-readable JSON envelope on stdout.
Errors: written to stderr.
Exit codes: 0 = success, 1 = operational error, 2 = usage error.

No fake OCR implementation exists in this module.  ``resume`` is read-only;
``finalize`` is the guarded finalization boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from finance_core.receipt_staging_runner.local_intake import (
    LocalIntakeError,
    require_local_runner_receipt,
    require_local_runner_receipt_proposal,
    validate_personal_conversion_command,
    validate_personal_fact_set_command,
)
from finance_core.receipt_staging_runner.models import (
    RunnerFinalizeError,
    RunnerManifestError,
    RunnerRecoveryError,
    RunnerResumeError,
    RunnerWorkspaceError,
    cli_error_envelope,
    cli_success_envelope,
    parse_runner_manifest,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def _emit_json(data: dict[str, object]) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _emit_error(error_type: str, message: str, *, reason: str | None = None) -> None:
    json.dump(cli_error_envelope(error_type, message, reason=reason), sys.stderr, indent=2)
    sys.stderr.write("\n")


def _read_manifest_bytes(manifest_path: str) -> bytes:
    p = Path(manifest_path)
    if not p.is_absolute():
        raise RunnerManifestError(f"Manifest path must be absolute: {manifest_path!r}")
    if not p.exists():
        raise RunnerManifestError(f"Manifest file does not exist: {manifest_path!r}")
    if p.is_dir():
        raise RunnerManifestError(f"Manifest path is a directory: {manifest_path!r}")
    return p.read_bytes()


def _open_workspace(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """Recover workspace and open staging database. Returns (workspace, manifest, conn)."""
    from finance_core.receipt_staging_runner.workspace import recover_runner_workspace
    from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
    from finance_core.staging_guard import open_staging_database

    raw = _read_manifest_bytes(args.manifest)
    manifest = parse_runner_manifest(raw)
    workspace = recover_runner_workspace(args.workspace, manifest)
    conn = open_staging_database(workspace.database_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    return workspace, manifest, conn


def _validate_actor(manifest: object, actor: str) -> None:
    """Validate that actor matches the manifest operator_actor_id."""
    if actor != manifest.operator_actor_id:  # type: ignore[attr-defined]
        raise LocalIntakeError(
            f"Actor {actor!r} does not match manifest operator {manifest.operator_actor_id!r}"  # type: ignore[attr-defined]
        )


# ---------------------------------------------------------------------------
# init command (B5.1a)
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    from finance_core.receipt_staging_runner.participants import bootstrap_participants
    from finance_core.receipt_staging_runner.workspace import create_runner_workspace
    from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS
    from finance_core.staging_guard import create_staging_database

    raw = _read_manifest_bytes(args.manifest)
    manifest = parse_runner_manifest(raw)
    workspace = create_runner_workspace(args.workspace, manifest)
    conn = create_staging_database(workspace.database_path, migration_paths=TEMP_DB_MIGRATION_PATHS)
    try:
        bootstrap_result = bootstrap_participants(conn, manifest)
    finally:
        conn.close()

    payload = {
        "workspace_path": workspace.workspace_path,
        "database_path": workspace.database_path,
        "manifest_hash": workspace.manifest_hash,
        "workspace_identity": workspace.workspace_identity,
        "participant_public_ids": list(bootstrap_result.participant_public_ids),
        "bootstrap_hash": bootstrap_result.bootstrap_hash,
    }
    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# recover command (B5.1a)
# ---------------------------------------------------------------------------


def _cmd_recover(args: argparse.Namespace) -> int:
    from finance_core.receipt_staging_runner.recovery import run_recovery

    raw = _read_manifest_bytes(args.manifest)
    manifest = parse_runner_manifest(raw)
    evidence = run_recovery(args.workspace, manifest, authorization_id=args.authorization_id)
    _emit_json(cli_success_envelope(evidence.to_json_dict()))
    return EXIT_OK


# ---------------------------------------------------------------------------
# intake command (Stage A)
# ---------------------------------------------------------------------------


def _cmd_intake(args: argparse.Namespace) -> int:
    from finance_core.intake.macos_vision_receipt_ocr import MacOSVisionOcrEngine
    from finance_core.receipt_staging_runner.local_intake import run_local_receipt_intake

    workspace, manifest, conn = _open_workspace(args)
    try:
        engine = MacOSVisionOcrEngine(args.ocr_helper, expected_version=args.ocr_version)
        result = run_local_receipt_intake(
            conn,
            workspace=workspace,
            manifest=manifest,
            source_image_path=args.source_image,
            engine=engine,
            public_id_prefix=args.public_id_prefix,
        )
    finally:
        conn.close()

    payload = {
        "source_basename": Path(result.source_image_path).name,
        "content_hash": result.content_hash,
        "file_size": result.file_size,
        "mime_type": result.mime_type,
        "workspace_copy_path": result.workspace_copy_path,
        "raw_intake_public_id": result.raw_intake_public_id,
        "local_evidence_public_id": result.local_evidence_public_id,
        "attachment_id": result.attachment_id,
        "extraction_public_id": result.extraction_public_id,
        "proposal_public_id": result.ingestion.proposal_public_id,
        "parse_status": result.ingestion.parse_status,
        "ambiguity_flags": list(result.ingestion.ambiguity_flags),
        "idempotent": result.idempotent,
        "next_command": "inspect",
    }
    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# inspect command (Stage B)
# ---------------------------------------------------------------------------


def _cmd_inspect(args: argparse.Namespace) -> int:
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash

    workspace, manifest, conn = _open_workspace(args)
    try:
        # Local-lineage guard: only B5.1b local proposals allowed.
        require_local_runner_receipt_proposal(
            conn, args.proposal_public_id, workspace=workspace, manifest=manifest
        )

        # Load proposal by public_id.
        row = conn.execute(
            "SELECT id, public_id, parsed_payload, parse_status "
            "FROM parser_outputs WHERE public_id = ?",
            (args.proposal_public_id,),
        ).fetchone()
        if row is None:
            raise LocalIntakeError(f"Proposal {args.proposal_public_id!r} not found")

        content_hash = compute_effective_proposal_content_hash(conn, {"id": row["id"]})
        payload_data = json.loads(row["parsed_payload"])

        payload = {
            "proposal_public_id": row["public_id"],
            "effective_content_hash": content_hash,
            "parse_status": row["parse_status"],
            "proposed_merchant": payload_data.get("merchant"),
            "proposed_amount": payload_data.get("amount"),
            "proposed_currency": payload_data.get("currency"),
            "proposed_transaction_date": payload_data.get("transaction_date"),
            "ambiguity_flags": payload_data.get("ambiguity_flags", []),
            "ocr_evidence": payload_data.get("ocr_evidence"),
            "next_command": "confirm",
        }
    finally:
        conn.close()

    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# confirm command (Stage C)
# ---------------------------------------------------------------------------


def _cmd_confirm(args: argparse.Namespace) -> int:
    from finance_core.parser_proposals import confirm_proposal
    from finance_core.parser_proposals.content_hash import compute_effective_proposal_content_hash

    workspace, manifest, conn = _open_workspace(args)
    try:
        _validate_actor(manifest, args.actor)

        # Local-lineage guard: only B5.1b local proposals allowed.
        require_local_runner_receipt_proposal(
            conn, args.proposal_public_id, workspace=workspace, manifest=manifest
        )

        row = conn.execute(
            "SELECT id, public_id FROM parser_outputs WHERE public_id = ?",
            (args.proposal_public_id,),
        ).fetchone()
        if row is None:
            raise LocalIntakeError(f"Proposal {args.proposal_public_id!r} not found")

        current_hash = compute_effective_proposal_content_hash(conn, {"id": row["id"]})
        if current_hash != args.expected_content_hash:
            raise LocalIntakeError(
                f"Expected content hash {args.expected_content_hash!r} does not match "
                f"current effective hash {current_hash!r}"
            )

        result = confirm_proposal(
            conn,
            row["id"],
            actor=args.actor,
            confirmation_public_id=f"pca_{args.proposal_public_id}",
        )
    finally:
        conn.close()

    payload = {
        "proposal_public_id": args.proposal_public_id,
        "confirmation_result": {k: v for k, v in result.items() if k != "id"},
        "next_command": "convert",
    }
    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# convert command (Stage D)
# ---------------------------------------------------------------------------


def _cmd_convert(args: argparse.Namespace) -> int:
    from finance_core.parser_proposals.receipt_facts_conversion import (
        ReceiptFactsConversionCommand,
        convert_confirmed_receipt_proposal_to_facts,
    )

    workspace, manifest, conn = _open_workspace(args)
    try:
        command_data = json.loads(Path(args.conversion_command).read_bytes())
        command = ReceiptFactsConversionCommand.from_mapping(command_data)

        # Local-lineage guard: confirm target is a B5.1b local proposal.
        require_local_runner_receipt_proposal(
            conn, command.proposal_public_id, workspace=workspace, manifest=manifest
        )

        # Personal-only enforcement BEFORE conversion.
        validate_personal_conversion_command(manifest, command)

        conversion = convert_confirmed_receipt_proposal_to_facts(conn, command)
    finally:
        conn.close()

    payload = {
        "receipt_public_id": conversion.receipt_public_id,
        "receipt_id": conversion.receipt_id,
        "conversion_result_hash": conversion.conversion_result_hash,
        "proposal_content_hash": conversion.proposal_content_hash,
        "idempotent": conversion.idempotent,
        "next_command": "fact-set",
    }
    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# fact-set command (Stage E)
# ---------------------------------------------------------------------------


def _cmd_fact_set(args: argparse.Namespace) -> int:
    from finance_core.parser_proposals.receipt_item_allocation_facts import (
        ReceiptItemAllocationFactsCommand,
        persist_receipt_item_allocation_facts,
    )

    workspace, manifest, conn = _open_workspace(args)
    try:
        command_data = json.loads(Path(args.fact_set_command).read_bytes())
        command = ReceiptItemAllocationFactsCommand.from_mapping(command_data)

        # Local-lineage guard: confirm receipt traces to local-file conversion.
        require_local_runner_receipt(
            conn, command.receipt_public_id, workspace=workspace, manifest=manifest
        )

        # Personal-only re-validation BEFORE persistence.
        validate_personal_fact_set_command(conn, command, manifest)

        result = persist_receipt_item_allocation_facts(conn, command)
    finally:
        conn.close()

    payload = {
        "fact_set_public_id": result.fact_set_public_id,
        "fact_set_version": result.fact_set_version,
        "fact_set_result_hash": result.fact_set_result_hash,
        "item_count": result.item_count,
        "allocation_count": result.allocation_count,
        "idempotent": result.idempotent,
        "next_command": "prepare",
    }
    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# prepare command (Stage F)
# ---------------------------------------------------------------------------


def _cmd_prepare(args: argparse.Namespace) -> int:
    from finance_core.receipt_finalization import prepare_receipt_calculation

    workspace, manifest, conn = _open_workspace(args)
    try:
        # Local-lineage guard: confirm receipt traces to local-file conversion.
        require_local_runner_receipt(
            conn, args.receipt_public_id, workspace=workspace, manifest=manifest
        )

        prepared = prepare_receipt_calculation(conn, args.receipt_public_id)
    finally:
        conn.close()

    payload = {
        "receipt_public_id": prepared.receipt_public_id,
        "calculation_snapshot_id": prepared.calculation_snapshot_id,
        "calculation_snapshot_hash": prepared.calculation_snapshot_hash,
        "currency": prepared.currency,
        "payer_participant_public_id": prepared.payer_participant_public_id,
        "next_command": "authorize",
        "next_required_arg": (
            f"--expected-calculation-snapshot-hash {prepared.calculation_snapshot_hash}"
        ),
    }
    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# authorize command (Stage G)
# ---------------------------------------------------------------------------


def _cmd_authorize(args: argparse.Namespace) -> int:
    from finance_core.receipt_finalization import (
        authorize_receipt_finalization,
        prepare_receipt_calculation,
    )

    workspace, manifest, conn = _open_workspace(args)
    try:
        _validate_actor(manifest, args.actor)

        # Local-lineage guard: confirm receipt traces to local-file conversion.
        require_local_runner_receipt(
            conn, args.receipt_public_id, workspace=workspace, manifest=manifest
        )

        prepared = prepare_receipt_calculation(conn, args.receipt_public_id)

        if prepared.calculation_snapshot_hash != args.expected_calculation_snapshot_hash:
            raise LocalIntakeError(
                f"Expected snapshot hash {args.expected_calculation_snapshot_hash!r} "
                f"does not match current {prepared.calculation_snapshot_hash!r}"
            )

        authorization = authorize_receipt_finalization(conn, prepared, actor_id=args.actor)
    finally:
        conn.close()

    payload = {
        "receipt_public_id": args.receipt_public_id,
        "authorization_id": prepared.authorization_id,
        "authorization_content_hash": authorization.content_hash,
        "calculation_snapshot_id": prepared.calculation_snapshot_id,
        "finalization_executed": False,
        "note": "Authorized but NOT finalized. B5.1b does not finalize.",
    }
    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# resume command (B5.1c, read-only)
# ---------------------------------------------------------------------------


def _cmd_resume(args: argparse.Namespace) -> int:
    from finance_core.receipt_staging_runner.resume_finalize import resume_runner_run

    raw = _read_manifest_bytes(args.manifest)
    manifest = parse_runner_manifest(raw)
    report = resume_runner_run(
        args.workspace,
        manifest,
        authorization_id=args.authorization_id,
    )
    _emit_json(cli_success_envelope(report.to_json_dict()))
    return EXIT_OK


# ---------------------------------------------------------------------------
# finalize command (B5.1c, guarded)
# ---------------------------------------------------------------------------


def _cmd_finalize(args: argparse.Namespace) -> int:
    from finance_core.receipt_staging_runner.resume_finalize import finalize_runner_run

    raw = _read_manifest_bytes(args.manifest)
    manifest = parse_runner_manifest(raw)
    _validate_actor(manifest, args.actor)

    report, output = finalize_runner_run(
        args.workspace,
        manifest,
        authorization_id=args.authorization_id,
    )
    payload = {
        "run_manifest": report.to_json_dict(),
        "finalization_result": {
            "finalization_public_id": output.finalization_public_id,
            "calculation_run_public_id": output.calculation_run_public_id,
            "obligations_created": output.obligations_created,
            "settlement_public_ids": list(output.settlement_public_ids),
            "status": output.status,
            "transaction_public_id": output.transaction_public_id,
            "audit_id": output.audit_id,
            "idempotency_key": output.idempotency_key,
        },
    }
    _emit_json(cli_success_envelope(payload))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="receipt_staging_runner",
        description="B5.1a/B5.1b staging receipt runner CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Common args helper.
    def _add_workspace_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--workspace", required=True, help="Absolute external workspace path")
        p.add_argument("--manifest", required=True, help="Absolute path to manifest JSON")

    # init
    init_p = sub.add_parser("init", help="Create a new staging workspace")
    _add_workspace_args(init_p)

    # recover
    recover_p = sub.add_parser("recover", help="Recover and verify an existing workspace")
    _add_workspace_args(recover_p)
    recover_p.add_argument("--authorization-id", default=None)

    # intake (Stage A)
    intake_p = sub.add_parser("intake", help="Safe-copy image, OCR, produce proposal")
    _add_workspace_args(intake_p)
    intake_p.add_argument("--source-image", required=True, help="Absolute path to source JPEG/PNG")
    intake_p.add_argument("--public-id-prefix", required=True, help="Caller-owned ID prefix")
    intake_p.add_argument(
        "--ocr-helper", required=True, help="Absolute path to macOS Vision helper"
    )
    intake_p.add_argument("--ocr-version", required=True, help="Expected helper version")

    # inspect (Stage B)
    inspect_p = sub.add_parser("inspect", help="Read-only proposal summary")
    _add_workspace_args(inspect_p)
    inspect_p.add_argument("--proposal-public-id", required=True)

    # confirm (Stage C)
    confirm_p = sub.add_parser("confirm", help="Explicit human confirmation")
    _add_workspace_args(confirm_p)
    confirm_p.add_argument("--proposal-public-id", required=True)
    confirm_p.add_argument("--expected-content-hash", required=True)
    confirm_p.add_argument("--actor", required=True)

    # convert (Stage D)
    convert_p = sub.add_parser("convert", help="Guarded conversion (personal-only)")
    _add_workspace_args(convert_p)
    convert_p.add_argument("--conversion-command", required=True, help="Path to command JSON")

    # fact-set (Stage E)
    factset_p = sub.add_parser("fact-set", help="Persist personal fact set")
    _add_workspace_args(factset_p)
    factset_p.add_argument("--fact-set-command", required=True, help="Path to command JSON")

    # prepare (Stage F)
    prepare_p = sub.add_parser("prepare", help="Prepare calculation snapshot")
    _add_workspace_args(prepare_p)
    prepare_p.add_argument("--receipt-public-id", required=True)

    # authorize (Stage G)
    authorize_p = sub.add_parser("authorize", help="Explicit human authorization")
    _add_workspace_args(authorize_p)
    authorize_p.add_argument("--receipt-public-id", required=True)
    authorize_p.add_argument("--expected-calculation-snapshot-hash", required=True)
    authorize_p.add_argument("--actor", required=True)

    # resume (B5.1c, read-only)
    resume_p = sub.add_parser("resume", help="Reconstruct the run manifest from durable truth")
    _add_workspace_args(resume_p)
    resume_p.add_argument("--authorization-id", required=True)

    # finalize (B5.1c, guarded)
    finalize_p = sub.add_parser("finalize", help="Resume and finalize a durably authorized receipt")
    _add_workspace_args(finalize_p)
    finalize_p.add_argument("--authorization-id", required=True)
    finalize_p.add_argument("--actor", required=True)

    return parser


_COMMANDS = {
    "init": _cmd_init,
    "recover": _cmd_recover,
    "intake": _cmd_intake,
    "inspect": _cmd_inspect,
    "confirm": _cmd_confirm,
    "convert": _cmd_convert,
    "fact-set": _cmd_fact_set,
    "prepare": _cmd_prepare,
    "authorize": _cmd_authorize,
    "resume": _cmd_resume,
    "finalize": _cmd_finalize,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handler = _COMMANDS.get(args.command)
    if handler is None:
        _emit_error("usage_error", f"Unknown command: {args.command}")
        return EXIT_USAGE

    try:
        return handler(args)
    except (RunnerManifestError, RunnerWorkspaceError) as exc:
        _emit_error(type(exc).__name__, str(exc))
        return EXIT_ERROR
    except RunnerRecoveryError as exc:
        _emit_error(type(exc).__name__, str(exc))
        return EXIT_ERROR
    except RunnerResumeError as exc:
        _emit_error(type(exc).__name__, str(exc))
        return EXIT_ERROR
    except RunnerFinalizeError as exc:
        _emit_error(type(exc).__name__, str(exc), reason=exc.reason)
        return EXIT_ERROR
    except LocalIntakeError as exc:
        _emit_error(type(exc).__name__, str(exc))
        return EXIT_ERROR
    except Exception as exc:
        _emit_error("unexpected_error", str(exc))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
