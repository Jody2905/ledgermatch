"""Read bank statements and ledger exports from CSV.

Every bank and accounting system exports differently, so column names are
configurable. A bad row never stops the whole file: it's reported with its
row number and the reason, and the rest still loads, so nothing is lost
or silently skipped.
"""

import contextlib
import csv
import io
from dataclasses import dataclass, field
from datetime import datetime

from .models import BankLine, LedgerEntry
from .money import ZERO, parse_amount

DEFAULT_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y")


@dataclass
class ColumnMap:
    """Which CSV column holds what. Use either `amount`, or `debit` and `credit`."""
    date: str = "Date"
    description: str = "Description"
    reference: str = "Reference"
    amount: str | None = "Amount"
    debit: str | None = None     # money out, written as a positive number
    credit: str | None = None    # money in
    voucher: str | None = None   # ledger only


BANK_COLUMNS = ColumnMap()
LEDGER_COLUMNS = ColumnMap(description="Payee", voucher="Voucher")


@dataclass
class LoadResult:
    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # (row number, message)

    @property
    def ok(self):
        return not self.errors


def parse_date(text, formats=DEFAULT_DATE_FORMATS):
    s = (text or "").strip()
    for f in formats:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {text!r}")


def _read(source):
    """Accepts a file path, an open text stream (e.g. an upload), or the CSV text itself."""
    if hasattr(source, "read"):
        return contextlib.nullcontext(source)
    if isinstance(source, str) and "\n" in source:
        return io.StringIO(source)
    return open(source, newline="", encoding="utf-8-sig")


def _amount(row, cols):
    if cols.amount:
        return parse_amount(row.get(cols.amount))
    debit = (row.get(cols.debit) or "").strip()
    credit = (row.get(cols.credit) or "").strip()
    if debit and credit:
        raise ValueError("row has both a debit and a credit")
    if not debit and not credit:
        raise ValueError("row has neither a debit nor a credit")
    value = parse_amount(credit) if credit else -parse_amount(debit)
    return value


def _load(source, cols, build, date_formats):
    result = LoadResult()
    with _read(source) as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            result.errors.append((1, "file is empty"))
            return result
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        needed = [cols.date, cols.description] + (
            [cols.amount] if cols.amount else [cols.debit, cols.credit])
        missing = [c for c in needed if c not in reader.fieldnames]
        if missing:
            result.errors.append((1, f"missing column(s): {', '.join(missing)}"))
            return result

        for row in reader:
            # The file's real line number. (Counting rows ourselves goes wrong,
            # because the CSV reader silently skips blank lines.)
            row_no = reader.line_num
            if not any((v or "").strip() for v in row.values()):
                continue
            try:
                amount = _amount(row, cols)
                if amount == ZERO:
                    raise ValueError("amount is zero")
                result.items.append(build(
                    row_no,
                    parse_date(row.get(cols.date), date_formats),
                    (row.get(cols.description) or "").strip(),
                    amount,
                    (row.get(cols.reference) or "").strip() if cols.reference else "",
                    row,
                ))
            except ValueError as e:
                result.errors.append((row_no, str(e)))
    return result


def load_bank(source, cols=BANK_COLUMNS, date_formats=DEFAULT_DATE_FORMATS):
    return _load(source, cols,
                 lambda n, d, desc, amt, ref, row: BankLine(n, d, desc, amt, ref),
                 date_formats)


def load_ledger(source, cols=LEDGER_COLUMNS, date_formats=DEFAULT_DATE_FORMATS):
    def build(n, d, payee, amt, ref, row):
        voucher = (row.get(cols.voucher) or "").strip() if cols.voucher else ""
        return LedgerEntry(n, d, payee, amt, ref, voucher)
    return _load(source, cols, build, date_formats)
