"""Detection (SPEC-v4 4.5): the stock daemon's `new_transaction` event, watch-load of the diskless watch-only
wallets, and the balance reconcile that covers missed events and daemon downtime.

Accounting of one (address, chain, asset): balance = baseline + all deposits, where baseline is the sum of what our
own mined transactions changed (payout.py, from their receipts). Events are exact: they are inserted as they come
(idempotent). The reconcile counts a positive difference only after the daemon has processed the block that was
the provider head at the reading: then every event of that reading is in, and an event always wins.
"""

import asyncio
import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from api import models as bitcart_models
from api.logging import get_exception_message, get_logger

from . import daemon, models
from .constants import BY_CURRENCY, NATIVE, STATE_SIGNER, USDT, WATCH_DAYS, Chain, Event, State, Status
from .crediting import CLOSED_REASONS, credit
from .hooks import lookup
from .lifecycle import CLOSING, close_invoice

if TYPE_CHECKING:
    from .plugin import Plugin

logger = get_logger(__name__)

LOAD_BATCH = 500  # addresses per batch_load call (2 wallets each)


def watched(table: Any = models.AcctpoolAddress) -> Any:
    return (table.status != Status.READY) & or_(table.status != Status.RETIRED, table.watch_until > func.now())


class Detector:
    def __init__(self, plugin: "Plugin") -> None:
        self.plugin = plugin
        self.load_tasks: dict[str, asyncio.Task[None]] = {}
        # reconcile: (address id, chain, asset) -> (provider head, balance, baseline) of a reading with a difference
        self.seen: dict[tuple[int, str, str], tuple[int, Decimal, Decimal]] = {}
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- the event of the stock daemon

    async def on_transaction(
        self, instance: Any, event: str, tx: str, from_address: str, to: str, amount: Any, contract: Any
    ) -> None:
        try:
            chain = BY_CURRENCY.get(instance.coin_name.lower())
            asset = NATIVE if not contract else USDT if chain and contract.lower() == chain.usdt.lower() else None
            if chain is None or asset is None:
                return
            db = await self.plugin.db()
            signer = await db.get_state(STATE_SIGNER) or {}
            if from_address and from_address.lower() == str(signer.get("fee_wallets", {}).get("evm", "")).lower():
                return  # our own funding
            table, payouts = models.AcctpoolAddress, models.AcctpoolPayout
            async with db.session() as session:
                if (await session.execute(select(payouts.id).where(payouts.tx_hash == tx.lower()).limit(1))).first():
                    return  # our own transaction
                address = (await session.execute(select(table).where(func.lower(table.address) == to.lower()))).scalar()
            if address is not None and address.status != Status.READY:
                if asset == NATIVE and await self.counted_by_balance(chain, address, tx.lower()):
                    return
                async with db.session() as session, session.begin():
                    new = await self.insert(session, address, chain, asset, tx.lower(), Decimal(amount), from_address)
                if new:
                    await self.settle(address.id)
        except Exception as e:
            logger.error(f"acctpool: new_transaction {tx}: {get_exception_message(e)}")

    async def counted_by_balance(self, chain: Chain, address: Any, tx_hash: str) -> bool:
        """M2: native coin sent inside a contract call comes from the daemon's trace queue, maybe after the reconcile
        counted it: a balance row whose reading head is at or above the block of the tx has it. No second deposit."""
        deposits = models.AcctpoolDeposit
        mine = (deposits.address_id == address.id) & (deposits.chain == chain.name) & (deposits.asset == NATIVE)
        async with (await self.plugin.db()).session() as session:
            rows = (await session.execute(select(deposits).where(mine, deposits.source == "balance"))).scalars().all()
        if not rows:
            return False
        block = int((await (await daemon.server(self.plugin.container, chain)).gettransaction(tx=tx_hash))["blockNumber"])
        counted = [row.tx_hash for row in rows if row.height is not None and row.height >= block]
        if counted:
            detail = {"tx": tx_hash, "balance_row": counted[0]}
            await (await self.plugin.db()).event(Event.DEPOSIT_DUPLICATE, chain.name, address.address, detail)
        return bool(counted)

    async def insert(
        self, session: Any, address: Any, chain: Chain, asset: str, tx_hash: str | None, amount: Decimal, sender: Any,
        height: int | None = None,
    ) -> bool:  # fmt: skip
        """One deposit row (idempotent on chain, hash, address, asset). tx_hash None: found by balance."""
        if amount <= 0:
            return False
        deposits, table = models.AcctpoolDeposit, models.AcctpoolAddress
        values = {
            "address_id": address.id, "chain": chain.name, "asset": asset, "amount": amount, "from_address": sender,
            "tx_hash": tx_hash or f"balance:{chain.name}:{address.address}:{asset}:{uuid.uuid4().hex[:16]}",
            "source": "event" if tx_hash else "balance", "height": height, "invoice_id": address.invoice_id,
        }  # fmt: skip
        new = (
            await session.execute(insert(deposits).values(**values).on_conflict_do_nothing().returning(deposits.id))
        ).first()
        if new is not None:  # a retired address is watched again for 30 days
            watch = func.now() + timedelta(days=WATCH_DAYS)
            await session.execute(
                update(table).where(table.id == address.id, table.status == Status.RETIRED).values(watch_until=watch)
            )
        return new is not None

    async def settle(self, address_id: int) -> None:
        """Every deposit of the address that is neither credited nor late: credit it through Bitcart while the
        invoice is open, else mark it late (event late_payment) and let the payout loop sweep it."""
        db = await self.plugin.db()
        table, deposits = models.AcctpoolAddress, models.AcctpoolDeposit
        async with self.locks[address_id]:
            async with db.session() as session:
                address = await session.get(table, address_id)
                rows = (
                    (
                        await session.execute(
                            select(deposits)
                            .where(deposits.address_id == address_id, deposits.credited.is_(False), deposits.late.is_(False))
                            .order_by(deposits.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            for key in dict.fromkeys((row.chain, row.asset, row.invoice_id) for row in rows):
                group = [row for row in rows if (row.chain, row.asset, row.invoice_id) == key]
                result = None
                if address.status == Status.IN_INVOICE and key[2] == address.invoice_id:
                    lookup_field = lookup(address.id, key[0], key[1])
                    result = await credit(self.plugin.container, key[2], lookup_field, self.plugin.hooks.request_data)
                    if result.reason == "error":
                        continue  # the reconcile loop tries again
                credited = result is not None and result.credited
                async with db.session() as session, session.begin():
                    ids = [row.id for row in group]
                    await session.execute(
                        update(deposits).where(deposits.id.in_(ids)).values(credited=credited, late=not credited)
                    )
                    locked = await session.get(table, address_id, with_for_update=True)
                    # not_found (no method of this invoice for the chain and asset) keeps the invoice open for
                    # the right payment; the close of the invoice starts the payout then
                    closed = result is not None and result.reason in CLOSED_REASONS
                    if not credited and (locked.status == Status.RETIRED or closed):
                        locked.status = Status.PENDING_PAYOUT
                detail = {"invoice": key[2], "asset": key[1], "tx": [row.tx_hash for row in group]}
                if credited:
                    await db.event(Event.CREDIT, key[0], address.address, {**detail, "status": result.status_after})
                else:
                    detail.update(amount=str(sum(row.amount for row in group)), reason=result.reason if result else None)
                    await db.event(Event.LATE_PAYMENT, key[0], address.address, detail)

    # -- watch-load

    def schedule_load(self, chain: Chain) -> None:
        task = self.load_tasks.get(chain.name)
        if task is None or task.done():
            self.load_tasks[chain.name] = asyncio.create_task(self.load_chain(chain))

    async def load_chain(self, chain: Chain) -> None:
        try:
            db = await self.plugin.db()
            table = models.AcctpoolAddress
            async with db.session() as session:
                rows = (await session.execute(select(table.address).where(watched()).order_by(table.id))).scalars().all()
            for first in range(0, len(rows), LOAD_BATCH):
                await self.load_addresses(chain, rows[first : first + LOAD_BATCH], quiet=False)
        except Exception as e:
            logger.error(f"acctpool: watch-load of {chain.name}: {get_exception_message(e)}")

    async def load_addresses(self, chain: Chain, addresses: list[str], quiet: bool = True) -> None:
        wallets = []
        for address in addresses:
            wallets += [{"xpub": address, "diskless": True}, {"xpub": address, "contract": chain.usdt, "diskless": True}]
        try:
            await (await daemon.server(self.plugin.container, chain)).batch_load(wallets=wallets)
        except Exception as e:
            if not quiet:
                raise
            # the watch loop loads it again, and the reconcile loop reads the balance meanwhile
            logger.error(f"acctpool: batch_load on {chain.name}: {get_exception_message(e)}")

    async def close_lost_invoices(self) -> None:
        """An open address whose Bitcart invoice is closed (its hook was lost) or gone (deleted)."""
        db = await self.plugin.db()
        table, invoices = models.AcctpoolAddress, bitcart_models.Invoice
        async with db.session() as session:
            ids = (
                (
                    await session.execute(
                        select(table.invoice_id)
                        .outerjoin(invoices, invoices.id == table.invoice_id)
                        .where(table.status == Status.IN_INVOICE)
                        .where(or_(invoices.id.is_(None), invoices.status.in_(CLOSING)))
                    )
                )
                .scalars()
                .all()
            )
        for invoice_id in ids:
            async with db.session() as session, session.begin():
                await close_invoice(session, invoice_id)

    # -- reconcile

    async def reconcile(self, chain: Chain, unwatched: bool = False) -> int:
        """Watched addresses without a payout in flight, and the ones with a first reading (unwatched=True: retired
        addresses past watch_until, the 30-day check; the daemon can still have them loaded until it restarts, so
        the same two readings, the next round finishes them). Returns the new deposits."""
        db = await self.plugin.db()
        table, payouts = models.AcctpoolAddress, models.AcctpoolPayout
        busy = select(payouts.address_id).where(payouts.chain == chain.name, payouts.state.in_(State.OPEN))
        past = (table.status == Status.RETIRED) & (table.watch_until <= func.now())
        which = past if unwatched else watched() | table.id.in_([key[0] for key in self.seen if key[1] == chain.name])
        async with db.session() as session:
            rows = (await session.execute(select(table).where(which, table.id.not_in(busy)))).scalars().all()
        srv = await daemon.server(self.plugin.container, chain)
        processed = await daemon.height(srv)
        readings = []
        for address in rows:
            for asset in (NATIVE, USDT):
                key = (address.id, chain.name, asset)
                if key in self.seen:
                    if processed > self.seen[key][0]:  # the daemon is past that head: the events of it are in
                        readings.append((address, asset, *self.seen.pop(key)))
                    continue
                try:
                    units = await daemon.balance(srv, chain, address.address, asset)
                except Exception as e:  # one address never stops the others
                    logger.error(f"acctpool: balance of {address.address} on {chain.name}: {get_exception_message(e)}")
                    continue
                readings.append((address, asset, None, daemon.coins(units, daemon.decimals(chain, asset)), None))
        head = await daemon.head(srv)  # after every reading: no reading is above it
        found = 0
        for address, asset, seen_head, balance, baseline in readings:
            try:
                final = seen_head is not None
                found += await self.compare(address, chain, asset, balance, baseline, seen_head or head, final)
            except Exception as e:
                logger.error(f"acctpool: reconcile of {address.address} on {chain.name}: {get_exception_message(e)}")
        # uncredited deposits whose credit call failed earlier
        deposits = models.AcctpoolDeposit
        async with db.session() as session:
            waiting = (
                await session.execute(
                    select(deposits.address_id).distinct()
                    .where(deposits.chain == chain.name, deposits.credited.is_(False), deposits.late.is_(False))
                    .where(deposits.created < func.now() - timedelta(minutes=1))
                )
            ).scalars().all()  # fmt: skip
        for address_id in waiting:
            try:
                await self.settle(address_id)
            except Exception as e:  # one address never stops the others
                logger.error(f"acctpool: settle of address {address_id}: {get_exception_message(e)}")
        return found

    async def compare(
        self, address: Any, chain: Chain, asset: str, balance: Decimal, baseline: Any, head: int, final: bool
    ) -> int:
        """balance - (baseline + deposits). A first reading with more money is kept (with its head); the final one
        records the difference, unless a payout of the address is open or finished since then (baseline moved)."""
        db = await self.plugin.db()
        payouts, key = models.AcctpoolPayout, (address.id, chain.name, asset)
        busy = select(payouts.id).where(payouts.address_id == address.id, payouts.chain == chain.name)
        async with db.session() as session, session.begin():
            if (await session.execute(busy.where(payouts.state.in_(State.OPEN)))).first():
                return 0
            now_baseline, expected = await self.expected(session, address.id, chain.name, asset)
            diff = balance - expected
            if diff <= 0 or (baseline is not None and baseline != now_baseline):
                return 0
            if not final:
                self.seen[key] = (head, balance, now_baseline)
                return 0
            new = await self.insert(session, address, chain, asset, None, diff, None, head)
        if new:
            await self.settle(address.id)
        return int(new)

    async def expected(self, session: Any, address_id: int, chain: str, asset: str) -> tuple[Decimal, Decimal]:
        """(baseline, baseline + every deposit)."""
        balances, deposits = models.AcctpoolBalance, models.AcctpoolDeposit
        row = await session.get(balances, (address_id, chain, asset))
        baseline = Decimal(row.baseline) if row else Decimal(0)
        total = (
            await session.execute(
                select(func.coalesce(func.sum(deposits.amount), 0)).where(
                    deposits.address_id == address_id, deposits.chain == chain, deposits.asset == asset
                )
            )
        ).scalar_one()
        return baseline, baseline + Decimal(total)
