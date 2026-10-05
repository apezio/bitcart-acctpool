"""Payout (SPEC-v4 4.6): fund the gas, sweep USDT, sweep the native coin, all to the destinations that the signer
pins. The daemon gives nonce, gas estimate, gas price, broadcast and receipts; the signer signs.

Every signature is a row first (`planned`, with its idempotency key and all request values), then `signed` (raw
bytes stored), then `broadcast`, then `confirmed` or `failed`. After a crash the same key gives the same bytes,
and `signed`/`broadcast` rows are broadcast again with the same bytes: no double spend. A transaction that is
not mined after `replace_after` seconds is replaced with the same nonce and fee +20% (at most 3 times); the
original stays `broadcast` until its replacement is broadcast. A receipt of any row of a nonce group (all rows
of one sender and nonce) finishes the whole group in one database transaction, with the baseline change that
the receipt shows (SPEC-v4 4.5). One open root transaction per address and chain; one open funding per chain.
"""

import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from api.ext import fxrate
from api.logging import get_exception_message, get_logger
from api.services.exchange_rate import ExchangeRateService

from . import daemon, feerule, models, secondcheck
from .constants import CHAINS, NATIVE, NATIVE_DECIMALS, STATE_SIGNER, USDT, Chain, Event, State, Status
from .lifecycle import retire
from .signer_client import SignerClient, SignerError

if TYPE_CHECKING:
    from .plugin import Plugin

logger = get_logger(__name__)

GAS_MARGIN = Decimal("1.2")
# A funding pays the sweep fee at 1.1 x the price (a price rise of 10% needs no second funding); a token sweep whose
# funded coin pays 80% of the price goes out at own // gas (0.8 x speed factor 1.25 = the daemon's own price).
FUND_MARGIN, FUND_TOLERANCE = Decimal("1.10"), Decimal("0.80")
MAX_REPLACEMENTS = 3
# Native coin is swept only when the value after the fee is at least 10 fees; less stays on the address (stranded).
# The signer refuses a native sweep whose fee is over 10% (native_max_fee_share_percent) of value + fee.
NATIVE_MIN_FEES = 10
ALREADY_SENT = ("already known", "known transaction", "already imported", "nonce too low", "underpriced")
QUIET = ("replaced", "not_needed")  # a failed row that is not a failure of the address
DAILY_CAP, CONFLICT = "daily_cap_exceeded", "idempotency_conflict"
SENT = "sent"  # error prefix: the signer got this key and its answer is not known (it may hold a signature)


def nonce_group(table: Any, row: Any) -> Any:
    """Every row of one signer nonce group (SPEC 3.4): the fee wallet's nonce for a funding, else the address's."""
    sender = (table.kind == "fund") if row.kind == "fund" else (table.kind != "fund") & (table.address_id == row.address_id)
    return (table.chain == row.chain) & (table.nonce == row.nonce) & sender


def number(value: Any) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


