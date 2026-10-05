"""Pool invoices made through Bitcart's own API (POST /invoices), with the plugin loaded by the stock registry."""

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import fakes
import helpers
import pytest
from conftest import Bitcart
from httpx import AsyncClient

from modules.forkedpool.acctpool import hooks, models

pytestmark = pytest.mark.anyio


def by_wallet(invoice: dict[str, Any], shop: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    payments = {payment["wallet_id"]: payment for payment in invoice["payments"]}
    return payments[shop["usdt"]["id"]], payments[shop["native"]["id"]], payments[shop["plain"]["id"]]


async def test_one_address_per_invoice(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any, daemons: dict[str, fakes.FakeDaemon]
) -> None:
    shop = await helpers.shop(client, token)
    ready = await helpers.add_ready(bitcart, signer, 3)
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    usdt, native, plain = by_wallet(invoice, shop)
    address = ready[0]
    address_id = (await helpers.address_row(bitcart, invoice["id"])).id
    # USDT and the native coin of the invoice share ONE address, and no sender address is asked for
    assert usdt["payment_address"] == native["payment_address"] == usdt["user_address"] == native["user_address"] == address
    assert usdt["payment_url"] == f"ethereum:{fakes.USDT['eth']}@31337/transfer?address={address}&uint256=5000000"
    assert native["payment_url"] == f"ethereum:{address}@31337?value=2500000000000000"  # 5 USD at 2000 USD
    assert usdt["lookup_field"] == f"acctpool:{address_id}:anvil:usdt"
    assert native["lookup_field"] == f"acctpool:{address_id}:anvil:native"
    assert usdt["metadata"]["acctpool"] == {
        "address_id": address_id, "chain": "anvil", "asset": "usdt", "decimals": 6, "amount_units": "5000000"
    }  # fmt: skip
    assert native["metadata"]["acctpool"]["asset"] == "native"
    # a wallet without a pool: the stock flow
    assert plain["payment_address"] == shop["plain"]["xpub"] and plain["user_address"] is None
    assert not daemons["eth"].called("add_request")
    # the watch-only wallets are in the daemon at once (both assets)
    loads = daemons["eth"].called("batch_load")
    assert loads[-1]["wallets"] == [
        {"xpub": address, "diskless": True}, {"xpub": address, "contract": fakes.USDT["eth"], "diskless": True}
    ]  # fmt: skip
    rows = await helpers.rows(bitcart, models.AcctpoolAddress)
    assert [(r.address, r.status, r.invoice_id) for r in rows] == [
        (ready[0], "in_invoice", invoice["id"]), (ready[1], "ready", None), (ready[2], "ready", None)
    ]  # fmt: skip
    # the get_request filter answers for a pool method: nothing paid yet
    request = await bitcart.plugin.hooks.get_request(None, None, _method(usdt, invoice))
    assert request == {"status": 0, "tx_hashes": [], "sent_amount": "0", "confirmations": 0}


def _method(payment: dict[str, Any], invoice: dict[str, Any]) -> Any:
    class Method:
        lookup_field = payment["lookup_field"]
        invoice_id = invoice["id"]
        meta = payment["metadata"]

    return Method()


async def test_invoices_at_the_same_time_get_different_addresses(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any
) -> None:
    shop = await helpers.shop(client, token)
    ready = await helpers.add_ready(bitcart, signer, 6)
    invoices = await asyncio.gather(*(helpers.invoice(client, token, shop["store"]["id"], price=5) for _ in range(5)))
    addresses = [by_wallet(invoice, shop)[0]["payment_address"] for invoice in invoices]
    assert len(set(addresses)) == 5 and set(addresses) <= set(ready)
    for invoice, address in zip(invoices, addresses, strict=True):
        assert by_wallet(invoice, shop)[1]["payment_address"] == address  # the native method of the same invoice


async def test_many_invoices_at_once_do_not_wait_for_the_pool(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any, daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """M3: the allocation holds one connection of the plugin's pool (2 + 8) and asks the daemon without a second
    one: 30 pool methods at once all get their address, without waiting for the pool timeout (10 s)."""
    shop = await helpers.shop(client, token)
    ready = await helpers.add_ready(bitcart, signer, 30)
    daemons["eth"].delay["getnonce"] = 0.2
    wallet = SimpleNamespace(id=shop["usdt"]["id"], currency="eth", contract=fakes.USDT["eth"])
    started = time.monotonic()
    methods = await asyncio.gather(*(
        bitcart.plugin.hooks.create_payment_method(
            None, wallet, None, "5", SimpleNamespace(id=f"m3-{i}", currency="USD", price="5"), None, None, False
        )
        for i in range(30)
    ))  # fmt: skip
    assert time.monotonic() - started < 8
    assert sorted(method["payment_address"] for method in methods) == sorted(ready)
    assert await helpers.events(bitcart, "stock_fallback") == []


@pytest.mark.parametrize("method", ["getnonce", "batch_load"])
async def test_slow_daemon_takes_the_stock_flow(
    bitcart: Bitcart,
    client: AsyncClient,
    token: str,
    signer: Any,
    daemons: dict[str, fakes.FakeDaemon],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """M3: the used-address check and the watch-only load have a time limit; a slower daemon gives the stock flow."""
    shop = await helpers.shop(client, token)
    await helpers.add_ready(bitcart, signer, 2)
    monkeypatch.setattr(hooks, "DAEMON_TIMEOUT", 0.3, raising=False)
    daemons["eth"].delay[method] = 1.5
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    usdt, native, _ = by_wallet(invoice, shop)
    assert usdt["payment_address"] == shop["usdt"]["xpub"] and native["payment_address"] == shop["native"]["xpub"]
    assert [e.detail["reason"] for e in await helpers.events(bitcart, "stock_fallback")] == ["daemon_slow"]


@pytest.mark.parametrize("case", ["over_cap", "engine_not_alive", "no_ready_address", "disabled"])
async def test_stock_flow(bitcart: Bitcart, client: AsyncClient, token: str, signer: Any, case: str) -> None:
    shop = await helpers.shop(client, token)
    await helpers.add_ready(bitcart, signer, 0 if case == "no_ready_address" else 2)
    price = 5
    if case == "over_cap":
        price = 501
    elif case == "engine_not_alive":
        await helpers.sql(bitcart, "UPDATE plugin_acctpool_state SET updated = now() - interval '6 minutes'")
    elif case == "disabled":
        await helpers.sql(bitcart, "UPDATE plugin_acctpool_pools SET enabled = false")
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=price)
    usdt, native, _ = by_wallet(invoice, shop)
    assert usdt["payment_address"] == shop["usdt"]["xpub"] and usdt["metadata"] == {}
    assert native["payment_address"] == shop["native"]["xpub"]
    events = await helpers.events(bitcart, "stock_fallback")
    if case == "disabled":
        assert events == []
    else:  # rate-limited: one event for the two methods
        assert [e.detail["reason"] for e in events] == [case]


async def test_expired_without_payment_retires_the_address(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any
) -> None:
    shop = await helpers.shop(client, token)
    await helpers.add_ready(bitcart, signer, 1)
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    await helpers.set_status(bitcart, invoice["id"], "expired")
    row = await helpers.address_row(bitcart, invoice["id"])
    assert row.status == "retired" and row.watch_until is not None


async def test_lost_hook_is_found_by_the_watch_loop(bitcart: Bitcart, client: AsyncClient, token: str, signer: Any) -> None:
    shop = await helpers.shop(client, token)
    await helpers.add_ready(bitcart, signer, 1)
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    await helpers.sql(bitcart, "UPDATE invoices SET status = 'invalid' WHERE id = :id", id=invoice["id"])  # no hook
    await bitcart.plugin.detector.close_lost_invoices()
    assert (await helpers.address_row(bitcart, invoice["id"])).status == "retired"
