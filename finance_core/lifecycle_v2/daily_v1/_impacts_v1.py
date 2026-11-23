"""Complete current/prefix inventory and deterministic account effects."""

from dataclasses import asdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from ..contracts import LifecycleError, canonical
from .contracts import (
    BUNDLE_FORMATS,
    REGISTRY_ID,
    account_ref,
    opening_ref,
    strict_json,
    validate_bundle,
)
from .movement_contracts import MOVEMENT_FORMAT
from .transfer_facts_v1 import TRANSFER_FORMAT, project_transfer_fact_v1


def _inventory(facts, *, daily_only=False):
    from ..effective_v1 import _business_fields
    from .accounts import bundle_ref, registered_values

    accounts, current = {}, {}
    for row in facts:
        value = strict_json(row["values_json"])
        if daily_only and type(value) is dict and "format" not in value:
            # Native expense validation uses the caller's Decimal context,
            # which an evidence callback can change between these reads.
            return None
        value = registered_values(value)
        if value.get("format") in BUNDLE_FORMATS:
            if value["account"]["account_id"] != row["record_id"]:
                raise LifecycleError("Account component belongs to another fact")
            accounts[row["record_id"]] = (bundle_ref(row), row)
        elif value.get("format") not in {MOVEMENT_FORMAT, TRANSFER_FORMAT}:
            _business_fields(canonical(value))
        current[row["record_id"]] = row
    return (
        tuple(accounts[k] for k in sorted(accounts)),
        tuple(current[k] for k in sorted(current)),
    )


def _prefix_inventory(conn, before_seq, rows):
    def collect():
        facts = (
            rows.table(conn, "facts") if before_seq is None else rows.facts_before(conn, before_seq)
        )
        return _inventory(facts, daily_only=True)

    return rows.daily_projection(conn, "prefix", REGISTRY_ID, before_seq, None, collect)


def account_inventory(conn, before_seq=None, *, _rows=None):
    inventory = _prefix_inventory(conn, before_seq, _rows) if _rows is not None else None
    if inventory is None:
        inventory = _inventory(
            conn.execute(
                "SELECT * FROM l2_facts WHERE (? IS NULL OR commit_seq<?)"
                " ORDER BY record_id,version",
                (before_seq, before_seq),
            )
        )
    accounts, _ = inventory
    return [
        {"fact_ref": asdict(ref), "values": strict_json(row["values_json"])}
        for ref, row in accounts
    ]


def effect(value):
    if value.get("format") != MOVEMENT_FORMAT or value["status"] == "VOID":
        return None
    amount = Decimal(value["account_amount"])
    if value["kind"] == "expense":
        amount = amount.copy_negate()
    return {
        "role": "debit" if value["kind"] == "expense" else "credit",
        "account_id": value["account_id"],
        "currency": value["account_currency"],
        "amount": str(amount),
        "funds_time": value["funds_time"],
        "opening_ref": value["opening_ref"],
        "coverage": value["coverage"],
    }


def selected_dependency(expected, values):
    ids = {values["account_id"]} if values["account_id"] else set()
    before = expected.before_values
    if before and before.get("format") == MOVEMENT_FORMAT and before["account_id"]:
        ids.add(before["account_id"])
    return [item for item in expected.accounts if item["values"]["account"]["account_id"] in ids]


def require_accounts(conn, expected, values, before_seq=None, *, _rows=None):
    actual = account_inventory(conn, before_seq, _rows=_rows)
    ids = {i["values"]["account"]["account_id"] for i in selected_dependency(expected, values)}
    if [i for i in actual if i["values"]["account"]["account_id"] in ids] != selected_dependency(
        expected, values
    ):
        raise LifecycleError("stale_movement_account_or_opening")


def current_impacts(conn, account_id, before_seq=None, *, _rows=None):
    return [
        {"fact_ref": row["fact_ref"], **effect(row["values"])}
        for row in opening_inventory(conn, account_id, before_seq, _rows=_rows)
    ]


def require_effect_baseline(value, account):
    if value["account_ref"] != asdict(account_ref(account)):
        # Rename may change display metadata but never account currency/type.
        if value["account_currency"] != account["account"]["currency"]:
            raise LifecycleError("Account currency changed")
    opening = account["opening"]
    active_ref = asdict(opening_ref(account)) if opening and opening["status"] == "ACTIVE" else None
    if value["opening_ref"] != active_ref:
        raise LifecycleError("Movement lacks current opening treatment")


