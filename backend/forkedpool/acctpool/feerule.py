"""Pay out now, or wait (SPEC-v4 4.6.2). A pure function: no I/O, no clock, Decimal only.

Polygon (and the anvil test chain) pay at once. Elsewhere: pay when the fee is at most 5% of the amount, or when
the amount is $50 or more and the fee at most $3. No price = wait. An admin withdraw skips this rule.
"""

from decimal import Decimal

MAX_FEE_SHARE = Decimal("0.05")
USD_RULE_MIN_AMOUNT = Decimal(50)
USD_RULE_MAX_FEE = Decimal(3)


def pay_now(pay_at_once: bool, amount_usd: Decimal | None, fee_usd: Decimal | None) -> bool:
    if pay_at_once:
        return True
    if amount_usd is None or fee_usd is None or not amount_usd.is_finite() or not fee_usd.is_finite():
        return False
    if amount_usd <= 0:
        return False
    return fee_usd <= amount_usd * MAX_FEE_SHARE or (amount_usd >= USD_RULE_MIN_AMOUNT and fee_usd <= USD_RULE_MAX_FEE)
