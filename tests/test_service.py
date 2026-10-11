from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ledgermatch import passwords, sample_data
from ledgermatch.db import init_db
from ledgermatch.service import ImportProblem, NotAllowed, ReconciliationService, from_cents, to_cents

PERIOD_END = date(2026, 10, 31)
PASSWORD = "correct-horse-battery"


@pytest.fixture
def svc(tmp_path):
    conn = init_db(tmp_path / "test.db")
    clock = lambda: datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    return ReconciliationService(conn, clock=clock)


@pytest.fixture
def people(svc):
    return {
        "maker": svc.create_user("kbaptiste", "Kerry Baptiste", "preparer", PASSWORD),
        "checker": svc.create_user("mjames", "Marcus James", "approver", PASSWORD),
        "other_preparer": svc.create_user("tlewis", "Tara Lewis", "preparer", PASSWORD),
    }


@pytest.fixture
def run_id(svc, people, tmp_path):
    """The demo month, imported by the preparer."""
    bank_balance, book_balance = sample_data.write(tmp_path / "files")
    account = svc.create_account("Consolidated Fund - Main Account", "Eastern Caribbean Bank", "4821")
    return svc.create_run(account, PERIOD_END,
                          tmp_path / "files" / "bank_statement_oct2026.csv",
                          tmp_path / "files" / "cash_book_oct2026.csv",
                          bank_balance, book_balance, people["maker"])


def issue(svc, run_id, kind):
    return next(i for i in svc.statement(run_id).issues if i.kind == kind)


def resolve_everything(svc, run_id, maker):
    """What a preparer would do with the two problems in the demo month."""
    s = svc.statement(run_id)
    dup_copy = s.investigate_ledger[[e.payee for e in s.investigate_ledger].index("Eastern Telecom")]
    svc.mark_error(run_id, maker, "ledger", dup_copy.line_no,
                   "Voucher PV10055 keyed twice; reversal journal JV-2611 raised")
    bank_line = svc.statement(run_id).investigate_bank[0]
    ledger_line = svc.statement(run_id).investigate_ledger[0]
    svc.manual_match(run_id, maker, [bank_line.line_no], [ledger_line.line_no],
                     "Cheque was for 1,520.00 per copy; books keyed 1,250.00. Correcting journal JV-2612")


# ---- Basics -----------------------------------------------------------------

def test_cents_conversion_is_exact():
    assert to_cents(Decimal("1234.56")) == 123456
    assert to_cents(Decimal("-0.01")) == -1
    assert from_cents(-123456) == Decimal("-1234.56")


def test_passwords_are_hashed_and_verified():
    stored = passwords.hash_password(PASSWORD)
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert passwords.verify_password(PASSWORD, stored)
    assert not passwords.verify_password("wrong-password", stored)
    assert stored != passwords.hash_password(PASSWORD)   # random salt each time
    with pytest.raises(ValueError):
        passwords.hash_password("short")


def test_authenticate(svc, people):
    assert svc.authenticate("KBaptiste ", PASSWORD)["id"] == people["maker"]
    assert svc.authenticate("kbaptiste", "nope-nope-nope") is None
    assert svc.authenticate("nobody", PASSWORD) is None


def test_only_last_four_digits_of_account_numbers(svc):
    with pytest.raises(ValueError):
        svc.create_account("Payroll", "Bank", "0012345678")


# ---- Importing ----------------------------------------------------------------

def test_import_saves_the_automatic_results(svc, run_id):
    s = svc.statement(run_id)
    assert s.balances
    assert {i.kind for i in s.issues} == {"possible_duplicate", "amount_mismatch"}
    assert svc.run(run_id)["status"] == "draft"
    assert svc.audit_trail(run_id)[0]["action"] == "created"


def test_files_with_bad_rows_are_refused_completely(svc, people):
    account = svc.create_account("Payroll Account", "Bank", "1111")
    bad_bank = "Date,Description,Reference,Amount\n01/10/2026,X,,-10.00\n02/10/2026,Y,,oops\n"
    ledger = "Date,Voucher,Payee,Reference,Amount\n01/10/2026,PV1,X,,-10.00\n"
    with pytest.raises(ImportProblem) as e:
        svc.create_run(account, PERIOD_END, bad_bank, ledger, Decimal("0"), Decimal("0"), people["maker"])
    assert "Bank statement row 3" in e.value.errors[0]
    assert not svc.runs()


