"""Fee budget of the token sweeps (SPEC 7.8), as a hostile caller.

The fee of a token sweep is paid from the native balance of the deposit address. That address can hold
native coin of a customer. Rule: the sum of gas_limit x max_fee_per_gas of the token sweeps that the signer
signed for a (chain, address) is never higher than the sum of the funding values that it signed for it.

All fundings of this file are real calls. No funding row is written into the journal by the test.
"""

import helpers
import pytest
from conftest import CAPS, DEST_SHOP_ANVIL, GWEI, RECOVERY_TOKEN, USDT_ANVIL

GAS_CAP = CAPS["gas_limit_cap"]
FEE_CAP = CAPS["max_fee_per_gas_cap_wei"]
HIGHEST = GAS_CAP * FEE_CAP  # the highest fee of one token sweep: 0.15 of the coin with the test config
FUND_MAX = CAPS["max_fund_value_wei"]


@pytest.fixture
async def api(bare):
    """The signer WITHOUT a funding in the journal: all fundings of this file are real calls."""
    return bare


def fee_of(result) -> int:
    return result["decoded"]["gas"] * result["decoded"]["max_fee_per_gas"]


def assert_sweep(env, result, *, index, nonce, gas, max_fee, amount=2000000, contract=USDT_ANVIL, max_priority_fee=0):
    tx = result["decoded"]
    assert (tx["type"], tx["chain_id"], tx["nonce"], tx["to"], tx["value"]) == (2, 31337, nonce, contract, 0)
    assert helpers.decode_transfer(tx["data"]) == (DEST_SHOP_ANVIL, amount)
    assert (tx["gas"], tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (gas, max_fee, max_priority_fee)
    assert tx["signer"] == env.address("shop", index)
    assert (result["from"], result["to"], result["chain_id"]) == (tx["signer"], contract, 31337)


def assert_fund(env, result, *, index, nonce, value, max_fee=60 * GWEI):
    tx = result["decoded"]
    assert (tx["type"], tx["chain_id"], tx["nonce"], tx["to"], tx["value"]) == (
        2,
        31337,
        nonce,
        env.address("shop", index),
        value,
    )
    assert (tx["data"], tx["gas"], tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (b"", 21000, max_fee, 30 * GWEI)
    assert tx["signer"] == env.fee_address


async def fund(api, env, key, index, nonce, value, **changes):
    status, result = await api.fund(key, index=index, nonce=nonce, value_wei=str(value), **changes)
    assert status == 200, result
    assert_fund(env, result, index=index, nonce=nonce, value=value, max_fee=int(changes.get("max_fee_per_gas_wei", 60 * GWEI)))
    return result


async def sweep(api, key, index, nonce, gas, max_fee, **changes):
    request = {"index": index, "nonce": nonce, "gas_limit": gas, "max_fee_per_gas_wei": str(max_fee)}
    return await api.sweep(key, max_priority_fee_per_gas_wei="0", **request, **changes)


async def refused(api, key, index, nonce, gas, max_fee, **changes):
    rows, signed = api.journal_rows(), len(api.transactions)
    status, result = await sweep(api, key, index, nonce, gas, max_fee, **changes)
    assert (status, result["error"]) == (403, "cap_exceeded"), result
    assert "signed funding" in result["detail"]
    assert api.journal_rows() == rows
    assert len(api.transactions) == signed
    assert api.audit_lines()[-1]["result"] == "cap_exceeded"


async def test_sweep_before_a_funding_is_refused(api, env):
    await refused(api, "budget-none-1", 3, 0, 70000, 60 * GWEI)
    await refused(api, "budget-none-2", 3, 0, 21000, 1)  # the smallest fee that is not 0
    await refused(api, "budget-none-3", 3, 0, 70000, 60 * GWEI, token=RECOVERY_TOKEN)
    await refused(api, "budget-none-4", 3, 0, GAS_CAP, FEE_CAP)
    assert api.state.journal.fund_total("anvil", env.address("shop", 3)) == 0
    assert api.state.journal.sweep_budget("anvil", env.address("shop", 3)) == 0
    # a fee of 0 uses nothing of the address (such a transaction is never in a block)
    status, result = await sweep(api, "budget-none-5", 3, 0, 70000, 0)
    assert status == 200
    assert_sweep(env, result, index=3, nonce=0, gas=70000, max_fee=0)


async def test_sweep_up_to_the_funding_and_not_more(api, env):
    value = 70000 * 60 * GWEI
    await fund(api, env, "budget-exact-f", 3, 0, value)
    await refused(api, "budget-exact-1", 3, 0, 70000, 60 * GWEI + 1)
    await refused(api, "budget-exact-2", 3, 0, 70001, 60 * GWEI)
    status, result = await sweep(api, "budget-exact-3", 3, 0, 70000, 60 * GWEI)
    assert status == 200
    assert_sweep(env, result, index=3, nonce=0, gas=70000, max_fee=60 * GWEI)
    assert fee_of(result) == value
    # the budget is used: the next nonce gets nothing, also not 1 wei for each gas
    await refused(api, "budget-exact-4", 3, 1, 21000, 1)
    # the address of the next index has its own budget, and it is empty
    await refused(api, "budget-exact-5", 4, 0, 21000, 1)


async def test_funding_of_another_address_or_chain_is_not_a_budget(api, env):
    await fund(api, env, "budget-other-f1", 4, 0, FUND_MAX)
    status, result = await api.fund("budget-other-f3", index=3, nonce=0, chain="polygon", value_wei=str(FUND_MAX))
    assert status == 200
    assert (result["decoded"]["chain_id"], result["decoded"]["to"], result["decoded"]["value"]) == (
        137,
        env.address("shop", 3),
        FUND_MAX,
    )
    assert (result["decoded"]["signer"], result["decoded"]["gas"], result["decoded"]["data"]) == (env.fee_address, 21000, b"")
    await refused(api, "budget-other-1", 3, 0, 70000, 60 * GWEI)  # index 3 on anvil has no funding


async def test_many_sweeps_with_the_highest_gas_values(api, env):
    """The attack: the address has a native deposit of a customer, and the caller signs the most expensive sweeps."""
    assert HIGHEST == 15 * 10**16
    await fund(api, env, "budget-many-f", 7, 0, FUND_MAX)  # 0.3: two sweeps of 0.15
    signed = []
    for nonce in range(40):
        status, result = await sweep(api, f"budget-many-{nonce:02d}", 7, nonce, GAS_CAP, FEE_CAP, amount="1")
        if status == 200:
            assert_sweep(env, result, index=7, nonce=nonce, gas=GAS_CAP, max_fee=FEE_CAP, amount=1)
            signed.append(result)
        else:
            assert (status, result["error"]) == (403, "cap_exceeded")
    assert len(signed) == 2
    assert sum(fee_of(result) for result in signed) == FUND_MAX
    # smaller fees, other amounts, a recovery token: nothing more
    await refused(api, "budget-many-x1", 7, 40, 21000, 1)
    await refused(api, "budget-many-x2", 7, 40, 70000, GWEI, amount=str(2**255))
    await refused(api, "budget-many-x3", 7, 40, 70000, GWEI, token=RECOVERY_TOKEN)
    assert api.state.journal.sweep_budget("anvil", env.address("shop", 7)) == 0


async def test_budget_can_never_be_more_than_the_funding_caps(api, env):
    """All fundings that the caps permit for one address, then the most expensive sweeps."""
    for nonce in range(3):
        await fund(api, env, f"budget-caps-f{nonce}", 8, nonce, FUND_MAX)
    status, result = await api.fund("budget-caps-f3", index=8, nonce=3, value_wei="1")
    assert (status, result["error"]) == (403, "cap_exceeded")
    total = 0
    for nonce in range(20):
        status, result = await sweep(api, f"budget-caps-{nonce:02d}", 8, nonce, GAS_CAP, FEE_CAP)
        if status == 200:
            assert_sweep(env, result, index=8, nonce=nonce, gas=GAS_CAP, max_fee=FEE_CAP)
            total += fee_of(result)
    assert total == 6 * HIGHEST == CAPS["max_fund_total_per_address_wei"]


async def test_sweep_replacement_counts_its_difference(api, env):
    await fund(api, env, "budget-repl-f", 9, 0, 10**16)
    fee = 60 * GWEI
    status, first = await sweep(api, "budget-repl-1", 9, 0, 70000, fee)
    assert status == 200
    assert_sweep(env, first, index=9, nonce=0, gas=70000, max_fee=fee)
    used = 70000 * fee
    journal = api.state.journal
    address = env.address("shop", 9)
    assert journal.sweep_budget("anvil", address) == 10**16 - used

    # the highest fee that the budget permits for this nonce: all of the funding, because the first one counts no more
    top = 10**16 // 70000
    await refused(api, "budget-repl-2", 9, 0, 70000, top + 1, replaces="budget-repl-1")
    status, second = await sweep(api, "budget-repl-3", 9, 0, 70000, top, replaces="budget-repl-1")
    assert status == 200
    assert_sweep(env, second, index=9, nonce=0, gas=70000, max_fee=top)
    assert journal.sweep_budget("anvil", address) == 10**16 - 70000 * top
    # a replacement with a lower gas limit and a higher fee for each gas: the highest of the nonce stays the measure
    status, third = await sweep(api, "budget-repl-4", 9, 0, 30000, top * 2, amount="5", replaces="budget-repl-3")
    assert status == 200
    assert_sweep(env, third, index=9, nonce=0, gas=30000, max_fee=top * 2, amount=5)
    assert journal.sweep_budget("anvil", address) == 10**16 - 70000 * top
    # a second nonce has the rest only
    rest = 10**16 - 70000 * top
    await refused(api, "budget-repl-5", 9, 1, 21000, rest // 21000 + 1)


async def test_replacements_cannot_add_up_to_more_than_the_funding(api, env):
    """Replacement after replacement at one nonce, each 10% higher: the sum that counts is the highest, and it has a limit."""
    await fund(api, env, "budget-chain-f", 10, 0, 10**16)
    fee = 10 * GWEI
    status, result = await sweep(api, "budget-chain-00", 10, 0, 70000, fee)
    assert status == 200
    assert_sweep(env, result, index=10, nonce=0, gas=70000, max_fee=fee)
    accepted = 1
    for step in range(1, 40):
        fee = fee * 11 // 10 + 1
        status, result = await sweep(
            api, f"budget-chain-{step:02d}", 10, 0, 70000, fee, replaces=f"budget-chain-{accepted - 1:02d}"
        )
        if status != 200:
            assert (status, result["error"]) == (403, "cap_exceeded")
            assert 70000 * fee > 10**16
            break
        assert_sweep(env, result, index=10, nonce=0, gas=70000, max_fee=fee)
        assert step == accepted
        accepted += 1
    else:
        pytest.fail("the replacements had no limit")
    assert accepted > 5
    assert 0 <= api.state.journal.sweep_budget("anvil", env.address("shop", 10)) < 10**16 // 10


async def test_funding_replacement_counts_one_time_in_the_budget(api, env):
    """A funding and its replacement have one nonce: only one of them can be in a block."""
    value = 70000 * 60 * GWEI
    await fund(api, env, "budget-frepl-1", 11, 5, value)
    await fund(api, env, "budget-frepl-2", 11, 5, value, max_fee_per_gas_wei=str(120 * GWEI), replaces="budget-frepl-1")
    await fund(api, env, "budget-frepl-3", 11, 5, value, max_fee_per_gas_wei=str(240 * GWEI), replaces="budget-frepl-2")
    assert api.state.journal.fund_total("anvil", env.address("shop", 11)) == value
    status, result = await sweep(api, "budget-frepl-s1", 11, 0, 70000, 60 * GWEI)
    assert status == 200
    assert_sweep(env, result, index=11, nonce=0, gas=70000, max_fee=60 * GWEI)
    # three funding signatures are not three budgets
    await refused(api, "budget-frepl-s2", 11, 1, 21000, 1)
    # a second funding with its own nonce is a second budget
    await fund(api, env, "budget-frepl-4", 11, 6, value)
    status, result = await sweep(api, "budget-frepl-s3", 11, 1, 70000, 60 * GWEI)
    assert status == 200
    assert_sweep(env, result, index=11, nonce=1, gas=70000, max_fee=60 * GWEI)
    await refused(api, "budget-frepl-s4", 11, 2, 21000, 1)


async def test_native_sweep_uses_the_budget_up(api, env):
    """SPEC 7.8 as corrected: budget = fundings - fees of token sweeps - (value + fee) of native sweeps, not below 0.

    The attack that this closes: a funding arrives, a native sweep moves it away, and the budget of the funding
    is then used for token sweeps that burn coin of a customer.
    """
    journal = api.state.journal
    address = env.address("shop", 12)
    value = 70000 * 60 * GWEI
    await fund(api, env, "budget-native-f1", 12, 0, FUND_MAX)
    assert journal.sweep_budget("anvil", address) == FUND_MAX
    # a native sweep of a part: value + gas_limit x max_fee go off the budget
    fee = 21000 * 60 * GWEI
    status, native = await api.sweep_native("budget-native-1", index=12, nonce=0, value_wei=str(10**17))
    assert status == 200
    assert (native["decoded"]["value"], native["decoded"]["data"], native["decoded"]["signer"]) == (10**17, b"", address)
    assert (native["decoded"]["gas"], native["decoded"]["max_fee_per_gas"], native["decoded"]["nonce"]) == (
        21000,
        60 * GWEI,
        0,
    )
    assert journal.sweep_budget("anvil", address) == FUND_MAX - 10**17 - fee
    # a native sweep of more than the budget (the deposit of a customer goes with it): the budget is 0, not less
    status, native = await api.sweep_native("budget-native-2", index=12, nonce=1, value_wei=str(5 * 10**18))
    assert status == 200
    assert (native["decoded"]["value"], native["decoded"]["nonce"], native["decoded"]["signer"]) == (5 * 10**18, 1, address)
    assert journal.sweep_budget("anvil", address) == 0
    await refused(api, "budget-native-3", 12, 2, 21000, 1)
    # a later token sweep needs a new funding, and gets that funding in full
    await fund(api, env, "budget-native-f2", 12, 1, value)
    assert journal.sweep_budget("anvil", address) == value
    await refused(api, "budget-native-4", 12, 2, 70000, 60 * GWEI + 1)
    status, result = await sweep(api, "budget-native-5", 12, 2, 70000, 60 * GWEI)
    assert status == 200
    assert_sweep(env, result, index=12, nonce=2, gas=70000, max_fee=60 * GWEI)
    assert journal.sweep_budget("anvil", address) == 0


async def test_native_sweep_replacement_counts_its_difference(api, env):
    journal = api.state.journal
    address = env.address("shop", 14)
    await fund(api, env, "budget-nrepl-f", 14, 0, FUND_MAX)
    fee = 21000 * 60 * GWEI
    status, first = await api.sweep_native("budget-nrepl-1", index=14, nonce=0, value_wei=str(10**17))
    assert status == 200
    assert journal.sweep_budget("anvil", address) == FUND_MAX - 10**17 - fee
    # the engine: same balance, double fee, so the value is lower by the fee. value + fee is the same: nothing more is used.
    bump = {"max_fee_per_gas_wei": str(120 * GWEI), "max_priority_fee_per_gas_wei": str(120 * GWEI)}
    status, second = await api.sweep_native(
        "budget-nrepl-2", index=14, nonce=0, value_wei=str(10**17 - fee), replaces="budget-nrepl-1", **bump
    )
    assert status == 200
    tx = second["decoded"]
    assert (tx["value"], tx["gas"], tx["max_fee_per_gas"], tx["nonce"], tx["signer"]) == (
        10**17 - fee,
        21000,
        120 * GWEI,
        0,
        address,
    )
    assert journal.sweep_budget("anvil", address) == FUND_MAX - 10**17 - fee


async def test_replay_of_a_sweep_uses_no_budget(api, env):
    value = 70000 * 60 * GWEI
    await fund(api, env, "budget-replay-f", 13, 0, value)
    status, first = await sweep(api, "budget-replay-1", 13, 0, 70000, 60 * GWEI)
    for _ in range(3):
        status, again = await sweep(api, "budget-replay-1", 13, 0, 70000, 60 * GWEI)
        assert status == 200
        assert again["raw_tx"] == first["raw_tx"]
        assert_sweep(env, again, index=13, nonce=0, gas=70000, max_fee=60 * GWEI)
    assert api.state.journal.sweep_budget("anvil", env.address("shop", 13)) == 0
    assert api.journal_rows() == 2
