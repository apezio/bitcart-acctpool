"""Regression tests for the findings of review A that have no other file: F07, F10, F11, F14 (proofs adapted to v4).

Each test is the proof test of the review (review/signer-a-proofs/), with the imports of this folder.
Where a test is not the same as the proof, the text says what is different and why.
"""

import pytest
from conftest import CAPS, CONFIG, GWEI, NO_LIMITS, make_api

# ---------------------------------------------------------------- F07: a newline at the end of a field


@pytest.mark.parametrize("key", ["f07-key-1\n", "abcdefgh\n"])
async def test_idempotency_key_with_a_newline_at_the_end_is_refused(api, key):
    status, result = await api.fund(key)
    assert (status, result.get("error")) == (400, "invalid_request"), f"key {key!r} was accepted, status {status}"
    assert api.journal_rows() == 0


@pytest.mark.parametrize("field", ["value_wei", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei"])
async def test_amount_with_a_newline_at_the_end_is_refused(api, field):
    request = {"max_fee_per_gas_wei": "5", "max_priority_fee_per_gas_wei": "5", "value_wei": "5", field: "5\n"}
    status, result = await api.fund("f07-amount-1", **request)
    assert (status, result.get("error")) == (400, "invalid_request"), f"{field} '5\\n' was accepted, status {status}"


async def test_replaces_with_a_newline_at_the_end_is_refused(api):
    assert (await api.fund("f07-repl-1\n"))[0] in (200, 400)
    status, result = await api.fund("f07-repl-2", max_fee_per_gas_wei=str(200 * 10**9), replaces="f07-repl-1\n")
    assert (status, result.get("error")) == (400, "invalid_request")
    assert "replaces:" in result["detail"] and "8 to 64" in result["detail"]


@pytest.mark.parametrize("field", ["amount", "gas_limit", "token", "chain", "store"])
async def test_other_fields_with_a_newline_at_the_end(api, field):
    values = {"amount": "5\n", "gas_limit": "70000\n", "token": "0x" + "11" * 20 + "\n", "chain": "anvil\n", "store": "shop\n"}
    status, result = await api.sweep("f07-sweep-1", **{field: values[field]})
    assert status == 400
    assert result["error"] in ("invalid_request", "unknown_chain", "unknown_store")
    assert api.journal_rows() == 0


# ---------------------------------------------------------------- F10: every call with the token has a limit


async def test_calls_with_the_token_to_unknown_paths_write_nothing(env, aiohttp_client):
    """F10 was about audit lines of such calls. v4: an unknown path or a wrong method never reaches a handler; it
    writes no audit line and no journal row (the request log line only). Status has a limit (test_api_limits.py)."""
    env.write_config(CONFIG.replace(NO_LIMITS, ""))
    api = await make_api(env, aiohttp_client)
    before = open(env.paths.audit, "rb").read()
    statuses = [(await api.get("/v1/nothing"))[0] for _ in range(100)]
    statuses += [(await api.call("PUT", "/v1/sign/fund", {"x": 1}))[0] for _ in range(100)]
    assert statuses == [404] * 100 + [405] * 100
    assert open(env.paths.audit, "rb").read() == before
    assert api.journal_rows() == 0


# ---------------------------------------------------------------- F11: the fee budget after a native sweep


async def test_budget_is_not_more_than_the_funded_coin_that_is_still_on_the_address(bare, env):
    """The proof test reads the budget with journal.fund_total(). That function is the funding total for the cap
    max_fund_total_per_address_wei; it must not go down, or the cap would be open again after a native sweep.
    The budget of SPEC 7.8 (corrected) is journal.sweep_budget(). This test reads that. The rest is the proof test.
    """
    api = bare
    value = CAPS["max_fund_value_wei"]  # 0.3
    address = env.address("shop", 3)
    assert (await api.fund("f11-fund-0", index=3, nonce=0, value_wei=str(value)))[0] == 200
    fee = 21000 * 60 * GWEI
    moved = value - fee
    status, native = await api.sweep_native("f11-native-0", index=3, nonce=0, value_wei=str(moved))
    assert status == 200 and native["from"] == address

    budget = api.state.journal.sweep_budget("anvil", address)
    left_of_the_funding = value - moved - fee
    assert budget <= left_of_the_funding
    assert api.state.journal.fund_total("anvil", address) == value
    # and the call itself: no token sweep with the budget of the funding that went away
    status, result = await api.sweep(
        "f11-sweep-0", index=3, nonce=1, gas_limit=21000, max_fee_per_gas_wei="1", max_priority_fee_per_gas_wei="0"
    )
    assert (status, result["error"]) == (403, "cap_exceeded")


# ---------------------------------------------------------------- F14: a replacement and the highest fee


async def test_replacement_must_be_higher_than_every_signature_of_the_nonce(api):
    assert (await api.sweep("f14-key-k1", max_fee_per_gas_wei=str(60 * GWEI)))[0] == 200
    assert (await api.sweep("f14-key-k2", max_fee_per_gas_wei=str(600 * GWEI), replaces="f14-key-k1"))[0] == 200
    status, result = await api.sweep("f14-key-k3", max_fee_per_gas_wei=str(66 * GWEI), amount="1", replaces="f14-key-k1")
    assert status == 400, "a 'replacement' with a lower fee than the signed K2 was signed"
    status, result = await api.sweep("f14-key-k3", max_fee_per_gas_wei=str(659 * GWEI), amount="1", replaces="f14-key-k1")
    assert status == 400
    status, result = await api.sweep("f14-key-k3", max_fee_per_gas_wei=str(660 * GWEI), amount="1", replaces="f14-key-k1")
    assert status == 200
    assert (result["decoded"]["max_fee_per_gas"], result["decoded"]["nonce"]) == (660 * GWEI, 0)


async def test_fee_zero_cannot_be_replaced_by_fee_zero(api):
    zero = {"max_fee_per_gas_wei": "0", "max_priority_fee_per_gas_wei": "0"}
    assert (await api.sweep("f14-key-z1", **zero))[0] == 200
    status, _ = await api.sweep("f14-key-z2", amount="7", replaces="f14-key-z1", **zero)
    assert status == 400, "0 is accepted as '10% higher' than 0"
    status, result = await api.sweep(
        "f14-key-z3", amount="7", replaces="f14-key-z1", max_fee_per_gas_wei="1", max_priority_fee_per_gas_wei="0"
    )
    assert status == 200
    assert result["decoded"]["max_fee_per_gas"] == 1
