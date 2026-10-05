"""Idempotency, replacement and the one-signature-per-nonce rule (SPEC 3.4)."""

import helpers
import pytest
from conftest import (
    CAPS,
    DEST_SHOP_ANVIL,
    GWEI,
    RECOVERY_TOKEN,
    REMOVE,
    USDT_ANVIL,
    fund_request,
    sweep_request,
)

FEE = 60 * GWEI
VALUE = 5 * 10**15


def assert_fund(env, result, *, index=3, nonce=0, value=VALUE, max_fee=FEE, max_priority_fee=30 * GWEI, chain_id=31337):
    tx = result["decoded"]
    assert tx["type"] == 2
    assert tx["chain_id"] == chain_id
    assert tx["nonce"] == nonce
    assert tx["to"] == env.address("shop", index)
    assert tx["value"] == value
    assert tx["data"] == b""
    assert tx["gas"] == 21000
    assert tx["max_fee_per_gas"] == max_fee
    assert tx["max_priority_fee_per_gas"] == max_priority_fee
    assert tx["signer"] == env.fee_address
    assert (result["from"], result["to"], result["chain_id"]) == (env.fee_address, tx["to"], chain_id)


def assert_sweep(
    env, result, *, index=3, nonce=0, amount=2000000, gas=70000, max_fee=FEE, max_priority_fee=30 * GWEI, contract=USDT_ANVIL
):
    tx = result["decoded"]
    assert tx["type"] == 2
    assert tx["chain_id"] == 31337
    assert tx["nonce"] == nonce
    assert tx["to"] == contract
    assert tx["value"] == 0
    assert helpers.decode_transfer(tx["data"]) == (DEST_SHOP_ANVIL, amount)
    assert tx["gas"] == gas
    assert tx["max_fee_per_gas"] == max_fee
    assert tx["max_priority_fee_per_gas"] == max_priority_fee
    assert tx["signer"] == env.address("shop", index)
    assert (result["from"], result["to"], result["chain_id"]) == (tx["signer"], contract, 31337)


async def refused(api, call, status, code, key, **changes):
    rows, signed = api.journal_rows(), len(api.transactions)
    got, result = await (api.fund if call == "fund" else api.sweep)(key, **changes)
    assert (got, result["error"]) == (status, code), result
    assert api.journal_rows() == rows
    assert len(api.transactions) == signed
    assert api.audit_lines()[-1]["result"] == code
    return result


def without_decoded(result):
    return {k: v for k, v in result.items() if k != "decoded"}


async def test_same_key_and_same_request_gives_the_same_response(api, env):
    status, first = await api.fund("idem-fund-1")
    assert status == 200
    assert_fund(env, first)
    for _ in range(3):
        status, again = await api.fund("idem-fund-1")
        assert status == 200
        assert without_decoded(again) == without_decoded(first)
        assert_fund(env, again)
    assert api.journal_rows() == 1
    lines = api.audit_lines()[-4:]
    assert [line.get("replay") for line in lines] == [None, True, True, True]
    assert {line["tx_hash"] for line in lines} == {first["tx_hash"]}

    status, first = await api.sweep("idem-sweep-1")
    assert_sweep(env, first)
    status, again = await api.sweep("idem-sweep-1")
    assert status == 200
    assert without_decoded(again) == without_decoded(first)
    assert_sweep(env, again)
    assert api.journal_rows() == 2


async def test_absent_optional_field_is_the_same_request_as_null(api, env):
    status, first = await api.sweep("idem-sweep-2")
    status, again = await api.sweep("idem-sweep-2", token=REMOVE, replaces=REMOVE)
    assert status == 200
    assert without_decoded(again) == without_decoded(first)
    assert_sweep(env, first)
    assert_sweep(env, again)
    assert api.journal_rows() == 1


FUND_CHANGES = [
    {"value_wei": str(VALUE + 1)},
    {"nonce": 1},
    {"index": 4},
    {"chain": "polygon"},
    {"max_fee_per_gas_wei": str(FEE + 1)},
    {"max_priority_fee_per_gas_wei": str(30 * GWEI + 1)},
    {"replaces": "idem-fund-0"},
    {"store": "other"},
    {"chain": "nochain"},
]