def test_one_reconciliation_per_account_and_period(svc, run_id, people, tmp_path):
    account = svc.run(run_id)["account_id"]
    with pytest.raises(NotAllowed):
        svc.create_run(account, PERIOD_END, tmp_path / "files" / "bank_statement_oct2026.csv",
                       tmp_path / "files" / "cash_book_oct2026.csv", Decimal("0"), Decimal("0"), people["maker"])


# ---- Resolving issues --------------------------------------------------------------

def test_marking_the_duplicate_as_an_error_makes_it_a_reversal(svc, run_id, people):
    s = svc.statement(run_id)
    copy = next(e for e in s.investigate_ledger if e.payee == "Eastern Telecom")
    svc.mark_error(run_id, people["maker"], "ledger", copy.line_no, "Voucher keyed twice; reversal JV-1")

    s = svc.statement(run_id)
    assert s.balances
    assert [i.kind for i in s.issues] == ["amount_mismatch"]
    assert s.book_corrections[0][1] == Decimal("3480.00")      # reversing a (3,480.00) payment
    assert "Eastern Telecom" in s.book_corrections[0][0]
    assert not any(e.payee == "Eastern Telecom" for e in s.outstanding_payments)


def test_manual_match_with_a_difference_becomes_a_book_correction(svc, run_id, people):
    s = svc.statement(run_id)
    b = s.investigate_bank[0]
    e = next(x for x in s.investigate_ledger if x.amount == Decimal("-1250.00"))
    svc.manual_match(run_id, people["maker"], [b.line_no], [e.line_no], "Books keyed 1,250 instead of 1,520")

    s = svc.statement(run_id)
    assert s.balances
    assert (s.book_corrections[0][1]) == Decimal("-270.00")
    assert [i.kind for i in s.issues] == ["possible_duplicate"]


def test_every_manual_change_needs_a_reason(svc, run_id, people):
    s = svc.statement(run_id)
    with pytest.raises(NotAllowed, match="reason"):
        svc.manual_match(run_id, people["maker"], [s.investigate_bank[0].line_no],
                         [s.investigate_ledger[0].line_no], "")
    with pytest.raises(NotAllowed, match="reason"):
        svc.mark_error(run_id, people["maker"], "ledger", s.investigate_ledger[0].line_no, "ok")


def test_a_line_cannot_be_matched_twice(svc, run_id, people):
    first = svc.matches(run_id)[0]
    with pytest.raises(NotAllowed, match="already matched"):
        svc.manual_match(run_id, people["maker"], first["bank"], first["ledger"], "Trying to match it again")


def test_money_in_cannot_match_money_out(svc, run_id, people):
    s = svc.statement(run_id)
    deposit = s.deposits_in_transit[0]
    payment = s.investigate_bank[0]
    with pytest.raises(NotAllowed, match="Money in"):
        svc.manual_match(run_id, people["maker"], [payment.line_no], [deposit.line_no], "Wrong pairing test")


def test_unmatching_puts_items_back(svc, run_id, people):
    before = len(svc.statement(run_id).outstanding_payments)
    m = next(m for m in svc.matches(run_id) if m["match"]["rule"] == "reference")
    svc.unmatch(run_id, people["maker"], m["match"]["id"], "Matched to the wrong cheque")
    s = svc.statement(run_id)
    assert len(s.outstanding_payments) + len(s.investigate_ledger) > before
    assert s.balances      # still balances: the items simply moved back to the unmatched lists


def test_dismissing_an_issue(svc, run_id, people):
    dup = issue(svc, run_id, "possible_duplicate")
    svc.dismiss_issue(run_id, people["maker"], dup.id, "Checked: two genuine identical invoices")
    assert [i.kind for i in svc.statement(run_id).issues] == ["amount_mismatch"]


# ---- Maker-checker ---------------------------------------------------------------

def test_cannot_submit_with_open_issues(svc, run_id, people):
    with pytest.raises(NotAllowed, match="issue"):
        svc.submit(run_id, people["maker"])


