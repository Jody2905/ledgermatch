import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledgermatch import sample_data  # noqa: E402
from ledgermatch.db import init_db  # noqa: E402
from ledgermatch.models import BankLine, LedgerEntry  # noqa: E402
from ledgermatch.service import ReconciliationService  # noqa: E402

_counter = {"b": 0, "l": 0}


def d(day, month=10):
    return date(2026, month, day)


def bank(day, amount, ref="", desc="ITEM"):
    _counter["b"] += 1
    return BankLine(_counter["b"], d(day), desc, Decimal(amount), ref)


def ledger(day, amount, ref="", payee="Payee", voucher=""):
    _counter["l"] += 1
    return LedgerEntry(_counter["l"], d(day), payee, Decimal(amount), ref, voucher)


# ---- Database fixtures shared by the service and export tests ----

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
