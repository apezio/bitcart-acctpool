"""Helpers that make Bitcart objects through Bitcart's own API, as a client does."""

import secrets
from typing import Any

import fakes
from httpx import AsyncClient
from sqlalchemy import select, text

PASSWORD = "test12345"  # a test account in a test database


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def post(client: AsyncClient, path: str, token: str | None, **body: Any) -> dict[str, Any]:
    response = await client.post(path, json=body, headers=auth(token) if token else {})
    assert response.status_code == 200, (path, response.status_code, response.text)
    return response.json()


async def superuser_token(client: AsyncClient) -> str:
    email = f"admin-{secrets.token_hex(4)}@example.com"
    user = await post(client, "/users", None, email=email, password=PASSWORD, is_superuser=True)
    assert user["is_superuser"] is True
    return (await post(client, "/token", None, email=email, password=PASSWORD, permissions=["full_control"]))["access_token"]


async def token_with(client: AsyncClient, admin_token: str, permissions: list[str], superuser: bool) -> str:
    email = f"user-{secrets.token_hex(4)}@example.com"
    await post(client, "/users", admin_token, email=email, password=PASSWORD, is_superuser=superuser)
    return (await post(client, "/token", None, email=email, password=PASSWORD, permissions=permissions))["access_token"]


def merchant_address(currency: str = "matic") -> str:
    return "0x" + secrets.token_hex(20)


async def token_wallet(client: AsyncClient, token: str, currency: str, **extra: Any) -> dict[str, Any]:
    return await post(
        client,
        "/wallets",
        token,
        name=f"usdt-{currency}-{secrets.token_hex(3)}",
        xpub=merchant_address(currency),
        currency=currency,
        contract=fakes.USDT[currency],
        **extra,
    )


async def native_wallet(client: AsyncClient, token: str, currency: str, **extra: Any) -> dict[str, Any]:
    name = f"native-{currency}-{secrets.token_hex(3)}"
    return await post(client, "/wallets", token, name=name, xpub=merchant_address(currency), currency=currency, **extra)


async def store(client: AsyncClient, token: str, wallets: list[str], **checkout: Any) -> dict[str, Any]:
    result = await post(client, "/stores", token, name=f"store-{secrets.token_hex(3)}", wallets=wallets)
    if checkout:
        response = await client.patch(f"/stores/{result['id']}/checkout_settings", json=checkout, headers=auth(token))
        assert response.status_code == 200, response.text
        result = response.json()
    return result


async def pool(client: AsyncClient, token: str, wallet_id: str, chain: str, **extra: Any) -> dict[str, Any]:
    body = {"wallet_id": wallet_id, "chain": chain, "store": "teststore", "enabled": True}
    return await post(client, "/plugins/acctpool/pools", token, **{**body, **extra})


async def invoice(client: AsyncClient, token: str, store_id: str, price: Any = 5, **extra: Any) -> dict[str, Any]:
    return await post(client, "/invoices", token, store_id=store_id, price=price, currency="USD", **extra)


async def add_ready(bitcart: Any, signer: Any, count: int, store: str = "teststore") -> list[str]:
    """Ready addresses straight into the table, as the derive-ahead job leaves them (the fake signer's)."""
    from modules.forkedpool.acctpool import models

    db = await bitcart.plugin.db()
    addresses = [signer.address(store, i) for i in range(count)]
    async with db.session() as session, session.begin():
        session.add_all(models.AcctpoolAddress(store=store, index=i, address=a) for i, a in enumerate(addresses))
    return addresses


async def rows(bitcart: Any, model: Any, *where: Any) -> list[Any]:
    db = await bitcart.plugin.db()
    async with db.session() as session:
        return list((await session.execute(select(model).where(*where).order_by(*model.__table__.primary_key))).scalars())


async def events(bitcart: Any, kind: str) -> list[Any]:
    from modules.forkedpool.acctpool import models

    return await rows(bitcart, models.AcctpoolEvent, models.AcctpoolEvent.kind == kind)


async def shop(client: AsyncClient, token: str, **extra: Any) -> dict[str, Any]:
    """A store with a USDT and a native wallet on the anvil chain (eth daemon), both in pool mode, and a USDT
    wallet on polygon without a pool."""
    usdt = await token_wallet(client, token, "eth")
    native = await native_wallet(client, token, "eth")
    plain = await token_wallet(client, token, "matic")
    store_row = await store(client, token, [usdt["id"], native["id"], plain["id"]])
    usdt_pool = await pool(client, token, usdt["id"], "anvil", **extra)
    native_pool = await pool(client, token, native["id"], "anvil", **extra)
    return {"store": store_row, "usdt": usdt, "native": native, "plain": plain, "pools": [usdt_pool, native_pool]}


async def sql(bitcart: Any, statement: str, **params: Any) -> Any:
    db = await bitcart.plugin.db()
    async with db.session() as session, session.begin():
        return await session.execute(text(statement), params)


async def invoice_row(bitcart: Any, invoice_id: str) -> dict[str, Any]:
    result = await sql(bitcart, "SELECT * FROM invoices WHERE id = :id", id=invoice_id)
    return dict(result.mappings().one())


async def method_rows(bitcart: Any, invoice_id: str) -> list[dict[str, Any]]:
    result = await sql(bitcart, "SELECT * FROM paymentmethods WHERE invoice_id = :id ORDER BY id", id=invoice_id)
    return [dict(row) for row in result.mappings().all()]


async def set_status(bitcart: Any, invoice_id: str, status: str) -> None:
    """A status change through Bitcart's own InvoiceService (the plugin hooks run, as in production)."""
    from dishka import Scope

    from api.services.crud.invoices import InvoiceService

    async with bitcart.container(scope=Scope.REQUEST) as container:
        service = await container.get(InvoiceService)
        await service.update_status(await service.get(invoice_id), status)


class Instance:
    """The coin object that the bitcart SDK passes to an event handler."""

    def __init__(self, coin: str) -> None:
        self.coin_name = coin.upper()


async def event(
    bitcart: Any, coin: str, tx: str, to: str, amount: str, contract: str | None, sender: str = "0x" + "ee" * 20
) -> None:
    """A new_transaction event of the stock daemon, as the SDK delivers it to the handler."""
    await bitcart.plugin.detector.on_transaction(Instance(coin), "new_transaction", tx, sender, to, amount, contract)


async def address_row(bitcart: Any, invoice_id: str) -> Any:
    from modules.forkedpool.acctpool import models

    (row,) = await rows(bitcart, models.AcctpoolAddress, models.AcctpoolAddress.invoice_id == invoice_id)
    return row
