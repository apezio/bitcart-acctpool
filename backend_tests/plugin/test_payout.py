"""The payout state machine with a fake daemon (and its small chain), the fake signer and a fake second opinion:
fund -> sweep -> native sweep, crash after each state, re-broadcast of the same bytes, replacement, second
opinion refusals, the fee rule."""

import ast
import asyncio
import inspect
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import fakes
import helpers
import pytest
from conftest import Bitcart
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from modules.forkedpool.acctpool import api as api_module
from modules.forkedpool.acctpool import feerule, models
from modules.forkedpool.acctpool import payout as payout_module
from modules.forkedpool.acctpool.constants import CHAINS
from modules.forkedpool.acctpool.payout import Payouts
from modules.forkedpool.acctpool.signer_client import SignerClient

pytestmark = pytest.mark.anyio
USDT = fakes.USDT["eth"]
ANVIL = CHAINS["anvil"]
ETH = 10**18


class World:
    def __init__(self, bitcart: Bitcart, signer: Any, chain: fakes.Chain, address: str, invoice: dict[str, Any]) -> None:
        self.bitcart, self.signer, self.chain, self.address, self.invoice = bitcart, signer, chain, address, invoice

    def payouts(self, replace_after: float = 600) -> Payouts:
        return Payouts(self.bitcart.plugin, SignerClient(self.signer.url, self.signer.token_file), replace_after)

    async def rows(self) -> list[Any]:
        return await helpers.rows(self.bitcart, models.AcctpoolPayout)

    async def states(self) -> list[tuple[str, str]]:
        return [(row.kind, row.state) for row in await self.rows()]

    async def status(self) -> str:
        return (await helpers.address_row(self.bitcart, self.invoice["id"])).status

    def destination(self) -> int:
        return self.chain.token[self.signer.destination("teststore", "usdt").lower()]


@pytest.fixture
async def world(
    bitcart: Bitcart, client: AsyncClient, token: str, signer: Any, second: Any, daemons: dict[str, fakes.FakeDaemon]
) -> World:
    """A paid USDT invoice on anvil: the address waits for its payout; the fee wallet has 10 ETH."""
    shop = await helpers.shop(client, token)
    await helpers.add_ready(bitcart, signer, 2)
    db = await bitcart.plugin.db()
    await db.set_state("signer", await SignerClient(signer.url, signer.token_file).status())
    invoice = await helpers.invoice(client, token, shop["store"]["id"], price=5)
    address = invoice["payments"][0]["payment_address"]
    chain = daemons["eth"].chain
    chain.native[signer.fee_wallet.lower()] = 10 * ETH
    await helpers.event(bitcart, "eth", chain.pay(address, token=5_000_000), address, "5", USDT)
    return World(bitcart, signer, chain, address, invoice)


async def test_usdt_fund_then_sweep(world: World) -> None:
    payouts = world.payouts()
    assert await world.status() == "pending_payout"
    await payouts.round(ANVIL)
    assert await world.rows() == []  # 1 confirmation, anvil needs 2
    world.chain.height += 1
    await payouts.round(ANVIL)
    assert await world.states() == [("fund", "planned")]
    await payouts.round(ANVIL)
    assert await world.states() == [("fund", "broadcast")]
    world.chain.mine()
    await payouts.round(ANVIL)  # the funding is confirmed, then the sweep is planned
    assert await world.states() == [("fund", "confirmed"), ("sweep", "planned")]
    (fund,) = world.signer.signed("fund")
    price = int(fund["max_fee_per_gas_wei"])
    assert price == int(world.chain.gas_price * Decimal("1.25"))
    assert int(fund["value_wei"]) == 60000 * price * 11 // 10  # gas estimate x 1.2, x price, x the fund margin 1.1
    await payouts.round(ANVIL)
    world.chain.mine()
    await payouts.round(ANVIL)
    assert await world.states() == [("fund", "confirmed"), ("sweep", "confirmed")]
    assert world.destination() == 5_000_000 and world.chain.token[world.address.lower()] == 0
    (sweep,) = world.signer.signed("sweep")
    assert (sweep["amount"], sweep["gas_limit"], sweep["nonce"], sweep["replaces"]) == ("5000000", 60000, 0, None)
    # the leftover gas money (16,000 gas) is less than 10 native fees: kept, the address is retired
    assert await world.status() == "retired"
    # baselines from the receipts: the sweep moved the 5 USDT; funding - fee (50,000 gas used) stays as native
    balances = {b.asset: b.baseline for b in await helpers.rows(world.bitcart, models.AcctpoolBalance)}
    assert balances == {"usdt": -5, "native": Decimal(16000 * price) / ETH}
    assert world.signer.signed("sweep_native") == []


async def test_native_payment_is_swept_in_one_transaction(world: World, daemons: dict[str, fakes.FakeDaemon]) -> None:
    world.chain.token[world.address.lower()] = 0
    await helpers.sql(world.bitcart, "DELETE FROM plugin_acctpool_deposits")
    await helpers.event(world.bitcart, "eth", world.chain.pay(world.address, native=ETH), world.address, "1", None)
    world.chain.height += 2
    payouts = world.payouts()
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    world.chain.mine()
    await payouts.round(ANVIL)
    (native,) = world.signer.signed("sweep_native")
    fee = 21000 * int(native["max_fee_per_gas_wei"])
    assert int(native["value_wei"]) == ETH - fee and native["max_priority_fee_per_gas_wei"] == native["max_fee_per_gas_wei"]
    assert world.chain.native[world.signer.destination("teststore", "native").lower()] == ETH - fee
    assert world.chain.native[world.address.lower()] == 0 and await world.status() == "retired"
    assert world.signer.signed("fund") == []


