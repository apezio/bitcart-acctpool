"""Detection: the new_transaction handler, crediting through Bitcart, late payments, watch-load and reconcile."""

from decimal import Decimal
from typing import Any

import fakes
import helpers
import pytest
from conftest import Bitcart
from httpx import AsyncClient

from modules.forkedpool.acctpool import daemon as plugin_daemon
from modules.forkedpool.acctpool import models
from modules.forkedpool.acctpool.constants import CHAINS

pytestmark = pytest.mark.anyio
USDT = fakes.USDT["eth"]


@pytest.fixture
async def open_invoice(bitcart: Bitcart, client: AsyncClient, token: str, signer: Any) -> dict[str, Any]:
    shop = await helpers.shop(client, token)
    await helpers.add_ready(bitcart, signer, 3)
    await (await bitcart.plugin.db()).set_state("signer", {"keystore": True, "fee_wallets": {"evm": signer.fee_wallet}})
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    address = next(p["payment_address"] for p in invoice["payments"] if p["wallet_id"] == shop["usdt"]["id"])
    return {"invoice": invoice, "address": address, "shop": shop}


async def deposits(bitcart: Bitcart) -> list[Any]:
    return await helpers.rows(bitcart, models.AcctpoolDeposit)


async def invoice_now(client: AsyncClient, token: str, invoice: dict[str, Any]) -> dict[str, Any]:
    response = await client.get(f"/invoices/{invoice['id']}", headers=helpers.auth(token))
    return response.json()


