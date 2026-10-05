"""Address allocation (SPEC-v4 4.4.2): ONE ready address per invoice; every pool method of the invoice (all
chains, both assets) gets the same one. A ready address goes to one invoice only, and never returns to ready.
A ready address that the chain shows as used (a nonce or a balance: the database was restored to an older state)
is not given out: it is retired (and so watched: money on it is found as a late payment) with an event."""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select

from . import models
from .constants import Event, Status
from .db import Database, lock_key
from .lifecycle import retire


async def allocate(
    db: Database, invoice_id: str, store: str, used: Callable[[str], Awaitable[bool]]
) -> tuple[models.AcctpoolAddress | None, str | None]:
    """(address, None) or (None, reason). The transaction is committed before the caller gives the address out."""
    refused: list[str] = []
    result = await _allocate(db, invoice_id, store, used, refused)
    for address in refused:  # committed: retired
        await db.event(Event.ADDRESS_IN_USE, None, address, {"invoice": invoice_id})
    return result


async def _allocate(db: Database, invoice_id: str, store: str, used: Any, refused: list[str]) -> Any:
    table = models.AcctpoolAddress
    async with db.session() as session, session.begin():
        # Bitcart makes the methods of one invoice one after the other or at the same time: the lock makes the
        # second one wait until the first one has committed, so it finds the address of the first one.
        await session.execute(select(func.pg_advisory_xact_lock(lock_key("invoice", invoice_id))))
        address = (await session.execute(select(table).where(table.invoice_id == invoice_id))).scalar_one_or_none()
        if address is not None:
            if address.store != store:
                return None, "other_store"
            if address.status != Status.IN_INVOICE:
                return None, "address_closed"
            return address, None
        while True:
            address = (
                await session.execute(
                    select(table)
                    .where(table.store == store)
                    .where(table.status == Status.READY)
                    .order_by(table.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if address is None:
                return None, "no_ready_address"
            if not await used(address.address):
                break
            retire(address)
            refused.append(address.address)
            await session.flush()
        address.status, address.invoice_id, address.assigned_at = Status.IN_INVOICE, invoice_id, func.now()
    return address, None