def review_impact_changes(before, after):
    """Per-account current effect replacement shown on the complete card."""
    changes = {}
    for value, sign in ((before, -1), (after, 1)):
        if value is None:
            continue
        impact = effect(value)
        if impact:
            key = (impact["account_id"], impact["currency"])
            amount = (
                Decimal(impact["amount"])
                if impact["coverage"]["treatment"] != "included"
                else Decimal(0)
            )
            with localcontext(
                Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)
            ):
                changes[key] = changes.get(key, Decimal(0)) + (
                    amount if sign == 1 else amount.copy_negate()
                )
    from finance_core.money import canonical_money_str

    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        return [
            {
                "account_id": account,
                "currency": currency,
                "balance_delta": canonical_money_str(amount, currency),
            }
            for (account, currency), amount in sorted(changes.items())
        ]


def map_owner(conn, account, before_seq=None, *, _rows=None):
    """Locate the original confirmed owner, never a later rename carrier.

    The operation's complete semantic audit verifies the evidence. This local
    lookup additionally binds the exact predecessor, owner, and carry chain.
    """
    mapping = account.get("opening_dependency_map")
    if mapping is None:
        return None
    from .accounts import bundle_ref

    facts = (
        tuple(
            row
            for row in _rows.many(conn, "record_facts", mapping["account_id"])
            if before_seq is None or row["commit_seq"] < before_seq
        )
        if _rows is not None
        else conn.execute(
            "SELECT * FROM l2_facts WHERE record_id=? AND (? IS NULL OR commit_seq<?)"
            " ORDER BY version",
            (mapping["account_id"], before_seq, before_seq),
        ).fetchall()
    )

    def locate():
        return _map_owner_fact(conn, account, facts, _rows=_rows)

    # A caller-supplied value must match the latest exact raw carrier before
    # it can hit the memo. The view also binds every referenced fact byte.
    if _rows is not None and facts and strict_json(facts[-1]["values_json"]) == account:
        owner = _rows.daily_projection(
            conn, "map_owner", REGISTRY_ID, before_seq, facts[-1]["values_json"], locate
        )
    else:
        owner = locate()
    return asdict(bundle_ref(owner))


def _map_owner_fact(conn, account, rows, *, _rows=None):
    from .accounts import bundle_ref
    from .opening_contracts_v1 import (
        REVIEW_FORMAT,
        TRANSFER_MAP_FORMAT,
        TRANSFER_REVIEW_FORMAT,
        subject,
    )

    mapping = account["opening_dependency_map"]
    review_format = (
        TRANSFER_REVIEW_FORMAT if mapping["format"] == TRANSFER_MAP_FORMAT else REVIEW_FORMAT
    )
    predecessor = mapping["predecessor_bundle_ref"]
    if len(rows) <= predecessor["version"]:
        raise LifecycleError("Opening map original owner absent")
    prior = rows[predecessor["version"] - 1]
    owner = rows[predecessor["version"]]
    owner_values = validate_bundle(strict_json(owner["values_json"]))
    if (
        asdict(bundle_ref(prior)) != predecessor
        or owner["version"] != predecessor["version"] + 1
        or owner["predecessor_sha256"] != prior["fact_sha256"]
        or owner_values["transition"] not in {"opening_set", "opening_correct"}
        or owner_values.get("opening_dependency_map") != mapping
        or owner_values["opening"] != account["opening"]
    ):
        raise LifecycleError("Opening map original owner differs")
    previous = owner
    for carrier in rows[predecessor["version"] + 1 :]:
        values = validate_bundle(strict_json(carrier["values_json"]))
        if (
            carrier["version"] != previous["version"] + 1
            or carrier["predecessor_sha256"] != previous["fact_sha256"]
            or values["transition"] != "account_rename"
            or values["opening"] != account["opening"]
            or values.get("opening_dependency_map") != mapping
        ):
            raise LifecycleError("Opening map carry chain differs")
        previous = carrier
    if strict_json(rows[-1]["values_json"]) != account:
        raise LifecycleError("Opening map carrier differs from prefix")
    if _rows is not None:
        review = _rows.one(conn, "review", owner["review_id"])
        decision = _rows.one(conn, "decision_version", owner["record_id"], owner["version"])
        event = _rows.one(conn, "event", owner["commit_seq"])
    else:
        review = conn.execute(
            "SELECT * FROM l2_reviews WHERE review_id=?", (owner["review_id"],)
        ).fetchone()
        decision = conn.execute(
            "SELECT * FROM l2_decisions WHERE record_id=? AND version=?",
            (owner["record_id"], owner["version"]),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM l2_events WHERE seq=?", (owner["commit_seq"],)
        ).fetchone()
    if (
        review is None
        or decision is None
        or event is None
        or review["values_json"] != owner["values_json"]
        or strict_json(review["material_json"]).get("format") != review_format
        or decision["review_id"] != owner["review_id"]
        or event["kind"] != "confirm"
        or event["entity_id"] != owner["record_id"]
        or event["operation_id"] != decision["operation_id"]
        or strict_json(event["payload_json"])["fact_sha256"] != owner["fact_sha256"]
    ):
        raise LifecycleError("Opening map lacks original complete confirmation")
    for entry in mapping["entries"]:
        ref = entry["movement_fact_ref"]
        fact = (
            _rows.one(conn, "fact_version", ref["record_id"], ref["version"])
            if _rows is not None
            else conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
                (ref["record_id"], ref["version"]),
            ).fetchone()
        )
        if (
            fact is None
            or asdict(bundle_ref(fact)) != ref
            or fact["commit_seq"] >= owner["commit_seq"]
        ):
            raise LifecycleError("Opening map referenced fact absent or changed")
        dependency = {
            "fact_ref": ref,
            "economic_event_id": fact["economic_event_id"],
            "values": strict_json(fact["values_json"]),
        }
        if dependency["values"].get("format") == TRANSFER_FORMAT:
            dependency["impact_role"] = entry["impact_role"]
        if subject(dependency, owner_values) != entry["subject"]:
            raise LifecycleError("Opening map subject differs from original fact")
    return owner


