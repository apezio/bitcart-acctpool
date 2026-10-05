"""Status changes of an address when its invoice closes (SPEC-v4 4.4.5). Used by the invoice hooks and by the
watch loop (for a hook that was lost: Bitcart's batch mark_invalid fires none). A second run changes nothing."""

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from api.invoices import InvoiceStatus

from . import models
from .constants import WATCH_DAYS, Status

CLOSING = (InvoiceStatus.COMPLETE, InvoiceStatus.EXPIRED, InvoiceStatus.INVALID, InvoiceStatus.REFUNDED)


def retire(address: models.AcctpoolAddress) -> None:
    address.status = Status.RETIRED
    address.retired_at = func.now()
    address.watch_until = func.now() + timedelta(days=WATCH_DAYS)
    address.withdraw_requested = False


async def close_invoice(session: Any, invoice_id: str) -> None:
    """In the caller's transaction. Money on the address (any chain or asset) = wait for the payout; none = retired,
    still watched for 30 days."""
    table = models.AcctpoolAddress
    address = (await session.execute(select(table).where(table.invoice_id == invoice_id).with_for_update())).scalar()
    if address is None or address.status != Status.IN_INVOICE:
        return
    deposits = models.AcctpoolDeposit
    paid = (await session.execute(select(deposits.id).where(deposits.address_id == address.id).limit(1))).first()
    if paid:
        address.status = Status.PENDING_PAYOUT
    else:
        retire(address)
