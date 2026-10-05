"""POST /v1/sign/sweep: the transaction shape and every refusal."""

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
    OTHER_TOKEN,
    RECOVERY_TOKEN,
    REMOVE,
    USDT_ANVIL,
    USDT_BNB,
    USDT_POLYGON,
)

FEE = 60 * GWEI


def assert_sweep(
    env, result, *, store="shop", index=3, contract=USDT_ANVIL, destination=DEST_SHOP_ANVIL, amount=2000000, **expect
):
    """Every field of the signed transaction. expect overrides the values of the default request."""
    tx = result["decoded"]
    sender = env.address(store, index)
    assert tx["type"] == 2
    assert tx["chain_id"] == expect.get("chain_id", 31337)
    assert tx["nonce"] == expect.get("nonce", 0)
    assert tx["to"] == contract
    assert tx["value"] == 0
    assert helpers.decode_transfer(tx["data"]) == (destination, amount)
    assert tx["gas"] == expect.get("gas", 70000)
    assert tx["max_fee_per_gas"] == expect.get("max_fee", FEE)
    assert tx["max_priority_fee_per_gas"] == expect.get("max_priority_fee", 30 * GWEI)
    assert tx["access_list"] == []
    assert tx["signer"] == sender
    assert tx["signer"] != env.fee_address
    assert result["from"] == sender
    assert result["to"] == contract
    assert result["chain_id"] == tx["chain_id"]
    assert result["tx_hash"] == tx["hash"]
    assert destination not in (result["from"], result["to"])


async def refused(api, status, code, **changes):
    rows, signed = api.journal_rows(), len(api.transactions)
    got, result = await api.sweep(changes.pop("key", "sweep-refused-1"), **changes)
    assert (got, result["error"]) == (status, code), result
    assert set(result) == {"error", "detail"}
    assert api.journal_rows() == rows
    assert len(api.transactions) == signed
    line = api.audit_lines()[-1]
    assert (line["call"], line["result"], line["tx_hash"]) == ("sign/sweep", code, None)
    return result


async def test_sweep_transaction_has_the_fixed_shape(api, env):
    status, result = await api.sweep("sweep-shape-1", index=12, nonce=0, gas_limit=70000, amount="2000000")
    assert status == 200
    assert_sweep(env, result, index=12)
    # the call data, byte for byte
    data = result["decoded"]["data"]
    assert data.hex() == "a9059cbb" + "00" * 12 + DEST_SHOP_ANVIL[2:].lower() + f"{2000000:064x}"


async def test_sweep_on_the_second_chain(api, env):
    status, result = await api.sweep("sweep-polygon-1", chain="polygon", index=1, nonce=4, amount="123456789")
    assert status == 200
    assert_sweep(
        env, result, index=1, contract=USDT_POLYGON, destination=DEST_SHOP_POLYGON, amount=123456789, chain_id=137, nonce=4
    )


async def test_sweep_on_bnb_with_18_decimals(api, env):
    """SPEC 8.4: no code for BNB. The differences are data: chain id 56, and USDT there has 18 decimals."""
    amount = 500 * 10**18  # 500 USDT
    status, result = await api.sweep("sweep-bnb-1", chain="bnb", index=2, nonce=1, amount=str(amount))
    assert status == 200
    assert_sweep(env, result, index=2, contract=USDT_BNB, destination=DEST_SHOP_BNB, amount=amount, chain_id=56, nonce=1)
    assert result["decoded"]["data"].hex() == "a9059cbb" + "00" * 12 + DEST_SHOP_BNB[2:].lower() + f"{amount:064x}"
    assert api.audit_lines()[-1]["amount"] == "500000000000000000000"
    # the address and the fee wallet are the same as on the other EVM chains
    status, fund = await api.fund("sweep-bnb-2", chain="bnb", index=2, nonce=0)
    assert status == 200
    tx = fund["decoded"]
    assert (tx["chain_id"], tx["to"], tx["signer"]) == (56, env.address("shop", 2), env.fee_address)
    assert (tx["gas"], tx["data"]) == (21000, b"")
    assert (tx["value"], tx["nonce"], tx["max_fee_per_gas"], tx["max_priority_fee_per_gas"]) == (5 * 10**15, 0, FEE, 30 * GWEI)
    assert result["from"] == fund["to"]
    # the recovery list of another chain is not the list of bnb
    await refused(api, 403, "token_not_allowed", chain="bnb", token=RECOVERY_TOKEN)


