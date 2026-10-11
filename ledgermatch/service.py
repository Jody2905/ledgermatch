"""Saved reconciliations, review and approval.

Workflow:
    draft ──submit──▶ submitted ──approve──▶ approved (locked)
      ▲                  │
      └─────reject───────┘ (back to the preparer, with a reason)

Rules enforced here:
  - Only the person who prepared a reconciliation can submit it.
  - Nobody can approve or reject their own work (segregation of duties),
    and only users with the approver role can approve or reject.
  - It can only be submitted when it balances AND no issue is left open.
  - Every manual change needs a written reason, and goes in the audit log.
  - Once approved, nothing can change.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from . import passwords
from .loaders import load_bank, load_ledger
from .matcher import Matcher
from .models import BankLine, LedgerEntry
from .money import CENT, fmt
from .reconcile import Statement

EDITABLE = ("draft", "rejected")
MIN_REASON = 5


class NotAllowed(Exception):
    """The action breaks a rule (wrong person, wrong status, missing reason...)."""


class NotFound(Exception):
    pass


class ImportProblem(Exception):
    """The files couldn't be loaded completely; holds every row-level error."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"{len(errors)} problem(s) in the uploaded files")


def to_cents(amount):
    return int((Decimal(amount).quantize(CENT) * 100).to_integral_value())


def from_cents(cents):
    return (Decimal(cents) / 100).quantize(CENT)


@dataclass
class OpenIssue:
    id: int
    kind: str
    detail: str


