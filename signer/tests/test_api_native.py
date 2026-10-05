"""POST /v1/sign/sweep_native (SPEC 8.3): the transaction shape, every refusal, the fee share rule, the nonce space."""

import random

import helpers
import pytest
from conftest import (
    ATTACKER,
    CAPS,
    DEST_OTHER_ANVIL,
    DEST_SHOP_ANVIL,
    DEST_SHOP_BNB,
    DEST_SHOP_POLYGON,
    GWEI,
    NATIVE_GAS_CAP_POLYGON,
    NATIVE_OTHER_ANVIL,
    NATIVE_OTHER_POLYGON,
    NATIVE_SHARE_POLYGON,
    NATIVE_SHOP_ANVIL,
    NATIVE_SHOP_BNB,
    PREFUND,
    RECOVERY_TOKEN,
    REMOVE,
    USDT_ANVIL,
)

FEE = 60 * GWEI
GAS = 21000
VALUE = 5 * 10**16
TOKEN_ADDRESSES = (USDT_ANVIL, RECOVERY_TOKEN, DEST_SHOP_ANVIL, DEST_OTHER_ANVIL, DEST_SHOP_POLYGON, DEST_SHOP_BNB)


def assert_native(env, result, *, store="shop", index=3, destination=NATIVE_SHOP_ANVIL, value=VALUE, **expect):
    """Every field of the signed transaction. expect overrides the values of the default request."""
    tx = result["decoded"]
    sender = env.address(store, index)
    assert tx["type"] == 2
    assert tx["chain_id"] == expect.get("chain_id", 31337)
    assert tx["nonce"] == expect.get("nonce", 0)
    assert tx["to"] == destination
    assert tx["value"] == value
    assert tx["data"] == b""
    assert tx["gas"] == expect.get("gas", GAS)
    assert tx["max_fee_per_gas"] == expect.get("max_fee", FEE)
    assert tx["max_priority_fee_per_gas"] == expect.get("max_priority_fee", expect.get("max_fee", FEE))
    assert tx["access_list"] == []
    assert tx["signer"] == sender
    assert tx["signer"] != env.fee_address
    assert tx["to"] not in TOKEN_ADDRESSES
    assert (result["from"], result["to"], result["chain_id"]) == (sender, destination, tx["chain_id"])
    assert result["tx_hash"] == tx["hash"]


async def refused(api, status, code, **changes):
    rows, signed = api.journal_rows(), len(api.transactions)
    got, result = await api.sweep_native(changes.pop("key", "native-refused-1"), **changes)
    assert (got, result["error"]) == (status, code), result
    assert set(result) == {"error", "detail"}
    assert api.journal_rows() == rows
    assert len(api.transactions) == signed
    line = api.audit_lines()[-1]
    assert (line["call"], line["result"], line["tx_hash"]) == ("sign/sweep_native", code, None)
    return result


async def test_native_sweep_has_the_fixed_shape(api, env):
    status, result = await api.sweep_native("native-shape-1", index=12, nonce=7, value_wei=str(VALUE))
    assert status == 200
    assert_native(env, result, index=12, nonce=7)
    # the request of the engine: priority fee = max fee, so that nothing of the balance stays
    assert result["decoded"]["max_priority_fee_per_gas"] == result["decoded"]["max_fee_per_gas"]


async def test_lower_priority_fee_is_signed_as_sent(api, env):
    status, result = await api.sweep_native("native-prio-1", max_priority_fee_per_gas_wei=str(2 * GWEI))
    assert status == 200
    assert_native(env, result, max_priority_fee=2 * GWEI)


async def test_native_sweep_of_a_second_store_goes_to_its_destination(api, env):
    await api.derive("other", 0, 4)
    status, result = await api.sweep_native("native-other-1", store="other", index=3)
    assert status == 200
    assert_native(env, result, store="other", index=3, destination=NATIVE_OTHER_ANVIL)
    assert result["from"] != env.address("shop", 3)


