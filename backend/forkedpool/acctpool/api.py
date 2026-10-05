"""REST API (SPEC-v4 4.8) under /api/plugins/acctpool. Every route but the page needs the server_management
scope (superusers only). Admin actions only set flags; the worker does the work. Balances come from the stock
daemon on demand; no route reaches the signer."""

import os
from datetime import datetime
from decimal import Decimal
from typing import Any

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaRoute
from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, update

from api import models as bitcart_models
from api import utils
from api.constants import AuthScopes
from api.db import AsyncSession
from api.services.coins import CoinService

from . import daemon, models, schemas
from .constants import (
    ALIVE_SECONDS,
    CHAINS,
    DEFAULT_SETTINGS,
    NATIVE,
    STATE_LEADER,
    STATE_SETTINGS,
    STATE_SIGNER,
    USDT,
    State,
    Status,
)
from .hooks import wallet_asset
from .lifecycle import CLOSING, close_invoice
from .payout import MAX_REPLACEMENTS, nonce_group

router = APIRouter(route_class=DishkaRoute)
ADMIN = Security(utils.authorization.auth_dependency, scopes=[AuthScopes.SERVER_MANAGEMENT])
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pool.html")
PAGE_HEADERS = {
    # the page has its script and style inside the file and talks to this API only
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def plain(row: Any) -> dict[str, Any]:
    """A table row as JSON: amounts as text (a float cannot hold them), times as ISO text."""
    out = {}
    for column in row.__table__.columns:
        value = getattr(row, column.key)
        out[column.key] = format(value, "f") if isinstance(value, Decimal) else (
            value.isoformat() if isinstance(value, datetime) else value
        )  # fmt: skip
    out.pop("raw_tx", None)
    return out


async def state(session: AsyncSession, key: str, max_age: float | None = None) -> dict[str, Any] | None:
    table = models.AcctpoolState
    age = func.extract("epoch", func.now() - table.updated)
    row = (await session.execute(select(table.value, age).where(table.key == key))).first()
    return None if row is None or (max_age is not None and float(row[1]) > max_age) else row[0]


async def balances(coins: CoinService, chain_name: str, address: str) -> dict[str, str | None]:
    chain = CHAINS[chain_name]
    try:
        srv = (await coins.get_coin(chain.currency)).server
        return {
            asset: format(
                daemon.coins(await daemon.balance(srv, chain, address, asset), daemon.decimals(chain, asset)).normalize(), "f"
            )
            for asset in (NATIVE, USDT)
        }
    except Exception:
        return {NATIVE: None, USDT: None}


@router.get("/ui", include_in_schema=False)
def page() -> HTMLResponse:
    # no data in the page: it asks for the token and sends it with every call
    with open(PAGE, encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers=PAGE_HEADERS)


@router.get("/status")
async def get_status(session: FromDishka[AsyncSession], coins: FromDishka[CoinService], user: Any = ADMIN) -> Any:
    signer = await state(session, STATE_SIGNER) or {}
    fee_wallet = (signer.get("fee_wallets") or {}).get("evm")
    pools = (await session.execute(select(models.AcctpoolPool).order_by(models.AcctpoolPool.id))).scalars().all()
    chains = sorted({pool.chain for pool in pools if pool.chain in CHAINS})
    table = models.AcctpoolAddress
    counts = (await session.execute(select(table.store, table.status, func.count()).group_by(table.store, table.status))).all()
    return {
        "engine_alive": await state(session, STATE_LEADER, ALIVE_SECONDS) is not None,
        "signer": {key: signer.get(key) for key in ("keystore", "seed_id", "config_sha256", "fee_wallets", "stores")},
        "settings": {**DEFAULT_SETTINGS, **(await state(session, STATE_SETTINGS) or {})},
        "fee_wallet": {name: await balances(coins, name, fee_wallet) if fee_wallet else None for name in chains},
        "addresses": [{"store": store, "status": status, "count": n} for store, status, n in counts],
        "pools": [plain(pool) for pool in pools],
    }


@router.post("/pools")
async def create_pool(data: schemas.PoolCreate, session: FromDishka[AsyncSession], user: Any = ADMIN) -> Any:
    wallet = await session.get(bitcart_models.Wallet, data.wallet_id)
    chain = CHAINS.get(data.chain)
    if wallet is None or chain is None or wallet.currency.lower() != chain.currency:
        raise HTTPException(422, "Unknown wallet or chain, or the wallet currency is not the chain's")
    asset = wallet_asset(chain, wallet.contract)
    if asset is None:
        raise HTTPException(422, "The wallet contract is not the USDT of the chain")
    pools = models.AcctpoolPool
    taken = select(pools.id).where(
        (pools.wallet_id == wallet.id) | ((pools.store == data.store) & (pools.chain == chain.name) & (pools.asset == asset))
    )
    if (await session.execute(taken)).first():
        raise HTTPException(422, "This wallet, or this store key with this chain and asset, has a pool already")
    pool = pools(**data.model_dump(), asset=asset)
    session.add(pool)
    await session.flush()
    return plain(pool)


@router.patch("/pools/{pool_id}")
async def update_pool(pool_id: int, data: schemas.PoolUpdate, session: FromDishka[AsyncSession], user: Any = ADMIN) -> Any:
    pool = await session.get(models.AcctpoolPool, pool_id, with_for_update=True)
    if pool is None:
        raise HTTPException(404, "No such pool")
    for key, value in data.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(pool, key, value)
    await session.flush()
    return plain(pool)


@router.put("/settings")
async def put_settings(data: schemas.Settings, session: FromDishka[AsyncSession], user: Any = ADMIN) -> Any:
    current = {**DEFAULT_SETTINGS, **(await state(session, STATE_SETTINGS) or {})}
    current.update({k: v if isinstance(v, int) else str(v) for k, v in data.model_dump(exclude_none=True).items()})
    table = models.AcctpoolState
    row = await session.get(table, STATE_SETTINGS, with_for_update=True)
    if row is None:
        session.add(table(key=STATE_SETTINGS, value=current))
    else:
        row.value, row.updated = current, func.now()
    return current


@router.get("/addresses")
async def get_addresses(
    session: FromDishka[AsyncSession],
    coins: FromDishka[CoinService],
    status: str | None = None,
    chain: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: Any = ADMIN,
) -> Any:
    table = models.AcctpoolAddress
    query = select(table)
    if status is not None:
        query = query.where(table.status == status)
    total = (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (await session.execute(query.order_by(table.id.desc()).limit(limit).offset(offset))).scalars().all()
    result = [plain(row) for row in rows]
    if chain in CHAINS:  # balances on that chain, from the daemon
        for row in result:
            row["balances"] = await balances(coins, chain, row["address"]) if row["status"] != Status.READY else None
    return {"count": total, "result": result}


@router.get("/addresses/{address_id}")
async def get_address(address_id: int, session: FromDishka[AsyncSession], user: Any = ADMIN) -> Any:
    address = await session.get(models.AcctpoolAddress, address_id)
    if address is None:
        raise HTTPException(404, "No such address")
    related = {}
    for name, table in (("deposits", models.AcctpoolDeposit), ("payouts", models.AcctpoolPayout)):
        rows = (await session.execute(select(table).where(table.address_id == address_id).order_by(table.id))).scalars()
        related[name] = [plain(row) for row in rows]
    return {**plain(address), **related}


@router.get("/events")
async def get_events(
    session: FromDishka[AsyncSession], kind: str | None = None, limit: int = Query(100, ge=1, le=1000), user: Any = ADMIN
) -> Any:
    table = models.AcctpoolEvent
    query = select(table) if kind is None else select(table).where(table.kind == kind)
    return [plain(row) for row in (await session.execute(query.order_by(table.id.desc()).limit(limit))).scalars()]


@router.post("/addresses/{address_id}/withdraw")
async def withdraw(address_id: int, session: FromDishka[AsyncSession], user: Any = ADMIN) -> Any:
    address = await session.get(models.AcctpoolAddress, address_id, with_for_update=True)
    if address is None:
        raise HTTPException(404, "No such address")
    if address.status == Status.IN_INVOICE:  # a deleted or closed invoice closes the address now
        invoice = await session.get(bitcart_models.Invoice, address.invoice_id)
        if invoice is None or invoice.status in CLOSING:
            await close_invoice(session, address.invoice_id)
            await session.flush()
            await session.refresh(address)
    if address.status in (Status.READY, Status.IN_INVOICE):
        raise HTTPException(409, "The address is not used, or its invoice is still open")
    address.withdraw_requested = True
    return plain(address)


@router.post("/withdraw-batch")
async def withdraw_batch(session: FromDishka[AsyncSession], user: Any = ADMIN) -> Any:
    table = models.AcctpoolAddress
    done = await session.execute(
        update(table)
        .where(table.status.in_((Status.PENDING_PAYOUT, Status.IN_PAYOUT)), table.withdraw_requested.is_(False))
        .values(withdraw_requested=True)
    )
    return {"requested": done.rowcount}


@router.post("/payouts/{payout_id}/retry")
async def retry(payout_id: int, session: FromDishka[AsyncSession], coins: FromDishka[CoinService], user: Any = ADMIN) -> Any:
    """One more replacement (same nonce, fee +20%, the signer's `replaces`) for an open payout that no block takes,
    also a stuck one: the worker makes it in its next round. Closes nothing. Refused when an attempt of the nonce
    has a receipt (the worker finishes the group with it) or when no attempt is sent yet."""
    table = models.AcctpoolPayout
    row = await session.get(table, payout_id)
    if row is None or row.state not in State.OPEN:
        raise HTTPException(409, "No such open payout")
    query = select(table).where(nonce_group(table, row)).order_by(table.id)
    srv = (await coins.get_coin(CHAINS[row.chain].currency)).server
    await daemon.head(srv)  # the daemon must answer: an error below means "no receipt", not "no daemon"
    for item in (await session.execute(query)).scalars().all():  # no row is locked during the daemon calls
        try:
            mined = item.tx_hash is not None and await srv.get_tx_status(tx=item.tx_hash)
        except Exception:
            mined = False
        if mined:
            raise HTTPException(409, "A transaction of this nonce is in a block: the worker finishes it")
    # then the rows are locked and read again: the worker may have changed them meanwhile
    group = (await session.execute(query.with_for_update().execution_options(populate_existing=True))).scalars().all()
    sent = [item for item in group if item.state in (State.SIGNED, State.BROADCAST)]
    if row.state not in State.OPEN or not sent or any(item.state == State.PLANNED for item in group):
        raise HTTPException(409, "Nothing is sent yet, or a replacement waits: the worker goes on with it")
    followed = sent[-1]
    followed.attempts, followed.error = min(int(followed.attempts), MAX_REPLACEMENTS - 1), None
    await session.flush()
    return plain(followed)