def _effect_money(amount, currency):
    from finance_core.money import canonical_money_str

    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        return canonical_money_str(amount, currency)


def resolve_effect_v1(
    conn, values, fact_ref, account, before_seq=None, *, _proposed=False, _rows=None
):
    """One current/prefix meaning for balances, dependencies and review effects."""
    impact = effect(values)
    if impact is None:
        return None
    owner, source = None, "direct"
    mapping = account.get("opening_dependency_map")
    treatment = values["coverage"]["treatment"]
    active = account["opening"] and account["opening"]["status"] == "ACTIVE"
    applicable = asdict(opening_ref(account)) if active else None
    if mapping is not None:
        owner = None if _proposed else map_owner(conn, account, before_seq, _rows=_rows)
        entries = [
            e
            for e in mapping["entries"]
            if e["movement_fact_ref"] == fact_ref and e["impact_role"] == impact["role"]
        ]
        if entries:
            treatment, source = entries[0]["treatment"], "map"
    if source == "direct":
        owner = None
        require_effect_baseline(values, account)
        if (active and treatment not in {"included", "excluded"}) or (
            not active and treatment != "no_baseline"
        ):
            raise LifecycleError("Movement lacks supported current treatment")
    if (
        impact["account_id"] != account["account"]["account_id"]
        or impact["currency"] != account["account"]["currency"]
    ):
        raise LifecycleError("Resolved impact account differs")
    return {
        "format": "finance-daily-resolved-effect-v1",
        "movement_fact_ref": fact_ref,
        "account_id": impact["account_id"],
        "impact_role": impact["role"],
        "currency": impact["currency"],
        "source": source,
        "opening_ref": applicable,
        "map_owner_fact_ref": owner,
        "treatment": treatment,
        "balance_impact": _effect_money(
            Decimal(0) if treatment == "included" else Decimal(impact["amount"]), impact["currency"]
        ),
    }


def resolve_transfer_effect_v1(
    conn, impact, fact_ref, account, before_seq=None, *, _proposed=False, _rows=None
):
    definition = account["account"]
    if (definition["account_id"], definition["currency"]) != (
        impact["account_id"],
        impact["currency"],
    ):
        raise LifecycleError("Transfer role refers to an absent or incompatible account")
    opening = account["opening"]
    active = opening is not None and opening["status"] == "ACTIVE"
    current = asdict(opening_ref(account)) if active else None
    treatment, owner, source = impact["coverage"]["treatment"], None, "direct"
    mapping = account.get("opening_dependency_map")
    if mapping is not None:
        entry = next(
            (
                row
                for row in mapping["entries"]
                if row["movement_fact_ref"] == fact_ref and row["impact_role"] == impact["role"]
            ),
            None,
        )
        if entry is not None:
            owner = None if _proposed else map_owner(conn, account, before_seq, _rows=_rows)
            treatment, source = entry["treatment"], "map"
    if source == "direct" and current != impact["opening_ref"]:
        raise LifecycleError("Transfer role lacks coverage for its current opening")
    return {
        "format": "finance-daily-transfer-effect-v1",
        "transfer_fact_ref": fact_ref,
        "account_id": impact["account_id"],
        "impact_role": impact["role"],
        "currency": impact["currency"],
        "source": source,
        "opening_ref": current,
        "map_owner_fact_ref": owner,
        "treatment": treatment,
        "recorded_change": impact["amount"],
        "balance_impact": _effect_money(
            Decimal(0) if treatment == "included" else Decimal(impact["amount"]), impact["currency"]
        ),
    }