async def test_native_sweep_on_other_chains(api, env):
    await api.derive("other", 0, 4)
    # polygon has native_gas_limit_cap 60000 and a fee share of 25% in the test config
    status, result = await api.sweep_native("native-polygon-1", chain="polygon", store="other", index=2, gas_limit=50000)
    assert status == 200
    assert_native(env, result, store="other", index=2, destination=NATIVE_OTHER_POLYGON, chain_id=137, gas=50000)
    status, result = await api.sweep_native("native-bnb-1", chain="bnb", index=1, nonce=3, value_wei=str(2 * 10**18))
    assert status == 200
    assert_native(env, result, index=1, destination=NATIVE_SHOP_BNB, chain_id=56, nonce=3, value=2 * 10**18)


async def test_store_without_native_destination_is_refused(api, env):
    # 'shop' has a token destination on polygon and no native destination there
    status, result = await api.sweep("native-nodest-0", chain="polygon")
    assert status == 200
    result = await refused(api, 400, "unknown_store", chain="polygon")
    assert "native destination" in result["detail"]
    # 'other' has no token destination on polygon; its native destination there is sufficient for a native sweep
    await api.derive("other", 0, 1)
    status, result = await api.sweep_native("native-nodest-1", chain="polygon", store="other", index=0)
    assert status == 200
    assert_native(env, result, store="other", index=0, destination=NATIVE_OTHER_POLYGON, chain_id=137)
    await refused(api, 400, "unknown_store", chain="bnb", store="other", index=0)


async def test_token_sweep_and_native_sweep_have_different_recipients(api, env):
    status, token = await api.sweep("native-both-1", index=6, nonce=0)
    status, native = await api.sweep_native("native-both-2", index=6, nonce=1)
    assert status == 200
    assert_native(env, native, index=6, nonce=1)
    assert (token["decoded"]["to"], token["decoded"]["value"]) == (USDT_ANVIL, 0)
    assert helpers.decode_transfer(token["decoded"]["data"]) == (DEST_SHOP_ANVIL, 2000000)
    assert token["decoded"]["signer"] == native["decoded"]["signer"] == env.address("shop", 6)


async def test_value_zero_is_refused(api):
    await refused(api, 400, "invalid_request", value_wei="0")
    await refused(api, 400, "invalid_request", value_wei="0", max_fee_per_gas_wei="0", max_priority_fee_per_gas_wei="0")


async def test_gas_limit_over_the_native_cap_is_refused(api, env):
    # default cap 21000: a transfer to a plain address. The cap of the token sweep (150000) is not the cap here.
    assert CAPS["gas_limit_cap"] > GAS
    big = str(10**18)
    await refused(api, 403, "cap_exceeded", gas_limit=GAS + 1, value_wei=big)
    await refused(api, 403, "cap_exceeded", gas_limit=CAPS["gas_limit_cap"], value_wei=big)
    await refused(api, 400, "invalid_request", gas_limit=GAS - 1)
    await refused(api, 400, "invalid_request", gas_limit=0)
    await api.derive("other", 0, 1)
    polygon = {"chain": "polygon", "store": "other", "index": 0, "value_wei": big}
    await refused(api, 403, "cap_exceeded", gas_limit=NATIVE_GAS_CAP_POLYGON + 1, **polygon)
    status, result = await api.sweep_native("native-gascap-1", gas_limit=NATIVE_GAS_CAP_POLYGON, **polygon)
    assert status == 200
    expect = {"destination": NATIVE_OTHER_POLYGON, "chain_id": 137, "gas": NATIVE_GAS_CAP_POLYGON, "value": 10**18}
    assert_native(env, result, store="other", index=0, **expect)


async def test_max_fee_over_the_cap_is_refused(api, env):
    cap = CAPS["max_fee_per_gas_cap_wei"]
    big = str(10**18)
    await refused(api, 403, "cap_exceeded", max_fee_per_gas_wei=str(cap + 1), max_priority_fee_per_gas_wei="0", value_wei=big)
    await refused(api, 400, "invalid_request", max_fee_per_gas_wei=str(cap), max_priority_fee_per_gas_wei=str(cap + 1))
    status, result = await api.sweep_native(
        "native-feecap-1", max_fee_per_gas_wei=str(cap), max_priority_fee_per_gas_wei=str(cap), value_wei=big
    )
    assert status == 200
    assert_native(env, result, max_fee=cap, value=10**18)


