from decimal import Decimal

from conftest import bank, d, ledger
from ledgermatch import sample_data
from ledgermatch.__main__ import main
from ledgermatch.loaders import load_bank, load_ledger
from ledgermatch.matcher import Matcher
from ledgermatch.money import total
from ledgermatch.reconcile import build_statement
from ledgermatch.report import render

OPENING = Decimal("10000.00")


def reconcile(bank_lines, ledger_entries, bank_balance=None, book_balance=None):
    """By default the closing balances are worked out from the transactions."""
    if bank_balance is None:
        bank_balance = OPENING + total(b.amount for b in bank_lines)
    if book_balance is None:
        book_balance = OPENING + total(e.amount for e in ledger_entries)
    result = Matcher().run(bank_lines, ledger_entries)
    return build_statement(result, d(31), bank_balance, book_balance), result


def test_timing_differences_are_sorted_into_the_right_sections():
    s, _ = reconcile(
        [bank(5, "-100.00", "1"), bank(31, "-15.00", desc="CHARGES"), bank(31, "4.20", desc="INTEREST")],
        [ledger(5, "-100.00", "1"), ledger(30, "-250.00", "2"), ledger(31, "900.00", "R9")])
    assert [e.amount for e in s.outstanding_payments] == [Decimal("-250.00")]
    assert [e.amount for e in s.deposits_in_transit] == [Decimal("900.00")]
    assert [b.description for b in s.bank_debits_not_in_books] == ["CHARGES"]
    assert [b.description for b in s.bank_credits_not_in_books] == ["INTEREST"]
    assert s.balances
    assert s.ready_to_sign_off


def test_wrong_closing_balance_shows_as_a_difference():
    s, _ = reconcile([bank(5, "-100.00", "1")], [ledger(5, "-100.00", "1")],
                     bank_balance=OPENING - Decimal("100.00") + Decimal("50.00"))
    assert s.difference == Decimal("50.00")
    assert not s.balances
    assert not s.ready_to_sign_off


def test_issues_block_sign_off_even_when_it_balances():
    s, _ = reconcile([bank(16, "-1520.00", "CHQ 7")], [ledger(15, "-1250.00", "7")])
    assert s.balances
    assert not s.ready_to_sign_off
    assert s.investigate_bank and s.investigate_ledger
    assert not s.outstanding_payments    # the mismatched cheque is NOT treated as outstanding


def test_a_duplicate_is_not_mistaken_for_an_outstanding_cheque():
    # Without the duplicate check, the extra copy would look like an unpresented cheque
    s, _ = reconcile([bank(12, "-3480.00", "CHQ 9")],
                     [ledger(9, "-3480.00", "9", voucher="PV1"), ledger(9, "-3480.00", "9", voucher="PV1")])
    assert not s.outstanding_payments
    assert len(s.investigate_ledger) == 1


def test_sample_month_end_to_end(tmp_path):
    bank_balance, book_balance = sample_data.write(tmp_path)
    b = load_bank(tmp_path / "bank_statement_oct2026.csv")
    led = load_ledger(tmp_path / "cash_book_oct2026.csv")
    assert b.ok and led.ok

    result = Matcher().run(b.items, led.items)
    s = build_statement(result, d(31), bank_balance, book_balance)

    assert s.balances
    assert {i.kind for i in s.issues} == {"possible_duplicate", "amount_mismatch"}
    assert len(s.issues) == 2
    assert {m.rule for m in result.matches} == {"reference", "amount_date", "grouped"}
    assert [e.amount for e in s.deposits_in_transit] == [Decimal("18420.75")]
    assert len(s.outstanding_payments) == 3
    assert [b.description for b in s.bank_debits_not_in_books] == ["SERVICE CHARGES"]
    assert [b.description for b in s.bank_credits_not_in_books] == ["INTEREST CREDIT"]


def test_sample_data_is_the_same_every_time(tmp_path):
    assert sample_data.generate(7) == sample_data.generate(7)


def test_report_text(tmp_path):
    s, r = reconcile([bank(5, "-100.00", "1")], [ledger(5, "-100.00", "1"), ledger(30, "-40.00", "2")])
    text = render(s, r)
    assert "BANK RECONCILIATION STATEMENT as at 31 October 2026" in text
    assert "DIFFERENCE" in text and "0.00" in text
    assert "READY FOR SIGN-OFF" in text


def test_command_line(tmp_path, capsys):
    assert main(["sample", "--folder", str(tmp_path)]) == 0
    bank_balance, book_balance = sample_data.closing_balances(*sample_data.generate())
    code = main(["reconcile", str(tmp_path / "bank_statement_oct2026.csv"),
                 str(tmp_path / "cash_book_oct2026.csv"),
                 "--bank-balance", str(bank_balance), "--book-balance", str(book_balance)])
    assert code == 0
    out = capsys.readouterr().out
    assert "BALANCES, but items need investigating" in out

    code = main(["reconcile", str(tmp_path / "bank_statement_oct2026.csv"),
                 str(tmp_path / "cash_book_oct2026.csv"),
                 "--bank-balance", "1.00", "--book-balance", str(book_balance)])
    assert code == 2
    assert "DOES NOT BALANCE" in capsys.readouterr().out
