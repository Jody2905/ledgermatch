"""The bank reconciliation statement.

The bank balance and the book balance rarely agree on any given day. The
statement explains every cent of the gap:

    Balance per bank statement
      + deposits in transit       (in the books, not yet on the statement)
      - outstanding payments      (cheques issued, not yet presented)
      +/- items under investigation, books side
    = Adjusted bank balance

    Balance per books
      + bank credits not yet recorded   (e.g. interest)
      - bank debits not yet recorded    (e.g. bank charges)
      +/- items under investigation, bank side
    = Adjusted book balance

If the two adjusted balances are equal, every difference is accounted for.
If not, either a balance was entered wrongly or transactions are missing
from the files, and the reconciliation can't be signed off.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .matcher import MatchResult
from .money import ZERO, total


@dataclass
class Statement:
    period_end: date
    bank_balance: Decimal
    book_balance: Decimal
    deposits_in_transit: list = field(default_factory=list)
    outstanding_payments: list = field(default_factory=list)
    bank_credits_not_in_books: list = field(default_factory=list)
    bank_debits_not_in_books: list = field(default_factory=list)
    investigate_ledger: list = field(default_factory=list)    # ledger items tied to an issue
    investigate_bank: list = field(default_factory=list)      # bank items tied to an issue
    issues: list = field(default_factory=list)
    # Errors that have been identified and explained: (description, amount) pairs
    bank_corrections: list = field(default_factory=list)      # mistakes the bank must correct
    book_corrections: list = field(default_factory=list)      # journals to post in the books

    @property
    def adjusted_bank(self):
        return (self.bank_balance + total(e.amount for e in self.deposits_in_transit)
                + total(e.amount for e in self.outstanding_payments)
                + total(e.amount for e in self.investigate_ledger)
                + total(amount for _, amount in self.bank_corrections))

    @property
    def adjusted_book(self):
        return (self.book_balance + total(b.amount for b in self.bank_credits_not_in_books)
                + total(b.amount for b in self.bank_debits_not_in_books)
                + total(b.amount for b in self.investigate_bank)
                + total(amount for _, amount in self.book_corrections))

    @property
    def difference(self):
        return self.adjusted_bank - self.adjusted_book

    @property
    def balances(self):
        """The adjusted balances agree."""
        return self.difference == ZERO

    @property
    def ready_to_sign_off(self):
        """Balances agree AND there's nothing left to investigate."""
        return self.balances and not self.issues


def build_statement(result: MatchResult, period_end, bank_balance, book_balance):
    """Sorts every unmatched item into the right part of the statement."""
    flagged_bank = {b.key for i in result.issues for b in i.bank}
    flagged_ledger = {e.key for i in result.issues for e in i.ledger}
    s = Statement(period_end, bank_balance, book_balance, issues=list(result.issues))

    for e in sorted(result.unmatched_ledger, key=lambda x: (x.date, x.line_no)):
        if e.key in flagged_ledger:
            s.investigate_ledger.append(e)
        elif e.amount > 0:
            s.deposits_in_transit.append(e)
        else:
            s.outstanding_payments.append(e)

    for b in sorted(result.unmatched_bank, key=lambda x: (x.date, x.line_no)):
        if b.key in flagged_bank:
            s.investigate_bank.append(b)
        elif b.amount > 0:
            s.bank_credits_not_in_books.append(b)
        else:
            s.bank_debits_not_in_books.append(b)
    return s