async def test_event_credits_the_invoice_once(
    bitcart: Bitcart, client: AsyncClient, token: str, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    chain, address, invoice = daemons["eth"].chain, open_invoice["address"], open_invoice["invoice"]
    first = chain.pay(address, token=2_000_000)
    await helpers.event(bitcart, "eth", first, address, "2", USDT)
    assert (await invoice_now(client, token, invoice))["status"] == "pending"  # part payment
    second = chain.pay(address, token=3_000_000)
    for _ in range(2):  # the same event twice: one deposit
        await helpers.event(bitcart, "eth", second, address, "3", USDT)
    now = await invoice_now(client, token, invoice)
    assert now["status"] == "complete" and Decimal(now["sent_amount"]) == 5 and now["tx_hashes"] == [first, second]
    rows = await deposits(bitcart)
    assert [(r.tx_hash, r.amount, r.asset, r.source, r.credited, r.late) for r in rows] == [
        (first, 2, "usdt", "event", True, False), (second, 3, "usdt", "event", True, False)
    ]  # fmt: skip
    assert (await helpers.address_row(bitcart, invoice["id"])).status == "pending_payout"


async def test_own_and_unknown_transactions_are_ignored(
    bitcart: Bitcart, open_invoice: dict[str, Any], signer: Any, daemons: dict[str, fakes.FakeDaemon]
) -> None:
    address = open_invoice["address"]
    chain = daemons["eth"].chain
    await helpers.event(bitcart, "eth", chain.pay(address, native=10**15), address, "0.001", None, sender=signer.fee_wallet)
    ours = chain.pay(address, native=10**15)
    await helpers.sql(
        bitcart,
        "INSERT INTO plugin_acctpool_payouts (address_id, chain, asset, kind, idempotency_key, nonce, gas_limit,"
        " max_fee_wei, value, tx_hash, state) VALUES (1, 'anvil', 'native', 'fund', 'k', 0, 1, 1, 1, :tx, 'confirmed')",
        tx=ours,
    )
    await helpers.event(bitcart, "eth", ours, address, "0.001", None)
    await helpers.event(bitcart, "eth", chain.pay("0x" + "12" * 20, token=1), "0x" + "12" * 20, "1", USDT)
    await helpers.event(bitcart, "eth", chain.pay(address, token=1), address, "1", "0x" + "34" * 20)  # another token
    assert await deposits(bitcart) == []


async def test_payment_after_expiry_is_late(
    bitcart: Bitcart, client: AsyncClient, token: str, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    address, invoice = open_invoice["address"], open_invoice["invoice"]
    await helpers.set_status(bitcart, invoice["id"], "expired")
    assert (await helpers.address_row(bitcart, invoice["id"])).status == "retired"
    tx = daemons["eth"].chain.pay(address, token=5_000_000)
    await helpers.event(bitcart, "eth", tx, address, "5", USDT)
    (row,) = await deposits(bitcart)
    assert (row.credited, row.late) == (False, True)
    assert (await invoice_now(client, token, invoice))["status"] == "expired"
    assert (await helpers.address_row(bitcart, invoice["id"])).status == "pending_payout"
    (late,) = await helpers.events(bitcart, "late_payment")
    assert late.detail["amount"] == "5.000000000000000000" and late.address == address


async def test_wrong_asset_does_not_pay_the_invoice(
    bitcart: Bitcart, client: AsyncClient, token: str, daemons: dict[str, fakes.FakeDaemon], signer: Any
) -> None:
    """A store with a USDT pool only: native coin on the address is late (it is swept), the invoice stays open."""
    wallet = await helpers.token_wallet(client, token, "eth")
    store = await helpers.store(client, token, [wallet["id"]])
    await helpers.pool(client, token, wallet["id"], "anvil")
    await helpers.add_ready(bitcart, signer, 1)
    invoice = await helpers.invoice(client, token, store["id"], price=5)
    address = invoice["payments"][0]["payment_address"]
    await helpers.event(bitcart, "eth", daemons["eth"].chain.pay(address, native=10**18), address, "1", None)
    (row,) = await deposits(bitcart)
    assert (row.asset, row.credited, row.late) == ("native", False, True)
    assert (await invoice_now(client, token, invoice))["status"] == "pending"
    assert (await helpers.address_row(bitcart, invoice["id"])).status == "in_invoice"
    await helpers.set_status(bitcart, invoice["id"], "expired")
    assert (await helpers.address_row(bitcart, invoice["id"])).status == "pending_payout"  # the money is swept


async def test_reconcile_credits_a_payment_without_event(
    bitcart: Bitcart, client: AsyncClient, token: str, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """The daemon was down (no event): the balance shows it. It is counted when the daemon has processed the head of
    the reading (an event of it would be in then)."""
    chain, address, invoice = daemons["eth"].chain, open_invoice["address"], open_invoice["invoice"]
    chain.pay(address, token=5_000_000)
    head = chain.height
    detector = bitcart.plugin.detector
    assert await detector.reconcile(CHAINS["anvil"]) == 0
    assert await detector.reconcile(CHAINS["anvil"]) == 0  # the daemon is not past that head yet
    assert await deposits(bitcart) == []
    chain.height += 1
    assert await detector.reconcile(CHAINS["anvil"]) == 1
    (row,) = await deposits(bitcart)
    assert (row.source, row.amount, row.credited, row.height) == ("balance", 5, True, head)
    assert row.tx_hash.startswith(f"balance:anvil:{address}:usdt:")
    now = await invoice_now(client, token, invoice)
    assert now["status"] == "complete" and Decimal(now["sent_amount"]) == 5 and now["tx_hashes"] == []
    for _ in range(3):  # nothing new: nothing more
        assert await detector.reconcile(CHAINS["anvil"]) == 0


async def test_reconcile_counts_from_the_baseline(
    bitcart: Bitcart, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """After our own transaction the baseline holds the balance: only money that came after it is new."""
    chain, address = daemons["eth"].chain, open_invoice["address"]
    tx = chain.pay(address, token=5_000_000, native=10**16)
    await helpers.event(bitcart, "eth", tx, address, "5", USDT)
    address_id = (await helpers.address_row(bitcart, open_invoice["invoice"]["id"])).id
    # what our own transactions changed: a funding of 0.01 native, a sweep of the 5 USDT
    await helpers.sql(
        bitcart,
        "INSERT INTO plugin_acctpool_balances VALUES (:a, 'anvil', 'native', 0.01), (:a, 'anvil', 'usdt', -5)",
        a=address_id,
    )
    chain.token[address.lower()] = 0  # swept
    detector = bitcart.plugin.detector
    for _ in range(2):
        chain.height += 1
        assert await detector.reconcile(CHAINS["anvil"]) == 0
    chain.pay(address, token=1_500_000)
    await detector.reconcile(CHAINS["anvil"])
    chain.height += 1
    assert await detector.reconcile(CHAINS["anvil"]) == 1
    assert [(r.source, r.amount, r.late) for r in await deposits(bitcart)][-1] == ("balance", Decimal("1.5"), True)


async def test_check_pending_loads_the_watched_addresses(
    bitcart: Bitcart, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon], signer: Any
) -> None:
    """After a daemon restart the diskless wallets are gone; Bitcart's check_pending hook loads them again."""
    daemons["eth"].loaded.clear()
    await helpers.sql(
        bitcart,
        "UPDATE plugin_acctpool_addresses SET status = 'retired', watch_until = now() - interval '1 day' WHERE index = 2",
    )
    await bitcart.registry.run_hook("check_pending", "eth")
    await bitcart.plugin.detector.load_tasks["anvil"]
    assert daemons["eth"].loaded == {open_invoice["address"].lower()}  # not the ready one, not the one past its watch


async def test_event_after_the_reconcile_is_not_counted_twice(
    bitcart: Bitcart, client: AsyncClient, token: str, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """The daemon was behind: the balance showed a part payment first, its event came later. One deposit only: the
    reconcile waits until the daemon is past the head of its reading (L6: no balance row that an event repeats)."""
    chain, address, invoice = daemons["eth"].chain, open_invoice["address"], open_invoice["invoice"]
    tx = chain.pay(address, token=2_000_000)
    chain.lag = 3  # the daemon has not processed the block of the payment
    for _ in range(3):
        chain.height += 1
        assert await bitcart.plugin.detector.reconcile(CHAINS["anvil"]) == 0
    assert await deposits(bitcart) == []
    await helpers.event(bitcart, "eth", tx, address, "2", USDT)
    chain.lag = 0
    assert await bitcart.plugin.detector.reconcile(CHAINS["anvil"]) == 0
    (row,) = await deposits(bitcart)
    assert (row.tx_hash, row.source, row.amount, row.credited) == (tx, "event", 2, True)
    now = await invoice_now(client, token, invoice)
    assert now["status"] == "pending" and Decimal(now["sent_amount"]) == 2


@pytest.mark.parametrize("event", [False, True])
async def test_retired_address_past_its_watch_is_checked_by_balance(
    bitcart: Bitcart, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon], event: bool
) -> None:
    """The 30-day check of an address past its watch: the same two readings as the reconcile (L6: the daemon can
    still have it loaded until it restarts, so an event can still come). The next reconcile round finishes the
    reading; then the address is watched again."""
    address, invoice = open_invoice["address"], open_invoice["invoice"]
    await helpers.set_status(bitcart, invoice["id"], "expired")
    await helpers.sql(bitcart, "UPDATE plugin_acctpool_addresses SET watch_until = now() - interval '1 day'")
    chain = daemons["eth"].chain
    tx = chain.pay(address, token=4_000_000)
    detector = bitcart.plugin.detector
    assert await detector.reconcile(CHAINS["anvil"]) == 0  # the watched set does not have it
    assert await detector.reconcile(CHAINS["anvil"], unwatched=True) == 0  # first reading
    if event:
        await helpers.event(bitcart, "eth", tx, address, "4", USDT)
    chain.height += 1
    assert await detector.reconcile(CHAINS["anvil"]) == (0 if event else 1)
    (row,) = await deposits(bitcart)
    assert (row.source, row.amount, row.late) == ("event" if event else "balance", 4, True)
    after = await helpers.address_row(bitcart, invoice["id"])
    assert after.status == "pending_payout"
    assert (
        await helpers.sql(bitcart, "SELECT watch_until > now() FROM plugin_acctpool_addresses WHERE id = :i", i=after.id)
    ).scalar()
    after = await helpers.address_row(bitcart, invoice["id"])
    assert after.status == "pending_payout"
    assert (
        await helpers.sql(bitcart, "SELECT watch_until > now() FROM plugin_acctpool_addresses WHERE id = :i", i=after.id)
    ).scalar()


async def test_deleted_invoice_counts_as_closed(
    bitcart: Bitcart, client: AsyncClient, token: str, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """M4: a payment to the address of a deleted invoice is late and the address goes to the payout; the watch loop
    closes the address of a deleted invoice without a payment (retired)."""
    address, invoice = open_invoice["address"], open_invoice["invoice"]
    unpaid = await helpers.invoice(client, token, open_invoice["shop"]["store"]["id"], price=5)
    for gone in (invoice, unpaid):
        assert (await client.delete(f"/invoices/{gone['id']}", headers=helpers.auth(token))).status_code == 200
    await helpers.event(bitcart, "eth", daemons["eth"].chain.pay(address, token=5_000_000), address, "5", USDT)
    (row,) = await deposits(bitcart)
    assert (row.credited, row.late) == (False, True)
    assert (await helpers.address_row(bitcart, invoice["id"])).status == "pending_payout"
    await bitcart.plugin.detector.close_lost_invoices()
    assert (await helpers.address_row(bitcart, unpaid["id"])).status == "retired"


async def test_payment_to_a_confirmed_invoice_is_late(
    bitcart: Bitcart, client: AsyncClient, token: str, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """L4: Bitcart takes no new money for a confirmed invoice: the deposit is late, not silently 'credited'."""
    address, invoice = open_invoice["address"], open_invoice["invoice"]
    await helpers.set_status(bitcart, invoice["id"], "confirmed")
    await helpers.event(bitcart, "eth", daemons["eth"].chain.pay(address, token=5_000_000), address, "5", USDT)
    (row,) = await deposits(bitcart)
    assert (row.credited, row.late) == (False, True)
    (late,) = await helpers.events(bitcart, "late_payment")
    assert late.detail["reason"] == "invoice_confirmed"
    assert (await helpers.address_row(bitcart, invoice["id"])).status == "in_invoice"  # closes with its invoice


async def test_one_address_does_not_stop_the_reconcile(
    bitcart: Bitcart,
    client: AsyncClient,
    token: str,
    open_invoice: dict[str, Any],
    daemons: dict[str, fakes.FakeDaemon],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L5: a daemon error for one address; the other addresses of the round are reconciled."""
    from modules.forkedpool.acctpool import detect

    chain, first = daemons["eth"].chain, open_invoice["address"]
    other = await helpers.invoice(client, token, open_invoice["shop"]["store"]["id"], price=5)
    second = next(p["payment_address"] for p in other["payments"] if p["wallet_id"] == open_invoice["shop"]["usdt"]["id"])
    chain.pay(first, token=1_000_000)
    chain.pay(second, token=2_000_000)
    real = detect.daemon.balance

    async def balance(srv: Any, on: Any, address: str, asset: str) -> int:
        if address.lower() == first.lower():
            raise RuntimeError("no answer for this address")
        return await real(srv, on, address, asset)

    monkeypatch.setattr(detect.daemon, "balance", balance)
    detector = bitcart.plugin.detector
    assert await detector.reconcile(CHAINS["anvil"]) == 0
    chain.height += 1
    assert await detector.reconcile(CHAINS["anvil"]) == 1
    (row,) = await deposits(bitcart)
    assert (row.amount, row.source) == (2, "balance")


async def test_trace_event_after_the_balance_row_is_not_counted_twice(
    bitcart: Bitcart, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """M2: native coin sent inside a contract call comes from the daemon's trace queue, maybe after the reconcile
    has counted it by balance. A balance row whose reading head is at or above the block of the tx has it: no
    second deposit, an event with both references. A later native payment is counted."""
    chain, address = daemons["eth"].chain, open_invoice["address"]
    tx = chain.pay(address, native=10**15)
    detector = bitcart.plugin.detector
    await detector.reconcile(CHAINS["anvil"])
    chain.height += 1
    assert await detector.reconcile(CHAINS["anvil"]) == 1
    await helpers.event(bitcart, "eth", tx, address, "0.001", None)
    (row,) = await deposits(bitcart)
    assert (row.source, row.amount) == ("balance", Decimal("0.001"))
    (duplicate,) = await helpers.events(bitcart, "deposit_duplicate")
    assert duplicate.detail == {"tx": tx, "balance_row": row.tx_hash}
    newer = chain.pay(address, native=2 * 10**15)
    await helpers.event(bitcart, "eth", newer, address, "0.002", None)
    assert [(r.tx_hash, r.source) for r in await deposits(bitcart)][-1] == (newer, "event")


async def test_one_address_does_not_stop_the_settle_pass(
    bitcart: Bitcart, open_invoice: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """L7: the pass over uncredited deposits at the end of the reconcile: an error for one address does not stop
    the others."""
    for address_id in (1, 2):
        await helpers.sql(
            bitcart,
            "INSERT INTO plugin_acctpool_deposits (address_id, chain, asset, tx_hash, amount, source, created)"
            " VALUES (:a, 'anvil', 'usdt', :t, 1, 'event', now() - interval '2 minutes')",
            a=address_id, t=f"0x{address_id:064x}",
        )  # fmt: skip
    detector = bitcart.plugin.detector
    calls = []

    async def settle(address_id: int) -> None:
        calls.append(address_id)
        if address_id == 1:
            raise RuntimeError("fault for one address")

    monkeypatch.setattr(detector, "settle", settle)
    await detector.reconcile(CHAINS["anvil"])
    assert sorted(calls) == [1, 2]


async def test_token_balance_is_read_through_the_wallet(
    bitcart: Bitcart, open_invoice: dict[str, Any], daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """`getaddressbalance_contract` leaks about 80 KB per call in the stock daemon (host out of memory, 2026-10-04):
    the reconcile reads USDT with `getbalance` of the diskless token wallet and never sends the leaking call."""
    daemon, address = daemons["eth"], open_invoice["address"]
    daemon.chain.pay(address, token=5_000_000)
    before = len(daemon.called("getbalance"))
    await bitcart.plugin.detector.reconcile(CHAINS["anvil"])
    assert len(daemon.called("getbalance")) > before
    assert daemon.called("getaddressbalance_contract") == []
    srv = await plugin_daemon.server(bitcart.plugin.container, CHAINS["anvil"])
    assert await plugin_daemon.balance(srv, CHAINS["anvil"], address, "usdt") == 5_000_000