async def test_fee_share_rule_default_10_percent(api, env):
    """Refused when gas_limit x max_fee > 10% of (value + gas_limit x max_fee): the value must be 9 x the fee or more."""
    fee = GAS * FEE
    await refused(api, 403, "cap_exceeded", value_wei=str(9 * fee - 1))
    await refused(api, 403, "cap_exceeded", value_wei=str(fee))
    await refused(api, 403, "cap_exceeded", value_wei="1")
    result = await refused(api, 403, "cap_exceeded", value_wei=str(3 * fee))  # the 25% of another chain is not the rule here
    assert "native_max_fee_share_percent" in result["detail"]
    status, result = await api.sweep_native("native-share-1", value_wei=str(9 * fee))
    assert status == 200
    assert_native(env, result, value=9 * fee)
    # the rule uses max_fee_per_gas, not the priority fee: a low priority fee does not make a high max fee acceptable
    await refused(api, 403, "cap_exceeded", nonce=1, value_wei=str(9 * fee - 1), max_priority_fee_per_gas_wei="0")
    # fee 0 has share 0 (such a transaction is never in a block)
    status, result = await api.sweep_native(
        "native-share-2", nonce=1, value_wei="1", max_fee_per_gas_wei="0", max_priority_fee_per_gas_wei="0"
    )
    assert status == 200
    assert_native(env, result, nonce=1, value=1, max_fee=0)


async def test_fee_share_rule_from_the_config(api, env):
    await api.derive("other", 0, 1)
    assert NATIVE_SHARE_POLYGON == 25  # the value must be 3 x the fee or more
    polygon = {"chain": "polygon", "store": "other", "index": 0}
    for gas in (GAS, NATIVE_GAS_CAP_POLYGON):
        fee = gas * FEE
        await refused(api, 403, "cap_exceeded", gas_limit=gas, value_wei=str(3 * fee - 1), nonce=gas, **polygon)
        request = {"gas_limit": gas, "value_wei": str(3 * fee), "nonce": gas, **polygon}
        status, result = await api.sweep_native(f"native-share-{gas}", **request)
        assert status == 200
        expect = {"destination": NATIVE_OTHER_POLYGON, "chain_id": 137, "gas": gas, "value": 3 * fee, "nonce": gas}
        assert_native(env, result, store="other", index=0, **expect)