@pytest.mark.parametrize("changes", FUND_CHANGES, ids=lambda c: next(iter(c)))
async def test_same_key_and_other_request_is_409(api, env, changes):
    status, first = await api.fund("idem-fund-3")
    assert status == 200
    assert_fund(env, first)
    await refused(api, "fund", 409, "idempotency_conflict", "idem-fund-3", **changes)
    # the first request still gives the first response
    status, again = await api.fund("idem-fund-3")
    assert without_decoded(again) == without_decoded(first)
    assert_fund(env, again)


@pytest.mark.parametrize(
    "changes",
    [{"amount": "2000001"}, {"gas_limit": 70001}, {"token": RECOVERY_TOKEN}, {"nonce": 1}, {"index": 4}],
    ids=lambda c: next(iter(c)),
)
async def test_same_key_and_other_sweep_request_is_409(api, env, changes):
    status, first = await api.sweep("idem-sweep-3")
    assert status == 200
    assert_sweep(env, first)
    await refused(api, "sweep", 409, "idempotency_conflict", "idem-sweep-3", **changes)


async def test_key_of_a_fund_cannot_be_used_for_a_sweep(api, env):
    status, first = await api.fund("idem-both-1")
    assert_fund(env, first)
    await refused(api, "sweep", 409, "idempotency_conflict", "idem-both-1")
    status, first = await api.sweep("idem-both-2")
    assert_sweep(env, first)
    await refused(api, "fund", 409, "idempotency_conflict", "idem-both-2")


async def test_replay_works_when_the_cap_is_reached(api, env):
    value = CAPS["max_fund_value_wei"]
    results = []
    for nonce in range(3):
        status, result = await api.fund(f"idem-cap-{nonce}", index=5, nonce=nonce, value_wei=str(value))
        assert status == 200
        assert_fund(env, result, index=5, nonce=nonce, value=value)
        results.append(result)
    await refused(api, "fund", 403, "cap_exceeded", "idem-cap-3", index=5, nonce=3, value_wei=str(value))
    status, again = await api.fund("idem-cap-0", index=5, nonce=0, value_wei=str(value))
    assert status == 200
    assert without_decoded(again) == without_decoded(results[0])
    assert_fund(env, again, index=5, nonce=0, value=value)


async def test_second_signature_for_one_nonce_is_409(api, env):
    status, first = await api.fund("nonce-fund-1", nonce=7)
    assert_fund(env, first, nonce=7)
    # other key, same (chain, fee wallet, nonce): every variation is refused
    for changes in (
        {},
        {"index": 4},
        {"value_wei": "1"},
        {"max_fee_per_gas_wei": str(2 * FEE)},
        {"store": "shop", "index": 0},
    ):
        await refused(api, "fund", 409, "idempotency_conflict", "nonce-fund-2", nonce=7, **changes)
    # the nonce is per chain and per sender
    status, other_chain = await api.fund("nonce-fund-3", nonce=7, chain="polygon")
    assert status == 200
    assert_fund(env, other_chain, nonce=7, chain_id=137)
    status, next_nonce = await api.fund("nonce-fund-4", nonce=8)
    assert status == 200
    assert_fund(env, next_nonce, nonce=8)

    status, first = await api.sweep("nonce-sweep-1", index=3, nonce=0)
    assert_sweep(env, first)
    for changes in (
        {},
        {"amount": "1"},
        {"gas_limit": 90000},
        {"max_fee_per_gas_wei": str(2 * FEE)},
        {"token": RECOVERY_TOKEN},
    ):
        await refused(api, "sweep", 409, "idempotency_conflict", "nonce-sweep-2", index=3, nonce=0, **changes)
    status, other_address = await api.sweep("nonce-sweep-3", index=4, nonce=0)
    assert status == 200
    assert_sweep(env, other_address, index=4)


