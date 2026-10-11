"""Command line.

  python -m ledgermatch sample                    write a demo month to ./sample
  python -m ledgermatch reconcile BANK LEDGER --bank-balance N --book-balance N [--period-end YYYY-MM-DD]
  python -m ledgermatch init-demo [--db ledgermatch.db]   database with demo users, an account and sample files
  python -m ledgermatch serve [--db ledgermatch.db]       start the web app on http://127.0.0.1:5000
"""

import argparse
import sys
from pathlib import Path

from . import sample_data
from .loaders import load_bank, load_ledger, parse_date
from .matcher import Matcher
from .money import fmt, parse_amount
from .reconcile import build_statement
from .report import render


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ledgermatch")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sample", help="write a demo month of bank and ledger data")
    p.add_argument("--folder", default="sample")

    r = sub.add_parser("reconcile", help="reconcile a bank statement against the books")
    r.add_argument("bank")
    r.add_argument("ledger")
    r.add_argument("--bank-balance", required=True, help="closing balance on the bank statement")
    r.add_argument("--book-balance", required=True, help="closing balance in the cash book")
    r.add_argument("--period-end", help="YYYY-MM-DD (default: last date in the files)")
    r.add_argument("--window", type=int, default=3, help="days allowed between booking and clearing")

    i = sub.add_parser("init-demo", help="create a database with demo users and an account")
    i.add_argument("--db", default="ledgermatch.db")
    i.add_argument("--folder", default="sample")
    i.add_argument("--reset", action="store_true", help="delete an existing database first")

    w = sub.add_parser("serve", help="start the web app (on this computer only)")
    w.add_argument("--db", default="ledgermatch.db")
    w.add_argument("--port", type=int, default=5000)

    args = parser.parse_args(argv)

    if args.command == "init-demo":
        return init_demo(Path(args.db), args.folder, args.reset)

    if args.command == "serve":
        if not Path(args.db).exists():
            print(f"No database at {args.db}. Run:  python -m ledgermatch init-demo", file=sys.stderr)
            return 1
        from .web import create_app
        print(f"LedgerMatch running at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
        create_app(args.db).run(host="127.0.0.1", port=args.port, debug=False)
        return 0

    if args.command == "sample":
        bank_bal, book_bal = sample_data.write(args.folder)
        print(f"Wrote {args.folder}/bank_statement_oct2026.csv and {args.folder}/cash_book_oct2026.csv")
        print(f"Closing balance per bank:  {fmt(bank_bal)}")
        print(f"Closing balance per books: {fmt(book_bal)}")
        print("\nNow run:")
        print(f"  python -m ledgermatch reconcile {args.folder}/bank_statement_oct2026.csv "
              f"{args.folder}/cash_book_oct2026.csv --bank-balance {bank_bal} --book-balance {book_bal}")
        return 0

    bank = load_bank(args.bank)
    ledger = load_ledger(args.ledger)
    for name, loaded in (("bank statement", bank), ("ledger", ledger)):
        for row, message in loaded.errors:
            print(f"WARNING {name} row {row}: {message}", file=sys.stderr)
    if not bank.items or not ledger.items:
        print("Nothing to reconcile: a file had no valid rows.", file=sys.stderr)
        return 1

    try:
        bank_balance = parse_amount(args.bank_balance)
        book_balance = parse_amount(args.book_balance)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    period_end = (parse_date(args.period_end) if args.period_end
                  else max(x.date for x in bank.items + ledger.items))

    result = Matcher(date_window_days=args.window).run(bank.items, ledger.items)
    statement = build_statement(result, period_end, bank_balance, book_balance)
    print(render(statement, result))
    return 0 if statement.balances else 2


DEMO_PASSWORD = "Reconcile-2026"
DEMO_USERS = [("kbaptiste", "Kerry Baptiste", "preparer"),
              ("tlewis", "Tara Lewis", "preparer"),
              ("mjames", "Marcus James", "approver")]


def init_demo(db_path, folder, reset):
    from .db import init_db
    from .service import ReconciliationService

    if db_path.exists():
        if not reset:
            print(f"{db_path} already exists. Use --reset to start again (this deletes it).", file=sys.stderr)
            return 1
        db_path.unlink()
    svc = ReconciliationService(init_db(db_path))
    for username, name, role in DEMO_USERS:
        svc.create_user(username, name, role, DEMO_PASSWORD)
    svc.create_account("Consolidated Fund - Main Account", "Eastern Caribbean Bank", "4821")
    svc.create_account("Payroll Account", "Eastern Caribbean Bank", "7730")
    svc.db.close()
    bank_bal, book_bal = sample_data.write(folder)

    print(f"Created {db_path}")
    print(f"\nDemo logins (password for all three: {DEMO_PASSWORD})")
    for username, name, role in DEMO_USERS:
        print(f"  {username:<10} {name:<15} {role}")
    print(f"\nSample files in {folder}/ for the New reconciliation page:")
    print(f"  Bank statement: bank_statement_oct2026.csv   closing balance {bank_bal}")
    print(f"  Cash book:      cash_book_oct2026.csv        closing balance {book_bal}")
    print("  Period end:     2026-10-31")
    print("\nStart the app with:  python -m ledgermatch serve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
