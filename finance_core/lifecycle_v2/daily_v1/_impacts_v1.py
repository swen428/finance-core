"""Complete current/prefix inventory and deterministic account effects."""

from dataclasses import asdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from ..contracts import LifecycleError, canonical
from .contracts import BUNDLE_FORMATS, account_ref, opening_ref, strict_json, validate_bundle
from .movement_contracts import MOVEMENT_FORMAT, validate_movement


def account_inventory(conn, before_seq=None):
    current = {}
    for row in conn.execute(
        "SELECT * FROM l2_facts WHERE (? IS NULL OR commit_seq<?) ORDER BY record_id,version",
        (before_seq, before_seq),
    ):
        value = strict_json(row["values_json"])
        if value.get("format") in BUNDLE_FORMATS:
            validate_bundle(value)
            if value["account"]["account_id"] != row["record_id"]:
                raise LifecycleError("Account component belongs to another fact")
            from .accounts import bundle_ref

            current[row["record_id"]] = {"fact_ref": asdict(bundle_ref(row)), "values": value}
        elif value.get("format") == MOVEMENT_FORMAT:
            validate_movement(value)
        else:
            from .accounts import registered_values

            registered_values(value)
            from ..effective_v1 import _business_fields

            _business_fields(canonical(value))
    return [current[k] for k in sorted(current)]


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


def require_accounts(conn, expected, values, before_seq=None):
    actual = account_inventory(conn, before_seq)
    ids = {i["values"]["account"]["account_id"] for i in selected_dependency(expected, values)}
    if [i for i in actual if i["values"]["account"]["account_id"] in ids] != selected_dependency(
        expected, values
    ):
        raise LifecycleError("stale_movement_account_or_opening")


def current_impacts(conn, account_id, before_seq=None):
    return [
        {"fact_ref": row["fact_ref"], **effect(row["values"])}
        for row in opening_inventory(conn, account_id, before_seq)
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


def map_owner(conn, account, before_seq=None):
    """Locate the original confirmed owner, never a later rename carrier.

    The operation's complete semantic audit verifies the evidence. This local
    lookup additionally binds the exact predecessor, owner, and carry chain.
    """
    mapping = account.get("opening_dependency_map")
    if mapping is None:
        return None
    from .accounts import bundle_ref
    from .opening_contracts_v1 import REVIEW_FORMAT, subject

    predecessor = mapping["predecessor_bundle_ref"]
    rows = conn.execute(
        "SELECT * FROM l2_facts WHERE record_id=? AND (? IS NULL OR commit_seq<?) ORDER BY version",
        (mapping["account_id"], before_seq, before_seq),
    ).fetchall()
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
    review = conn.execute(
        "SELECT * FROM l2_reviews WHERE review_id=?", (owner["review_id"],)
    ).fetchone()
    decision = conn.execute(
        "SELECT * FROM l2_decisions WHERE record_id=? AND version=?",
        (owner["record_id"], owner["version"]),
    ).fetchone()
    event = conn.execute("SELECT * FROM l2_events WHERE seq=?", (owner["commit_seq"],)).fetchone()
    if (
        review is None
        or decision is None
        or event is None
        or review["values_json"] != owner["values_json"]
        or strict_json(review["material_json"]).get("format") != REVIEW_FORMAT
        or decision["review_id"] != owner["review_id"]
        or event["kind"] != "confirm"
        or event["entity_id"] != owner["record_id"]
        or event["operation_id"] != decision["operation_id"]
        or strict_json(event["payload_json"])["fact_sha256"] != owner["fact_sha256"]
    ):
        raise LifecycleError("Opening map lacks original complete confirmation")
    for entry in mapping["entries"]:
        ref = entry["movement_fact_ref"]
        fact = conn.execute(
            "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
            (ref["record_id"], ref["version"]),
        ).fetchone()
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
        if subject(dependency, owner_values) != entry["subject"]:
            raise LifecycleError("Opening map subject differs from original fact")
    return asdict(bundle_ref(owner))


def _effect_money(amount, currency):
    from finance_core.money import canonical_money_str

    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        return canonical_money_str(amount, currency)


def resolve_effect_v1(conn, values, fact_ref, account, before_seq=None, *, _proposed=False):
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
        owner = None if _proposed else map_owner(conn, account, before_seq)
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


def opening_inventory(conn, account_id, before_seq=None):
    """Complete current active rows, including included and no-baseline rows."""
    from .accounts import bundle_ref, registered_values

    if conn.execute(
        "SELECT 1 FROM l2_events WHERE kind IN ('import','legacy_archive') "
        "AND (? IS NULL OR seq<?) LIMIT 1",
        (before_seq, before_seq),
    ).fetchone():
        raise LifecycleError("unsupported_ledger_scope: opening dependency inventory")
    accounts = {
        i["fact_ref"]["record_id"]: i["values"] for i in account_inventory(conn, before_seq)
    }
    current = {}
    for row in conn.execute(
        "SELECT * FROM l2_facts WHERE (? IS NULL OR commit_seq<?) ORDER BY record_id,version",
        (before_seq, before_seq),
    ):
        registered_values(strict_json(row["values_json"]))
        current[row["record_id"]] = row
    result = []
    for record_id in sorted(current):
        row = current[record_id]
        values = strict_json(row["values_json"])
        impact = effect(values)
        if impact and impact["account_id"] == account_id:
            ref = asdict(bundle_ref(row))
            result.append(
                {
                    "fact_ref": ref,
                    "economic_event_id": row["economic_event_id"],
                    "values": values,
                    "resolved_effect": resolve_effect_v1(
                        conn, values, ref, accounts[account_id], before_seq
                    ),
                }
            )
    return result


def movement_review_effects(conn, expected, after, before_seq=None):
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
        result = resolve_effect_v1(conn, values, ref, account, before_seq)
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
