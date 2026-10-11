"""Money handling.

Money is NEVER stored in a float. Floats can't represent most decimal amounts
exactly (0.1 + 0.2 == 0.30000000000000004), so totals drift and a
reconciliation can be "off by a cent" for no real reason. Every amount here
is a Decimal with exactly two decimal places.
"""

from decimal import Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def parse_amount(text):
    """Turn text from a bank or ledger export into an exact amount.

    Accepts:  1234.50   1,234.50   -200   (200.00)   $1,000   EC$1,000
    Refuses anything with more than 2 decimal places rather than silently
    rounding it, because a rounded amount would no longer match the source.
    """
    if text is None:
        raise ValueError("amount is missing")
    s = str(text).strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):      # accounting style for negatives
        negative = True
        s = s[1:-1].strip()
    for symbol in ("EC$", "XCD", "$", ","):
        s = s.replace(symbol, "")
    s = s.strip()
    if not s:
        raise ValueError("amount is empty")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a valid amount: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"not a valid amount: {text!r}")
    if value != value.quantize(CENT):
        raise ValueError(f"amount has more than 2 decimal places: {text!r}")
    value = value.quantize(CENT)
    return -value if negative else value


def fmt(amount):
    """Accounting format: 1,234.50 and (1,234.50) for negatives."""
    if amount < 0:
        return f"({-amount:,.2f})"
    return f"{amount:,.2f}"


def total(amounts):
    return sum(amounts, ZERO)
