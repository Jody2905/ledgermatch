from datetime import date
from decimal import Decimal

import pytest

from ledgermatch.loaders import ColumnMap, load_bank, load_ledger, parse_date
from ledgermatch.models import normalize_reference
from ledgermatch.money import fmt, parse_amount


# ---- Money ---------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("1234.50", "1234.50"),
    ("1,234.50", "1234.50"),
    ("-200", "-200.00"),
    ("(200.00)", "-200.00"),
    ("$1,000", "1000.00"),
    ("EC$1,000.5", "1000.50"),
    ("  42  ", "42.00"),
])
def test_amounts_are_parsed_exactly(text, expected):
    assert parse_amount(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "abc", "12.345", "NaN", "Infinity", None, "1.2.3"])
def test_bad_amounts_are_refused_not_rounded(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_decimal_avoids_float_drift():
    # With floats: 0.1 + 0.2 == 0.30000000000000004
    assert parse_amount("0.10") + parse_amount("0.20") == parse_amount("0.30")


def test_accounting_format():
    assert fmt(Decimal("1234.5")) == "1,234.50"
    assert fmt(Decimal("-1234.5")) == "(1,234.50)"


# ---- References -----------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("CHQ 004512", "4512"),
    ("Cheque No. 4512", "004512"),
    ("eft-pay-2610", "EFT PAY 2610"),
    ("REF: RCPT632084", "RCPT632084"),
])
def test_references_from_different_systems_compare_equal(a, b):
    assert normalize_reference(a) == normalize_reference(b)


def test_different_references_stay_different():
    assert normalize_reference("CHQ 4512") != normalize_reference("CHQ 4513")
    assert normalize_reference("") == ""


# ---- Dates ------------------------------------------------------------------

@pytest.mark.parametrize("text", ["2026-10-05", "05/10/2026", "05-Oct-2026", "5 Oct 2026"])
def test_common_date_formats(text):
    assert parse_date(text) == date(2026, 10, 5)


def test_unknown_date_is_refused():
    with pytest.raises(ValueError):
        parse_date("October fifth")


# ---- Loading files ---------------------------------------------------------

BANK_CSV = """Date,Description,Reference,Amount
01/10/2026,CHEQUE PAID,CHQ 004501,-6627.07
02/10/2026,DEPOSIT,RCPT1,6865.30

03/10/2026,BROKEN,,not-money
32/10/2026,BAD DATE,,10.00
04/10/2026,ZERO,,0.00
"""


def test_bad_rows_are_reported_with_row_numbers_and_the_rest_still_loads():
    result = load_bank(BANK_CSV)
    assert len(result.items) == 2
    assert [row for row, _ in result.errors] == [5, 6, 7]   # blank line 4 is simply skipped
    assert "not a valid amount" in result.errors[0][1]
    assert "unrecognised date" in result.errors[1][1]
    assert "zero" in result.errors[2][1]


def test_missing_columns_are_reported():
    result = load_bank("Date,Description\n01/10/2026,x\n")
    assert not result.items
    assert "Amount" in result.errors[0][1]


def test_debit_and_credit_columns():
    csv_text = """Posted,Narrative,Ref,Money Out,Money In
01/10/2026,Cheque,4501,250.00,
02/10/2026,Deposit,R1,,1000.00
03/10/2026,Both?,R2,5.00,5.00
"""
    cols = ColumnMap(date="Posted", description="Narrative", reference="Ref",
                     amount=None, debit="Money Out", credit="Money In")
    result = load_bank(csv_text, cols)
    assert [b.amount for b in result.items] == [Decimal("-250.00"), Decimal("1000.00")]
    assert "both a debit and a credit" in result.errors[0][1]


def test_ledger_loads_voucher_numbers():
    result = load_ledger("Date,Voucher,Payee,Reference,Amount\n01/10/2026,PV1,Acme,4501,-10.00\n")
    assert result.items[0].voucher == "PV1"
    assert result.items[0].payee == "Acme"
