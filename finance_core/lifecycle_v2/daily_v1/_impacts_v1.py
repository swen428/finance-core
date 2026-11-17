"""Complete current/prefix inventory and deterministic account effects."""

from dataclasses import asdict
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from ..contracts import LifecycleError, canonical
from .contracts import BUNDLE_FORMAT, account_ref, opening_ref, strict_json, validate_bundle
from .movement_contracts import MOVEMENT_FORMAT, validate_movement


def account_inventory(conn, before_seq=None):
    current = {}
    for row in conn.execute(
        "SELECT * FROM l2_facts WHERE (? IS NULL OR commit_seq<?) ORDER BY record_id,version",
        (before_seq, before_seq),
    ):
        value = strict_json(row["values_json"])
        if value.get("format") == BUNDLE_FORMAT:
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
    current = {}
    for row in conn.execute(
        "SELECT * FROM l2_facts WHERE (? IS NULL OR commit_seq<?) ORDER BY record_id,version",
        (before_seq, before_seq),
    ):
        current[row["record_id"]] = row
    result = []
    for identity in sorted(current):
        row = current[identity]
        value = strict_json(row["values_json"])
        if value.get("format") == MOVEMENT_FORMAT:
            impact = effect(validate_movement(value))
            if impact and impact["account_id"] == account_id:
                from .accounts import bundle_ref

                result.append({"fact_ref": asdict(bundle_ref(row)), **impact})
    return result


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
