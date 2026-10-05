"""The admin REST API: superusers only, pools, settings, list, manual and batch withdraw."""

from typing import Any

import fakes
import helpers
import pytest
from conftest import Bitcart
from httpx import AsyncClient

from modules.forkedpool.acctpool import models, readyfill
from modules.forkedpool.acctpool.constants import CHAINS
from modules.forkedpool.acctpool.signer_client import SignerClient

pytestmark = pytest.mark.anyio
BASE = "/plugins/acctpool"


async def test_admin_only(client: AsyncClient, token: str) -> None:
    for method, path in (("GET", "/status"), ("GET", "/addresses"), ("POST", "/withdraw-batch"), ("GET", "/events")):
        assert (await client.request(method, BASE + path)).status_code == 401
    limited = await helpers.token_with(client, token, ["full_control"], superuser=False)
    assert (await client.get(BASE + "/status", headers=helpers.auth(limited))).status_code == 403
    page = await client.get(BASE + "/ui")
    assert page.status_code == 200 and "default-src 'none'" in page.headers["content-security-policy"]


async def test_pools_and_settings(bitcart: Bitcart, client: AsyncClient, token: str) -> None:
    usdt = await helpers.token_wallet(client, token, "eth")
    other = await helpers.post(
        client, "/wallets", token, name="other", xpub=helpers.merchant_address(), currency="eth", contract="0x" + "34" * 20
    )
    refused = await client.post(
        BASE + "/pools", json={"wallet_id": other["id"], "chain": "anvil", "store": "s"}, headers=helpers.auth(token)
    )
    assert refused.status_code == 422  # only the USDT of the chain, or no contract
    wrong = await client.post(
        BASE + "/pools", json={"wallet_id": usdt["id"], "chain": "polygon", "store": "s"}, headers=helpers.auth(token)
    )
    assert wrong.status_code == 422  # an eth wallet cannot be a polygon pool
    pool = await helpers.pool(client, token, usdt["id"], "anvil", enabled=False, min_withdraw="1.5")
    assert (pool["asset"], pool["enabled"], pool["min_withdraw"]) == ("usdt", False, "1.5")
    changed = await client.patch(f"{BASE}/pools/{pool['id']}", json={"enabled": True}, headers=helpers.auth(token))
    assert changed.json()["enabled"] is True
    floats = await client.put(BASE + "/settings", json={"speed_factor": 1.5}, headers=helpers.auth(token))
    assert floats.status_code == 422
    settings = await client.put(
        BASE + "/settings", json={"speed_factor": "1.5", "ready_target": 10}, headers=helpers.auth(token)
    )
    assert settings.json() == {"ready_target": 10, "max_invoice_usd": "500", "speed_factor": "1.5"}
    status = (await client.get(BASE + "/status", headers=helpers.auth(token))).json()
    assert status["settings"]["speed_factor"] == "1.5" and status["engine_alive"] is True


async def test_withdraw(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any, daemons: dict[str, fakes.FakeDaemon]
) -> None:
    shop = await helpers.shop(client, token)
    await helpers.add_ready(bitcart, signer, 2)
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    row = await helpers.address_row(bitcart, invoice["id"])
    open_invoice = await client.post(f"{BASE}/addresses/{row.id}/withdraw", headers=helpers.auth(token))
    assert open_invoice.status_code == 409
    address = invoice["payments"][0]["payment_address"]
    await helpers.event(bitcart, "eth", daemons["eth"].chain.pay(address, token=5_000_000), address, "5", fakes.USDT["eth"])
    listed = (await client.get(BASE + "/addresses?status=pending_payout&chain=anvil", headers=helpers.auth(token))).json()
    assert listed["count"] == 1 and listed["result"][0]["balances"] == {"native": "0", "usdt": "5"}
    detail = (await client.get(f"{BASE}/addresses/{row.id}", headers=helpers.auth(token))).json()
    assert [d["amount"] for d in detail["deposits"]] == ["5.000000000000000000"] and detail["payouts"] == []
    batch = await client.post(BASE + "/withdraw-batch", headers=helpers.auth(token))
    assert batch.json() == {"requested": 1}
    assert (await helpers.address_row(bitcart, invoice["id"])).withdraw_requested is True


