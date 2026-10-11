"""Prints a reconciliation as a plain-text report."""

from .money import fmt, total


def _line(label, amount, width=62):
    return f"  {label:<{width - 16}}{fmt(amount):>16}"


def _items(title, items, describe):
    out = []
    if items:
        out.append(f"    {title}:")
        for x in items:
            out.append(f"      {x.date:%d %b}  {describe(x)[:40]:<40}{fmt(x.amount):>14}")
    return out


def render(statement, result):
    s = statement
    out = [
        "=" * 66,
        f"  BANK RECONCILIATION STATEMENT as at {s.period_end:%d %B %Y}",
        "=" * 66,
        "",
        _line("Balance per bank statement", s.bank_balance),
        _line("Add: deposits in transit", total(e.amount for e in s.deposits_in_transit)),
        *_items("deposits in transit", s.deposits_in_transit, lambda e: e.payee),
        _line("Less: outstanding payments", total(e.amount for e in s.outstanding_payments)),
        *_items("outstanding payments", s.outstanding_payments, lambda e: f"#{e.reference} {e.payee}"),
    ]
    if s.investigate_ledger:
        out.append(_line("Items under investigation (books)", total(e.amount for e in s.investigate_ledger)))
    out += [
        "  " + "-" * 60,
        _line("ADJUSTED BANK BALANCE", s.adjusted_bank),
        "",
        _line("Balance per books", s.book_balance),
        _line("Add: bank credits not yet recorded", total(b.amount for b in s.bank_credits_not_in_books)),
        *_items("credits", s.bank_credits_not_in_books, lambda b: b.description),
        _line("Less: bank debits not yet recorded", total(b.amount for b in s.bank_debits_not_in_books)),
        *_items("debits", s.bank_debits_not_in_books, lambda b: b.description),
    ]
    if s.investigate_bank:
        out.append(_line("Items under investigation (bank)", total(b.amount for b in s.investigate_bank)))
    out += [
        "  " + "-" * 60,
        _line("ADJUSTED BOOK BALANCE", s.adjusted_book),
        "",
        _line("DIFFERENCE", s.difference),
        "",
        f"  Matched: {sum(len(m.bank) for m in result.matches)} bank lines to "
        f"{sum(len(m.ledger) for m in result.matches)} ledger entries "
        f"({', '.join(f'{r}: {n}' for r, n in _rule_counts(result).items())})",
    ]
    if s.issues:
        out += ["", f"  NEEDS INVESTIGATION ({len(s.issues)}):"]
        for i in s.issues:
            out.append(f"    [{i.kind}] {i.detail}")
    out += ["", "  Status: " + (
        "READY FOR SIGN-OFF" if s.ready_to_sign_off
        else "BALANCES, but items need investigating before sign-off" if s.balances
        else "DOES NOT BALANCE - check the balances entered and that both files are complete")]
    return "\n".join(out)


def _rule_counts(result):
    counts = {}
    for m in result.matches:
        counts[m.rule] = counts.get(m.rule, 0) + 1
    return counts