async def test_replacement_with_10_percent_more_fee(api, env):
    status, first = await api.fund("repl-fund-1", nonce=2)
    assert_fund(env, first, nonce=2)
    higher = FEE * 11 // 10
    status, second = await api.fund("repl-fund-2", nonce=2, max_fee_per_gas_wei=str(higher), replaces="repl-fund-1")
    assert status == 200
    assert_fund(env, second, nonce=2, max_fee=higher)
    assert second["tx_hash"] != first["tx_hash"]
    assert api.audit_lines()[-1]["replaces"] == "repl-fund-1"
    # a replacement of the replacement
    third_fee = higher * 11 // 10
    status, third = await api.fund(
        "repl-fund-3",
        nonce=2,
        max_fee_per_gas_wei=str(third_fee),
        max_priority_fee_per_gas_wei=str(40 * GWEI),
        replaces="repl-fund-2",
    )
    assert status == 200
    assert_fund(env, third, nonce=2, max_fee=third_fee, max_priority_fee=40 * GWEI)
    # a replacement is idempotent too
    status, again = await api.fund(
        "repl-fund-3",
        nonce=2,
        max_fee_per_gas_wei=str(third_fee),
        max_priority_fee_per_gas_wei=str(40 * GWEI),
        replaces="repl-fund-2",
    )
    assert without_decoded(again) == without_decoded(third)
    assert_fund(env, again, nonce=2, max_fee=third_fee, max_priority_fee=40 * GWEI)
    assert api.journal_rows() == 3


