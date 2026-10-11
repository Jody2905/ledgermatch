"""The two sides of a reconciliation.

Sign convention (used on BOTH sides, which keeps all the arithmetic simple):
    money coming in   (deposits, receipts, interest)  -> positive
    money going out   (payments, cheques, charges)    -> negative
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_PREFIXES = re.compile(r"^(CHEQUE|CHQ|CHK|REF|EFT|TRF|TFR|DD|NO|NUM)+")


def normalize_reference(ref):
    """Make references comparable across systems.

    The bank might print 'CHQ 004512' where the ledger has '4512'. Both become
    '4512': uppercase, letters and digits only, common prefixes and leading
    zeros removed.
    """
    if not ref:
        return ""
    s = re.sub(r"[^A-Za-z0-9]", "", str(ref)).upper()
    s = _PREFIXES.sub("", s)
    s = s.lstrip("0")
    return s


@dataclass(frozen=True)
class BankLine:
    line_no: int
    date: date
    description: str
    amount: Decimal
    reference: str = ""

    @property
    def key(self):
        return f"B{self.line_no}"

    @property
    def ref(self):
        return normalize_reference(self.reference)


@dataclass(frozen=True)
class LedgerEntry:
    line_no: int
    date: date
    payee: str
    amount: Decimal
    reference: str = ""
    voucher: str = ""

    @property
    def key(self):
        return f"L{self.line_no}"

    @property
    def ref(self):
        return normalize_reference(self.reference)
