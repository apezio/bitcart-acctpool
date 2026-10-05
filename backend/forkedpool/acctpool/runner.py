"""The worker loops (SPEC-v4 2.B): one leader among the worker processes (a Postgres advisory lock held on a
connection of its own), and one asyncio task per loop, each with its own error barrier and backoff."""

import asyncio
import json
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text

from api.logging import get_exception_message, get_logger
from api.services.coins import CoinService

from . import daemon, models, readyfill
from .constants import CHAINS, NATIVE, STATE_LEADER, STATE_SIGNER, TEST_CHAIN_ENV, Event, Status
from .db import lock_key, short_error
from .payout import Payouts
from .signer_client import SignerClient, SignerError

if TYPE_CHECKING:
    from .plugin import Plugin

logger = get_logger(__name__)

LEADER_KEY = lock_key("leader")
TIMES_ENV = "ACCTPOOL_TEST_TIMES"  # tests only, and only together with ACCTPOOL_TEST_CHAIN
RETIRED_CHECK_DAYS = 30
UNSWEPT_HIGH = 100  # addresses waiting for their payout


@dataclass
class Times:
    payout: float = 30
    reconcile: float = 60
    watch: float = 600
    ready: float = 60
    state: float = 60
    replace_after: float = 600
    retry: float = 10

    @classmethod
    def from_env(cls) -> "Times":
        times = cls()
        if os.environ.get(TEST_CHAIN_ENV):
            for name, value in json.loads(os.environ.get(TIMES_ENV) or "{}").items():
                setattr(times, name, float(value))
        return times


class Runner:
    def __init__(self, plugin: "Plugin") -> None:
        self.plugin = plugin
        self.times = Times.from_env()
        self.signer = SignerClient()
        self.payouts = Payouts(plugin, self.signer, self.times.replace_after)
        self.worker = f"{socket.gethostname()}:{os.getpid()}"
        self.main: asyncio.Task[None] | None = None
        self.loops: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if self.main is None:
            self.main = asyncio.create_task(self.lead())

    async def stop(self) -> None:
        if self.main is not None:
            self.main.cancel()  # its `finally` stops the loops
            await asyncio.gather(self.main, return_exceptions=True)
            self.main = None
        await self.signer.close()

    async def lead(self) -> None:
        db = await self.plugin.db()
        while True:
            try:
                async with db.engine.connect() as connection:
                    await connection.execution_options(isolation_level="AUTOCOMMIT")
                    if (await connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_KEY})).scalar():
                        try:
                            self.start_loops()
                            while all(not task.done() for task in self.loops):
                                await asyncio.sleep(self.times.retry)
                                await connection.execute(text("SELECT 1"))  # a lost connection = a lost lock
                        finally:
                            for task in self.loops:
                                task.cancel()
                            await asyncio.gather(*self.loops, return_exceptions=True)
                            # closed, not given back to the pool: the server frees the lock at once
                            await connection.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"acctpool: leader: {get_exception_message(e)}")
            await asyncio.sleep(self.times.retry)

    def start_loops(self) -> None:
        jobs: list[tuple[str, float, Callable[[], Awaitable[Any]]]] = [
            ("state", self.times.state, self.state_round),
            ("ready", self.times.ready, self.ready_round),
            ("watch", self.times.watch, self.watch_round),
        ]
        for chain in CHAINS.values():
            jobs.append((f"reconcile {chain.name}", self.times.reconcile, lambda c=chain: self.reconcile_round(c)))
            jobs.append((f"payout {chain.name}", self.times.payout, lambda c=chain: self.payout_round(c)))
        self.loops = [asyncio.create_task(self.loop(name, interval, job)) for name, interval, job in jobs]

    async def loop(self, name: str, interval: float, job: Callable[[], Awaitable[Any]]) -> None:
        failures = 0
        while True:
            try:
                await job()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                logger.error(f"acctpool: loop {name}: {get_exception_message(e)}")
                await (await self.plugin.db()).event(
                    Event.ERROR, None, None, {"loop": name, "error": short_error(e)}, every=600
                )
            await asyncio.sleep(min(interval * 2**failures, 600) if failures else interval)

    async def chains_in_use(self) -> list[Any]:
        """Chains with a pool whose daemon Bitcart has (BITCART_CRYPTOS)."""
        async with (await self.plugin.db()).session() as session:
            names = (await session.execute(select(models.AcctpoolPool.chain).distinct())).scalars().all()
        enabled = (await self.plugin.container.get(CoinService)).cryptos
        return [CHAINS[name] for name in names if name in CHAINS and CHAINS[name].currency in enabled]

    async def state_round(self) -> None:
        db = await self.plugin.db()
        await db.set_state(STATE_LEADER, {"worker": self.worker})
        try:
            status = await self.signer.status()
            await db.set_state(STATE_SIGNER, status)
        except SignerError as e:
            await db.event(Event.SIGNER_DOWN, None, None, {"detail": str(e)}, every=600)
            return
        addresses = models.AcctpoolAddress
        async with db.session() as session:
            waiting = (
                await session.execute(
                    select(func.count()).where(addresses.status.in_((Status.PENDING_PAYOUT, Status.IN_PAYOUT)))
                )
            ).scalar_one()
        if waiting > UNSWEPT_HIGH:
            await db.event(Event.UNSWEPT_HIGH, None, None, {"addresses": waiting}, every=3600)
        fee_wallet = (status.get("fee_wallets") or {}).get("evm")
        for chain in await self.chains_in_use() if fee_wallet else []:
            srv = await daemon.server(self.plugin.container, chain)
            price = int(await srv.getfeerate())
            # the float must pay about 10 USDT payouts (funding + sweep)
            if await daemon.balance(srv, chain, fee_wallet, NATIVE) < 10 * (chain.token_gas_limit + 21000) * price:
                await db.event(Event.FEE_WALLET_LOW, chain.name, fee_wallet, {}, every=3600)

    async def ready_round(self) -> None:
        await readyfill.fill(self.plugin, self.signer)
        await readyfill.check(self.plugin, self.signer)

    async def watch_round(self) -> None:
        await self.plugin.detector.close_lost_invoices()
        for chain in await self.chains_in_use():
            await self.plugin.detector.load_chain(chain)

    async def payout_round(self, chain: Any) -> None:
        if chain in await self.chains_in_use():
            await self.payouts.round(chain)

    async def reconcile_round(self, chain: Any) -> None:
        if chain not in await self.chains_in_use():
            return
        detector = self.plugin.detector
        await detector.reconcile(chain)
        db = await self.plugin.db()
        key = f"retired_check:{chain.name}"
        if await db.get_state(key, RETIRED_CHECK_DAYS * 86400) is None:
            await detector.reconcile(chain, unwatched=True)
            await db.set_state(key, {})