def test_full_approval_workflow(svc, run_id, people):
    resolve_everything(svc, run_id, people["maker"])
    assert svc.statement(run_id).ready_to_sign_off

    svc.submit(run_id, people["maker"])
    svc.approve(run_id, people["checker"])

    run = svc.run(run_id)
    assert run["status"] == "approved"
    assert run["approver_name"] == "Marcus James"
    actions = [a["action"] for a in svc.audit_trail(run_id)]
    assert actions == ["created", "mark_error", "manual_match", "submitted", "approved"]


def test_nobody_can_approve_their_own_work(svc, run_id, people):
    # Even an approver who prepared a reconciliation can't approve it
    account = svc.create_account("Second Account", "Bank", "2222")
    csv_bank = "Date,Description,Reference,Amount\n01/10/2026,X,1,-10.00\n"
    csv_ledger = "Date,Voucher,Payee,Reference,Amount\n01/10/2026,PV1,X,1,-10.00\n"
    own = svc.create_run(account, PERIOD_END, csv_bank, csv_ledger, Decimal("-10"), Decimal("-10"), people["checker"])
    svc.submit(own, people["checker"])
    with pytest.raises(NotAllowed, match="someone else"):
        svc.approve(own, people["checker"])


def test_only_approvers_approve_and_only_the_preparer_submits(svc, run_id, people):
    resolve_everything(svc, run_id, people["maker"])
    with pytest.raises(NotAllowed, match="Only the person who prepared"):
        svc.submit(run_id, people["other_preparer"])
    svc.submit(run_id, people["maker"])
    with pytest.raises(NotAllowed, match="Only approvers"):
        svc.approve(run_id, people["other_preparer"])


def test_rejection_sends_it_back_for_changes(svc, run_id, people):
    resolve_everything(svc, run_id, people["maker"])
    svc.submit(run_id, people["maker"])
    svc.reject(run_id, people["checker"], "Attach the journal numbers to the corrections")
    run = svc.run(run_id)
    assert run["status"] == "rejected"
    assert "journal numbers" in run["rejection_reason"]

    m = next(m for m in svc.matches(run_id) if m["match"]["rule"] == "manual")
    svc.unmatch(run_id, people["maker"], m["match"]["id"], "Redoing with the journal reference")   # editable again
    s = svc.statement(run_id)
    svc.manual_match(run_id, people["maker"], [s.investigate_bank[0].line_no], [s.investigate_ledger[0].line_no],
                     "Keying error 1,250 vs 1,520; corrected by JV-2612")
    svc.submit(run_id, people["maker"])
    assert svc.run(run_id)["status"] == "submitted"


def test_approved_reconciliations_are_locked(svc, run_id, people):
    resolve_everything(svc, run_id, people["maker"])
    svc.submit(run_id, people["maker"])
    svc.approve(run_id, people["checker"])
    m = svc.matches(run_id)[0]
    with pytest.raises(NotAllowed, match="approved"):
        svc.unmatch(run_id, people["maker"], m["match"]["id"], "Trying to change history")
    with pytest.raises(NotAllowed):
        svc.reject(run_id, people["checker"], "Too late to reject")


def test_cannot_submit_when_it_does_not_balance(svc, people):
    account = svc.create_account("Third Account", "Bank", "3333")
    csv_bank = "Date,Description,Reference,Amount\n01/10/2026,X,1,-10.00\n"
    csv_ledger = "Date,Voucher,Payee,Reference,Amount\n01/10/2026,PV1,X,1,-10.00\n"
    run = svc.create_run(account, PERIOD_END, csv_bank, csv_ledger, Decimal("-10"), Decimal("-15"), people["maker"])
    with pytest.raises(NotAllowed, match="doesn't balance"):
        svc.submit(run, people["maker"])


def test_only_the_preparer_can_change_their_reconciliation(svc, run_id, people):
    s = svc.statement(run_id)
    item = s.investigate_ledger[0]
    for intruder in (people["other_preparer"], people["checker"]):
        with pytest.raises(NotAllowed, match="Only the person who prepared"):
            svc.mark_error(run_id, intruder, "ledger", item.line_no, "Not my reconciliation to change")
    assert svc.statement(run_id).investigate_ledger == s.investigate_ledger
