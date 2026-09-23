"""Fixed local entry point for human-reviewed D2b corrections.

The only normal database/key/actor authority is the configured owner policy.
Commands accept target IDs, editable fields and reasons, never a verifier.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from finance_core.application.corrections import CorrectionService

from .d2_source import D2OriginalSourceVerifier
from .local_authority import LocalApprovalAuthority
from .policy import open_local_authority_connection, provision


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m finance_core.correction_adapters.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("provision", help="create the first owner-only local policy")
    setup.add_argument("--database", required=True, type=Path)
    setup.add_argument("--actor", required=True)
    show = commands.add_parser("show", help="verify current values and complete history")
    show.add_argument("transaction_id")
    preview = commands.add_parser("preview", help="persist one exact unconsumed plan")
    preview.add_argument("transaction_id")
    preview.add_argument("--reason", required=True)
    for option in ("amount", "currency", "date", "merchant"):
        preview.add_argument(f"--{option}")
    confirm = commands.add_parser("confirm", help="review and confirm on a real terminal")
    confirm.add_argument("plan_id")
    return parser


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "provision":
            policy = provision(args.database, args.actor)
            _emit(
                {
                    "status": "provisioned",
                    "key_id": policy.key_id,
                    "instance_id": policy.instance_id,
                }
            )
            return 0
        with open_local_authority_connection() as conn:
            authority = LocalApprovalAuthority()
            service = CorrectionService(conn, D2OriginalSourceVerifier(), authority)
            if args.command == "show":
                _emit(asdict(service.lookup(args.transaction_id)))
            elif args.command == "preview":
                changes = {
                    name: value
                    for name in ("amount", "currency", "date", "merchant")
                    if (value := getattr(args, name)) is not None
                }
                _emit(asdict(service.preview(args.transaction_id, changes, args.reason)))
            else:
                committed = service.recover(args.plan_id)
                if committed is not None:
                    _emit(asdict(committed))
                    return 0
                plan = service.read_plan(args.plan_id)
                signed = authority.sign_with_terminal(plan)
                _emit(asdict(service.apply(args.plan_id, signed)))
            return 0
    except (ValueError, RuntimeError, sqlite3.Error, OSError) as exc:
        print(f"correction refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