class ReconciliationService:

    def __init__(self, conn, clock=None, matcher=None):
        self.db = conn
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.matcher = matcher or Matcher()

    # ==================================================================
    # Users and accounts
    # ==================================================================

    def create_user(self, username, display_name, role, password):
        username = (username or "").strip().lower()
        if not username or not display_name or not display_name.strip():
            raise ValueError("Username and name are required")
        if role not in ("preparer", "approver"):
            raise ValueError("Role must be preparer or approver")
        with self.db:
            cur = self.db.execute(
                "INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
                (username, display_name.strip(), role, passwords.hash_password(password)))
        return cur.lastrowid

    def authenticate(self, username, password):
        row = self.db.execute("SELECT * FROM users WHERE username = ? AND active = 1",
                              ((username or "").strip().lower(),)).fetchone()
        if row and passwords.verify_password(password or "", row["password_hash"]):
            return row
        return None

    def create_account(self, name, bank_name, last_digits):
        digits = (last_digits or "").strip()
        if not (digits.isdigit() and len(digits) == 4):
            raise ValueError("Store only the last 4 digits of the account number")
        with self.db:
            cur = self.db.execute("INSERT INTO accounts (name, bank_name, last_digits) VALUES (?, ?, ?)",
                                  (name.strip(), bank_name.strip(), digits))
        return cur.lastrowid

    def accounts(self):
        return self.db.execute("SELECT * FROM accounts ORDER BY name").fetchall()

    # ==================================================================
    # Creating a reconciliation run
    # ==================================================================

    def create_run(self, account_id, period_end, bank_source, ledger_source,
                   bank_balance, book_balance, preparer_id):
        preparer = self._user(preparer_id)
        if not self.db.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            raise NotFound("Unknown account")
        if self.db.execute("SELECT 1 FROM runs WHERE account_id = ? AND period_end = ?",
                           (account_id, period_end.isoformat())).fetchone():
            raise NotAllowed("A reconciliation for this account and period already exists")

        bank = load_bank(bank_source)
        ledger = load_ledger(ledger_source)
        # A reconciliation built on a partial file is worthless, so any bad row stops the import
        errors = [f"Bank statement row {r}: {m}" for r, m in bank.errors] + \
                 [f"Ledger row {r}: {m}" for r, m in ledger.errors]
        if not bank.items:
            errors.append("Bank statement has no transactions")
        if not ledger.items:
            errors.append("Ledger has no transactions")
        if errors:
            raise ImportProblem(errors)

        result = self.matcher.run(bank.items, ledger.items)
        now = self._now()
        with self.db:
            run_id = self.db.execute(
                """INSERT INTO runs (account_id, period_end, bank_balance, book_balance, prepared_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account_id, period_end.isoformat(), to_cents(bank_balance), to_cents(book_balance),
                 preparer["id"], now)).lastrowid
            self.db.executemany(
                "INSERT INTO bank_lines (run_id, line_no, date, description, reference, amount) VALUES (?,?,?,?,?,?)",
                [(run_id, b.line_no, b.date.isoformat(), b.description, b.reference, to_cents(b.amount))
                 for b in bank.items])
            self.db.executemany(
                """INSERT INTO ledger_entries (run_id, line_no, date, payee, reference, voucher, amount)
                   VALUES (?,?,?,?,?,?,?)""",
                [(run_id, e.line_no, e.date.isoformat(), e.payee, e.reference, e.voucher, to_cents(e.amount))
                 for e in ledger.items])
            for m in result.matches:
                self._insert_match(run_id, m.rule, m.note, 0, None,
                                   [b.line_no for b in m.bank], [e.line_no for e in m.ledger])
            for issue in result.issues:
                issue_id = self.db.execute("INSERT INTO issues (run_id, kind, detail) VALUES (?, ?, ?)",
                                           (run_id, issue.kind, issue.detail)).lastrowid
                self.db.executemany("INSERT INTO issue_items (issue_id, side, line_no) VALUES (?, ?, ?)",
                                    [(issue_id, "bank", b.line_no) for b in issue.bank]
                                    + [(issue_id, "ledger", e.line_no) for e in issue.ledger])
            self._audit(run_id, preparer["id"], "created",
                        f"{len(bank.items)} bank lines, {len(ledger.items)} ledger entries; "
                        f"{len(result.matches)} automatic matches, {len(result.issues)} issues", now)
        return run_id

    # ==================================================================
    # Resolving items (only while the run is a draft or was rejected)
    # ==================================================================

    def manual_match(self, run_id, user_id, bank_lines, ledger_lines, reason):
        run, user = self._editable(run_id, user_id)
        reason = self._reason(reason)
        if not bank_lines or not ledger_lines:
            raise NotAllowed("Choose at least one bank line and one ledger entry")
        bank = [self._line(run_id, "bank", n) for n in bank_lines]
        ledger = [self._line(run_id, "ledger", n) for n in ledger_lines]
        bank_total = sum(r["amount"] for r in bank)
        ledger_total = sum(r["amount"] for r in ledger)
        if (bank_total > 0) != (ledger_total > 0):
            raise NotAllowed("Money in can't be matched against money out")
        difference = bank_total - ledger_total
        with self.db:
            match_id = self._insert_match(run_id, "manual", reason, difference, user["id"],
                                          bank_lines, ledger_lines)
            detail = f"bank {list(bank_lines)} with ledger {list(ledger_lines)}: {reason}"
            if difference:
                detail += f" (difference {fmt(from_cents(difference))} to correct in the books)"
            self._audit(run_id, user["id"], "manual_match", detail)
        return match_id

    def unmatch(self, run_id, user_id, match_id, reason):
        run, user = self._editable(run_id, user_id)
        reason = self._reason(reason)
        match = self.db.execute("SELECT * FROM matches WHERE id = ? AND run_id = ?", (match_id, run_id)).fetchone()
        if not match:
            raise NotFound("Unknown match")
        with self.db:
            self.db.execute("DELETE FROM match_items WHERE match_id = ?", (match_id,))
            self.db.execute("DELETE FROM matches WHERE id = ?", (match_id,))
            self._audit(run_id, user["id"], "unmatch", f"match {match_id} ({match['rule']}): {reason}")

    def mark_error(self, run_id, user_id, side, line_no, reason):
        """E.g. a voucher keyed twice: it will be listed as a reversal to post."""
        run, user = self._editable(run_id, user_id)
        reason = self._reason(reason)
        self._line(run_id, side, line_no)   # checks it exists and isn't matched or already marked
        table = "bank_lines" if side == "bank" else "ledger_entries"
        with self.db:
            self.db.execute(f"UPDATE {table} SET error_reason = ? WHERE run_id = ? AND line_no = ?",
                            (reason, run_id, line_no))
            self._audit(run_id, user["id"], "mark_error", f"{side} line {line_no}: {reason}")

    def clear_error(self, run_id, user_id, side, line_no):
        run, user = self._editable(run_id, user_id)
        table = self._table(side)
        with self.db:
            updated = self.db.execute(
                f"UPDATE {table} SET error_reason = NULL WHERE run_id = ? AND line_no = ? AND error_reason IS NOT NULL",
                (run_id, line_no)).rowcount
            if not updated:
                raise NotFound("That line isn't marked as an error")
            self._audit(run_id, user["id"], "clear_error", f"{side} line {line_no}")

    def dismiss_issue(self, run_id, user_id, issue_id, reason):
        """For a warning that turns out to be fine after checking."""
        run, user = self._editable(run_id, user_id)
        reason = self._reason(reason)
        with self.db:
            updated = self.db.execute(
                "UPDATE issues SET dismissed_by = ?, dismiss_reason = ? WHERE id = ? AND run_id = ? AND dismissed_by IS NULL",
                (user["id"], reason, issue_id, run_id)).rowcount
            if not updated:
                raise NotFound("Unknown or already dismissed issue")
            self._audit(run_id, user["id"], "dismiss_issue", f"issue {issue_id}: {reason}")

    # ==================================================================
    # Maker-checker workflow
    # ==================================================================

    def submit(self, run_id, user_id):
        run, user = self._editable(run_id, user_id)
        if run["prepared_by"] != user["id"]:
            raise NotAllowed("Only the person who prepared this reconciliation can submit it")
        s = self.statement(run_id)
        if not s.balances:
            raise NotAllowed(f"It doesn't balance yet: difference {fmt(s.difference)}")
        if s.issues:
            raise NotAllowed(f"{len(s.issues)} issue(s) still need resolving")
        with self.db:
            self.db.execute("UPDATE runs SET status = 'submitted', submitted_at = ?, rejection_reason = NULL "
                            "WHERE id = ?", (self._now(), run_id))
            self._audit(run_id, user["id"], "submitted")

    def approve(self, run_id, user_id):
        run, user = self._reviewable(run_id, user_id)
        s = self.statement(run_id)
        if not s.ready_to_sign_off:     # belt and braces: nothing can have changed, but check anyway
            raise NotAllowed("This reconciliation no longer balances")
        now = self._now()
        with self.db:
            self.db.execute("UPDATE runs SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
                            (user["id"], now, run_id))
            self._audit(run_id, user["id"], "approved", "", now)

    def reject(self, run_id, user_id, reason):
        run, user = self._reviewable(run_id, user_id)
        reason = self._reason(reason)
        with self.db:
            self.db.execute("UPDATE runs SET status = 'rejected', rejection_reason = ? WHERE id = ?",
                            (reason, run_id))
            self._audit(run_id, user["id"], "rejected", reason)

    # ==================================================================
    # Reading
    # ==================================================================

    def record_export(self, run_id, user_id):
        """Downloads are logged too: who took a copy of the figures, and when."""
        self._user(user_id)
        self.run(run_id)
        with self.db:
            self._audit(run_id, user_id, "exported", "downloaded the Excel copy")

    def run(self, run_id):
        row = self.db.execute(
            """SELECT r.*, a.name AS account_name, a.bank_name, a.last_digits,
                      p.display_name AS preparer_name, ap.display_name AS approver_name
               FROM runs r
               JOIN accounts a ON a.id = r.account_id
               JOIN users p ON p.id = r.prepared_by
               LEFT JOIN users ap ON ap.id = r.approved_by
               WHERE r.id = ?""", (run_id,)).fetchone()
        if not row:
            raise NotFound("Unknown reconciliation")
        return row

    def runs(self):
        return self.db.execute(
            """SELECT r.*, a.name AS account_name, p.display_name AS preparer_name
               FROM runs r JOIN accounts a ON a.id = r.account_id JOIN users p ON p.id = r.prepared_by
               ORDER BY r.period_end DESC, a.name""").fetchall()

    def matches(self, run_id):
        rows = self.db.execute(
            """SELECT m.*, u.display_name AS created_by_name FROM matches m
               LEFT JOIN users u ON u.id = m.created_by WHERE m.run_id = ? ORDER BY m.id""", (run_id,)).fetchall()
        items = self.db.execute("SELECT * FROM match_items WHERE run_id = ?", (run_id,)).fetchall()
        result = []
        for m in rows:
            mine = [i for i in items if i["match_id"] == m["id"]]
            result.append({"match": m,
                           "bank": [i["line_no"] for i in mine if i["side"] == "bank"],
                           "ledger": [i["line_no"] for i in mine if i["side"] == "ledger"]})
        return result

    def audit_trail(self, run_id):
        return self.db.execute(
            """SELECT l.*, u.display_name AS user_name FROM audit_log l
               LEFT JOIN users u ON u.id = l.user_id WHERE l.run_id = ? ORDER BY l.id""", (run_id,)).fetchall()

    def statement(self, run_id):
        """Rebuilds the reconciliation statement from what's saved, including manual work."""
        run = self.run(run_id)
        bank = {r["line_no"]: r for r in self.db.execute(
            "SELECT * FROM bank_lines WHERE run_id = ? ORDER BY date, line_no", (run_id,))}
        ledger = {r["line_no"]: r for r in self.db.execute(
            "SELECT * FROM ledger_entries WHERE run_id = ? ORDER BY date, line_no", (run_id,))}
        matched = {(r["side"], r["line_no"]) for r in self.db.execute(
            "SELECT side, line_no FROM match_items WHERE run_id = ?", (run_id,))}

        def resolved(side, line_no):
            row = (bank if side == "bank" else ledger)[line_no]
            return (side, line_no) in matched or row["error_reason"] is not None

        # An issue stays open until every item in it is matched or explained, or it's dismissed
        open_issues, flagged = [], set()
        for issue in self.db.execute("SELECT * FROM issues WHERE run_id = ? AND dismissed_by IS NULL ORDER BY id",
                                     (run_id,)).fetchall():
            pending = [(i["side"], i["line_no"]) for i in self.db.execute(
                "SELECT side, line_no FROM issue_items WHERE issue_id = ?", (issue["id"],))
                if not resolved(i["side"], i["line_no"])]
            if pending:
                open_issues.append(OpenIssue(issue["id"], issue["kind"], issue["detail"]))
                flagged.update(pending)

        s = Statement(date.fromisoformat(run["period_end"]), from_cents(run["bank_balance"]),
                      from_cents(run["book_balance"]), issues=open_issues)

        for n, r in ledger.items():
            amount = from_cents(r["amount"])
            if r["error_reason"] is not None:
                label = f"Reverse: {r['payee']}" + (f" ({r['voucher']})" if r["voucher"] else "")
                s.book_corrections.append((f"{label}: {r['error_reason']}", -amount))
            elif ("ledger", n) not in matched:
                item = LedgerEntry(n, date.fromisoformat(r["date"]), r["payee"], amount, r["reference"], r["voucher"])
                if ("ledger", n) in flagged:
                    s.investigate_ledger.append(item)
                elif amount > 0:
                    s.deposits_in_transit.append(item)
                else:
                    s.outstanding_payments.append(item)

        for n, r in bank.items():
            amount = from_cents(r["amount"])
            if r["error_reason"] is not None:
                s.bank_corrections.append((f"Bank error: {r['description']}: {r['error_reason']}", -amount))
            elif ("bank", n) not in matched:
                item = BankLine(n, date.fromisoformat(r["date"]), r["description"], amount, r["reference"])
                if ("bank", n) in flagged:
                    s.investigate_bank.append(item)
                elif amount > 0:
                    s.bank_credits_not_in_books.append(item)
                else:
                    s.bank_debits_not_in_books.append(item)

        for m in self.db.execute("SELECT * FROM matches WHERE run_id = ? AND difference != 0 ORDER BY id", (run_id,)):
            s.book_corrections.append((f"Correction: {m['note']}", from_cents(m["difference"])))
        return s

    # ==================================================================
    # Helpers
    # ==================================================================

    def _insert_match(self, run_id, rule, note, difference, user_id, bank_lines, ledger_lines):
        match_id = self.db.execute(
            "INSERT INTO matches (run_id, rule, note, difference, created_by) VALUES (?, ?, ?, ?, ?)",
            (run_id, rule, note, difference, user_id)).lastrowid
        self.db.executemany("INSERT INTO match_items (match_id, run_id, side, line_no) VALUES (?, ?, ?, ?)",
                            [(match_id, run_id, "bank", n) for n in bank_lines]
                            + [(match_id, run_id, "ledger", n) for n in ledger_lines])
        return match_id

    def _line(self, run_id, side, line_no):
        """A line that exists and is still available: not matched, not marked as an error."""
        row = self.db.execute(f"SELECT * FROM {self._table(side)} WHERE run_id = ? AND line_no = ?",
                              (run_id, line_no)).fetchone()
        if not row:
            raise NotFound(f"No {side} line {line_no}")
        if self.db.execute("SELECT 1 FROM match_items WHERE run_id = ? AND side = ? AND line_no = ?",
                           (run_id, side, line_no)).fetchone():
            raise NotAllowed(f"{side.capitalize()} line {line_no} is already matched; unmatch it first")
        if row["error_reason"] is not None:
            raise NotAllowed(f"{side.capitalize()} line {line_no} is marked as an error")
        return row

    @staticmethod
    def _table(side):
        if side not in ("bank", "ledger"):
            raise ValueError("side must be 'bank' or 'ledger'")
        return "bank_lines" if side == "bank" else "ledger_entries"

    def _user(self, user_id):
        row = self.db.execute("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
        if not row:
            raise NotAllowed("Unknown or inactive user")
        return row

    def _editable(self, run_id, user_id):
        user = self._user(user_id)
        run = self.run(run_id)
        if run["status"] not in EDITABLE:
            raise NotAllowed(f"This reconciliation is {run['status']} and can't be changed")
        if run["prepared_by"] != user["id"]:
            raise NotAllowed("Only the person who prepared this reconciliation can change it")
        return run, user

    def _reviewable(self, run_id, user_id):
        user = self._user(user_id)
        run = self.run(run_id)
        if run["status"] != "submitted":
            raise NotAllowed("Only submitted reconciliations can be approved or rejected")
        if user["role"] != "approver":
            raise NotAllowed("Only approvers can approve or reject reconciliations")
        if run["prepared_by"] == user["id"]:
            raise NotAllowed("You prepared this reconciliation, so someone else must review it")
        return run, user

    @staticmethod
    def _reason(reason):
        text = (reason or "").strip()
        if len(text) < MIN_REASON:
            raise NotAllowed("Please give a reason (at least a few words)")
        if len(text) > 500:
            raise NotAllowed("Please keep the reason under 500 characters")
        return text

    def _audit(self, run_id, user_id, action, detail="", at=None):
        self.db.execute("INSERT INTO audit_log (run_id, user_id, action, detail, at) VALUES (?, ?, ?, ?, ?)",
                        (run_id, user_id, action, detail, at or self._now()))

    def _now(self):
        return self.clock().isoformat(timespec="seconds")