def opening_inventory(conn, account_id, before_seq=None, *, include_transfers=False, _rows=None):
    """Complete current active rows, including included and no-baseline rows."""
    from .accounts import bundle_ref, registered_values

    if (
        any(
            event["kind"] in {"import", "legacy_archive"}
            and (before_seq is None or event["seq"] < before_seq)
            for event in _rows.table(conn, "events")
        )
        if _rows is not None
        else conn.execute(
            "SELECT 1 FROM l2_events WHERE kind IN ('import','legacy_archive') "
            "AND (? IS NULL OR seq<?) LIMIT 1",
            (before_seq, before_seq),
        ).fetchone()
    ):
        raise LifecycleError("unsupported_ledger_scope: opening dependency inventory")
    inventory = _prefix_inventory(conn, before_seq, _rows) if _rows is not None else None
    if inventory is not None:
        account_rows, current = inventory
        accounts = {ref.record_id: strict_json(row["values_json"]) for ref, row in account_rows}
    else:
        accounts = {
            i["fact_ref"]["record_id"]: i["values"] for i in account_inventory(conn, before_seq)
        }
        latest = {}
        for row in conn.execute(
            "SELECT * FROM l2_facts WHERE (? IS NULL OR commit_seq<?) ORDER BY record_id,version",
            (before_seq, before_seq),
        ):
            registered_values(strict_json(row["values_json"]))
            latest[row["record_id"]] = row
        current = (latest[k] for k in sorted(latest))
    result = []
    for row in current:
        values = strict_json(row["values_json"])
        if values.get("format") == TRANSFER_FORMAT:
            roles = sorted(project_transfer_fact_v1(values).effects, key=lambda role: role["role"])
            for role in roles:
                if role["account_id"] != account_id:
                    continue
                if not include_transfers:
                    raise LifecycleError("unsupported_transfer_opening_dependencies")
                ref = asdict(bundle_ref(row))
                result.append(
                    {
                        "fact_ref": ref,
                        "economic_event_id": row["economic_event_id"],
                        "values": values,
                        "impact_role": role["role"],
                        "resolved_effect": resolve_transfer_effect_v1(
                            conn, role, ref, accounts[account_id], before_seq, _rows=_rows
                        ),
                    }
                )
            continue
        impact = effect(values)
        if impact and impact["account_id"] == account_id:
            ref = asdict(bundle_ref(row))
            result.append(
                {
                    "fact_ref": ref,
                    "economic_event_id": row["economic_event_id"],
                    "values": values,
                    "resolved_effect": resolve_effect_v1(
                        conn, values, ref, accounts[account_id], before_seq, _rows=_rows
                    ),
                }
            )
    return result


def movement_review_effects(conn, expected, after, before_seq=None, *, _rows=None):
    """Versioned proof for new cards; historical v1 cards retain their formula."""
    accounts = {i["fact_ref"]["record_id"]: i["values"] for i in expected.accounts}
    resolved, changes = [], {}
    for values, ref, sign in (
        (expected.before_values, expected.target_bundle_ref, -1),
        (after, None, 1),
    ):
        if values is None or effect(values) is None:
            resolved.append(None)
            continue
        account = accounts[values["account_id"]]
        result = resolve_effect_v1(conn, values, ref, account, before_seq, _rows=_rows)
        resolved.append(result)
        key = (result["account_id"], result["currency"])
        with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
            amount = Decimal(result["balance_impact"])
            changes[key] = changes.get(key, Decimal(0)) + (
                amount if sign == 1 else amount.copy_negate()
            )
    return (
        [
            {
                "account_id": key[0],
                "currency": key[1],
                "balance_delta": _effect_money(value, key[1]),
            }
            for key, value in sorted(changes.items())
        ],
        {
            "format": "finance-daily-movement-review-effects-v1",
            "before": resolved[0],
            "after": resolved[1],
        },
    )
