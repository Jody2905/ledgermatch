"""Matches bank statement lines to ledger entries.

Runs in passes, from most certain to least certain, the same order an
accountant works through a reconciliation by hand:

  1. Reference + amount   cheque/EFT number and amount both agree
  2. Amount + date        same amount within a few days (the bank often
                          clears a payment a day or two after it's booked)
  3. Grouped deposits     one bank deposit that equals the sum of several
                          receipts banked together

A match is only made when there is exactly ONE sensible candidate. When
there's a choice, it is left for a person to decide rather than guessed:
a wrong automatic match hides a real problem, which is worse than no match.

Then it looks for problems that need a person's attention:
  - same reference but different amount (a keying error)
  - possible duplicates
  - ambiguous matches (several equally good candidates)
"""

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from .money import fmt, total


@dataclass
class Match:
    bank: list
    ledger: list
    rule: str          # "reference", "amount_date" or "grouped"
    note: str = ""


@dataclass
class Issue:
    kind: str          # "amount_mismatch", "possible_duplicate", "ambiguous"
    bank: list
    ledger: list
    detail: str


@dataclass
class MatchResult:
    matches: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    unmatched_bank: list = field(default_factory=list)
    unmatched_ledger: list = field(default_factory=list)

    def issues_of(self, kind):
        return [i for i in self.issues if i.kind == kind]