class Payouts:
    def __init__(self, plugin: "Plugin", signer: SignerClient, replace_after: float = 600) -> None:
        self.plugin = plugin
        self.signer = signer
        self.replace_after = replace_after

    async def round(self, chain: Chain) -> None:
        db = await self.plugin.db()
        signer_state = await db.get_state(STATE_SIGNER)
        if not signer_state or not signer_state.get("keystore"):
            return  # the state loop writes signer_down
        srv = await daemon.server(self.plugin.container, chain)
        table = models.AcctpoolPayout
        async with db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(table).where(table.chain == chain.name, table.state.in_(State.OPEN)).order_by(table.id)
                    )
                )
                .scalars()
                .all()
            )
        for row in rows:
            await self.guarded(self.advance(chain, srv, row, signer_state), chain, row.address_id)
        addresses = models.AcctpoolAddress
        has_deposit = select(models.AcctpoolDeposit.address_id).where(models.AcctpoolDeposit.chain == chain.name)
        async with db.session() as session:
            due = (
                (
                    await session.execute(
                        select(addresses)
                        .where(addresses.status.in_((Status.PENDING_PAYOUT, Status.IN_PAYOUT, Status.RETIRED)))
                        .where(
                            or_(
                                addresses.withdraw_requested,
                                (addresses.status != Status.RETIRED) & addresses.id.in_(has_deposit),
                            )
                        )
                        .order_by(addresses.id)
                    )
                )
                .scalars()
                .all()
            )
        for address in due:
            await self.guarded(self.plan(chain, srv, address, signer_state), chain, address.id)

    async def guarded(self, job: Any, chain: Chain, address_id: int) -> None:
        """One address never stops the round of the others."""
        try:
            await job
        except Exception as e:
            logger.error(f"acctpool: payout on {chain.name}, address {address_id}: {get_exception_message(e)}")
            db = await self.plugin.db()
            await db.event(Event.ERROR, chain.name, str(address_id), {"error": f"{type(e).__name__}: {e}"[:300]}, every=600)

    # -- new work

    async def plan(self, chain: Chain, srv: Any, address: Any, signer_state: dict[str, Any]) -> None:
        db = await self.plugin.db()
        table = models.AcctpoolPayout
        async with db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(table)
                        .where(table.address_id == address.id, table.chain == chain.name)
                        .order_by(table.id.desc())
                    )
                )
                .scalars()
                .all()
            )
        if any(row.state in State.OPEN for row in rows):
            return
        last = next((row for row in rows if row.error not in QUIET), None)
        if last is not None and last.state == State.FAILED and not address.withdraw_requested:
            return  # payout_failed was written: an admin withdraw starts a new try
        if not await self.confirmed_enough(chain, srv, address):
            return
        withdraw = address.withdraw_requested
        price = await self.price(srv)
        token = await daemon.balance(srv, chain, address.address, USDT)
        native = await daemon.balance(srv, chain, address.address, NATIVE)
        usd = await self.native_usd(chain)
        if token > 0:
            if not withdraw and daemon.coins(token, chain.usdt_decimals) < await self.min_withdraw(address, chain, USDT):
                return
            destination = signer_state["stores"][address.store]["destinations"][chain.name]
            gas = await self.token_gas(chain, srv, address.address, destination, token)
            fee_usd = None if usd is None else daemon.coins((gas + chain.native_gas_limit) * price, NATIVE_DECIMALS) * usd
            if not withdraw and not feerule.pay_now(chain.pay_at_once, daemon.coins(token, chain.usdt_decimals), fee_usd):
                return await self.waiting(chain, address)
            own = await self.own_native(address.id, chain, native, None)
            fee = sweep_fee(own, gas, price)
            if fee is None:
                return await self.fund(chain, srv, address, funding(own, gas, price), price, signer_state)
            nonce = await srv.getnonce(address=address.address, pending=True)
            return await self.add(address, chain, USDT, "sweep", nonce, gas, fee, token)
        value = sweepable(chain, native, price)
        if value:
            amount = daemon.coins(value, NATIVE_DECIMALS)
            if not withdraw and amount < await self.min_withdraw(address, chain, NATIVE):
                return
            fee_usd = None if usd is None else daemon.coins(native - value, NATIVE_DECIMALS) * usd
            if not withdraw and not feerule.pay_now(chain.pay_at_once, None if usd is None else amount * usd, fee_usd):
                return await self.waiting(chain, address)
            nonce = await srv.getnonce(address=address.address, pending=True)
            return await self.add(address, chain, NATIVE, "sweep_native", nonce, chain.native_gas_limit, price, value)
        await self.maybe_retire(address.id)

    async def price(self, srv: Any) -> int:
        """Gas price of every new transaction: the daemon's price x the speed factor (the same in plan and retire)."""
        return int(await srv.getfeerate(multiplier=str((await (await self.plugin.db()).settings())["speed_factor"])))

    async def confirmed_enough(self, chain: Chain, srv: Any, address: Any) -> bool:
        """Every deposit of the address on this chain has payout_confirmations: the newest one is the one in the
        highest block, which is not always the highest id (a balance row keeps its older reading head)."""
        db = await self.plugin.db()
        table = models.AcctpoolDeposit
        async with db.session() as session:
            rows = (
                await session.execute(select(table).where(table.address_id == address.id, table.chain == chain.name))
            ).scalars()
            deposits = list(rows)
        if not deposits:
            return bool(address.withdraw_requested)
        return min([await daemon.confirmations(srv, row) for row in deposits]) >= chain.payout_confirmations

    async def token_gas(self, chain: Chain, srv: Any, address: str, destination: str, amount: int) -> int:
        tx = {"from": address, "to": chain.usdt, "data": daemon.transfer_data(destination, amount), "value": 0}
        try:
            estimate = int(await srv.get_default_gas(tx=tx))
        except Exception as e:
            logger.warning(f"acctpool: no gas estimate on {chain.name}, the fallback is used: {type(e).__name__}")
            estimate = chain.token_gas_limit
        return int(Decimal(estimate) * GAS_MARGIN)

    async def native_usd(self, chain: Chain) -> Decimal | None:
        if chain.pay_at_once:
            return None
        try:
            service = await self.plugin.container.get(ExchangeRateService)
            rate, _ = await fxrate.calculate_rules(service, fxrate.get_default_rules(), chain.currency.upper(), "USD")
        except Exception as e:
            logger.warning(f"acctpool: no USD price of {chain.currency}: {type(e).__name__}")
            return None
        return rate if isinstance(rate, Decimal) and rate.is_finite() and rate > 0 else None

    async def min_withdraw(self, address: Any, chain: Chain, asset: str) -> Decimal:
        db = await self.plugin.db()
        pools = models.AcctpoolPool
        async with db.session() as session:
            value = (
                await session.execute(
                    select(pools.min_withdraw).where(
                        pools.store == address.store, pools.chain == chain.name, pools.asset == asset
                    )
                )
            ).scalar()
        return Decimal(value or 0)

    async def waiting(self, chain: Chain, address: Any) -> None:
        """The fee rule said wait: the round tries again later. An alert after 24 h."""
        db = await self.plugin.db()
        table = models.AcctpoolAddress
        old = select(table.id).where(table.id == address.id, table.assigned_at < func.now() - timedelta(hours=24))
        async with db.session() as session:
            if (await session.execute(old)).first():
                await db.event(Event.PAYOUT_WAITING_24H, chain.name, address.address, {}, every=86400)

    async def own_native(self, address_id: int, chain: Chain, balance: int, nonce: int | None) -> int:
        """Native coin of the address that a token sweep may spend: the signer's rule (journal.sweep_budget, SPEC 7.8),
        in signing order, capped by the balance. Signatures of one kind and nonce exclude each other on chain: each
        counts only what it has above the highest earlier one. With `nonce`, the fee already signed for that token
        sweep nonce is added back (a replacement pays only the difference)."""
        db = await self.plugin.db()
        table = models.AcctpoolPayout
        async with db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(table)
                        .where(table.address_id == address_id, table.chain == chain.name, table.raw_tx.is_not(None))
                        .order_by(table.id)
                    )
                )
                .scalars()
                .all()
            )
        budget, highest = 0, {}
        for row in rows:
            fee = 0 if row.kind == "fund" else int(row.gas_limit) * int(row.max_fee_wei)
            size = (0 if row.kind == "sweep" else int(row.value)) + fee
            group = (row.kind, int(row.nonce))
            size, highest[group] = max(0, size - highest.get(group, 0)), max(size, highest.get(group, 0))
            if row.kind == "fund":
                budget += size
            elif row.kind == "sweep":
                budget -= size
            else:
                budget = min(budget, max(0, budget - size))
        return min(budget + highest.get(("sweep", nonce), 0), balance)

    async def fund(self, chain: Chain, srv: Any, address: Any, value: int, price: int, signer_state: dict[str, Any]) -> None:
        db = await self.plugin.db()
        table = models.AcctpoolPayout
        async with db.session() as session:
            busy = (
                await session.execute(
                    select(table.id).where(table.chain == chain.name, table.kind == "fund", table.state.in_(State.OPEN))
                )
            ).first()
        if busy:
            return  # one funding at a time on a chain
        fee_wallet = signer_state["fee_wallets"]["evm"]
        if await daemon.balance(srv, chain, fee_wallet, NATIVE) < value + chain.native_gas_limit * price:
            await db.event(Event.FEE_WALLET_LOW, chain.name, fee_wallet, {"needed_wei": str(value)}, every=3600)
            return
        nonce = await srv.getnonce(address=fee_wallet, pending=True)
        await self.add(address, chain, NATIVE, "fund", nonce, chain.native_gas_limit, price, value)

    async def add(self, address: Any, chain: Chain, asset: str, kind: str, nonce: int, gas: int, price: int, value: int,
                  replaces: Any = None) -> None:  # fmt: skip
        db = await self.plugin.db()
        async with db.session() as session, session.begin():
            session.add(
                models.AcctpoolPayout(
                    address_id=address.id, chain=chain.name, asset=asset, kind=kind,
                    idempotency_key=f"ap-{uuid.uuid4().hex}", nonce=nonce, gas_limit=gas, max_fee_wei=price,
                    value=value, replaces_id=None if replaces is None else replaces.id,
                    attempts=0 if replaces is None else replaces.attempts + 1,
                )
            )  # fmt: skip
            if replaces is None:
                row = await session.get(models.AcctpoolAddress, address.id, with_for_update=True)
                if row.status == Status.PENDING_PAYOUT:
                    row.status = Status.IN_PAYOUT

    # -- open rows

    async def advance(self, chain: Chain, srv: Any, row: Any, signer_state: dict[str, Any]) -> None:
        row = await self.row(row.id)  # read again: an earlier row of this round can have finished its nonce group
        if row.state == State.PLANNED:
            if row.kind == "fund" and not row.replaces_id and not sent(row) and await self.not_needed(chain, srv, row):
                return
            if row.error == DAILY_CAP and row.updated.astimezone(UTC).date() == datetime.now(UTC).date():
                return  # the signer's fee-wallet cap of this UTC day: the next day
            if row.replaces_id and await self.mined(chain, srv, row):
                return  # a transaction of this nonce is in a block: nothing to sign
            if not await self.sign(chain, srv, row, signer_state):
                return
        if row.state == State.SIGNED:
            await self.send(chain, srv, row, signer_state)
        elif row.state == State.BROADCAST:
            await self.follow(chain, srv, row, signer_state)

    async def send(self, chain: Chain, srv: Any, row: Any, signer_state: dict[str, Any]) -> None:
        if any(item.id > row.id and item.raw_tx and item.state in State.OPEN for item in await self.group(row)):
            return  # a newer attempt of this nonce is signed: that one is sent and followed (M1)
        try:
            await srv.broadcast(tx=row.raw_tx)
        except Exception as e:
            if not any(text in str(e).lower() for text in ALREADY_SENT):
                if await self.mined(chain, srv, row):
                    return  # in a block, whatever the node says (L1)
                if row.error != "stuck":
                    await self.update(row, error=f"broadcast: {str(e)[:200]}")
                if await self.age(row) >= self.replace_after:  # no node takes it: the same nonce, a higher fee
                    await self.replace(chain, srv, row, signer_state)
                return
        await self.broadcasted(row)

    async def sign(self, chain: Chain, srv: Any, row: Any, signer_state: dict[str, Any]) -> bool:
        db = await self.plugin.db()
        address = await self.address_of(row)
        replaces = await self.row(row.replaces_id) if row.replaces_id else None
        fields = {
            "idempotency_key": row.idempotency_key, "chain": chain.name, "store": address.store, "index": address.index,
            "nonce": int(row.nonce), "max_fee_per_gas_wei": str(int(row.max_fee_wei)),
            "max_priority_fee_per_gas_wei": str(int(row.max_fee_wei)),
            "replaces": replaces.idempotency_key if replaces else None,
        }  # fmt: skip
        sender = signer_state["fee_wallets"]["evm"] if row.kind == "fund" else address.address
        if row.kind == "fund":
            fields["value_wei"] = str(int(row.value))
            # the funded address has the token to sweep; a replacement or a row that the signer may have signed
            # must go out whatever the address has now (its fee-wallet nonce is taken)
            check_asset, need = USDT, 0 if row.replaces_id or sent(row) else 1
        elif row.kind == "sweep":
            fields.update(gas_limit=int(row.gas_limit), amount=str(int(row.value)), token=None)
            check_asset, need = USDT, int(row.value)
        else:
            fields.update(gas_limit=int(row.gas_limit), value_wei=str(int(row.value)))
            check_asset, need = NATIVE, int(row.value) + int(row.gas_limit) * int(row.max_fee_wei)

        async def daemon_read() -> tuple[int, int]:
            return await daemon.balance(srv, chain, address.address, check_asset), await daemon.head(srv)

        try:
            await secondcheck.check(chain, address.address, check_asset, need, daemon_read)
        except (secondcheck.Down, secondcheck.Mismatch) as e:
            # the row waits (a replacement too: its original stays broadcast and followed)
            kind = Event.SECOND_OPINION_DOWN if isinstance(e, secondcheck.Down) else Event.SECOND_OPINION_MISMATCH
            await db.event(kind, chain.name, address.address, {"payout": row.id, "detail": str(e)}, every=600)
            await self.update(row, error=f"{SENT}: {kind}" if sent(row) else kind)
            return False
        await self.update(row, error=SENT)  # also a crash after the signer's commit leaves the mark
        try:
            raw, tx_hash = await self.signer.sign(row.kind, fields, sender, chain.chain_id)
        except SignerError as e:
            if not e.refused:
                await db.event(Event.SIGNER_DOWN, chain.name, None, {"detail": str(e)}, every=600)
                await self.update(row, error=f"{SENT}: {e}"[:300])
            elif e.code == DAILY_CAP:
                await db.event(Event.FEE_WALLET_LOW, chain.name, sender, {"reason": DAILY_CAP}, every=3600)
                await self.update(row, error=DAILY_CAP, updated=func.now())
            elif e.code == CONFLICT and replaces is None:
                await self.conflict(chain, srv, row, sender)
            else:
                await self.refused(row, replaces, f"signer: {e}")
            return False
        await self.update(row, state=State.SIGNED, raw_tx=raw, tx_hash=tx_hash, error=None)
        return True

    async def conflict(self, chain: Chain, srv: Any, row: Any, sender: str) -> None:
        """The signer has a signature for this sender and nonce (the key of this unsigned row has none): the daemon's
        nonce was stale. Another nonce from the daemon: the row takes it. The same one: a receipt of that nonce ends
        this row with its group (finish); without a receipt the row waits with an alert. Rows are closed only by a
        receipt, so a signature of ours with no open row is left only by a database restored to an older state."""
        nonce = int(await srv.getnonce(address=sender, pending=True))
        if nonce != int(row.nonce):
            return await self.update(row, nonce=nonce, error=None)
        if await self.mined(chain, srv, row):
            return
        await self.update(row, error=CONFLICT)
        detail = {"payout": row.id, "reason": "nonce_conflict", "nonce": nonce}
        await (await self.plugin.db()).event(Event.PAYOUT_FAILED, chain.name, str(row.address_id), detail, every=3600)

    async def refused(self, row: Any, replaces: Any, reason: str) -> None:
        """A clear no of the signer: the row fails, the address waits for an admin; the original of a replacement
        is followed further without new replacements. One transaction."""
        db = await self.plugin.db()
        table = models.AcctpoolPayout
        async with db.session() as session, session.begin():
            stored = await session.get(table, row.id, with_for_update=True)
            stored.state, stored.error, stored.updated = State.FAILED, reason[:300], func.now()
            if replaces is not None:
                original = await session.get(table, replaces.id, with_for_update=True)
                original.error, original.attempts = "stuck", MAX_REPLACEMENTS
            elif row.kind == "fund":  # a top-up (L2): the sweep that it tops up gets no new replacement
                sweeps = select(table).where(table.address_id == row.address_id, table.chain == row.chain,
                                             table.kind == "sweep", table.state.in_(State.OPEN))  # fmt: skip
                for sweep in (await session.execute(sweeps.with_for_update())).scalars():
                    sweep.error, sweep.attempts = "stuck", MAX_REPLACEMENTS
            (await session.get(models.AcctpoolAddress, row.address_id, with_for_update=True)).withdraw_requested = False
        await db.event(Event.PAYOUT_FAILED, row.chain, str(row.address_id), {"payout": row.id, "reason": reason[:300]})

    async def not_needed(self, chain: Chain, srv: Any, row: Any) -> bool:
        """H1: a funding that no signer holds, whose purpose is gone: a sweep that was open when it was planned (so
        a top-up) has a receipt now, or the address has no token left. It ends `not_needed`, with no alert."""
        table = models.AcctpoolPayout
        topped = select(table.id).where(
            table.address_id == row.address_id, table.chain == row.chain, table.kind == "sweep",
            table.created < row.created, table.updated > row.created,
            or_(table.state == State.CONFIRMED, table.error == "reverted"),
        )  # fmt: skip
        async with (await self.plugin.db()).session() as session:
            mined = (await session.execute(topped)).first()
        if not mined and await daemon.balance(srv, chain, (await self.address_of(row)).address, USDT) > 0:
            return False
        await self.update(row, state=State.FAILED, error="not_needed")
        return True

    async def broadcasted(self, row: Any) -> None:
        """The row is sent; the older open rows of its nonce group (the ones it replaces) are not followed any more.
        A newer attempt stays: it has a higher fee, and the signer may hold it (M1)."""
        table = models.AcctpoolPayout
        async with (await self.plugin.db()).session() as session, session.begin():
            older = select(table).where(nonce_group(table, row), table.state.in_(State.OPEN), table.id <= row.id)
            others = (await session.execute(older.with_for_update())).scalars().all()
            for other in others:
                other.state, other.error, other.updated = (
                    (State.BROADCAST, None, func.now()) if other.id == row.id else (State.FAILED, "replaced", func.now())
                )
        row.state = State.BROADCAST

    async def follow(self, chain: Chain, srv: Any, row: Any, signer_state: dict[str, Any]) -> None:
        if await self.mined(chain, srv, row):
            return
        try:
            await srv.broadcast(tx=row.raw_tx)  # the same bytes again: a node can drop a transaction
        except Exception as e:
            if "nonce too low" in str(e).lower():
                return  # another transaction of this nonce is mined: its receipt shows up in a next round
        if await self.age(row) >= self.replace_after:
            await self.replace(chain, srv, row, signer_state)

    async def mined(self, chain: Chain, srv: Any, row: Any) -> bool:
        """A receipt of any transaction of the row's nonce group finishes the group."""
        group = await self.group(row)
        for item in group:
            receipt = await self.receipt(srv, item.tx_hash) if item.tx_hash else None
            if receipt is not None:
                await self.finish(chain, item, group, receipt)
                return True
        return False

    async def receipt(self, srv: Any, tx_hash: str) -> dict[str, Any] | None:
        try:
            return await srv.get_tx_status(tx=tx_hash)
        except Exception as e:  # not mined (TxNotFoundError), or the daemon has no answer now
            logger.debug(f"acctpool: no receipt of {tx_hash}: {type(e).__name__}")
            return None

    async def replace(self, chain: Chain, srv: Any, row: Any, signer_state: dict[str, Any]) -> None:
        """Same nonce, fee +20%. A token sweep may spend only funded coin (signer budget): it goes out at what that
        coin pays when this is 10% above the highest fee of the nonce (the signer's floor), else a top-up first."""
        db = await self.plugin.db()
        table = models.AcctpoolPayout
        async with db.session() as session:
            waiting = (
                await session.execute(select(table.id).where(table.replaces_id == row.id, table.state.in_(State.OPEN)))
            ).first()
        if waiting:
            return  # its replacement is not sent yet
        if row.attempts >= MAX_REPLACEMENTS:
            if row.error != "stuck":
                await self.update(row, error="stuck")
                detail = {"payout": row.id, "reason": "stuck"}
                await db.event(Event.PAYOUT_FAILED, chain.name, str(row.address_id), detail, every=86400)
            return
        price = await self.replacement_price(row)
        gas = int(row.gas_limit)
        address = await self.address_of(row)
        value = int(row.value)
        if row.kind == "sweep_native":
            value = int(row.value) + gas * (int(row.max_fee_wei) - price)  # value + fee stays the balance
        elif row.kind == "sweep":
            native = await daemon.balance(srv, chain, address.address, NATIVE)
            own = await self.own_native(row.address_id, chain, native, int(row.nonce))
            top = max(int(item.max_fee_wei) for item in await self.group(row) if item.raw_tx)
            fee = sweep_fee(own, gas, price, max(top + 1, -(-top * 11 // 10)))
            if fee is None:
                return await self.fund(chain, srv, address, funding(own, gas, price), price, signer_state)
            price = fee
        await self.add(address, chain, row.asset, row.kind, int(row.nonce), gas, price, value, replaces=row)

    async def replacement_price(self, row: Any) -> int:
        """20% above the highest fee of every signed attempt of the nonce, also a closed one: the signer refuses a
        replacement that is not 10% above the highest one it has (M1)."""
        return max(int(item.max_fee_wei) for item in await self.group(row) if item.raw_tx) * 12 // 10 + 1

    async def finish(self, chain: Chain, item: Any, group: list[Any], receipt: dict[str, Any]) -> None:
        """One transaction: the mined row confirmed (or failed: reverted), the other rows of its group failed, and the
        baseline moved by what the receipt shows (SPEC-v4 4.5): a funding adds its value; a sweep takes its fee
        (gasUsed x the price paid) and, when it did not revert, the amount it moved. The price paid is max_fee_wei: the
        priority fee is the max fee, so base + min(priority, max - base) = max (anvil's receipt effectiveGasPrice is
        not always what it charged)."""
        ok = int(receipt.get("status", 0)) == 1
        fee, value = number(receipt["gasUsed"]) * int(item.max_fee_wei), int(item.value)
        if item.kind == "fund":
            deltas = {NATIVE: value if ok else 0}
        elif item.kind == "sweep":
            deltas = {NATIVE: -fee, USDT: -value if ok else 0}
        else:
            deltas = {NATIVE: -fee - (value if ok else 0)}
        db = await self.plugin.db()
        table, balances = models.AcctpoolPayout, models.AcctpoolBalance
        async with db.session() as session, session.begin():
            for other in group:
                stored = await session.get(table, other.id, with_for_update=True)
                if other.id != item.id and stored.state in State.OPEN:
                    stored.state, stored.error, stored.updated = State.FAILED, "replaced", func.now()
            stored = await session.get(table, item.id, with_for_update=True)
            if stored.state == State.CONFIRMED or stored.error == "reverted":
                return  # finished before
            stored.state, stored.error, stored.updated = (State.CONFIRMED, None, func.now()) if ok else (
                State.FAILED, "reverted", func.now())  # fmt: skip
            for asset, delta in deltas.items():
                change = daemon.coins(delta, daemon.decimals(chain, asset))
                known = await session.get(balances, (item.address_id, chain.name, asset), with_for_update=True)
                if known is None:
                    session.add(balances(address_id=item.address_id, chain=chain.name, asset=asset, baseline=change))
                else:
                    known.baseline += change
            if not ok:
                (await session.get(models.AcctpoolAddress, item.address_id, with_for_update=True)).withdraw_requested = False
        detail = {"payout": item.id, "kind": item.kind, "tx": item.tx_hash}
        if ok:
            await db.event(Event.PAYOUT, chain.name, str(item.address_id), detail)
        else:
            await db.event(Event.PAYOUT_FAILED, chain.name, str(item.address_id), {**detail, "reason": "reverted"})

    async def maybe_retire(self, address_id: int) -> None:
        """Retired (and watched for 30 days) when nothing is open and no chain with a deposit has money left."""
        db = await self.plugin.db()
        payouts, deposits = models.AcctpoolPayout, models.AcctpoolDeposit
        async with db.session() as session:
            address = await session.get(models.AcctpoolAddress, address_id)
            busy = (
                await session.execute(
                    select(payouts.id).where(payouts.address_id == address_id, payouts.state.in_(State.OPEN))
                )
            ).first()
            chains = (
                (await session.execute(select(deposits.chain).distinct().where(deposits.address_id == address_id)))
                .scalars()
                .all()
            )
        if busy or address.status not in (Status.PENDING_PAYOUT, Status.IN_PAYOUT, Status.RETIRED):
            return
        for name in chains:
            chain = CHAINS.get(name)
            if chain is None:
                continue
            srv = await daemon.server(self.plugin.container, chain)
            if await daemon.balance(srv, chain, address.address, USDT) > 0:
                return
            if sweepable(chain, await daemon.balance(srv, chain, address.address, NATIVE), await self.price(srv)):
                return  # round() sweeps it
        async with db.session() as session, session.begin():
            locked = await session.get(models.AcctpoolAddress, address_id, with_for_update=True)
            if locked.status != Status.RETIRED or locked.withdraw_requested:
                retire(locked)

    # -- small helpers

    async def row(self, row_id: int) -> Any:
        async with (await self.plugin.db()).session() as session:
            return await session.get(models.AcctpoolPayout, row_id)

    async def group(self, row: Any) -> list[Any]:
        table = models.AcctpoolPayout
        async with (await self.plugin.db()).session() as session:
            return list((await session.execute(select(table).where(nonce_group(table, row)).order_by(table.id))).scalars())

    async def age(self, row: Any) -> float:
        """Seconds since the row's last state change (database clock)."""
        table = models.AcctpoolPayout
        async with (await self.plugin.db()).session() as session:
            age = func.extract("epoch", func.now() - table.updated)
            return float((await session.execute(select(age).where(table.id == row.id))).scalar_one())

    async def address_of(self, row: Any) -> Any:
        async with (await self.plugin.db()).session() as session:
            return await session.get(models.AcctpoolAddress, row.address_id)

    async def update(self, row: Any, **values: Any) -> None:
        async with (await self.plugin.db()).session() as session, session.begin():
            stored = await session.get(models.AcctpoolPayout, row.id, with_for_update=True)
            for key, value in values.items():
                setattr(stored, key, value)
                setattr(row, key, value)
            if "state" in values:
                stored.updated = func.now()


def sent(row: Any) -> bool:
    return str(row.error).startswith(SENT)


def sweep_fee(own: int, gas: int, price: int, floor: int = 0) -> int | None:
    """max_fee_wei of a token sweep that its funded coin pays (signer budget): the price, or own // gas when that is
    at least FUND_TOLERANCE x the price and `floor`; None: a funding first."""
    fee = min(price, own // gas)
    return fee if fee >= max(floor, price * FUND_TOLERANCE) else None


def funding(own: int, gas: int, price: int) -> int:
    """Value of a funding: the sweep fee at FUND_MARGIN x the price, rounded up per gas unit, less the funded coin."""
    return gas * math.ceil(price * FUND_MARGIN) - own


def sweepable(chain: Chain, native: int, price: int) -> int:
    """Native value that a native sweep moves (balance - fee), or 0: the value must be at least 10 fees (the
    signer refuses a fee over 10%). Less stays on the address (stranded)."""
    fee = chain.native_gas_limit * price
    return native - fee if native - fee >= NATIVE_MIN_FEES * fee else 0