@pytest.mark.parametrize("short", [0, 1])
async def test_native_is_swept_only_at_10_fees(world: World, short: int) -> None:
    """The signer refuses a native sweep whose fee is over 10% of value + fee: the engine sweeps only when the value
    is at least 10 fees. One wei less stays on the address (stranded) and the address is retired."""
    fee = 21000 * int(world.chain.gas_price * Decimal("1.25"))
    paid = 11 * fee - short
    world.chain.token[world.address.lower()] = 0
    await helpers.sql(world.bitcart, "DELETE FROM plugin_acctpool_deposits")
    tx_hash = world.chain.pay(world.address, native=paid)
    await helpers.event(world.bitcart, "eth", tx_hash, world.address, str(Decimal(paid) / ETH), None)
    world.chain.height += 2
    payouts = world.payouts()
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    world.chain.mine()
    await payouts.round(ANVIL)
    values = [int(row["value_wei"]) for row in world.signer.signed("sweep_native")]
    assert values == ([10 * fee] if short == 0 else [])
    assert world.chain.native[world.address.lower()] == (0 if short == 0 else paid)
    assert await world.status() == "retired"


@pytest.mark.parametrize("lost", ["answer_of_the_signer", "broadcast", "state_after_broadcast"])
async def test_crash_resumes_with_the_same_bytes(world: World, lost: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that dies between two steps: the next worker finishes with the same signature, no second one."""
    world.chain.height += 1
    await world.payouts().round(ANVIL)  # fund planned
    dying = world.payouts()
    real = dying.update

    async def update(row: Any, **values: Any) -> None:
        wanted = {"answer_of_the_signer": "signed", "state_after_broadcast": "broadcast"}.get(lost)
        if wanted and values.get("state") == wanted:
            raise KeyboardInterrupt("the worker is killed")
        await real(row, **values)

    async def broadcasted(row: Any) -> None:
        raise KeyboardInterrupt("the worker is killed")

    monkeypatch.setattr(dying, "update", update)
    if lost == "state_after_broadcast":
        monkeypatch.setattr(dying, "broadcasted", broadcasted)
    if lost == "broadcast":
        world.chain.broadcast_error = "connection refused"
        await dying.round(ANVIL)
        assert await world.states() == [("fund", "signed")]
        world.chain.broadcast_error = None
    else:
        with pytest.raises(KeyboardInterrupt):
            await dying.round(ANVIL)
    expected = {"answer_of_the_signer": "planned", "broadcast": "signed", "state_after_broadcast": "signed"}[lost]
    assert await world.states() == [("fund", expected)]
    fresh = world.payouts()
    await fresh.round(ANVIL)
    world.chain.mine()
    await fresh.round(ANVIL)
    assert await world.states() == [("fund", "confirmed"), ("sweep", "planned")]
    assert len(set(world.chain.broadcasts)) == 1  # every broadcast was the same bytes
    assert len({body["idempotency_key"] for body in world.signer.signed("fund")}) == 1
    assert world.chain.nonces[world.signer.fee_wallet.lower()] == 1


async def test_replacement_after_timeout(world: World) -> None:
    world.chain.height += 1
    payouts = world.payouts(replace_after=0)
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)  # fund broadcast, not mined
    await payouts.round(ANVIL)  # not mined after the time: a replacement is planned; the original stays followed
    rows = await world.rows()
    assert [(r.state, r.error, r.attempts) for r in rows] == [("broadcast", None, 0), ("planned", None, 1)]
    assert int(rows[1].max_fee_wei) == int(rows[0].max_fee_wei) * 12 // 10 + 1 and rows[1].nonce == rows[0].nonce
    await payouts.round(ANVIL)
    assert [(r.state, r.error) for r in await world.rows()] == [("failed", "replaced"), ("broadcast", None)]
    first, second = world.signer.signed("fund")
    assert second["replaces"] == first["idempotency_key"] and second["value_wei"] == first["value_wei"]
    world.chain.mine()  # the higher fee wins
    await payouts.round(ANVIL)
    rows = await world.rows()
    assert [(r.kind, r.state) for r in rows[:2]] == [("fund", "failed"), ("fund", "confirmed")]
    assert rows[1].tx_hash in world.chain.mined and rows[0].tx_hash not in world.chain.mined


async def test_the_replaced_transaction_can_still_win(world: World) -> None:
    world.chain.height += 1
    payouts = world.payouts(replace_after=0)
    for _ in range(3):
        await payouts.round(ANVIL)
    first = (await world.rows())[0]
    world.chain.mine()  # mined before the replacement was signed
    await payouts.round(ANVIL)  # the receipt finishes the group: the replacement is never signed
    await payouts.round(ANVIL)
    rows = await world.rows()
    assert [(r.id, r.state) for r in rows[:2]] == [(first.id, "confirmed"), (rows[1].id, "failed")]
    assert rows[1].tx_hash is None and len(world.signer.signed("fund")) == 1
    assert world.chain.nonces[world.signer.fee_wallet.lower()] == 1


async def test_sweep_replacement_is_funded_first(world: World) -> None:
    """The signer lets a token sweep spend only what was funded for it: a higher fee than the funded coin pays needs
    a top-up first, sized with the fund margin."""
    payouts = world.payouts()
    await top_up_planned(world, payouts)
    for _ in range(5):
        await payouts.round(ANVIL)
        world.chain.mine()
    rows = await world.rows()
    funds = [r for r in rows if r.kind == "fund"]
    sweeps = [r for r in rows if r.kind == "sweep"]
    price = int(sweeps[1].max_fee_wei)
    assert price == int(sweeps[0].max_fee_wei) * 12 // 10 + 1  # the full replacement price: the top-up pays it
    assert int(funds[1].value) == int(sweeps[0].gas_limit) * -(-price * 11 // 10) - int(funds[0].value)
    assert sweeps[1].replaces_id == sweeps[0].id and sweeps[1].state == "confirmed"
    assert world.destination() == 5_000_000 and len(funds) == 2


@pytest.mark.parametrize("rise", ["1.05", "1.30", "1.50"])
async def test_one_funding_while_the_gas_price_rises(world: World, rise: str) -> None:
    """The gas price rises after the funding. Up to the fund margin (10%) the sweep pays the full price; while the
    funded coin covers FUND_TOLERANCE (80%) of the fee it pays own // gas; above that a second funding comes,
    sized with the margin."""
    world.chain.height += 1
    payouts = world.payouts()
    await payouts.round(ANVIL)  # fund planned at the price of now
    world.chain.gas_price = int(world.chain.gas_price * Decimal(rise))
    price = int(world.chain.gas_price * Decimal("1.25"))
    for _ in range(2):
        await payouts.round(ANVIL)
        world.chain.mine()
    gas, own = 60000, int((await world.rows())[0].value)  # the first funding
    if rise == "1.50":
        assert await world.states() == [("fund", "confirmed"), ("fund", "planned")]
        assert int((await world.rows())[-1].value) == gas * -(-price * 11 // 10) - own
        return
    assert await world.states() == [("fund", "confirmed"), ("sweep", "planned")]
    sweep = (await world.rows())[-1]
    assert int(sweep.max_fee_wei) == min(price, own // gas) == (price if rise == "1.05" else own // gas)
    for _ in range(2):
        await payouts.round(ANVIL)
        world.chain.mine()
    assert await world.states() == [("fund", "confirmed"), ("sweep", "confirmed")]
    assert world.destination() == 5_000_000 and len(world.signer.signed("fund")) == 1


async def test_sweep_replacement_without_a_top_up(world: World) -> None:
    """The funded coin (the fund margin) pays a replacement that is 10% above the sweep (the signer's floor): it goes
    out at own // gas, below the 20% replacement price, with no top-up."""
    payouts = world.payouts()
    await sweep_broadcast(world, payouts)
    fund, sweep = await world.rows()
    world.chain.min_fee = int(sweep.max_fee_wei) + 1
    payouts.replace_after = 0
    await payouts.round(ANVIL)
    replacement = (await world.rows())[-1]
    assert (replacement.kind, replacement.state, replacement.replaces_id) == ("sweep", "planned", sweep.id)
    fee = int(fund.value) // int(sweep.gas_limit)
    assert int(replacement.max_fee_wei) == fee == -(-int(sweep.max_fee_wei) * 11 // 10)
    assert fee < int(sweep.max_fee_wei) * 12 // 10 + 1
    for _ in range(3):
        await payouts.round(ANVIL)
        world.chain.mine()
    sweeps = [(r.state, r.error) for r in await world.rows() if r.kind == "sweep"]
    assert sweeps == [("failed", "replaced"), ("confirmed", None)]
    assert len(world.signer.signed("fund")) == 1 and world.destination() == 5_000_000


@pytest.mark.parametrize("problem", ["lie", "down"])
async def test_second_opinion_holds_the_signature(world: World, second: Any, problem: str) -> None:
    world.chain.height += 1
    setattr(second, problem, True)
    payouts = world.payouts()
    for _ in range(3):
        await payouts.round(ANVIL)
    assert await world.states() == [("fund", "planned")]
    assert world.signer.signed() == []
    kind = "second_opinion_mismatch" if problem == "lie" else "second_opinion_down"
    assert len(await helpers.events(world.bitcart, kind)) == 1
    setattr(second, problem, False)
    await payouts.round(ANVIL)
    assert await world.states() == [("fund", "broadcast")]


async def test_signer_refusal_stops_until_an_admin_asks(world: World) -> None:
    world.chain.height += 1
    payouts = world.payouts()
    await payouts.round(ANVIL)
    world.signer.next_error = (403, {"error": "cap_exceeded", "detail": "value_wei is over max_fund_value_wei"})
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    assert await world.states() == [("fund", "failed")]
    (event,) = await helpers.events(world.bitcart, "payout_failed")
    assert "cap_exceeded" in event.detail["reason"]
    await helpers.sql(world.bitcart, "UPDATE plugin_acctpool_addresses SET withdraw_requested = true")
    await payouts.round(ANVIL)
    assert await world.states() == [("fund", "failed"), ("fund", "planned")]


async def sweep_broadcast(world: World, payouts: Payouts, rise: str = "1") -> None:
    """fund planned (then the gas price x rise), broadcast, mined; confirmed + sweep planned; sweep broadcast (not
    mined)."""
    world.chain.height += 1
    await payouts.round(ANVIL)
    world.chain.gas_price = int(world.chain.gas_price * Decimal(rise))
    for _ in range(2):
        world.chain.mine()
        await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    assert [(r.kind, r.state) for r in await world.rows()] == [("fund", "confirmed"), ("sweep", "broadcast")]


async def test_original_mined_before_its_replacement_is_signed(world: World, second: Any) -> None:
    """H1: the sweep is replaced (planned), the second opinion is down, and the original is mined meanwhile. The
    receipt finishes the group: the replacement is never signed and no mismatch comes from the balance it moved."""
    payouts = world.payouts()
    await sweep_broadcast(world, payouts)
    world.chain.min_fee = int((await world.rows())[-1].max_fee_wei) + 1  # the chain does not take the sweep now
    payouts.replace_after = 0
    for _ in range(8):  # a top-up funding first (signer budget), then the replacement of the sweep is planned
        await payouts.round(ANVIL)
        if [r for r in await world.rows() if r.kind == "sweep" and r.replaces_id and r.state == "planned"]:
            break
        world.chain.mine()
    second.down = True
    await payouts.round(ANVIL)
    sweeps = [(r.state, r.replaces_id is not None) for r in await world.rows() if r.kind == "sweep"]
    assert sweeps == [("broadcast", False), ("planned", True)]  # the original stays followed
    world.chain.min_fee = 0
    world.chain.mine()  # the original is mined
    second.down = False
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    sweeps = [(r.state, r.error) for r in await world.rows() if r.kind == "sweep"]
    assert sweeps == [("confirmed", None), ("failed", "replaced")]
    assert len(world.signer.signed("sweep")) == 1 and world.destination() == 5_000_000
    assert await helpers.events(world.bitcart, "second_opinion_mismatch") == []
    assert await helpers.events(world.bitcart, "payout_failed") == []


async def top_up_planned(world: World, payouts: Payouts) -> Any:
    """The gas price rose 9% after the funding, so the sweep used up the fund margin; the chain does not take its fee:
    its replacement needs a top-up funding first."""
    await sweep_broadcast(world, payouts, "1.09")
    world.chain.min_fee = int((await world.rows())[-1].max_fee_wei) + 1
    payouts.replace_after = 0
    await payouts.round(ANVIL)
    top_up = (await world.rows())[-1]
    assert (top_up.kind, top_up.state, top_up.replaces_id) == ("fund", "planned", None)
    return top_up


async def test_orphaned_top_up_is_closed_unsigned(world: World, second: Any) -> None:
    """H1: the sweep is mined before its top-up funding is signed. The funding ends `not_needed` (never signed, no
    alert, the admin flag untouched); the chain's fundings are free again and the address retires."""
    payouts = world.payouts()
    await top_up_planned(world, payouts)
    second.down = True
    await payouts.round(ANVIL)
    world.chain.min_fee = 0
    world.chain.mine()  # the original sweep is mined
    second.down = False
    for _ in range(3):
        await payouts.round(ANVIL)
    last = (await world.rows())[-1]
    assert (last.kind, last.state, last.error, last.raw_tx) == ("fund", "failed", "not_needed", None)
    assert len(world.signer.signed("fund")) == 1 and await helpers.events(world.bitcart, "payout_failed") == []
    assert await helpers.events(world.bitcart, "second_opinion_mismatch") == []
    assert await world.status() == "retired"


async def test_top_up_whose_signer_answer_was_lost_is_sent(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """H1: the signer signed the top-up but its answer was lost. The row may hold a signature: it is not closed but
    sent with the same key (the signer's stored answer), so no fee-wallet nonce is left signed and never sent."""
    payouts = world.payouts()
    await top_up_planned(world, payouts)
    real = payouts.signer.sign

    async def lost(*args: Any) -> Any:
        await real(*args)
        raise payout_module.SignerError("unavailable", "TimeoutError")

    monkeypatch.setattr(payouts.signer, "sign", lost)
    await payouts.round(ANVIL)
    monkeypatch.undo()
    assert (await world.rows())[-1].state == "planned"
    world.chain.min_fee = 0
    world.chain.mine()  # the original sweep is mined
    for _ in range(3):
        await payouts.round(ANVIL)
        world.chain.mine()
    funds = [r for r in await world.rows() if r.kind == "fund"]
    assert [r.state for r in funds] == ["confirmed", "confirmed"]
    assert len({body["idempotency_key"] for body in world.signer.signed("fund")}) == 2
    assert await helpers.events(world.bitcart, "second_opinion_mismatch") == []


async def test_replacement_of_a_top_up_after_its_sweep_is_mined(world: World) -> None:
    """H1: a signed top-up that no node keeps must still be replaced after its sweep is mined (its fee-wallet nonce
    is open): the replacement needs no token on the address."""
    payouts = world.payouts()
    await top_up_planned(world, payouts)
    await payouts.round(ANVIL)
    top_up = (await world.rows())[-1]
    assert (top_up.kind, top_up.state) == ("fund", "broadcast")
    del world.chain.mempool[top_up.tx_hash]  # every node dropped it
    world.chain.broadcast_error = "txpool is full"
    world.chain.min_fee = 0
    world.chain.mine()  # the original sweep is mined
    for _ in range(3):
        await payouts.round(ANVIL)
    (replacement,) = [r for r in await world.rows() if r.replaces_id == top_up.id]
    assert replacement.kind == "fund" and replacement.raw_tx is not None
    assert await helpers.events(world.bitcart, "second_opinion_mismatch") == []


async def test_refused_top_up_stops_the_sweep_replacements(world: World) -> None:
    """L2: the signer refuses a top-up: the sweep it tops up is stuck (no new top-up every round, one alert)."""
    payouts = world.payouts()
    await top_up_planned(world, payouts)
    world.signer.next_error = (403, {"error": "cap_exceeded", "detail": "max_fund_total_per_address_wei"})
    for _ in range(4):
        await payouts.round(ANVIL)
    rows = await world.rows()
    assert [(r.kind, r.state) for r in rows] == [
        ("fund", "confirmed"), ("sweep", "broadcast"), ("fund", "failed")
    ]  # fmt: skip
    assert (rows[1].error, rows[1].attempts) == ("stuck", payout_module.MAX_REPLACEMENTS)
    assert len(await helpers.events(world.bitcart, "payout_failed")) == 1


async def test_newest_signed_attempt_is_sent(world: World) -> None:
    """M1: the original and its replacement are both signed and no node takes them; then the nodes take them
    again. The newest signed attempt is sent (an older one is not, and a newer one is never closed by it), and the
    next replacement is priced above every signed attempt of the nonce: nothing is stuck."""
    world.chain.height += 1
    payouts = world.payouts(replace_after=0)
    await payouts.round(ANVIL)
    world.chain.broadcast_error = "max fee per gas less than block base fee"
    await payouts.round(ANVIL)  # the original is signed, no node takes it: its replacement is planned
    await payouts.round(ANVIL)  # the replacement is signed, no node takes it: a second replacement is planned
    rows = await world.rows()
    assert [(r.state, r.replaces_id) for r in rows] == [("signed", None), ("signed", rows[0].id), ("planned", rows[1].id)]
    tries = world.chain.broadcasts.count(rows[0].raw_tx)
    world.chain.broadcast_error = None
    world.chain.min_fee = int(rows[2].max_fee_wei)  # only the newest fee is enough for a block
    for _ in range(3):
        await payouts.round(ANVIL)
        world.chain.mine()
    funds = [r for r in await world.rows() if r.kind == "fund"]
    assert [(r.state, r.error) for r in funds[:3]] == [("failed", "replaced")] * 2 + [("confirmed", None)]
    assert world.chain.broadcasts.count(rows[0].raw_tx) == tries  # not sent again after its replacement was signed
    assert await helpers.events(world.bitcart, "payout_failed") == []


async def test_replacement_is_priced_above_every_signed_attempt(world: World) -> None:
    """M1: a signed attempt that is not open any more still counts (the signer has it): a replacement of the open
    original is priced 20% above the highest signed fee of the nonce, not above the original's."""
    world.chain.height += 1
    world.chain.min_fee = 10**30
    payouts = world.payouts(replace_after=0)
    for _ in range(4):
        await payouts.round(ANVIL)  # the original is sent, then its replacement: the original is failed/replaced
    first, second = await world.rows()
    change = "UPDATE plugin_acctpool_payouts SET state = :s, error = :e WHERE id = :i"
    await helpers.sql(world.bitcart, change, s="broadcast", e=None, i=first.id)
    await helpers.sql(world.bitcart, change, s="failed", e="replaced", i=second.id)
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    third = (await world.rows())[2]
    assert third.replaces_id == first.id and third.raw_tx is not None
    assert int(third.max_fee_wei) == int(second.max_fee_wei) * 12 // 10 + 1
    assert await helpers.events(world.bitcart, "payout_failed") == []


async def test_stuck_signed_row_alerts_once_and_its_receipt_finishes_it(world: World) -> None:
    """L1: a signed attempt at the replacement limit whose broadcast keeps failing stays `stuck` with one alert; when a
    node took it after all (whatever it says), its receipt finishes the nonce group."""
    world.chain.height += 1
    world.chain.broadcast_error = "max fee per gas less than block base fee"
    payouts = world.payouts(replace_after=0)
    for _ in range(12):
        await payouts.round(ANVIL)
    last = (await world.rows())[-1]
    assert (last.state, last.error, last.attempts) == ("signed", "stuck", payout_module.MAX_REPLACEMENTS)
    assert len(await helpers.events(world.bitcart, "payout_failed")) == 1
    effect = world.chain.effects[last.raw_tx]
    world.chain.mempool[effect["hash"]] = effect
    world.chain.mine()
    await payouts.round(ANVIL)
    funds = [r.state for r in await world.rows() if r.kind == "fund"]
    assert funds[: payout_module.MAX_REPLACEMENTS + 1] == ["failed"] * payout_module.MAX_REPLACEMENTS + ["confirmed"]


async def test_two_leaders_plan_one_payout(world: World) -> None:
    """L3: two leaders for a moment (the lock handover) plan the same address at the same time. The partial unique
    indexes of the migration (one open root row per address and chain for sweeps, one open root funding per chain,
    one open replacement per original) refuse the second row: no second funding can be planned or signed."""
    world.chain.height += 1
    first, second = world.payouts(), world.payouts()
    state = await (await world.bitcart.plugin.db()).get_state("signer")
    address = await helpers.address_row(world.bitcart, world.invoice["id"])
    srv = await payout_module.daemon.server(world.bitcart.plugin.container, ANVIL)
    results = await asyncio.gather(
        *(payouts.plan(ANVIL, srv, address, state) for payouts in (first, second)), return_exceptions=True
    )
    assert await world.states() == [("fund", "planned")]
    assert [type(r).__name__ for r in results if r is not None] in ([], ["IntegrityError"])
    (fund,) = await world.rows()
    with pytest.raises(IntegrityError):  # another root funding on the chain while one is open
        await first.add(address, ANVIL, "native", "fund", int(fund.nonce) + 1, 21000, 1, 1)
    await first.add(address, ANVIL, "native", "fund", int(fund.nonce), 21000, 2, 1, replaces=fund)
    with pytest.raises(IntegrityError):  # a second open replacement of the same original
        await second.add(address, ANVIL, "native", "fund", int(fund.nonce), 21000, 2, 1, replaces=fund)
    assert [(r.kind, r.replaces_id) for r in await world.rows()] == [("fund", None), ("fund", fund.id)]


async def test_finish_is_one_transaction(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """M1: the original is mined while its replacement is the broadcast row. A fault in the finish changes nothing
    (the replacement is not failed alone); the next round confirms the original."""
    world.chain.height += 1
    payouts = world.payouts(replace_after=0)
    for _ in range(3):
        await payouts.round(ANVIL)  # fund planned, broadcast, replacement planned
    world.chain.broadcast_error = "replacement transaction underpriced"  # the node keeps the original
    await payouts.round(ANVIL)
    world.chain.broadcast_error = None
    assert [(r.state, r.error) for r in await world.rows()] == [("failed", "replaced"), ("broadcast", None)]
    world.chain.mine()

    def broken(*args: Any) -> Any:
        raise RuntimeError("fault during the finish")

    monkeypatch.setattr(payout_module.daemon, "coins", broken)
    await payouts.round(ANVIL)
    assert [(r.state, r.error) for r in await world.rows()] == [("failed", "replaced"), ("broadcast", None)]
    monkeypatch.undo()
    await payouts.round(ANVIL)
    rows = await world.rows()
    assert [(r.kind, r.state) for r in rows[:2]] == [("fund", "confirmed"), ("fund", "failed")]
    (balance,) = await helpers.rows(world.bitcart, models.AcctpoolBalance)
    assert balance.baseline == Decimal(int(rows[0].value)) / ETH


async def test_deposit_before_the_finish_is_not_absorbed(world: World) -> None:
    """M2: the baseline comes from the receipt. A payment that is on the address before the finish, without an
    event yet, is still found by the reconcile (a baseline from the balance would hide it)."""
    await helpers.sql(world.bitcart, "UPDATE plugin_acctpool_pools SET min_withdraw = 2")  # 1 USDT waits
    payouts = world.payouts()
    await sweep_broadcast(world, payouts)
    world.chain.mine()
    world.chain.pay(world.address, token=1_000_000)
    await payouts.round(ANVIL)
    assert [r.state for r in await world.rows()] == ["confirmed", "confirmed"]
    detector = world.bitcart.plugin.detector
    await detector.reconcile(ANVIL)
    world.chain.height += 1
    assert await detector.reconcile(ANVIL) == 1
    last = (await helpers.rows(world.bitcart, models.AcctpoolDeposit))[-1]
    assert (last.source, last.asset, last.amount, last.late) == ("balance", "usdt", 1, True)


async def test_fee_budget_counts_a_funding_nonce_once(world: World) -> None:
    """M3: the signer's rule: a funding and its replacement (one fee-wallet nonce) count once."""
    address_id = (await helpers.address_row(world.bitcart, world.invoice["id"])).id
    for key, state in (("k-first", "failed"), ("k-second", "confirmed")):
        await helpers.sql(
            world.bitcart,
            "INSERT INTO plugin_acctpool_payouts (address_id, chain, asset, kind, idempotency_key, nonce, gas_limit,"
            " max_fee_wei, value, raw_tx, state) VALUES (:a, 'anvil', 'native', 'fund', :k, 7, 21000, 1, :v, '0x02', :s)",
            a=address_id, k=key, v=10**15, s=state,
        )  # fmt: skip
    assert await world.payouts().own_native(address_id, ANVIL, ETH, None) == 10**15


async def test_daily_cap_waits_for_the_next_utc_day(world: World) -> None:
    """M5: the fee wallet's daily cap is not a failure: the row waits for the next UTC day."""
    world.chain.height += 1
    payouts = world.payouts()
    await payouts.round(ANVIL)
    world.signer.next_error = (403, {"error": "daily_cap_exceeded", "detail": "the fee wallet spend of this UTC day"})
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    assert [(r.state, r.error) for r in await world.rows()] == [("planned", "daily_cap_exceeded")]
    assert world.signer.signed() == [] and await helpers.events(world.bitcart, "payout_failed") == []
    assert [e.detail for e in await helpers.events(world.bitcart, "fee_wallet_low")] == [{"reason": "daily_cap_exceeded"}]
    await helpers.sql(world.bitcart, "UPDATE plugin_acctpool_payouts SET updated = updated - interval '1 day'")
    await payouts.round(ANVIL)
    assert await world.states() == [("fund", "broadcast")]


async def test_stale_nonce_is_read_again(world: World, client: AsyncClient, token: str) -> None:
    """M5: the daemon gave a stale nonce that the signer has signed for another key: the row takes the new nonce."""
    other = await helpers.invoice(client, token, world.invoice["store_id"], price=5)
    other_address = other["payments"][0]["payment_address"]
    await helpers.event(world.bitcart, "eth", world.chain.pay(other_address, token=5_000_000), other_address, "5", USDT)
    world.chain.height += 1
    payouts = world.payouts()
    await payouts.round(ANVIL)  # the first address: fund planned (the other waits: one funding at a time)
    await payouts.round(ANVIL)
    world.chain.mine()
    world.chain.stale[world.signer.fee_wallet.lower()] = 0
    for _ in range(3):
        await payouts.round(ANVIL)
    funds = [(int(r.nonce), r.state) for r in await world.rows() if r.kind == "fund"]
    assert funds == [(0, "confirmed"), (1, "broadcast")]
    assert await helpers.events(world.bitcart, "payout_failed") == []


def assert_no_group_closed_without_receipt(world: World, rows: list[Any]) -> None:
    """Invariant: a nonce group with a signature of ours has an open row until some attempt of it has a receipt."""
    groups: dict[tuple[Any, ...], list[Any]] = {}
    for row in rows:
        sender = "fee wallet" if row.kind == "fund" else row.address_id
        groups.setdefault((row.chain, sender, int(row.nonce)), []).append(row)
    for key, group in groups.items():
        signed = any(row.raw_tx for row in group)
        mined = any(row.tx_hash in world.chain.mined for row in group if row.tx_hash)
        still_open = any(row.state in ("planned", "signed", "broadcast") for row in group)
        assert not signed or mined or still_open, f"nonce group {key} closed without a receipt"


async def stuck_funding(world: World, payouts: Payouts) -> list[Any]:
    """A funding that no block takes (fee too low for the chain), replaced until MAX_REPLACEMENTS: stuck."""
    world.chain.height += 1
    world.chain.min_fee = 10**30
    for _ in range(12):
        await payouts.round(ANVIL)
        assert_no_group_closed_without_receipt(world, await world.rows())
    rows = await world.rows()
    assert [(r.state, r.error, r.attempts) for r in rows][-1] == ("broadcast", "stuck", payout_module.MAX_REPLACEMENTS)
    return rows


async def test_retry_after_stuck(world: World, client: AsyncClient, token: str) -> None:
    """A stuck payout: an admin retry allows exactly one more replacement (same nonce, fee +20%, `replaces`)."""
    payouts = world.payouts(replace_after=0)
    rows = await stuck_funding(world, payouts)
    stuck = rows[-1]
    assert len(await helpers.events(world.bitcart, "payout_failed")) == 1
    answer = await client.post(f"/plugins/acctpool/payouts/{stuck.id}/retry", headers=helpers.auth(token))
    assert answer.status_code == 200, answer.text
    assert [(r.state, r.error) for r in await world.rows()][-1] == ("broadcast", None)  # nothing is closed
    for _ in range(4):
        await payouts.round(ANVIL)
        assert_no_group_closed_without_receipt(world, await world.rows())
    rows = await world.rows()
    assert len(rows) == len([r for r in rows if r.kind == "fund"]) == payout_module.MAX_REPLACEMENTS + 2
    last = rows[-1]
    assert (last.replaces_id, int(last.nonce)) == (stuck.id, int(stuck.nonce))
    assert int(last.max_fee_wei) == int(stuck.max_fee_wei) * 12 // 10 + 1
    assert world.signer.signed("fund")[-1]["replaces"] == stuck.idempotency_key
    assert (last.state, last.error) == ("broadcast", "stuck")  # one more only, then stuck again
    assert len(await helpers.events(world.bitcart, "payout_failed")) == 1  # the stuck alert: once a day (L1)
    world.chain.min_fee = 0
    world.chain.mine()
    await payouts.round(ANVIL)
    rows = await world.rows()
    funds = [r for r in rows if r.kind == "fund"]
    assert [r.state for r in funds] == ["failed"] * (len(funds) - 1) + ["confirmed"]
    assert {r.error for r in funds[:-1]} == {"replaced"}
    assert_no_group_closed_without_receipt(world, rows)


async def test_retry_when_the_original_already_mined(world: World, client: AsyncClient, token: str) -> None:
    """The first attempt of a stuck nonce is mined after all: retry is refused, the worker finishes the group from
    that receipt (the original confirmed, every other attempt replaced)."""
    payouts = world.payouts(replace_after=0)
    rows = await stuck_funding(world, payouts)
    original = rows[0]
    for tx_hash in [h for h in world.chain.mempool if h != original.tx_hash]:
        del world.chain.mempool[tx_hash]  # every node kept only the first one
    world.chain.min_fee = 0
    world.chain.mine()
    answer = await client.post(f"/plugins/acctpool/payouts/{rows[-1].id}/retry", headers=helpers.auth(token))
    assert answer.status_code == 409
    assert [(r.state, r.error) for r in await world.rows()][-1] == ("broadcast", "stuck")  # the API closed nothing
    await payouts.round(ANVIL)
    funds = [r for r in await world.rows() if r.kind == "fund"]
    assert [(r.id, r.state) for r in funds if r.state != "failed"] == [(original.id, "confirmed")]
    assert all(r.error == "replaced" for r in funds if r.state == "failed")


async def test_retry_locks_no_row_while_it_reads_receipts(
    world: World, client: AsyncClient, token: str, daemons: dict[str, fakes.FakeDaemon]
) -> None:
    """L5: retry reads the receipts of the nonce group (daemon calls, up to the SDK timeout each) before it locks
    the rows, so the worker's writes to that group are not blocked meanwhile."""
    rows = await stuck_funding(world, world.payouts(replace_after=0))
    daemons["eth"].delay["get_tx_status"] = 0.5
    call = asyncio.create_task(client.post(f"/plugins/acctpool/payouts/{rows[-1].id}/retry", headers=helpers.auth(token)))
    await asyncio.sleep(0.3)
    lock = "SELECT id FROM plugin_acctpool_payouts WHERE id = :i FOR UPDATE NOWAIT"
    await helpers.sql(world.bitcart, lock, i=rows[-1].id)  # the worker can take the row now
    answer = await call
    assert answer.status_code == 200, answer.text
    assert [(r.state, r.error) for r in await world.rows()][-1] == ("broadcast", None)


async def test_retry_is_refused_while_nothing_is_sent(world: World, client: AsyncClient, token: str) -> None:
    world.chain.height += 1
    payouts = world.payouts()
    await payouts.round(ANVIL)
    (row,) = await world.rows()
    answer = await client.post(f"/plugins/acctpool/payouts/{row.id}/retry", headers=helpers.auth(token))
    assert answer.status_code == 409
    assert (await client.post("/plugins/acctpool/payouts/999/retry", headers=helpers.auth(token))).status_code == 409


async def test_retry_after_the_signer_refused_the_replacement(world: World, client: AsyncClient, token: str) -> None:
    """The signer refuses a replacement: the replacement (never signed) fails, the original stays open and stuck.
    Retry gives it one more replacement; no signed row is ever closed without a receipt."""
    world.chain.height += 1
    world.chain.min_fee = 10**30
    payouts = world.payouts(replace_after=0)
    for _ in range(3):
        await payouts.round(ANVIL)
    world.signer.next_error = (403, {"error": "cap_exceeded", "detail": "max fee"})
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    rows = await world.rows()
    assert [(r.state, r.error) for r in rows] == [("broadcast", "stuck"), ("failed", rows[1].error)]
    assert rows[1].raw_tx is None and "cap_exceeded" in rows[1].error
    assert_no_group_closed_without_receipt(world, rows)
    answer = await client.post(f"/plugins/acctpool/payouts/{rows[1].id}/retry", headers=helpers.auth(token))
    assert answer.status_code == 409  # a failed row is not open
    answer = await client.post(f"/plugins/acctpool/payouts/{rows[0].id}/retry", headers=helpers.auth(token))
    assert answer.status_code == 200, answer.text
    for _ in range(3):
        await payouts.round(ANVIL)
        assert_no_group_closed_without_receipt(world, await world.rows())
    rows = await world.rows()
    assert (rows[-1].state, rows[-1].replaces_id) == ("broadcast", rows[0].id)
    world.chain.min_fee = 0
    world.chain.mine()
    await payouts.round(ANVIL)
    assert [r.state for r in await world.rows() if r.kind == "fund"][-1] == "confirmed"


def test_rows_leave_open_only_with_a_receipt_or_unsigned() -> None:
    """Invariant in the code: `failed` is written only by finish() (a receipt), refused() (a row the signer did not
    sign), broadcasted() (the attempts that a newer, followed attempt of the same nonce replaces) and not_needed()
    (a funding that was never sent to the signer, or that the signer refused: no signature of it exists). The admin
    API closes no payout."""

    def writes_failed(function: ast.AST) -> bool:
        assigned = [n.value for n in ast.walk(function) if isinstance(n, ast.Assign | ast.keyword)]
        return any(isinstance(n, ast.Attribute) and n.attr == "FAILED" for value in assigned for n in ast.walk(value))

    writers = set()
    for node in ast.walk(ast.parse(inspect.getsource(payout_module))):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and writes_failed(node):
            writers.add(node.name)
    assert writers == {"finish", "refused", "broadcasted", "not_needed"}
    assert "FAILED" not in inspect.getsource(api_module)


async def test_same_nonce_conflict_on_a_mined_nonce_ends_the_unsigned_row(
    world: World, client: AsyncClient, token: str
) -> None:
    """A stale daemon nonce two times in a row, and that nonce is mined (ours): the unsigned row ends with the
    receipt of its nonce group (no replacement of a mined transaction, no payout_failed); the next try takes the
    new nonce."""
    other = await helpers.invoice(client, token, world.invoice["store_id"], price=5)
    other_address = other["payments"][0]["payment_address"]
    await helpers.event(world.bitcart, "eth", world.chain.pay(other_address, token=5_000_000), other_address, "5", USDT)
    world.chain.height += 1
    payouts = world.payouts()
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    world.chain.mine()
    wallet = world.signer.fee_wallet.lower()
    world.chain.stale[wallet] = 0
    await payouts.round(ANVIL)  # first funding confirmed; the other address plans nonce 0 (stale)
    world.chain.stale[wallet] = 0
    await payouts.round(ANVIL)  # conflict, and the daemon says 0 again
    funds = [(int(r.nonce), r.state, r.error, r.replaces_id) for r in await world.rows() if r.kind == "fund"]
    assert funds[:2] == [(0, "confirmed", None, None), (0, "failed", "replaced", None)]
    for _ in range(3):
        await payouts.round(ANVIL)
    funds = [(int(r.nonce), r.state) for r in await world.rows() if r.kind == "fund"]
    assert funds[-1] == (1, "broadcast")
    assert await helpers.events(world.bitcart, "payout_failed") == []


async def test_same_nonce_conflict_without_a_receipt_waits(world: World) -> None:
    """The signer has a signature for this nonce that no row of ours has (a database restored to an older state):
    the row is never closed; it waits with an alert, and goes on when the daemon gives another nonce."""
    world.chain.height += 1
    wallet = world.signer.fee_wallet
    world.signer.nonces[("anvil", wallet, 0)] = "0x" + "ab" * 32
    payouts = world.payouts()
    for _ in range(3):
        await payouts.round(ANVIL)
    rows = await world.rows()
    assert [(r.state, r.error, r.replaces_id) for r in rows] == [("planned", "idempotency_conflict", None)]
    (event,) = await helpers.events(world.bitcart, "payout_failed")
    assert event.detail["reason"] == "nonce_conflict"
    world.chain.nonces[wallet.lower()] = 1  # that transaction came into a block after all
    for _ in range(2):
        await payouts.round(ANVIL)
    assert [(int(r.nonce), r.state) for r in await world.rows()] == [(1, "broadcast")]


async def test_signed_row_that_no_node_takes_is_replaced(world: World) -> None:
    """L2: a signed transaction with a broadcast error gets the replacement path after replace_after."""
    world.chain.height += 1
    payouts = world.payouts(replace_after=0)
    await payouts.round(ANVIL)
    world.chain.broadcast_error = "max fee per gas less than block base fee"
    await payouts.round(ANVIL)  # signed, no node takes it: replaced at once (replace_after 0)
    assert [(r.state, r.replaces_id is not None) for r in await world.rows()] == [("signed", False), ("planned", True)]
    world.chain.broadcast_error = None
    for _ in range(2):
        await payouts.round(ANVIL)
        world.chain.mine()
    await payouts.round(ANVIL)
    funds = sorted((r.state, r.error) for r in await world.rows() if r.kind == "fund")
    assert funds == [("confirmed", None), ("failed", "replaced")]


async def test_balance_deposit_confirmations_count_from_the_provider_head(world: World) -> None:
    """L3: a deposit found by balance has the provider head of its reading; confirmations count from the head
    now, not from the block that the daemon has processed (it lags after a restart)."""
    await helpers.sql(
        world.bitcart, "UPDATE plugin_acctpool_deposits SET source = 'balance', height = :h", h=world.chain.height - 1
    )
    world.chain.lag = 5
    address = await helpers.address_row(world.bitcart, world.invoice["id"])
    srv = await payout_module.daemon.server(world.bitcart.plugin.container, ANVIL)
    assert await world.payouts().confirmed_enough(ANVIL, srv, address)


async def test_confirmations_come_from_the_newest_block(world: World) -> None:
    """L4: a balance row that the reconcile records after an event row has its older reading head. The newest
    deposit is the one in the highest block, not the highest id: the event tx (1 confirmation, anvil needs 2)."""
    address = await helpers.address_row(world.bitcart, world.invoice["id"])
    await helpers.sql(
        world.bitcart,
        "INSERT INTO plugin_acctpool_deposits (address_id, chain, asset, tx_hash, amount, source, height, invoice_id)"
        " VALUES (:a, 'anvil', 'usdt', 'balance:l4', 1, 'balance', :h, :i)",
        a=address.id, h=world.chain.height - 5, i=world.invoice["id"],
    )  # fmt: skip
    srv = await payout_module.daemon.server(world.bitcart.plugin.container, ANVIL)
    assert not await world.payouts().confirmed_enough(ANVIL, srv, address)
    payment = next(p for p in world.invoice["payments"] if p["lookup_field"].endswith(":usdt"))
    method = SimpleNamespace(lookup_field=payment["lookup_field"], invoice_id=world.invoice["id"], meta=payment["metadata"])
    assert (await world.bitcart.plugin.hooks.request_data(method))["confirmations"] == 1
    world.chain.height += 1
    assert await world.payouts().confirmed_enough(ANVIL, srv, address)


@pytest.mark.parametrize("change", [{"from": "0x" + "ab" * 20}, {"chain_id": 1}])
async def test_signer_answer_for_another_sender_or_chain_is_not_used(world: World, change: dict[str, Any]) -> None:
    """L7: the signed sender and chain id must be the ones that the second opinion checked."""
    world.chain.height += 1
    payouts = world.payouts()
    await payouts.round(ANVIL)
    world.signer.answer_changes = change
    await payouts.round(ANVIL)
    await payouts.round(ANVIL)
    assert [(r.state, r.raw_tx) for r in await world.rows()] == [("planned", None)]
    assert world.chain.broadcasts == [] and await helpers.events(world.bitcart, "signer_down")


def test_fee_rule() -> None:
    assert feerule.pay_now(True, None, None)
    assert not feerule.pay_now(False, Decimal(100), None)
    assert feerule.pay_now(False, Decimal(100), Decimal(5))  # 5%
    assert not feerule.pay_now(False, Decimal(40), Decimal(2.5))  # over 5% and under $50
    assert feerule.pay_now(False, Decimal(50), Decimal(3))  # $50 or more and at most $3
    assert not feerule.pay_now(False, Decimal(50), Decimal("3.01"))
