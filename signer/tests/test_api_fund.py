"""POST /v1/sign/fund: the transaction shape and every refusal."""

import pytest
from conftest import ATTACKER, CAPS, ETHER, GWEI, REMOVE, USDT_ANVIL

FEE = 60 * GWEI
SPEND_MAX_VALUE = CAPS["max_fund_value_wei"] + 21000 * FEE


async def refused(api, status, code, **changes):
    """The request is refused, nothing is signed, nothing is in the journal, the refusal is in the audit log."""
    rows, signed = api.journal_rows(), len(api.transactions)
    got, result = await api.fund(changes.pop("key", "fund-refused-1"), **changes)
    assert (got, result["error"]) == (status, code), result
    assert set(result) == {"error", "detail"}
    assert api.journal_rows() == rows
    assert len(api.transactions) == signed
    line = api.audit_lines()[-1]
    assert (line["call"], line["result"], line["tx_hash"]) == ("sign/fund", code, None)
    return result


async def test_fund_transaction_has_the_fixed_shape(api, env):
    status, result = await api.fund("fund-shape-1", index=12, nonce=5, value_wei="4000000000000000")
    assert status == 200
    assert result["from"] == env.fee_address
    assert result["to"] == env.address("shop", 12)
    assert result["chain_id"] == 31337
    tx = result["decoded"]
    assert tx["type"] == 2
    assert tx["chain_id"] == 31337
    assert tx["nonce"] == 5
    assert tx["to"] == env.address("shop", 12)
    assert tx["value"] == 4000000000000000
    assert tx["data"] == b""
    assert tx["gas"] == 21000
    assert tx["max_fee_per_gas"] == 60 * GWEI
    assert tx["max_priority_fee_per_gas"] == 30 * GWEI
    assert tx["access_list"] == []
    assert tx["signer"] == env.fee_address
    assert tx["hash"] == result["tx_hash"]


async def test_fund_on_the_second_chain_has_its_chain_id(api, env):
    status, result = await api.fund("fund-polygon-1", chain="polygon", index=0, nonce=9)
    assert status == 200
    tx = result["decoded"]
    assert (tx["chain_id"], result["chain_id"]) == (137, 137)
    # one fee wallet and one deposit address for all EVM chains
    assert tx["signer"] == env.fee_address
    assert tx["to"] == env.address("shop", 0)
    assert (tx["nonce"], tx["gas"], tx["data"], tx["value"]) == (9, 21000, b"", 5 * 10**15)


