"""Derive-ahead and the tamper check (SPEC-v4 4.4.1). Worker only: the signer is not reachable from the backend.

fill: keep `ready_target` ready addresses per signer store, so the backend never needs the signer for an invoice.
check: every address that was just given to an invoice is derived again. An address that the signer does not
give for its index was changed in the database: a customer who pays to it pays to someone else. The invoice is
set to invalid (if it is still pending, nobody has paid yet) and an alert event is written.
"""

from dishka import Scope
from sqlalchemy import func, select

from api.invoices import InvoiceStatus
from api.logging import get_exception_message, get_logger
from api.services.crud.invoices import InvoiceService

from . import models
from .constants import Event, Status
from .signer_client import MAX_DERIVE, SignerClient

logger = get_logger(__name__)


async def fill(plugin: object, signer: SignerClient) -> int:
    """Derive-ahead from after the highest index of the database AND of the signer: after a restore of an older
    database, the signer still knows the indexes that it gave out since."""
    db = await plugin.db()  # type: ignore[attr-defined]
    target = int((await db.settings())["ready_target"])
    table = models.AcctpoolAddress
    made = 0
    async with db.session() as session:
        stores = (await session.execute(select(models.AcctpoolPool.store).distinct())).scalars().all()
    given = {name: store.get("highest_index") for name, store in ((await signer.status()).get("stores") or {}).items()}
    for store in stores:
        async with db.session() as session, session.begin():
            ready = (
                await session.execute(select(func.count()).where(table.store == store, table.status == Status.READY))
            ).scalar_one()
            highest = (await session.execute(select(func.max(table.index)).where(table.store == store))).scalar_one()
            known = [value for value in (highest, given.get(store)) if isinstance(value, int)]
            first = max(known) + 1 if known else 0
            count = min(target - ready, MAX_DERIVE)
            if count <= 0:
                continue
            for index, address in enumerate(await signer.derive(store, first, count), first):
                session.add(table(store=store, index=index, address=address))
            made += count
        if ready + count < target // 5:
            await db.event(Event.READY_LOW, None, None, {"store": store, "ready": ready + count}, every=3600)
    return made


async def check(plugin: object, signer: SignerClient) -> None:
    db = await plugin.db()  # type: ignore[attr-defined]
    table = models.AcctpoolAddress
    async with db.session() as session:
        rows = (
            (await session.execute(select(table).where(table.checked.is_(False), table.status != Status.READY)))
            .scalars()
            .all()
        )
    for row in rows:
        try:  # one row never stops the check of the others; a failed one is checked again in the next round
            derived = (await signer.derive(row.store, row.index, 1))[0]
            if derived != row.address:
                detail = {"invoice": row.invoice_id, "index": row.index}
                await db.event(Event.ADDRESS_MISMATCH, None, row.address, detail, every=86400)
                if row.invoice_id is not None:  # Bitcart's get(None) has no id filter: it would take any invoice
                    await invalidate(plugin, row.invoice_id)
            async with db.session() as session, session.begin():
                (await session.get(table, row.id)).checked = True
        except Exception as e:
            logger.error(f"acctpool: tamper check of address {row.id}: {get_exception_message(e)}")


async def invalidate(plugin: object, invoice_id: str | None) -> None:
    async with plugin.container(scope=Scope.REQUEST) as container:  # type: ignore[attr-defined]
        service = await container.get(InvoiceService)
        invoice = await service.get(invoice_id)
        if invoice.status == InvoiceStatus.PENDING:
            await service.update_status(invoice, InvoiceStatus.INVALID)
