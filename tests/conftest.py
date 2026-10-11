import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledgermatch.models import BankLine, LedgerEntry  # noqa: E402

_counter = {"b": 0, "l": 0}


def d(day, month=10):
    return date(2026, month, day)


def bank(day, amount, ref="", desc="ITEM"):
    _counter["b"] += 1
    return BankLine(_counter["b"], d(day), desc, Decimal(amount), ref)


def ledger(day, amount, ref="", payee="Payee", voucher=""):
    _counter["l"] += 1
    return LedgerEntry(_counter["l"], d(day), payee, Decimal(amount), ref, voucher)
