"""Generates a realistic month of treasury activity for testing and demos.

Writes two files, a bank statement and the cash book (ledger), that agree
except for the things a real reconciliation has to find:

  - cheques issued late in the month that haven't been presented yet
  - receipts banked on the last day that aren't on the statement yet
  - bank charges and interest the books don't know about yet
  - three receipts the bank shows as a single deposit
  - a payment keyed into the books as 1,250.00 when it was really 1,520.00
  - a payment voucher accidentally entered twice in the books
  - payments that cleared a day or two after they were booked

All names are fictional.
"""

import csv
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .money import total

OPENING_BALANCE = Decimal("2500000.00")
PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 31)

VENDORS = [
    "Island Office Supplies Ltd", "Windward Fuel Services", "Kingstown Printing Co",
    "Caribbean Data Systems", "Grenadines Cleaning Services", "Harbour Security Ltd",
    "Arnos Vale Catering", "Leeward Building Contractors", "Eastern Telecom",
    "National Water Authority", "Valley Electrical Supplies", "Bay Street Motors",
]
RECEIPT_SOURCES = [
    "Customs and Excise collections", "Inland Revenue collections",
    "Licensing fees", "Port charges", "Court fines and fees",
]


def _business_days():
    d = PERIOD_START
    while d <= PERIOD_END:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def generate(seed=7):
    """Returns (bank_rows, ledger_rows) as lists of dicts."""
    rng = random.Random(seed)
    bank, ledger = [], []
    cheque_no = 4500
    voucher_no = 10000
    days = list(_business_days())

    def money(lo, hi):
        return Decimal(rng.randint(lo * 100, hi * 100)) / 100

    def book(d, payee, amount, ref):
        nonlocal voucher_no
        voucher_no += 1
        ledger.append({"Date": d, "Payee": payee, "Amount": amount, "Reference": ref,
                       "Voucher": f"PV{voucher_no}"})

    # ---- Ordinary activity, all of which clears normally ----
    for d in days[:-3]:
        for _ in range(rng.randint(1, 3)):                       # vendor payments by cheque
            cheque_no += 1
            amount = -money(300, 25000)
            book(d, rng.choice(VENDORS), amount, f"{cheque_no}")
            cleared = d + timedelta(days=rng.choice([0, 1, 1, 2]))
            if rng.random() < 0.25:
                # Paid by EFT: the bank statement shows no reference at all,
                # so these can only be matched on amount and date
                bank.append({"Date": cleared, "Description": "EFT PAYMENT", "Amount": amount, "Reference": ""})
            else:
                bank.append({"Date": cleared, "Description": "CHEQUE PAID", "Amount": amount,
                             "Reference": f"CHQ {cheque_no:06d}"})
        if rng.random() < 0.6:                                    # revenue receipts
            amount = money(2000, 60000)
            ref = f"RCPT{rng.randint(100000, 999999)}"
            book(d, rng.choice(RECEIPT_SOURCES), amount, ref)
            bank.append({"Date": d, "Description": "DEPOSIT", "Amount": amount, "Reference": ref})

    # Payroll by EFT on the 28th
    payroll = -Decimal("412650.00")
    book(date(2026, 10, 28), "Public service salaries - October", payroll, "EFT-PAY-2610")
    bank.append({"Date": date(2026, 10, 28), "Description": "EFT SALARIES BATCH", "Amount": payroll,
                 "Reference": "EFT-PAY-2610"})

    # ---- Three receipts banked together as one deposit ----
    grouped = [Decimal("1840.00"), Decimal("2215.50"), Decimal("960.25")]
    for amount in grouped:
        book(date(2026, 10, 22), "Licensing fees", amount, "")
    bank.append({"Date": date(2026, 10, 22), "Description": "DEPOSIT - COUNTER", "Amount": total(grouped),
                 "Reference": ""})

    # ---- A keying error: books say 1,250.00, the cheque was really 1,520.00 ----
    cheque_no += 1
    book(date(2026, 10, 15), "Valley Electrical Supplies", Decimal("-1250.00"), f"{cheque_no}")
    bank.append({"Date": date(2026, 10, 16), "Description": "CHEQUE PAID", "Amount": Decimal("-1520.00"),
                 "Reference": f"CHQ {cheque_no:06d}"})

    # ---- The same voucher entered twice ----
    cheque_no += 1
    book(date(2026, 10, 9), "Eastern Telecom", Decimal("-3480.00"), f"{cheque_no}")
    ledger.append(dict(ledger[-1]))     # identical copy, same voucher number
    bank.append({"Date": date(2026, 10, 12), "Description": "CHEQUE PAID", "Amount": Decimal("-3480.00"),
                 "Reference": f"CHQ {cheque_no:06d}"})

    # ---- Timing differences at month end ----
    for d in days[-3:]:                                           # cheques not yet presented
        cheque_no += 1
        book(d, rng.choice(VENDORS), -money(500, 9000), f"{cheque_no}")
    book(PERIOD_END, "Customs and Excise collections", Decimal("18420.75"), "RCPT990001")  # deposit in transit

    # ---- Bank-only items ----
    bank.append({"Date": PERIOD_END, "Description": "SERVICE CHARGES", "Amount": Decimal("-85.00"),
                 "Reference": ""})
    bank.append({"Date": PERIOD_END, "Description": "INTEREST CREDIT", "Amount": Decimal("312.40"),
                 "Reference": ""})

    bank.sort(key=lambda r: r["Date"])
    ledger.sort(key=lambda r: r["Date"])
    return bank, ledger


def closing_balances(bank_rows, ledger_rows):
    """Both accounts start from the same opening balance."""
    return (OPENING_BALANCE + total(r["Amount"] for r in bank_rows),
            OPENING_BALANCE + total(r["Amount"] for r in ledger_rows))


def write(folder, seed=7):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    bank, ledger = generate(seed)
    _write_csv(folder / "bank_statement_oct2026.csv", ["Date", "Description", "Reference", "Amount"], bank)
    _write_csv(folder / "cash_book_oct2026.csv", ["Date", "Voucher", "Payee", "Reference", "Amount"], ledger)
    return closing_balances(bank, ledger)


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "Date": r["Date"].strftime("%d/%m/%Y"), "Amount": f"{r['Amount']:.2f}"})