async def test_replacement_with_less_than_10_percent_is_refused(api, env):
    status, first = await api.fund("repl-low-1")
    assert_fund(env, first)
    for fee in (FEE * 11 // 10 - 1, FEE + 1, FEE, FEE - 1, 30 * GWEI):
        await refused(api, "fund", 400, "invalid_request", "repl-low-2", max_fee_per_gas_wei=str(fee), replaces="repl-low-1")
    status, first = await api.sweep("repl-low-3")
    assert_sweep(env, first)
    for fee in (FEE * 11 // 10 - 1, FEE):
        await refused(api, "sweep", 400, "invalid_request", "repl-low-4", max_fee_per_gas_wei=str(fee), replaces="repl-low-3")


async def test_10_percent_is_not_rounded_down(api, env):
    status, first = await api.fund("repl-round-1", max_fee_per_gas_wei="7", max_priority_fee_per_gas_wei="1")
    assert_fund(env, first, max_fee=7, max_priority_fee=1)
    # 7 x 1.1 = 7.7: 7 is refused, 8 is the first value that is 10% higher
    await refused(
        api,
        "fund",
        400,
        "invalid_request",
        "repl-round-2",
        max_fee_per_gas_wei="7",
        max_priority_fee_per_gas_wei="1",
        replaces="repl-round-1",
    )
    status, second = await api.fund(
        "repl-round-3", max_fee_per_gas_wei="8", max_priority_fee_per_gas_wei="1", replaces="repl-round-1"
    )
    assert status == 200
    assert_fund(env, second, max_fee=8, max_priority_fee=1)


FUND_REPLACEMENT_CHANGES = [
    {"chain": "polygon"},
    {"store": "other"},
    {"index": 4},
    {"nonce": 1},
    {"value_wei": str(VALUE + 1)},
    {"value_wei": str(VALUE - 1)},
    {"value_wei": "0"},
]


@pytest.mark.parametrize("changes", FUND_REPLACEMENT_CHANGES, ids=lambda c: f"{next(iter(c))}={next(iter(c.values()))}")
async def test_fund_replacement_must_have_the_same_fields(api, env, changes):
    await api.derive("other", 0, 5)
    status, first = await api.fund("repl-same-1")
    assert_fund(env, first)
    result = await refused(
        api, "fund", 400, "invalid_request", "repl-same-2", max_fee_per_gas_wei=str(2 * FEE), replaces="repl-same-1", **changes
    )
    assert "replaces" in result["detail"]


@pytest.mark.parametrize(
    "changes",
    [{"chain": "polygon"}, {"store": "other"}, {"index": 4}, {"nonce": 1}, {"token": RECOVERY_TOKEN}],
    ids=lambda c: next(iter(c)),
)
async def test_sweep_replacement_must_have_the_same_fields(api, env, changes):
    await api.derive("other", 0, 5)
    status, first = await api.sweep("repl-same-3")
    assert_sweep(env, first)
    result = await refused(
        api,
        "sweep",
        400,
        "invalid_request",
        "repl-same-4",
        max_fee_per_gas_wei=str(2 * FEE),
        replaces="repl-same-3",
        **changes,
    )
    assert "replaces" in result["detail"]


async def test_sweep_replacement_can_change_the_amount_and_the_gas_limit(api, env):
    status, first = await api.sweep("repl-amount-1", amount="2000000")
    assert_sweep(env, first)
    status, second = await api.sweep(
        "repl-amount-2", amount="3500000", gas_limit=90000, max_fee_per_gas_wei=str(2 * FEE), replaces="repl-amount-1"
    )
    assert status == 200
    assert_sweep(env, second, amount=3500000, gas=90000, max_fee=2 * FEE)
    await refused(
        api,
        "sweep",
        400,
        "invalid_request",
        "repl-amount-3",
        amount="0",
        max_fee_per_gas_wei=str(3 * FEE),
        replaces="repl-amount-2",
    )


async def test_recovery_sweep_replacement_keeps_the_token(api, env):
    status, first = await api.sweep("repl-token-1", token=RECOVERY_TOKEN)
    assert_sweep(env, first, contract=RECOVERY_TOKEN)
    await refused(
        api,
        "sweep",
        400,
        "invalid_request",
        "repl-token-2",
        token=None,
        max_fee_per_gas_wei=str(2 * FEE),
        replaces="repl-token-1",
    )
    status, second = await api.sweep(
        "repl-token-3", token=RECOVERY_TOKEN, max_fee_per_gas_wei=str(2 * FEE), replaces="repl-token-1"
    )
    assert status == 200
    assert_sweep(env, second, contract=RECOVERY_TOKEN, max_fee=2 * FEE)


async def test_replacement_of_another_kind_or_an_unknown_key_is_refused(api, env):
    await api.derive("shop", 0, 1)
    status, fund = await api.fund("repl-kind-1")
    assert_fund(env, fund)
    status, sweep = await api.sweep("repl-kind-2")
    assert_sweep(env, sweep)
    await refused(
        api, "sweep", 400, "invalid_request", "repl-kind-3", max_fee_per_gas_wei=str(2 * FEE), replaces="repl-kind-1"
    )
    await refused(api, "fund", 400, "invalid_request", "repl-kind-4", max_fee_per_gas_wei=str(2 * FEE), replaces="repl-kind-2")
    await refused(
        api, "fund", 400, "invalid_request", "repl-kind-5", max_fee_per_gas_wei=str(2 * FEE), replaces="repl-unknown-key"
    )
    await refused(api, "fund", 400, "invalid_request", "repl-kind-6", max_fee_per_gas_wei=str(2 * FEE), replaces="repl-kind-6")


async def test_caps_apply_to_a_replacement(api, env):
    cap = CAPS["max_fee_per_gas_cap_wei"]
    status, first = await api.fund("repl-cap-1", max_fee_per_gas_wei=str(cap - 1))
    assert_fund(env, first, max_fee=cap - 1)
    await refused(api, "fund", 403, "cap_exceeded", "repl-cap-2", max_fee_per_gas_wei=str(cap + 1), replaces="repl-cap-1")
    await refused(api, "fund", 403, "cap_exceeded", "repl-cap-2", max_fee_per_gas_wei=str(cap * 2), replaces="repl-cap-1")
    # at the cap there is no room for 10% more
    await refused(api, "fund", 400, "invalid_request", "repl-cap-2", max_fee_per_gas_wei=str(cap), replaces="repl-cap-1")

    status, first = await api.sweep("repl-cap-3")
    assert_sweep(env, first)
    await refused(
        api,
        "sweep",
        403,
        "cap_exceeded",
        "repl-cap-4",
        gas_limit=CAPS["gas_limit_cap"] + 1,
        max_fee_per_gas_wei=str(2 * FEE),
        replaces="repl-cap-3",
    )
    await refused(api, "sweep", 403, "cap_exceeded", "repl-cap-4", max_fee_per_gas_wei=str(cap + 1), replaces="repl-cap-3")


async def test_replacement_counts_one_time_in_the_funding_total(api, env):
    value = CAPS["max_fund_value_wei"]
    status, result = await api.fund("repl-total-0", index=5, nonce=0, value_wei=str(value))
    assert_fund(env, result, index=5, nonce=0, value=value)
    fee = FEE
    for step in (1, 2):
        fee = fee * 2
        status, result = await api.fund(
            f"repl-total-0-{step}",
            index=5,
            nonce=0,
            value_wei=str(value),
            max_fee_per_gas_wei=str(fee),
            replaces="repl-total-0",
        )
        assert status == 200
        assert_fund(env, result, index=5, nonce=0, value=value, max_fee=fee)
    assert api.state.journal.fund_total("anvil", env.address("shop", 5)) == value
    for nonce in (1, 2):
        status, result = await api.fund(f"repl-total-{nonce}", index=5, nonce=nonce, value_wei=str(value))
        assert status == 200
        assert_fund(env, result, index=5, nonce=nonce, value=value)
    assert api.state.journal.fund_total("anvil", env.address("shop", 5)) == 3 * value
    await refused(api, "fund", 403, "cap_exceeded", "repl-total-3", index=5, nonce=3, value_wei="1")
    # a replacement at the cap is possible: it adds nothing to the total
    status, result = await api.fund(
        "repl-total-2-1", index=5, nonce=2, value_wei=str(value), max_fee_per_gas_wei=str(2 * FEE), replaces="repl-total-2"
    )
    assert status == 200
    assert_fund(env, result, index=5, nonce=2, value=value, max_fee=2 * FEE)
    assert api.state.journal.fund_total("anvil", env.address("shop", 5)) == 3 * value


async def test_replacement_adds_only_the_fee_difference_to_the_daily_spend(api, env):
    journal = api.state.journal
    status, result = await api.fund("repl-day-1")
    assert_fund(env, result)
    assert journal.daily_spend("anvil", "2026-09-29") == VALUE + 21000 * FEE
    status, result = await api.fund("repl-day-2", max_fee_per_gas_wei=str(2 * FEE), replaces="repl-day-1")
    assert_fund(env, result, max_fee=2 * FEE)
    assert journal.daily_spend("anvil", "2026-09-29") == VALUE + 21000 * 2 * FEE
    # on the next day only the difference counts for that day
    env.clock.now = env.clock.now.replace(day=30)
    status, result = await api.fund("repl-day-3", max_fee_per_gas_wei=str(3 * FEE), replaces="repl-day-2")
    assert_fund(env, result, max_fee=3 * FEE)
    assert journal.daily_spend("anvil", "2026-09-29") == VALUE + 21000 * 2 * FEE
    assert journal.daily_spend("anvil", "2026-09-30") == 21000 * FEE


async def test_daily_cap_applies_to_a_replacement(api, env):
    value = CAPS["max_fund_value_wei"]
    cap = CAPS["fee_wallet_daily_cap_wei"]
    for nonce in range(6):
        status, result = await api.fund(f"repl-daily-{nonce}", index=nonce, nonce=nonce, value_wei=str(value))
        assert status == 200
        assert_fund(env, result, index=nonce, nonce=nonce, value=value)
    # a 7th funding that leaves room for 100 gwei x 21000 gas in the daily cap
    room = 21000 * 100 * GWEI
    last = cap - api.state.journal.daily_spend("anvil", "2026-09-29") - 21000 * FEE - room
    status, result = await api.fund("repl-daily-6", index=6, nonce=6, value_wei=str(last))
    assert status == 200
    assert_fund(env, result, index=6, nonce=6, value=last)
    assert cap - api.state.journal.daily_spend("anvil", "2026-09-29") == room
    top_fee = FEE + 100 * GWEI
    await refused(
        api, "fund", 403, "daily_cap_exceeded", "repl-daily-x", index=0, nonce=0, value_wei=str(value),
        max_fee_per_gas_wei=str(top_fee + 1), replaces="repl-daily-0",
    )  # fmt: skip
    status, result = await api.fund(
        "repl-daily-y", index=0, nonce=0, value_wei=str(value), max_fee_per_gas_wei=str(top_fee), replaces="repl-daily-0"
    )
    assert status == 200
    assert_fund(env, result, index=0, nonce=0, value=value, max_fee=top_fee)
    assert api.state.journal.daily_spend("anvil", "2026-09-29") == cap


async def test_requests_of_this_file_are_the_spec_examples():
    assert set(fund_request("k" * 8)) == {
        "idempotency_key", "chain", "store", "index", "nonce", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei",
        "value_wei", "replaces",
    }  # fmt: skip
    assert set(sweep_request("k" * 8)) == {
        "idempotency_key", "chain", "store", "index", "nonce", "gas_limit", "max_fee_per_gas_wei",
        "max_priority_fee_per_gas_wei", "amount", "token", "replaces",
    }  # fmt: skip
