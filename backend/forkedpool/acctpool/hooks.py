"""Bitcart filters and hooks (SPEC-v4 4.4). Every function here catches its own errors: a fault in the plugin
leaves Bitcart with the stock flow, never with a broken invoice."""

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from api.invoices import InvoiceStatus
from api.logging import get_exception_message, get_logger

from . import daemon, models
from .allocation import allocate
from .constants import ALIVE_SECONDS, BY_CURRENCY, CHAINS, LOOKUP_PREFIX, META_KEY, NATIVE, STATE_LEADER, USDT, Event
from .db import short_error
from .lifecycle import CLOSING, close_invoice

if TYPE_CHECKING:
    from .plugin import Plugin

logger = get_logger(__name__)

# electrum request statuses that api/invoices.py STATUS_MAPPING knows
PR_UNPAID, PR_PAID, PR_UNCONFIRMED = 0, 3, 7
FALLBACK_EVENT_SECONDS = 600
DAEMON_TIMEOUT = 10  # seconds for the daemon calls of one allocation; a slower daemon gives the stock flow


def pending_request() -> dict[str, Any]:
    return {"status": PR_UNPAID, "tx_hashes": [], "sent_amount": "0", "confirmations": 0}


def payment_url(contract: str | None, chain_id: int, address: str, units: int) -> str:
    """The same text as daemons/eth.py get_payment_uri makes, for a token and for the native coin."""
    if contract:
        return f"ethereum:{contract}@{chain_id}/transfer?address={address}" + (f"&uint256={units}" if units else "")
    return f"ethereum:{address}@{chain_id}" + (f"?value={units}" if units else "")


def lookup(address_id: int, chain: str, asset: str) -> str:
    return f"{LOOKUP_PREFIX}{address_id}:{chain}:{asset}"


def parse_lookup(value: Any) -> tuple[int, str, str] | None:
    if not isinstance(value, str) or not value.startswith(LOOKUP_PREFIX):
        return None
    address_id, chain, asset = value[len(LOOKUP_PREFIX) :].split(":")
    return int(address_id), chain, asset


def wallet_asset(chain: Any, contract: str | None) -> str | None:
    if not contract:
        return NATIVE
    return USDT if contract.lower() == chain.usdt.lower() else None