async def test_batch_withdraw_includes_in_payout(bitcart: Bitcart, client: AsyncClient, token: str, signer: Any) -> None:
    """M5: an address that a payout has moved to in_payout (and whose payout failed) is in the batch too."""
    await helpers.add_ready(bitcart, signer, 2)
    await helpers.sql(bitcart, "UPDATE plugin_acctpool_addresses SET status = 'in_payout', invoice_id = 'x' WHERE index = 0")
    batch = await client.post(BASE + "/withdraw-batch", headers=helpers.auth(token))
    assert batch.json() == {"requested": 1}


async def test_withdraw_after_the_invoice_was_deleted(bitcart: Bitcart, client: AsyncClient, token: str, signer: Any) -> None:
    """M4: the address of a deleted invoice is closed by the withdraw call (no wait for the watch loop)."""
    shop = await helpers.shop(client, token)
    await helpers.add_ready(bitcart, signer, 1)
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    row = await helpers.address_row(bitcart, invoice["id"])
    assert (await client.delete(f"/invoices/{invoice['id']}", headers=helpers.auth(token))).status_code == 200
    answer = await client.post(f"{BASE}/addresses/{row.id}/withdraw", headers=helpers.auth(token))
    assert answer.status_code == 200 and answer.json()["status"] == "retired"


async def test_used_ready_address_is_not_given_out(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any, daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """M6: after a restore of an older database a ready address can be one that was given out since. One with a
    balance or a nonce is retired (watched: its money is found as a late payment), with an event."""
    shop = await helpers.shop(client, token)
    addresses = await helpers.add_ready(bitcart, signer, 3)
    chain = daemons["eth"].chain
    chain.pay(addresses[0], token=4_000_000)
    chain.nonces[addresses[1].lower()] = 1
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    assert invoice["payments"][0]["payment_address"] == addresses[2]
    rows = await helpers.rows(bitcart, models.AcctpoolAddress)
    assert [row.status for row in rows] == ["retired", "retired", "in_invoice"]
    assert sorted(e.address for e in await helpers.events(bitcart, "address_in_use")) == sorted(addresses[:2])
    detector = bitcart.plugin.detector
    await detector.reconcile(CHAINS["anvil"])
    chain.height += 1
    assert await detector.reconcile(CHAINS["anvil"]) == 1
    (deposit,) = await helpers.rows(bitcart, models.AcctpoolDeposit)
    assert (deposit.address_id, deposit.amount, deposit.late) == (rows[0].id, 4, True)
    assert (await helpers.rows(bitcart, models.AcctpoolAddress))[0].status == "pending_payout"


async def test_derive_ahead_continues_after_the_signers_highest_index(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any
) -> None:
    """M6: the signer gave out indexes up to 9 that the (restored) database does not have: no index is used again."""
    await helpers.shop(client, token)
    signer.highest["teststore"] = 9
    db = await bitcart.plugin.db()
    await db.set_state("settings", {"ready_target": 3})
    await readyfill.fill(bitcart.plugin, SignerClient(signer.url, signer.token_file))
    assert [row.index for row in await helpers.rows(bitcart, models.AcctpoolAddress)] == [10, 11, 12]


async def test_tamper_check_goes_on_after_a_row_without_invoice(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any
) -> None:
    """L7: an address that the signer does not derive and that has no invoice (allocation retired it as used): an
    alert, nothing to invalidate (Bitcart's get(None) has no id filter: it would take ANY invoice). The check goes
    on: the next such address has its pending invoice set invalid; a right address leaves its invoice alone."""
    shop = await helpers.shop(client, token)
    wrong = ["0x" + "01" * 20, "0x" + "02" * 20]
    db = await bitcart.plugin.db()
    async with db.session() as session, session.begin():
        session.add_all(
            models.AcctpoolAddress(store="teststore", index=i, address=a)
            for i, a in enumerate([*wrong, signer.address("teststore", 2)])
        )
    await helpers.sql(bitcart, "UPDATE plugin_acctpool_addresses SET status = 'retired' WHERE index = 0")
    tampered = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    other = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    assert [i["payments"][0]["payment_address"] for i in (tampered, other)] == [wrong[1], signer.address("teststore", 2)]
    await readyfill.check(bitcart.plugin, SignerClient(signer.url, signer.token_file))
    assert sorted(e.address for e in await helpers.events(bitcart, "address_mismatch")) == wrong
    assert (await helpers.invoice_row(bitcart, tampered["id"]))["status"] == "invalid"
    assert (await helpers.invoice_row(bitcart, other["id"]))["status"] == "pending"
    assert [row.checked for row in await helpers.rows(bitcart, models.AcctpoolAddress)] == [True, True, True]