async def test_fund_for_a_second_store_uses_its_account(api, env):
    await api.derive("other", 0, 5)
    status, result = await api.fund("fund-other-1", store="other", index=4)
    assert status == 200
    tx = result["decoded"]
    assert tx["to"] == env.address("other", 4)
    assert tx["to"] != env.address("shop", 4)
    assert tx["signer"] == env.fee_address
    assert (tx["chain_id"], tx["nonce"], tx["gas"], tx["data"], tx["value"]) == (31337, 0, 21000, b"", 5 * 10**15)
    assert (tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (60 * GWEI, 30 * GWEI)


async def test_value_zero_and_fee_zero_are_signed_as_sent(api, env):
    status, result = await api.fund("fund-zero-1", value_wei="0", max_fee_per_gas_wei="0", max_priority_fee_per_gas_wei="0")
    assert status == 200
    tx = result["decoded"]
    assert (tx["value"], tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (0, 0, 0)
    assert (tx["to"], tx["signer"], tx["gas"], tx["data"], tx["chain_id"], tx["nonce"]) == (
        env.address("shop", 3),
        env.fee_address,
        21000,
        b"",
        31337,
        0,
    )


async def test_max_fee_over_the_cap_is_refused(api, env):
    cap = CAPS["max_fee_per_gas_cap_wei"]
    await refused(api, 403, "cap_exceeded", max_fee_per_gas_wei=str(cap + 1))
    await refused(api, 403, "cap_exceeded", max_fee_per_gas_wei=str(cap * 1000))
    status, result = await api.fund("fund-feecap-1", max_fee_per_gas_wei=str(cap))
    assert status == 200
    tx = result["decoded"]
    assert tx["max_fee_per_gas"] == cap
    assert (tx["to"], tx["signer"], tx["value"], tx["gas"], tx["data"]) == (
        env.address("shop", 3),
        env.fee_address,
        5 * 10**15,
        21000,
        b"",
    )
    assert (tx["chain_id"], tx["nonce"], tx["max_priority_fee_per_gas"]) == (31337, 0, 30 * GWEI)


async def test_priority_fee_over_max_fee_is_refused(api):
    await refused(
        api, 400, "invalid_request", max_fee_per_gas_wei=str(10 * GWEI), max_priority_fee_per_gas_wei=str(10 * GWEI + 1)
    )
    # the priority fee cannot be a way around the cap
    cap = CAPS["max_fee_per_gas_cap_wei"]
    await refused(api, 400, "invalid_request", max_fee_per_gas_wei=str(cap), max_priority_fee_per_gas_wei=str(cap + 1))


async def test_value_over_the_cap_is_refused(api, env):
    cap = CAPS["max_fund_value_wei"]
    await refused(api, 403, "cap_exceeded", value_wei=str(cap + 1))
    await refused(api, 403, "cap_exceeded", value_wei=str(100 * ETHER))
    status, result = await api.fund("fund-valuecap-1", value_wei=str(cap))
    assert status == 200
    tx = result["decoded"]
    assert tx["value"] == cap
    assert (tx["to"], tx["signer"], tx["gas"], tx["data"], tx["chain_id"], tx["nonce"]) == (
        env.address("shop", 3),
        env.fee_address,
        21000,
        b"",
        31337,
        0,
    )
    assert (tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (60 * GWEI, 30 * GWEI)


async def test_funding_total_per_address_over_the_cap_is_refused(api, env):
    value = CAPS["max_fund_value_wei"]  # 3 of these are the total cap exactly
    for nonce in range(3):
        status, result = await api.fund(f"fund-total-{nonce}", index=5, nonce=nonce, value_wei=str(value))
        assert status == 200
        tx = result["decoded"]
        assert (tx["to"], tx["value"], tx["nonce"], tx["signer"]) == (env.address("shop", 5), value, nonce, env.fee_address)
        assert (tx["gas"], tx["data"], tx["chain_id"]) == (21000, b"", 31337)
        assert (tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (60 * GWEI, 30 * GWEI)
    await refused(api, 403, "cap_exceeded", index=5, nonce=3, value_wei="1")
    await refused(api, 403, "cap_exceeded", index=5, nonce=3, value_wei=str(value))
    # the total is per (chain, address): the next address and the other chain are not changed by it
    for key, changes in {"fund-total-a": {"index": 6}, "fund-total-b": {"index": 5, "chain": "polygon", "nonce": 0}}.items():
        status, result = await api.fund(key, **{"nonce": 3, "value_wei": str(value), **changes})
        assert status == 200
        tx = result["decoded"]
        assert tx["to"] == env.address("shop", changes["index"])
        assert tx["chain_id"] == (137 if "chain" in changes else 31337)
        assert (tx["value"], tx["nonce"], tx["signer"], tx["gas"], tx["data"]) == (
            value,
            changes.get("nonce", 3),
            env.fee_address,
            21000,
            b"",
        )
        assert (tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (60 * GWEI, 30 * GWEI)


async def test_daily_spend_over_the_cap_is_refused(api, env):
    value = CAPS["max_fund_value_wei"]
    cap = CAPS["fee_wallet_daily_cap_wei"]
    for nonce in range(6):  # 6 x 0.30126 = 1.80756 of 2.0
        status, result = await api.fund(f"fund-daily-{nonce}", index=nonce, nonce=nonce, value_wei=str(value))
        assert status == 200
        tx = result["decoded"]
        assert (tx["to"], tx["value"], tx["nonce"], tx["signer"]) == (
            env.address("shop", nonce),
            value,
            nonce,
            env.fee_address,
        )
        assert (tx["gas"], tx["data"], tx["chain_id"], tx["max_fee_per_gas"]) == (21000, b"", 31337, FEE)
        assert tx["max_priority_fee_per_gas"] == 30 * GWEI
    assert api.state.journal.daily_spend("anvil", "2026-09-29") == 6 * SPEND_MAX_VALUE
    await refused(api, 403, "daily_cap_exceeded", index=6, nonce=6, value_wei=str(value))

    # the gas part counts: value + 21000 x max fee
    rest = cap - 6 * SPEND_MAX_VALUE
    await refused(api, 403, "daily_cap_exceeded", index=6, nonce=6, value_wei=str(rest - 21000 * FEE + 1))
    await refused(
        api, 403, "daily_cap_exceeded", index=6, nonce=6, value_wei=str(rest - 21000 * FEE), max_fee_per_gas_wei=str(FEE + 1)
    )
    status, result = await api.fund("fund-daily-exact", index=6, nonce=6, value_wei=str(rest - 21000 * FEE))
    assert status == 200
    tx = result["decoded"]
    assert (tx["to"], tx["value"], tx["nonce"], tx["signer"]) == (
        env.address("shop", 6),
        rest - 21000 * FEE,
        6,
        env.fee_address,
    )
    assert (tx["gas"], tx["data"], tx["chain_id"], tx["max_fee_per_gas"]) == (21000, b"", 31337, FEE)
    assert tx["max_priority_fee_per_gas"] == 30 * GWEI
    assert api.state.journal.daily_spend("anvil", "2026-09-29") == cap
    await refused(
        api,
        403,
        "daily_cap_exceeded",
        index=7,
        nonce=7,
        value_wei="0",
        max_fee_per_gas_wei="1",
        max_priority_fee_per_gas_wei="0",
    )

    # the cap is per chain
    status, result = await api.fund("fund-daily-polygon", chain="polygon", index=7, nonce=0, value_wei=str(value))
    assert status == 200
    tx = result["decoded"]
    assert (tx["chain_id"], tx["to"], tx["value"], tx["signer"]) == (137, env.address("shop", 7), value, env.fee_address)
    assert (tx["gas"], tx["data"], tx["nonce"], tx["max_fee_per_gas"]) == (21000, b"", 0, FEE)
    assert tx["max_priority_fee_per_gas"] == 30 * GWEI

    # 23:59:59 of the same UTC day: refused. 00:00:00 of the next day: a new total.
    env.clock.now = env.clock.now.replace(hour=23, minute=59, second=59)
    await refused(api, 403, "daily_cap_exceeded", index=7, nonce=7, value_wei=str(value))
    env.clock.now = env.clock.now.replace(day=30, hour=0, minute=0, second=0)
    status, result = await api.fund("fund-daily-nextday", index=7, nonce=7, value_wei=str(value))
    assert status == 200
    tx = result["decoded"]
    assert (tx["to"], tx["value"], tx["nonce"], tx["signer"]) == (env.address("shop", 7), value, 7, env.fee_address)
    assert (tx["gas"], tx["data"], tx["chain_id"], tx["max_fee_per_gas"]) == (21000, b"", 31337, FEE)
    assert tx["max_priority_fee_per_gas"] == 30 * GWEI
    assert api.state.journal.daily_spend("anvil", "2026-09-30") == SPEND_MAX_VALUE
    assert api.state.journal.daily_spend("anvil", "2026-09-29") == cap


async def test_index_that_was_not_given_out_is_refused(api, env):
    await refused(api, 400, "invalid_request", index=20)  # the fixture gave out 0..19
    await refused(api, 400, "invalid_request", index=2**31 - 1)
    await refused(api, 400, "invalid_request", store="other", index=0)  # no derive call for this store
    status, result = await api.fund("fund-index-19", index=19)
    assert status == 200
    tx = result["decoded"]
    assert (tx["to"], tx["signer"], tx["value"], tx["gas"], tx["data"]) == (
        env.address("shop", 19),
        env.fee_address,
        5 * 10**15,
        21000,
        b"",
    )
    assert (tx["chain_id"], tx["nonce"], tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (31337, 0, FEE, 30 * GWEI)
    await api.derive("shop", 20, 1)
    status, result = await api.fund("fund-index-20", index=20, nonce=1)
    assert status == 200
    tx = result["decoded"]
    assert (tx["to"], tx["signer"], tx["nonce"], tx["value"], tx["gas"], tx["data"], tx["chain_id"]) == (
        env.address("shop", 20),
        env.fee_address,
        1,
        5 * 10**15,
        21000,
        b"",
        31337,
    )
    assert (tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (FEE, 30 * GWEI)


async def test_unknown_chain_and_store_are_refused(api):
    await refused(api, 400, "unknown_chain", chain="ethereum")
    await refused(api, 400, "unknown_chain", chain="31337")
    await refused(api, 400, "unknown_chain", chain="Anvil")
    await refused(api, 400, "unknown_store", store="nostore")
    await refused(api, 400, "unknown_store", store="Shop")
    # 'other' has no destination on polygon: an address there could not be swept
    await api.derive("other", 0, 1)
    await refused(api, 400, "unknown_store", store="other", chain="polygon", index=0)


FORBIDDEN_FIELDS = {
    "to": ATTACKER,
    "to_address": ATTACKER,
    "destination": ATTACKER,
    "recipient": ATTACKER,
    "address": ATTACKER,
    "from": ATTACKER,
    "data": "0xa9059cbb",
    "input": "0xa9059cbb",
    "raw": "0x02f8",
    "raw_tx": "0x02f8",
    "chain_id": 1,
    "chainId": 1,
    "contract": USDT_ANVIL,
    "token": None,
    "gas": 500000,
    "gas_limit": 500000,
    "amount": "1",
    "value": "1",
    "account": 9000,
    "path": "m/44'/60'/9000'/0'/0'",
    "family": "evm",
    "seed_id": "0" * 16,
    "type": 0,
    "gas_price": "1",
    "access_list": [],
    "": 1,
    "x" * 100: 1,
}


@pytest.mark.parametrize("name", sorted(FORBIDDEN_FIELDS))
async def test_fund_has_no_field_for_a_destination_or_data(api, name):
    result = await refused(api, 400, "invalid_request", **{name: FORBIDDEN_FIELDS[name]})
    assert "unknown field" in result["detail"]
    assert ATTACKER not in str(api.audit_lines()[-1])


BAD_VALUES = {
    "idempotency_key": [
        None,
        "",
        "short-7",
        "k" * 65,
        "key with space",
        "key\nnewline",
        "kéy-unicode-1",
        12345678,
        ["k" * 8],
        REMOVE,
    ],
    "chain": [None, "", 137, True, ["anvil"], {"name": "anvil"}, "c" * 33, REMOVE],
    "store": [None, "", 1, False, ["shop"], "s" * 33, REMOVE],
    "index": [None, "3", -1, 2**31, 3.0, 3.5, True, [3], REMOVE],
    "nonce": [None, "0", -1, 2**63, 2**64, 0.0, 1.5, False, [0], REMOVE],
    "value_wei": [
        None,
        4000,
        4000.0,
        "",
        " 1",
        "1 ",
        "+1",
        "-1",
        "01",
        "1e18",
        "0x10",
        "1.0",
        "1,000",
        "٣",
        "9" * 79,
        str(2**256),
        True,
        ["1"],
        REMOVE,
    ],
    "max_fee_per_gas_wei": [None, 60, "", "-1", "01", "1e9", "0x1", "6" * 79, str(2**256), False, REMOVE],
    "max_priority_fee_per_gas_wei": [None, 30, "", "-1", "01", "1e9", "0x1", REMOVE],
    "replaces": ["", "short-7", "k" * 65, 5, False, ["fund-refused-0"]],
}


@pytest.mark.parametrize("name", sorted(BAD_VALUES))
async def test_fund_field_types_and_ranges(api, name):
    for value in BAD_VALUES[name]:
        changes = {name: value}
        if name == "idempotency_key":
            changes["key"] = "unused"
        result = await refused(api, 400, "invalid_request", **changes)
        assert name in result["detail"], (name, value)


async def test_optional_field_replaces_can_be_absent(api, env):
    status, result = await api.fund("fund-noreplaces-1", replaces=REMOVE)
    assert status == 200
    tx = result["decoded"]
    assert (tx["to"], tx["signer"], tx["value"], tx["gas"], tx["data"], tx["chain_id"], tx["nonce"]) == (
        env.address("shop", 3),
        env.fee_address,
        5 * 10**15,
        21000,
        b"",
        31337,
        0,
    )
    assert (tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (FEE, 30 * GWEI)


async def test_audit_line_of_a_fund(api):
    status, result = await api.fund("fund-audit-1", index=4, nonce=8)
    assert status == 200
    line = api.audit_lines()[-1]
    assert line["call"] == "sign/fund"
    assert line["result"] == "ok"
    assert line["chain"] == "anvil"
    assert line["store"] == "shop"
    assert line["index"] == 4
    assert line["nonce"] == 8
    assert line["value_wei"] == "5000000000000000"
    assert line["amount"] is None
    assert line["max_fee_per_gas_wei"] == str(FEE)
    assert line["tx_hash"] == result["tx_hash"]
    assert line["idempotency_key"] == "fund-audit-1"
    assert len(line["prev"]) == 64
    assert line["seq"] == len(api.audit_lines())
    assert line["ts"].startswith("20")
    assert "raw_tx" not in line