def test_usdt_addresses_of_the_spec_table_have_a_correct_checksum():
    # SPEC 8.1. This is a check of the letters (EIP-55), by the independent encoder; it is not a check against the chains.
    table = [
        "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "0x55d398326f99059fF775485246999027B3197955",
    ]
    for address in table:
        assert helpers.checksum_address(bytes.fromhex(address[2:])) == address
    assert table[2] == USDT_BNB


async def test_sweep_of_a_second_store_goes_to_its_destination(api, env):
    await api.derive("other", 0, 4)
    status, result = await api.sweep("sweep-other-1", store="other", index=3)
    assert status == 200
    assert_sweep(env, result, store="other", index=3, destination=DEST_OTHER_ANVIL)
    assert result["from"] != env.address("shop", 3)


async def test_largest_amount(api, env):
    status, result = await api.sweep("sweep-max-1", amount=str(2**256 - 1))
    assert status == 200
    assert_sweep(env, result, amount=2**256 - 1)
    await refused(api, 400, "invalid_request", amount=str(2**256), nonce=1)


async def test_amount_zero_is_refused(api):
    await refused(api, 400, "invalid_request", amount="0")


async def test_gas_limit_over_the_cap_is_refused(api, env):
    cap = CAPS["gas_limit_cap"]
    await refused(api, 403, "cap_exceeded", gas_limit=cap + 1)
    await refused(api, 403, "cap_exceeded", gas_limit=30_000_000)
    await refused(api, 400, "invalid_request", gas_limit=20999)  # less than the gas of any transaction
    await refused(api, 400, "invalid_request", gas_limit=0)
    status, result = await api.sweep("sweep-gascap-1", gas_limit=cap)
    assert status == 200
    assert_sweep(env, result, gas=cap)


async def test_max_fee_over_the_cap_is_refused(api, env):
    cap = CAPS["max_fee_per_gas_cap_wei"]
    await refused(api, 403, "cap_exceeded", max_fee_per_gas_wei=str(cap + 1))
    await refused(api, 400, "invalid_request", max_fee_per_gas_wei=str(cap), max_priority_fee_per_gas_wei=str(cap + 1))
    status, result = await api.sweep("sweep-feecap-1", max_fee_per_gas_wei=str(cap), max_priority_fee_per_gas_wei=str(cap))
    assert status == 200
    assert_sweep(env, result, max_fee=cap, max_priority_fee=cap)


async def test_token_that_is_not_in_the_recovery_list_is_refused(api):
    await refused(api, 403, "token_not_allowed", token=OTHER_TOKEN)
    await refused(api, 403, "token_not_allowed", token=ATTACKER)
    # the USDT contract of another chain, and the pinned one itself: 'token' is for the recovery list only
    await refused(api, 403, "token_not_allowed", token=USDT_POLYGON)
    await refused(api, 403, "token_not_allowed", token=USDT_ANVIL)
    # the list is per chain: polygon has an empty list
    await refused(api, 403, "token_not_allowed", chain="polygon", token=RECOVERY_TOKEN)
    await refused(api, 403, "token_not_allowed", chain="polygon", token=USDT_POLYGON)


async def test_token_must_be_a_checksummed_address(api):
    for token in (
        RECOVERY_TOKEN.lower(),
        RECOVERY_TOKEN.upper().replace("0X", "0x"),
        RECOVERY_TOKEN[:-2],
        RECOVERY_TOKEN[2:],
        "",
        "usdt",
        1,
        True,
        [RECOVERY_TOKEN],
        {"address": RECOVERY_TOKEN},
    ):
        result = await refused(api, 400, "invalid_request", token=token)
        assert "token" in result["detail"]


async def test_token_of_the_recovery_list_is_swept_to_the_pinned_destination(api, env):
    status, result = await api.sweep("sweep-recovery-1", token=RECOVERY_TOKEN, amount="777")
    assert status == 200
    assert_sweep(env, result, contract=RECOVERY_TOKEN, amount=777)
    assert api.audit_lines()[-1]["token"] == RECOVERY_TOKEN


async def test_index_that_was_not_given_out_is_refused(api, env):
    await refused(api, 400, "invalid_request", index=20)
    await refused(api, 400, "invalid_request", index=2**31 - 1)
    await refused(api, 400, "invalid_request", store="other", index=0)
    status, result = await api.sweep("sweep-index-19", index=19)
    assert status == 200
    assert_sweep(env, result, index=19)


async def test_unknown_chain_and_store_are_refused(api):
    await refused(api, 400, "unknown_chain", chain="ethereum")
    await refused(api, 400, "unknown_chain", chain="137")
    await refused(api, 400, "unknown_store", store="nostore")
    await api.derive("other", 0, 1)
    await refused(api, 400, "unknown_store", store="other", chain="polygon", index=0)  # no pinned destination