async def test_a_native_sweep_cannot_sign_a_deposit_away_as_fee(api, env):
    """The attack of SPEC 8.3: highest fee, smallest value. Then: many requests, and the sum of what was signed.

    This is the rule of the native sweep. The fee of a token sweep has its own rule: test_api_budget.py.
    """
    cap = CAPS["max_fee_per_gas_cap_wei"]
    highest = {"max_fee_per_gas_wei": str(cap), "max_priority_fee_per_gas_wei": str(cap)}
    for value in ("1", str(GAS * cap), str(9 * GAS * cap - 1)):
        await refused(api, 403, "cap_exceeded", value_wei=value, **highest)

    chooser = random.Random(8300)
    signed_fee = signed_value = accepted = 0
    for nonce in range(60):
        max_fee = chooser.choice([1, GWEI, FEE, cap // 7, cap])
        fee = GAS * max_fee
        value = chooser.choice([1, fee, 5 * fee, 9 * fee - 1, 9 * fee, 9 * fee + 1, 20 * fee, 10**18])
        status, result = await api.sweep_native(
            f"native-sum-{nonce:03d}",
            index=nonce % 20,
            nonce=nonce,
            value_wei=str(value),
            max_fee_per_gas_wei=str(max_fee),
            max_priority_fee_per_gas_wei=str(max_fee),
        )
        assert (status == 200) == (value >= 9 * fee), (value, fee, result)
        if status == 200:
            accepted += 1
            assert_native(env, result, index=nonce % 20, nonce=nonce, value=value, max_fee=max_fee)
            signed_fee += result["decoded"]["gas"] * result["decoded"]["max_fee_per_gas"]
            signed_value += result["decoded"]["value"]
        else:
            assert (status, result["error"]) == (403, "cap_exceeded")
    assert 10 < accepted < 60
    # of all coin that the signed transactions can move, at most 10% can be fee
    assert signed_fee * 100 <= 10 * (signed_fee + signed_value)
    for tx in api.transactions:
        assert tx["to"] == NATIVE_SHOP_ANVIL
        assert tx["data"] == b""


async def test_index_that_was_not_given_out_is_refused(api, env):
    await refused(api, 400, "invalid_request", index=20)
    await refused(api, 400, "invalid_request", index=2**31 - 1)
    await refused(api, 400, "invalid_request", store="other", index=0)
    status, result = await api.sweep_native("native-index-19", index=19)
    assert status == 200
    assert_native(env, result, index=19)


async def test_unknown_chain_and_store_are_refused(api):
    await refused(api, 400, "unknown_chain", chain="ethereum")
    await refused(api, 400, "unknown_chain", chain="31337")
    await refused(api, 400, "unknown_store", store="nostore")


FORBIDDEN_FIELDS = {
    "to": ATTACKER,
    "to_address": ATTACKER,
    "destination": ATTACKER,
    "native_destination": ATTACKER,
    "recipient": ATTACKER,
    "address": ATTACKER,
    "from": ATTACKER,
    "data": "0xa9059cbb" + "00" * 12 + ATTACKER[2:] + "ff" * 32,
    "input": "0x00",
    "raw": "0x02f8",
    "raw_tx": "0x02f8",
    "chain_id": 1,
    "chainId": 1,
    "contract": ATTACKER,
    "token": None,
    "amount": "1",
    "value": "1",
    "gas": 21000,
    "account": 9000,
    "path": "m/44'/60'/9000'/0'/0'",
    "family": "evm",
    "seed_id": "0" * 16,
    "type": 0,
    "gas_price": "1",
    "access_list": [],
    "fee_share": 100,
    "native_max_fee_share_percent": 100,
}


@pytest.mark.parametrize("name", sorted(FORBIDDEN_FIELDS))
async def test_native_sweep_has_no_field_for_a_destination_or_data(api, name):
    result = await refused(api, 400, "invalid_request", **{name: FORBIDDEN_FIELDS[name]})
    assert "unknown field" in result["detail"]
    assert ATTACKER not in str(api.audit_lines()[-1])


BAD_VALUES = {
    "idempotency_key": [None, "", "short-7", "k" * 65, "key with space", 12345678, REMOVE],
    "chain": [None, "", 31337, True, ["anvil"], "c" * 33, REMOVE],
    "store": [None, "", 1, ["shop"], "s" * 33, REMOVE],
    "index": [None, "3", -1, 2**31, 3.0, True, REMOVE],
    "nonce": [None, "0", -1, 2**63, 0.0, False, REMOVE],
    "gas_limit": [None, "21000", -1, 21000.0, True, 2**63, REMOVE],
    "value_wei": [None, 5000, 5.0, "", " 1", "+1", "-1", "01", "1e18", "0x10", "1.0", "9" * 79, str(2**256), True, REMOVE],
    "max_fee_per_gas_wei": [None, 60, "", "-1", "01", "1e9", REMOVE],
    "max_priority_fee_per_gas_wei": [None, 30, "", "-1", "01", REMOVE],
    "replaces": ["", "short-7", "k" * 65, 5, False],
}


@pytest.mark.parametrize("name", sorted(BAD_VALUES))
async def test_native_sweep_field_types_and_ranges(api, name):
    for value in BAD_VALUES[name]:
        changes = {name: value}
        if name == "idempotency_key":
            changes["key"] = "unused"
        result = await refused(api, 400, "invalid_request", **changes)
        assert name in result["detail"], (name, value)


def without_decoded(result):
    return {k: v for k, v in result.items() if k != "decoded"}


async def test_idempotency(api, env):
    status, first = await api.sweep_native("native-idem-1")
    assert_native(env, first)
    status, again = await api.sweep_native("native-idem-1", replaces=REMOVE)
    assert status == 200
    assert without_decoded(again) == without_decoded(first)
    assert_native(env, again)
    assert api.journal_rows() == 1
    assert api.audit_lines()[-1]["replay"] is True
    for changes in ({"value_wei": str(VALUE + 1)}, {"nonce": 1}, {"index": 4}, {"chain": "bnb"}, {"gas_limit": 21001}):
        rows = api.journal_rows()
        status, result = await api.sweep_native("native-idem-1", **changes)
        assert (status, result["error"]) == (409, "idempotency_conflict")
        assert api.journal_rows() == rows
    # a key is for one kind of request
    status, result = await api.sweep("native-idem-1")
    assert (status, result["error"]) == (409, "idempotency_conflict")
    status, result = await api.fund("native-idem-1")
    assert (status, result["error"]) == (409, "idempotency_conflict")


async def test_token_sweep_and_native_sweep_share_the_nonces_of_the_address(api, env):
    """One signature per (chain, sender, nonce) for all kinds: only one of the two could be in a block."""
    status, token = await api.sweep("native-nonce-1", index=6, nonce=0)
    assert status == 200
    result = await refused(api, 409, "idempotency_conflict", key="native-nonce-2", index=6, nonce=0)
    assert "signed already" in result["detail"]
    status, native = await api.sweep_native("native-nonce-3", index=6, nonce=1)
    assert status == 200
    assert_native(env, native, index=6, nonce=1)
    # and the other direction
    rows = api.journal_rows()
    status, result = await api.sweep("native-nonce-4", index=6, nonce=1)
    assert (status, result["error"]) == (409, "idempotency_conflict")
    status, result = await api.sweep("native-nonce-5", index=6, nonce=1, token=RECOVERY_TOKEN)
    assert (status, result["error"]) == (409, "idempotency_conflict")
    assert api.journal_rows() == rows
    # the space is per chain and per address
    status, other_chain = await api.sweep_native("native-nonce-6", chain="bnb", index=6, nonce=1)
    assert status == 200
    assert_native(env, other_chain, index=6, nonce=1, chain_id=56, destination=NATIVE_SHOP_BNB)
    status, other_address = await api.sweep_native("native-nonce-7", index=7, nonce=1)
    assert status == 200
    assert_native(env, other_address, index=7, nonce=1)
    # the nonce of the fee wallet is another space: a funding with the same number is no conflict
    status, fund = await api.fund("native-nonce-8", index=6, nonce=1)
    assert status == 200
    assert fund["decoded"]["signer"] == env.fee_address


async def test_replacement_cannot_change_the_kind(api, env):
    status, token = await api.sweep("native-kind-1", index=8, nonce=0)
    status, native = await api.sweep_native("native-kind-2", index=8, nonce=1)
    assert_native(env, native, index=8, nonce=1)
    bump = str(2 * FEE)
    high = {"max_fee_per_gas_wei": bump, "max_priority_fee_per_gas_wei": bump}
    request = {"index": 8, "nonce": 0, "replaces": "native-kind-1", **high}
    result = await refused(api, 400, "invalid_request", key="native-kind-3", **request)
    assert "same kind" in result["detail"]
    rows = api.journal_rows()
    status, result = await api.sweep("native-kind-4", index=8, nonce=1, max_fee_per_gas_wei=bump, replaces="native-kind-2")
    assert (status, result["error"]) == (400, "invalid_request")
    assert "same kind" in result["detail"]
    status, result = await api.fund("native-kind-5", index=8, nonce=1, max_fee_per_gas_wei=bump, replaces="native-kind-2")
    assert (status, result["error"]) == (400, "invalid_request")
    assert api.journal_rows() == rows


async def test_replacement_with_a_higher_fee_and_a_lower_value(api, env):
    balance = 10**17
    fee = GAS * FEE
    status, first = await api.sweep_native("native-repl-1", index=9, value_wei=str(balance - fee))
    assert_native(env, first, index=9, value=balance - fee)

    higher = FEE * 11 // 10
    low = {"max_priority_fee_per_gas_wei": "0", "index": 9, "replaces": "native-repl-1"}
    for max_fee in (FEE, FEE + 1, higher - 1):
        await refused(api, 400, "invalid_request", key="native-repl-2", max_fee_per_gas_wei=str(max_fee), **low)
    for changes in ({"index": 10}, {"nonce": 1}, {"chain": "bnb"}, {"store": "other"}):
        await api.derive("other", 0, 20)
        request = {"index": 9, "max_fee_per_gas_wei": str(higher), "max_priority_fee_per_gas_wei": "0", **changes}
        result = await refused(api, 400, "invalid_request", key="native-repl-2", replaces="native-repl-1", **request)
        assert "replaces" in result["detail"]

    # the engine: same balance, higher fee, so the value is lower
    new_fee = GAS * higher
    status, second = await api.sweep_native(
        "native-repl-3",
        index=9,
        value_wei=str(balance - new_fee),
        max_fee_per_gas_wei=str(higher),
        max_priority_fee_per_gas_wei=str(higher),
        replaces="native-repl-1",
    )
    assert status == 200
    assert_native(env, second, index=9, value=balance - new_fee, max_fee=higher)
    assert second["tx_hash"] != first["tx_hash"]
    assert api.audit_lines()[-1]["replaces"] == "native-repl-1"


async def test_caps_apply_to_a_replacement(api, env):
    cap = CAPS["max_fee_per_gas_cap_wei"]
    status, first = await api.sweep_native("native-replcap-1", value_wei=str(10**18))
    assert_native(env, first, value=10**18)
    replacement = {"key": "native-replcap-2", "replaces": "native-replcap-1", "max_priority_fee_per_gas_wei": "0"}
    await refused(api, 403, "cap_exceeded", max_fee_per_gas_wei=str(cap + 1), value_wei=str(10**18), **replacement)
    await refused(api, 403, "cap_exceeded", max_fee_per_gas_wei=str(2 * FEE), gas_limit=GAS + 1, **replacement)
    # the fee share rule: a replacement cannot move the value into the fee
    await refused(api, 403, "cap_exceeded", max_fee_per_gas_wei=str(cap), value_wei=str(9 * GAS * cap - 1), **replacement)
    await refused(api, 403, "cap_exceeded", max_fee_per_gas_wei=str(2 * FEE), value_wei="1", **replacement)
    await refused(api, 400, "invalid_request", max_fee_per_gas_wei=str(2 * FEE), value_wei="0", **replacement)


async def test_native_sweep_is_not_in_the_totals_of_the_fee_wallet(api, env):
    journal = api.state.journal
    address = env.address("shop", 3)
    assert (journal.fund_total("anvil", address), journal.sweep_budget("anvil", address)) == (PREFUND, PREFUND)
    status, result = await api.sweep_native("native-total-1", value_wei=str(10**18))
    assert status == 200
    assert_native(env, result, value=10**18)
    assert journal.daily_spend("anvil", "2026-09-29") == 0
    assert journal.fund_total("anvil", NATIVE_SHOP_ANVIL) == 0
    # the totals of the fee wallet are as before. The budget of the token sweeps is used up: the coin went away.
    assert (journal.fund_total("anvil", address), journal.sweep_budget("anvil", address)) == (PREFUND, 0)
    row = journal.get_signature("native-total-1")
    assert (row["kind"], row["value_wei"], row["fee_wei"], row["spend_delta_wei"]) == (
        "sweep_native",
        str(10**18),
        str(GAS * FEE),
        "0",
    )
    assert (row["from_address"], row["to_address"]) == (env.address("shop", 3), NATIVE_SHOP_ANVIL)


async def test_audit_line_of_a_native_sweep(api):
    status, result = await api.sweep_native("native-audit-1", index=4, nonce=2)
    assert status == 200
    line = api.audit_lines()[-1]
    assert (line["call"], line["result"], line["chain"], line["store"]) == ("sign/sweep_native", "ok", "anvil", "shop")
    assert (line["index"], line["nonce"], line["value_wei"], line["amount"]) == (4, 2, str(VALUE), None)
    assert (line["max_fee_per_gas_wei"], line["gas_limit"]) == (str(FEE), GAS)
    assert line["tx_hash"] == result["tx_hash"]
    assert line["idempotency_key"] == "native-audit-1"
