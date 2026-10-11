from decimal import Decimal

from conftest import bank, ledger
from ledgermatch.matcher import Matcher


def run(bank_lines, ledger_entries, **kwargs):
    return Matcher(**kwargs).run(bank_lines, ledger_entries)


def matched_pairs(result):
    return [(tuple(b.key for b in m.bank), tuple(e.key for e in m.ledger), m.rule) for m in result.matches]


# ---- Pass 1: reference + amount ---------------------------------------------

def test_matches_on_reference_and_amount_even_with_different_formats():
    b = bank(5, "-500.00", "CHQ 004512")
    e = ledger(3, "-500.00", "4512")
    r = run([b], [e])
    assert matched_pairs(r) == [((b.key,), (e.key,), "reference")]
    assert not r.unmatched_bank and not r.unmatched_ledger


def test_reference_match_works_beyond_the_date_window():
    # A cheque presented two weeks late still matches on its number
    b = bank(20, "-500.00", "CHQ 4512")
    e = ledger(3, "-500.00", "4512")
    assert run([b], [e]).matches[0].rule == "reference"


# ---- Pass 2: amount + date ----------------------------------------------------

def test_matches_on_amount_within_date_window():
    b = bank(7, "-99.99")
    e = ledger(5, "-99.99")
    r = run([b], [e])
    assert r.matches[0].rule == "amount_date"
    assert "cleared 2 day(s) after booking" in r.matches[0].note


def test_no_match_outside_date_window():
    r = run([bank(15, "-99.99")], [ledger(5, "-99.99")])
    assert not r.matches


def test_closest_date_wins_when_unique():
    b = bank(10, "-50.00")
    near = ledger(9, "-50.00")
    far = ledger(7, "-50.00", payee="Other")
    r = run([b], [far, near])
    assert matched_pairs(r)[0][1] == (near.key,)


def test_equally_good_candidates_are_left_for_a_person():
    b = bank(10, "-50.00")
    r = run([b], [ledger(9, "-50.00", payee="Alpha"), ledger(11, "-50.00", payee="Beta")])
    assert not r.matches
    assert len(r.issues_of("ambiguous")) == 1


def test_signs_must_agree():
    # A 50.00 receipt must never match a 50.00 payment
    r = run([bank(10, "50.00")], [ledger(10, "-50.00")])
    assert not r.matches


# ---- Pass 3: grouped deposits ---------------------------------------------

def test_one_deposit_matching_several_receipts():
    b = bank(22, "5015.75")
    parts = [ledger(22, "1840.00"), ledger(22, "2215.50"), ledger(21, "960.25")]
    r = run([b], parts + [ledger(22, "77.00")])
    assert r.matches[0].rule == "grouped"
    assert sorted(e.key for e in r.matches[0].ledger) == sorted(p.key for p in parts)
    assert len(r.unmatched_ledger) == 1


def test_grouping_is_skipped_when_more_than_one_combination_fits():
    b = bank(22, "300.00")
    r = run([b], [ledger(22, "100.00"), ledger(22, "200.00"), ledger(22, "150.00"), ledger(22, "150.00", payee="x")])
    assert not r.matches


def test_grouping_never_mixes_receipts_and_payments():
    r = run([bank(22, "100.00")], [ledger(22, "150.00"), ledger(22, "-50.00")])
    assert not r.matches


# ---- Problems for a person --------------------------------------------------

def test_amount_mismatch_on_same_reference_is_flagged_with_transposition_hint():
    b = bank(16, "-1520.00", "CHQ 4537")
    e = ledger(15, "-1250.00", "4537")
    r = run([b], [e])
    assert not r.matches
    issue = r.issues_of("amount_mismatch")[0]
    assert "(270.00)" in issue.detail
    assert "divisible by 9" in issue.detail


def test_mismatch_not_divisible_by_nine_gets_no_transposition_hint():
    r = run([bank(16, "-1520.00", "CHQ 4537")], [ledger(15, "-1500.00", "4537")])
    assert "divisible by 9" not in r.issues_of("amount_mismatch")[0].detail


def test_double_keyed_voucher_matches_once_and_flags_the_copy():
    b = bank(12, "-3480.00", "CHQ 4539")
    first = ledger(9, "-3480.00", "4539", payee="Eastern Telecom", voucher="PV55")
    copy = ledger(9, "-3480.00", "4539", payee="Eastern Telecom", voucher="PV55")
    r = run([b], [first, copy])
    assert len(r.matches) == 1
    assert len(r.unmatched_ledger) == 1
    dup = r.issues_of("possible_duplicate")
    assert len(dup) == 1 and "PV55" in dup[0].detail
    assert not r.issues_of("amount_mismatch")


def test_genuine_repeated_transactions_are_not_flagged_once_all_match():
    # Two identical bank fees, both recorded in the books: nothing wrong
    r = run([bank(31, "-5.00", desc="FEE"), bank(31, "-5.00", desc="FEE")],
            [ledger(31, "-5.00", payee="Bank fee"), ledger(31, "-5.00", payee="Bank fee")])
    assert len(r.matches) == 2
    assert not r.issues


def test_everything_ends_up_matched_or_unmatched_exactly_once():
    bank_lines = [bank(1, "-10.00", "1"), bank(2, "-20.00"), bank(3, "30.00"), bank(4, "-40.00", "9")]
    ledger_entries = [ledger(1, "-10.00", "1"), ledger(2, "-20.00"), ledger(3, "10.00"), ledger(3, "20.00"),
                      ledger(4, "-45.00", "9"), ledger(5, "-7.00")]
    r = run(bank_lines, ledger_entries)
    seen_bank = [b.key for m in r.matches for b in m.bank] + [b.key for b in r.unmatched_bank]
    seen_ledger = [e.key for m in r.matches for e in m.ledger] + [e.key for e in r.unmatched_ledger]
    assert sorted(seen_bank) == sorted(b.key for b in bank_lines)
    assert sorted(seen_ledger) == sorted(e.key for e in ledger_entries)
    assert Decimal("0") == sum(
        (sum(b.amount for b in m.bank) - sum(e.amount for e in m.ledger) for m in r.matches), Decimal("0"))