class Hooks:
    def __init__(self, plugin: "Plugin") -> None:
        self.plugin = plugin

    def register(self) -> None:
        context = self.plugin.context
        context.register_filter("create_payment_method", self.create_payment_method)
        context.register_filter("post_create_payment_method", self.post_create_payment_method)
        context.register_filter("get_request", self.get_request)
        context.register_hook("invoice_status", self.invoice_status)
        context.register_hook("invoice_complete", self.invoice_complete)
        context.register_hook("invoice_expired", self.invoice_expired)
        context.register_hook("check_pending", self.check_pending)

    async def create_payment_method(
        self, method: Any, wallet: Any, coin: Any, amount: Any, invoice: Any, product: Any, store: Any, lightning: Any
    ) -> Any:
        if method is not None or lightning:
            return method
        try:
            result, reason = await self.pool_method(wallet, Decimal(amount), invoice)
        except Exception as e:
            result, reason = None, short_error(e)
            logger.error(f"acctpool: create_payment_method failed for invoice {invoice.id}: {get_exception_message(e)}")
        if result is None and reason is not None:
            db = await self.plugin.db()
            detail = {"invoice": invoice.id, "wallet": wallet.id, "reason": reason}
            await db.event(Event.STOCK_FALLBACK, None, None, detail, every=FALLBACK_EVENT_SECONDS)
        return result if result is not None else method

    async def pool_method(self, wallet: Any, amount: Decimal, invoice: Any) -> tuple[dict[str, Any] | None, str | None]:
        """(method, None) for pool mode; (None, reason) for the stock flow; (None, None) when no event is due."""
        db = await self.plugin.db()
        pools = models.AcctpoolPool
        async with db.session() as session:
            pool = (await session.execute(select(pools).where(pools.wallet_id == wallet.id))).scalar_one_or_none()
            if pool is None or not pool.enabled:
                return None, None
            # the chains of the store's pools, read here: the allocation transaction opens no second connection
            names = await session.execute(select(pools.chain).distinct().where(pools.store == pool.store, pools.enabled))
            store_chains = [CHAINS[name] for name in names.scalars() if name in CHAINS]
        chain = CHAINS.get(pool.chain)
        if chain is None or str(wallet.currency).lower() != chain.currency:
            return None, "unknown_chain"
        asset = wallet_asset(chain, wallet.contract)
        if asset != pool.asset:
            return None, "wallet_asset"
        settings = await db.settings()
        # the cap is in USD; for another invoice currency a rate would decide, and the stock flow is the safe side
        if str(invoice.currency).upper() != "USD" or Decimal(invoice.price) > Decimal(settings["max_invoice_usd"]):
            return None, "over_cap"
        if await db.get_state(STATE_LEADER, ALIVE_SECONDS) is None:
            return None, "engine_not_alive"  # nothing would watch the address or pay it out
        try:
            used = lambda address: asyncio.wait_for(self.used(store_chains, address), DAEMON_TIMEOUT)  # noqa: E731
            address, reason = await allocate(db, invoice.id, pool.store, used)
            if address is None:
                return None, reason
            await asyncio.wait_for(self.plugin.detector.load_addresses(chain, [address.address]), DAEMON_TIMEOUT)
        except TimeoutError:
            return None, "daemon_slow"
        decimals = daemon.decimals(chain, asset)
        units = daemon.units(amount, decimals)
        contract = chain.usdt if asset == USDT else None
        meta = {"address_id": address.id, "chain": chain.name, "asset": asset, "decimals": decimals}
        return {
            "payment_address": address.address,
            "payment_url": payment_url(contract, chain.chain_id, address.address, units),
            "lookup_field": lookup(address.id, chain.name, asset),
            "metadata": {META_KEY: {**meta, "amount_units": str(units)}},
        }, None

    async def used(self, chains: list[Any], address: str) -> bool:
        """The chain knows the address: a nonce or a balance on a chain of the store's enabled pools."""
        for chain in chains:
            srv = await daemon.server(self.plugin.container, chain)
            if int(await srv.getnonce(address=address, pending=True)) > 0:
                return True
            if any([await daemon.balance(srv, chain, address, asset) > 0 for asset in (NATIVE, USDT)]):
                return True
        return False

    async def post_create_payment_method(self, data: Any, invoice: Any, wallet: Any) -> Any:
        # the customer pays to an address of its own: no sender address to ask for
        if isinstance(data, dict) and parse_lookup(data.get("lookup_field")) is not None:
            data["user_address"] = data["payment_address"]
        return data

    async def get_request(self, value: Any, coin: Any, method: Any) -> Any:
        if value or parse_lookup(getattr(method, "lookup_field", None)) is None:
            return value
        try:
            return await self.request_data(method)
        except Exception as e:
            # Never None for a pool method: Bitcart would ask the stock daemon for a request it does not know
            logger.error(f"acctpool: get_request failed for {method.lookup_field}: {get_exception_message(e)}")
            return pending_request()

    async def request_data(self, method: Any) -> dict[str, Any]:
        """What a stock daemon answers for its request: from the deposit rows of this method, confirmations of the
        newest deposit (the one in the highest block: the fewest confirmations) from the daemon."""
        address_id, chain_name, asset = parse_lookup(method.lookup_field)  # type: ignore[misc]
        chain = CHAINS[chain_name]
        table = models.AcctpoolDeposit
        db = await self.plugin.db()
        async with db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(table)
                        .where(table.address_id == address_id, table.chain == chain_name, table.asset == asset)
                        .where(table.invoice_id == method.invoice_id, table.late.is_(False))
                        .order_by(table.id)
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return pending_request()
        srv = await daemon.server(self.plugin.container, chain)
        confirmations = min([await daemon.confirmations(srv, row) for row in rows])
        sent = sum((row.amount for row in rows), Decimal(0))
        info = (getattr(method, "meta", None) or {}).get(META_KEY) or {}
        expected = daemon.coins(int(info.get("amount_units", 0)), int(info.get("decimals", 0)))
        status = PR_UNPAID if sent < expected else (PR_PAID if confirmations >= 1 else PR_UNCONFIRMED)
        return {
            "status": status,
            "tx_hashes": [row.tx_hash for row in rows if row.source == "event"],
            "sent_amount": format(sent.normalize(), "f"),
            "confirmations": confirmations,
        }

    async def invoice_status(self, invoice: Any, status: Any) -> None:
        await self.invoice_closed(invoice, str(status))

    async def invoice_complete(self, invoice: Any) -> None:
        await self.invoice_closed(invoice, InvoiceStatus.COMPLETE)

    async def invoice_expired(self, invoice: Any) -> None:
        await self.invoice_closed(invoice, InvoiceStatus.EXPIRED)

    async def invoice_closed(self, invoice: Any, status: str) -> None:
        if status not in CLOSING:
            return
        try:
            db = await self.plugin.db()
            async with db.session() as session, session.begin():
                await close_invoice(session, invoice.id)
        except Exception as e:
            logger.error(f"acctpool: status hook failed for invoice {invoice.id}: {get_exception_message(e)}")

    async def check_pending(self, currency: str) -> None:
        """Bitcart runs this after the daemon websocket (re)connects: diskless wallets are gone after a daemon
        restart, so load them again (in the background: 3,000 addresses take about 20 s)."""
        chain = BY_CURRENCY.get(str(currency).lower())
        if chain is not None:
            self.plugin.detector.schedule_load(chain)