FORBIDDEN_FIELDS = {
    "to": ATTACKER,
    "to_address": ATTACKER,
    "destination": ATTACKER,
    "recipient": ATTACKER,
    "address": ATTACKER,
    "from": ATTACKER,
    "spender": ATTACKER,
    "data": "0x095ea7b3" + "00" * 12 + ATTACKER[2:] + "ff" * 32,
    "input": "0xa9059cbb",
    "raw": "0x02f8",
    "raw_tx": "0x02f8",
    "chain_id": 1,
    "chainId": 1,
    "contract": ATTACKER,
    "usdt": ATTACKER,
    "value": "1",
    "value_wei": "1",
    "gas": 500000,
    "account": 9000,
    "path": "m/44'/60'/9000'/0'/0'",
    "family": "evm",
    "seed_id": "0" * 16,
    "method": "approve",
    "type": 0,
    "gas_price": "1",
    "access_list": [],
}


@pytest.mark.parametrize("name", sorted(FORBIDDEN_FIELDS))
async def test_sweep_has_no_field_for_a_destination_contract_chain_id_or_data(api, name):
    result = await refused(api, 400, "invalid_request", **{name: FORBIDDEN_FIELDS[name]})
    assert "unknown field" in result["detail"]
    assert ATTACKER not in str(api.audit_lines()[-1])


BAD_VALUES = {
    "idempotency_key": [None, "", "short-7", "k" * 65, "key with space", 12345678, REMOVE],
    "chain": [None, "", 31337, True, ["anvil"], "c" * 33, REMOVE],
    "store": [None, "", 1, ["shop"], "s" * 33, REMOVE],
    "index": [None, "3", -1, 2**31, 3.0, True, REMOVE],
    "nonce": [None, "0", -1, 2**63, 0.0, False, REMOVE],
    "gas_limit": [None, "70000", -1, 70000.0, True, 2**63, REMOVE],
    "amount": [None, 2000000, 2.0, "", " 1", "+1", "-1", "01", "1e6", "0x10", "1.0", "9" * 79, True, REMOVE],
    "max_fee_per_gas_wei": [None, 60, "", "-1", "01", "1e9", REMOVE],
    "max_priority_fee_per_gas_wei": [None, 30, "", "-1", "01", REMOVE],
    "replaces": ["", "short-7", "k" * 65, 5, False],
}


@pytest.mark.parametrize("name", sorted(BAD_VALUES))
async def test_sweep_field_types_and_ranges(api, name):
    for value in BAD_VALUES[name]:
        changes = {name: value}
        if name == "idempotency_key":
            changes["key"] = "unused"
        result = await refused(api, 400, "invalid_request", **changes)
        assert name in result["detail"], (name, value)


async def test_optional_fields_can_be_absent(api, env):
    status, result = await api.sweep("sweep-optional-1", token=REMOVE, replaces=REMOVE)
    assert status == 200
    assert_sweep(env, result)


async def test_no_request_can_move_tokens_to_another_address(api, env):
    """All signed sweeps of a session with many different requests: the recipient is always the pinned one."""
    await api.derive("other", 0, 20)
    count = 0
    for store, destination in (("shop", DEST_SHOP_ANVIL), ("other", DEST_OTHER_ANVIL)):
        for index in (0, 7, 19):
            for amount in (1, 10**6, 2**200):
                count += 1
                status, result = await api.sweep(
                    f"sweep-many-{count:03d}", store=store, index=index, nonce=count, amount=str(amount)
                )
                assert status == 200
                assert_sweep(env, result, store=store, index=index, destination=destination, amount=amount, nonce=count)
    assert len(api.transactions) == 18
    for tx in api.transactions:
        recipient, _ = helpers.decode_transfer(tx["data"])
        assert recipient in (DEST_SHOP_ANVIL, DEST_OTHER_ANVIL)
        assert tx["to"] == USDT_ANVIL
        assert tx["value"] == 0
        assert tx["chain_id"] == 31337


async def test_audit_line_of_a_sweep(api):
    status, result = await api.sweep("sweep-audit-1", index=4, nonce=2, amount="55")
    assert status == 200
    line = api.audit_lines()[-1]
    assert (line["call"], line["result"], line["chain"], line["store"]) == ("sign/sweep", "ok", "anvil", "shop")
    assert (line["index"], line["nonce"], line["amount"], line["value_wei"]) == (4, 2, "55", None)
    assert line["max_fee_per_gas_wei"] == str(FEE)
    assert line["tx_hash"] == result["tx_hash"]
    assert line["idempotency_key"] == "sweep-audit-1"
    assert "token" not in line
