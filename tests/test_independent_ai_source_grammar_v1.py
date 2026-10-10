"""Independent finite grammar oracle: raw terminals and complete consumption.

Expectations below are specified before the one completed-D V0, not derived
from the source helper. Lexical acceptance is separate from Money precision.
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal

import pytest

from finance_core.money import MoneyValidationError, validate_amount_for_currency
from finance_core.parser_proposals.independent_ai_source_grammar import assess_source_money

POSITIONS = ("prefix", "suffix", "code_number", "number_code", "number_inner")
WHITESPACE = {" ", "\t", "\r", "\n"}
WRAPPERS = {":", ";", ",", "!", "?"}
DIGITS = set("0123456789")
# 40 fixed category/compatibility representatives, five positions = 200.
UNICODE_CUES = (
    "\u2212",
    "\uff0d",
    "\ufe63",
    "\u2010",
    "\u2011",
    "\u2012",
    "\u2013",
    "\u2014",
    "\u207b",
    "\u208b",
    "\u02d7",
    "\u2796",
    "\uff0b",
    "\ufe62",
    "\uff08",
    "\uff09",
    "\ufe59",
    "\ufe5a",
    "\u00a0",
    "\u202f",
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202e",
    "\u2066",
    "\u2069",
    "\u0301",
    "\u20dd",
    "\u0665",
    "\uff15",
    "\u00b5",
    "\u00bd",
    "\u066b",
    "\u066c",
    "\uff0e",
    "\uff04",
)


def _position(position, character):
    templates = {
        "prefix": character + "SGD5.80",
        "suffix": "SGD5.80" + character,
        "code_number": "SGD" + character + "5.80",
        "number_code": "5.80" + character + "SGD",
        "number_inner": "SGD5" + character + ".80",
    }
    return "Coffee " + templates[position] + " at Cafe"


def _partition_is_exact(assessment):
    assert "".join(item.raw for item in assessment.partition) == assessment.text
    end = 0
    for item in assessment.partition:
        assert item.start == end
        assert assessment.text[item.start : item.end] == item.raw
        end = item.end
    assert end == len(assessment.text)
    for span in assessment.spans:
        assert assessment.text[span.number_start : span.number_end] == span.number


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("codepoint", range(128))
def test_fixed_ascii_raw_terminal_positions(position, codepoint):
    character = chr(codepoint)
    allowed = {
        "prefix": WHITESPACE | WRAPPERS,
        "suffix": WHITESPACE | WRAPPERS | DIGITS,
        "code_number": WHITESPACE | DIGITS,
        "number_code": WHITESPACE | DIGITS,
        "number_inner": DIGITS,
    }[position]
    raw = _position(position, character)
    assessment = assess_source_money(raw)
    assert assessment.qualified is (character in allowed), (position, codepoint, assessment)
    _partition_is_exact(assessment)
    if character in allowed:
        assert assessment.currency_state == "SUPPORTED_EXPLICIT"
        assert assessment.currencies == {"SGD"}
        assert len(assessment.spans) == 1
        expected = {
            "prefix": "5.80",
            "suffix": "5.80" + character if character in DIGITS else "5.80",
            "code_number": character + "5.80" if character in DIGITS else "5.80",
            "number_code": "5.80" + character if character in DIGITS else "5.80",
            "number_inner": "5" + character + ".80",
        }[position]
        assert assessment.spans[0].number == expected
        assert assessment.supports_quote(expected, expected)
        # Lexically complete 5.807 must still fail old Money's quantum rule.
        if position in {"suffix", "number_code"} and character in DIGITS - {"0"}:
            with pytest.raises(MoneyValidationError):
                validate_amount_for_currency(Decimal(expected), "SGD")
    else:
        assert assessment.currency_state == "PRESENT_UNSUPPORTED_OR_AMBIGUOUS"
        assert not assessment.supports_quote("5.80", "5.80")


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("character", UNICODE_CUES)
def test_fixed_unicode_unknown_terminals_do_not_restart_at_digits(position, character):
    assessment = assess_source_money(_position(position, character))
    assert not assessment.qualified, (
        position,
        hex(ord(character)),
        unicodedata.category(character),
    )
    assert assessment.currency_state == "PRESENT_UNSUPPORTED_OR_AMBIGUOUS"
    assert not assessment.supports_quote("5.80", "5.80")
    _partition_is_exact(assessment)


# 34 fixed whole-clause unknown/accounting/currency/number cases.
UNSUPPORTED = (
    "ＵＳＤ 5.80",
    "ＳＧＤ 5.80",
    "SGＤ5.80",
    "＄5.80",
    "US$5.80",
    "S$5.80",
    "$SGD5.80",
    "USD＄5.80",
    "UЅD 5.80",
    "ABC 5.80",
    "abc 5.80",
    "MYR 5.80",
    "5.80 CAD",
    "€5.80",
    "￥5.80",
    "5.80元",
    "- : SGD5.80",
    "SGD (5.80)",
    "(SGD 5.80)",
    "SGD5.80-",
    "SGD1,234.56",
    "SGD5,80",
    "SGD5e2",
    "SGD5/2",
    "SGD５.８０",
    "SGD٥.٨٠",
    "SGD5.80\u2060",
    "\u02d7 SGD5.80",
    "SGD 5.80 \u2212",
    "minus SGD5.80",
    "negative SGD5.80",
    "负5.80 SGD",
    "负数SGD5.80",
    "SGD5.80 or SGD20.00",
)


@pytest.mark.parametrize("clause", UNSUPPORTED)
def test_fixed_unknown_currency_and_money_clauses_never_become_absent(clause):
    assessment = assess_source_money("Coffee " + clause + " at Cafe")
    assert not assessment.qualified
    assert assessment.currency_state == "PRESENT_UNSUPPORTED_OR_AMBIGUOUS"
    assert not assessment.supports_quote("5.80", "5.80")
    _partition_is_exact(assessment)


# 12 literal supported controls. No financial-value conversion is the oracle.
SUPPORTED = (
    ("Coffee SGD5.80 2026-10-08", "SUPPORTED_EXPLICIT", None),
    ("咖啡SGD5.80 2026-10-08", "SUPPORTED_EXPLICIT", None),
    ("Coffee sgd5.80 2026-10-08", "SUPPORTED_EXPLICIT", None),
    ("Cafe\u0301 SGD 5.80 2026-10-08", "SUPPORTED_EXPLICIT", None),
    ("Coffee 5.80 at Cafe", "PROVEN_ABSENT", None),
    ("Coffee 5.80SGD at Cafe", "SUPPORTED_EXPLICIT", None),
    ("Coffee 5.80 SGD at Cafe", "SUPPORTED_EXPLICIT", None),
    ("TOTAL:\tSGD\n5.80", "SUPPORTED_EXPLICIT", None),
    ("Change amount to $5.80", "SUPPORTED_EXPLICIT", "SGD"),
    ("Change amount to $ 5.80", "SUPPORTED_EXPLICIT", "USD"),
    ("Change description to minus special", "PROVEN_ABSENT", None),
    ("Minus is Cafe Coffee SGD5.80", "SUPPORTED_EXPLICIT", None),
)


@pytest.mark.parametrize("raw,state,bare", SUPPORTED)
def test_fixed_supported_money_prose_and_dates(raw, state, bare):
    assessment = assess_source_money(raw, bare_currency=bare)
    assert assessment.qualified, assessment.reasons
    assert assessment.currency_state == state
    _partition_is_exact(assessment)
    if assessment.spans:
        assert [span.number for span in assessment.spans] == ["5.80"]
        assert assessment.supports_quote("5.80", "5.80")
        assert not assessment.supports_quote("5", "5.80")


@pytest.mark.parametrize(
    "raw,accepted",
    [
        ("Coffee SGD5.80 then SGD5.80", False),
        ("Coffee SGD5.80 then SGD－5.80", False),
        ("Coffee SGD－5.80 then SGD5.80", False),
        ("Coffee SGD5.80 then SGD20.00", False),
        ("Coffee SGD5.80 2026-10-08", True),
        ("Coffee SGD5.80 2026-13-08", False),
        ("Coffee SGD5.80 x2026-10-08", False),
        ("Coffee SGD5.80 2026-10-081", False),
    ],
)
def test_fixed_quote_occurrences_and_complete_date_boundaries(raw, accepted):
    assessment = assess_source_money(raw)
    assert assessment.qualified is accepted
    assert assessment.supports_quote("5.80", "5.80") is accepted
    _partition_is_exact(assessment)


def test_fixed_bare_dollar_without_known_currency_is_not_absent():
    assessment = assess_source_money("Coffee $5.80")
    assert not assessment.qualified
    assert assessment.currency_state == "PRESENT_UNSUPPORTED_OR_AMBIGUOUS"


def test_fixed_receipt_alias_only_after_original_joint_role_proof():
    assert not assess_source_money("TOTAL S$5.80").qualified
    total = assess_source_money("TOTAL S$5.80", receipt_currency="SGD")
    assert total.qualified
    assert total.currencies == {"SGD"}