class Matcher:

    def __init__(self, date_window_days=3, max_group_size=3, max_group_candidates=15):
        self.window = date_window_days
        self.max_group = max_group_size
        self.max_candidates = max_group_candidates   # keeps the combination search small

    def run(self, bank_lines, ledger_entries):
        bank = sorted(bank_lines, key=lambda b: (b.date, b.line_no))
        ledger = sorted(ledger_entries, key=lambda e: (e.date, e.line_no))
        open_bank = {b.key: b for b in bank}
        open_ledger = {e.key: e for e in ledger}
        result = MatchResult()

        self._flag_duplicates(bank, ledger, result)
        self._pass_reference(open_bank, open_ledger, result)
        self._pass_amount_date(open_bank, open_ledger, result)
        self._pass_grouped(open_bank, open_ledger, result)
        self._flag_amount_mismatches(open_bank, open_ledger, result)

        result.unmatched_bank = list(open_bank.values())
        result.unmatched_ledger = list(open_ledger.values())
        result.issues = [i for i in result.issues if self._still_open(i, open_bank, open_ledger)]
        return result

    @staticmethod
    def _still_open(issue, open_bank, open_ledger):
        """Drop warnings that later passes resolved.

        - ambiguous: the bank line found a match after all
        - possible_duplicate: every copy matched something, so they were
          genuine repeated transactions (e.g. two identical bank fees)
        """
        if issue.kind == "ambiguous":
            return issue.bank[0].key in open_bank
        if issue.kind == "possible_duplicate":
            return any(x.key in open_bank or x.key in open_ledger for x in issue.bank + issue.ledger)
        return True

    # ------------------------------------------------------------------
    # Matching passes
    # ------------------------------------------------------------------

    def _pass_reference(self, open_bank, open_ledger, result):
        for b in list(open_bank.values()):
            if not b.ref:
                continue
            candidates = [e for e in open_ledger.values() if e.ref == b.ref and e.amount == b.amount]
            chosen = self._single_best(b, candidates)
            if chosen:
                self._record(result, open_bank, open_ledger, [b], [chosen], "reference")

    def _pass_amount_date(self, open_bank, open_ledger, result):
        for b in list(open_bank.values()):
            candidates = [e for e in open_ledger.values()
                          if e.amount == b.amount and abs((b.date - e.date).days) <= self.window]
            if not candidates:
                continue
            chosen = self._single_best(b, candidates)
            if chosen:
                note = "" if chosen.date == b.date else f"cleared {(b.date - chosen.date).days} day(s) after booking"
                self._record(result, open_bank, open_ledger, [b], [chosen], "amount_date", note)
            else:
                result.issues.append(Issue(
                    "ambiguous", [b], candidates,
                    f"{len(candidates)} ledger entries of {fmt(b.amount)} on equally close dates; "
                    f"choose the right one by hand"))

    def _pass_grouped(self, open_bank, open_ledger, result):
        """One bank line that equals several ledger entries added together."""
        for b in list(open_bank.values()):
            candidates = [e for e in open_ledger.values()
                          if (e.amount > 0) == (b.amount > 0)
                          and abs(e.amount) < abs(b.amount)
                          and 0 <= (b.date - e.date).days <= self.window]
            if len(candidates) < 2 or len(candidates) > self.max_candidates:
                continue
            found = [group
                     for size in range(2, self.max_group + 1)
                     for group in combinations(candidates, size)
                     if total(e.amount for e in group) == b.amount]
            if len(found) == 1:
                group = list(found[0])
                self._record(result, open_bank, open_ledger, [b], group, "grouped",
                             f"{len(group)} ledger entries banked together")

    # ------------------------------------------------------------------
    # Problems for a person to look at
    # ------------------------------------------------------------------

    def _flag_amount_mismatches(self, open_bank, open_ledger, result):
        for b in open_bank.values():
            if not b.ref:
                continue
            for e in open_ledger.values():
                if e.ref == b.ref and e.amount != b.amount and (e.amount > 0) == (b.amount > 0):
                    diff = b.amount - e.amount
                    detail = (f"reference {b.reference or e.reference}: bank shows {fmt(b.amount)}, "
                              f"books show {fmt(e.amount)} (difference {fmt(diff)})")
                    # Swapping two digits (1520 -> 1250) always gives a difference divisible by 9
                    if (abs(diff) * 100) % 9 == 0:
                        detail += "; difference is divisible by 9, a sign of transposed digits"
                    result.issues.append(Issue("amount_mismatch", [b], [e], detail))

    def _flag_duplicates(self, bank, ledger, result):
        groups = defaultdict(list)
        for b in bank:
            groups[("bank", b.date, b.amount, b.ref, b.description.lower())].append(b)
        for e in ledger:
            # The same voucher, or the same reference and amount, entered twice
            key = ("ledger", e.voucher) if e.voucher else ("ledger", e.ref, e.amount, e.payee.lower())
            groups[key].append(e)
        for key, items in groups.items():
            if len(items) > 1:
                side = key[0]
                first = items[0]
                if side == "bank":
                    what = f"bank statement: {first.description} {fmt(first.amount)} on {first.date:%d %b}"
                else:
                    voucher = f" (voucher {first.voucher})" if first.voucher else ""
                    what = f"books: {first.payee}{voucher} {fmt(first.amount)}"
                result.issues.append(Issue(
                    "possible_duplicate",
                    items if side == "bank" else [],
                    items if side == "ledger" else [],
                    f"entered {len(items)} times in the {what}"))

    # ------------------------------------------------------------------

    @staticmethod
    def _single_best(b, candidates):
        """The only candidate, or the unique closest by date; None if it's a tie."""
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            return None
        ranked = sorted(candidates, key=lambda e: (abs((b.date - e.date).days), e.line_no))
        best_gap = abs((b.date - ranked[0].date).days)
        tied = [e for e in ranked if abs((b.date - e.date).days) == best_gap]
        if len(tied) == 1:
            return ranked[0]
        # Exact copies of each other (a double-keyed entry): match one, and the
        # leftover copy stays unmatched and is reported as a possible duplicate
        if len({Matcher._identity(e) for e in tied}) == 1:
            return tied[0]
        return None

    @staticmethod
    def _identity(item):
        return (item.date, item.amount, item.ref, getattr(item, "payee", getattr(item, "description", "")),
                getattr(item, "voucher", ""))

    @staticmethod
    def _record(result, open_bank, open_ledger, bank_items, ledger_items, rule, note=""):
        for b in bank_items:
            del open_bank[b.key]
        for e in ledger_items:
            del open_ledger[e.key]
        result.matches.append(Match(bank_items, ledger_items, rule, note))
