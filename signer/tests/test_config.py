"""Config validation (SPEC 3.2): every rule has a config that breaks it."""

import hashlib

import helpers
import pytest
from conftest import (
    CONFIG,
    DEST_SHOP_ANVIL,
    NATIVE_GAS_CAP_POLYGON,
    NATIVE_OTHER_ANVIL,
    NATIVE_OTHER_POLYGON,
    NATIVE_SHARE_POLYGON,
    NATIVE_SHOP_ANVIL,
    NATIVE_SHOP_BNB,
    NO_LIMITS,
    RECOVERY_TOKEN,
    USDT_ANVIL,
    USDT_BNB,
)

from acctpool_signer.config import load_config, parse_config
from acctpool_signer.errors import ConfigError

SPEC_EXAMPLE = """
[signer]
listen = "0.0.0.0:7070"

[chains.polygon]
family = "evm"
chain_id = 137
usdt = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
gas_limit_cap = 150000
max_fee_per_gas_cap_wei = "2000000000000"
max_fund_value_wei = "300000000000000000"
max_fund_total_per_address_wei = "900000000000000000"
fee_wallet_daily_cap_wei = "20000000000000000000"

[stores.forkednet]
account = 1
[stores.forkednet.destinations]
polygon = "0x1111111111111111111111111111111111111111"

# optional, empty in normal operation. A token listed here may be swept with /v1/sign/sweep and "token" set.
[recovery_tokens]
polygon = []
"""

GOOD = CONFIG.replace("{port}", "7070")


def test_example_of_the_spec_is_refused_with_its_destination():
    """The destination of the SPEC 3.2 example is the address 0x...01: a precompiled contract. Tokens there are lost."""
    example = SPEC_EXAMPLE.replace("0x1111111111111111111111111111111111111111", "0x0000000000000000000000000000000000000001")
    with pytest.raises(ConfigError) as info:
        parse_config(example.encode())
    assert "below 0x10000" in str(info.value)


def test_example_of_the_spec_is_valid_with_another_destination():
    config = parse_config(SPEC_EXAMPLE.encode())
    assert (config.listen_host, config.listen_port) == ("0.0.0.0", 7070)
    polygon = config.chains["polygon"]
    assert polygon.chain_id == 137
    assert polygon.usdt == "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
    assert polygon.gas_limit_cap == 150000
    assert polygon.max_fee_per_gas_cap_wei == 2000 * 10**9
    assert polygon.max_fund_value_wei == 3 * 10**17
    assert polygon.max_fund_total_per_address_wei == 9 * 10**17
    assert polygon.fee_wallet_daily_cap_wei == 20 * 10**18
    assert polygon.recovery_tokens == ()
    assert config.stores["forkednet"].account == 1
    assert config.stores["forkednet"].destinations == {"polygon": "0x1111111111111111111111111111111111111111"}
    assert config.sha256 == hashlib.sha256(SPEC_EXAMPLE.encode()).hexdigest()
    # the optional keys of SPEC 7.6 and 8.3 have their defaults
    assert (config.max_signatures_per_minute, config.max_derive_per_minute, config.max_other_per_minute) == (30, 10, 120)
    assert (polygon.native_gas_limit_cap, polygon.native_max_fee_share_percent) == (21000, 10)
    assert config.stores["forkednet"].native_destinations == {}


def test_placeholder_of_the_example_file_is_refused():
    # deploy/pools.toml.example (SPEC 7.1): the signer must not start with the example destination
    placeholder = "0xREPLACE_WITH_THE_PAYOUT_ADDRESS_OF_THIS_STORE"
    with pytest.raises(ConfigError):
        parse_config(SPEC_EXAMPLE.replace("0x1111111111111111111111111111111111111111", placeholder).encode())
    with pytest.raises(ConfigError):
        parse_config((SPEC_EXAMPLE + f'[stores.forkednet.native_destinations]\npolygon = "{placeholder}"\n').encode())


def test_optional_keys():
    config = parse_config(GOOD.encode())
    assert (config.max_signatures_per_minute, config.max_derive_per_minute) == (1000000, 1000000)
    assert config.max_other_per_minute == 1000000
    anvil, polygon, bnb = (config.chains[name] for name in ("anvil", "polygon", "bnb"))
    assert (anvil.native_gas_limit_cap, anvil.native_max_fee_share_percent) == (21000, 10)
    assert polygon.native_gas_limit_cap == NATIVE_GAS_CAP_POLYGON
    assert polygon.native_max_fee_share_percent == NATIVE_SHARE_POLYGON
    assert (bnb.chain_id, bnb.usdt) == (56, USDT_BNB)
    assert config.stores["shop"].native_destinations == {"anvil": NATIVE_SHOP_ANVIL, "bnb": NATIVE_SHOP_BNB}
    assert config.stores["other"].native_destinations == {"anvil": NATIVE_OTHER_ANVIL, "polygon": NATIVE_OTHER_POLYGON}
    # one limit in the file, the other from the default
    config = parse_config(GOOD.replace(NO_LIMITS, "max_derive_per_minute = 3").encode())
    assert (config.max_signatures_per_minute, config.max_derive_per_minute, config.max_other_per_minute) == (30, 3, 120)


def test_recovery_tokens_are_optional():
    config = parse_config(SPEC_EXAMPLE.split("# optional")[0].encode())
    assert config.chains["polygon"].recovery_tokens == ()


def test_test_config_is_valid(tmp_path):
    path = tmp_path / "pools.toml"
    path.write_text(GOOD)
    config = load_config(str(path))
    assert set(config.chains) == {"anvil", "polygon", "bnb"}
    assert config.chains["anvil"].recovery_tokens == (RECOVERY_TOKEN,)
    assert config.chains["polygon"].recovery_tokens == ()
    assert config.stores["other"].destinations == {"anvil": config.stores["other"].destinations["anvil"]}


MAX_VALUE_LINE = 'max_fund_value_wei = "300000000000000000"'
MAX_TOTAL_LINE = 'max_fund_total_per_address_wei = "900000000000000000"'
GAS_CAP_LINE = f"native_gas_limit_cap = {NATIVE_GAS_CAP_POLYGON}"
SHARE_LINE = f"native_max_fee_share_percent = {NATIVE_SHARE_POLYGON}"
NATIVE_TABLE = "[stores.other.native_destinations]"


def broken(old: str, new: str, count: int = 1) -> bytes:
    assert old in GOOD
    return GOOD.replace(old, new, count).encode()


BAD = {
    "not TOML": b"[signer\nlisten = 1",
    "not UTF-8": b"\xff\xfe",
    "empty": b"",
    "no signer table": broken(f'[signer]\nlisten = "127.0.0.1:7070"\n{NO_LIMITS}', ""),
    "no listen": broken('listen = "127.0.0.1:7070"', ""),
    "limit 0": broken("max_signatures_per_minute = 1000000", "max_signatures_per_minute = 0"),
    "limit negative": broken("max_derive_per_minute = 1000000", "max_derive_per_minute = -1"),
    "limit is a string": broken("max_derive_per_minute = 1000000", 'max_derive_per_minute = "10"'),
    "limit is a float": broken("max_signatures_per_minute = 1000000", "max_signatures_per_minute = 30.0"),
    "limit too high": broken("max_signatures_per_minute = 1000000", "max_signatures_per_minute = 1000001"),
    "limit with a wrong name": broken("max_signatures_per_minute", "max_signatures_per_min"),
    "native gas limit cap under 21000": broken(GAS_CAP_LINE, "native_gas_limit_cap = 20999"),
    "native gas limit cap is a string": broken(GAS_CAP_LINE, 'native_gas_limit_cap = "21000"'),
    "native fee share 0": broken(SHARE_LINE, "native_max_fee_share_percent = 0"),
    "native fee share 101": broken(SHARE_LINE, "native_max_fee_share_percent = 101"),
    "native fee share is a float": broken(SHARE_LINE, "native_max_fee_share_percent = 2.5"),
    "native fee share with a wrong name": broken("native_max_fee_share_percent", "native_max_fee_share"),
    "native destination chain not in chains": broken(f"{NATIVE_TABLE}\nanvil", f"{NATIVE_TABLE}\nbase"),
    "native destination in lower case": broken(NATIVE_SHOP_ANVIL, NATIVE_SHOP_ANVIL.lower()),
    "native destination is the zero address": broken(NATIVE_SHOP_ANVIL, "0x" + "00" * 20),
    "native destination is the USDT contract": broken(NATIVE_SHOP_ANVIL, USDT_ANVIL),
    "native destination is a recovery token": broken(NATIVE_SHOP_ANVIL, RECOVERY_TOKEN),
    "native destinations is not a table": broken(
        f"{NATIVE_TABLE}\nanvil", f'native_destinations = "x"\n{NATIVE_TABLE}\nanvil'
    ),
    "chain id of bnb used two times": broken("chain_id = 56", "chain_id = 137"),
    "destination below 0x10000": broken(DEST_SHOP_ANVIL, "0x0000000000000000000000000000000000000009"),
    "destination 0xffff": broken(DEST_SHOP_ANVIL, helpers.checksum_address((0xFFFF).to_bytes(20, "big"))),
    "native destination below 0x10000": broken(NATIVE_SHOP_ANVIL, "0x0000000000000000000000000000000000000002"),
    "funding value cap over the total cap": broken(MAX_VALUE_LINE, 'max_fund_value_wei = "900000000000000001"'),
    "total cap over the daily cap": broken(MAX_TOTAL_LINE, 'max_fund_total_per_address_wei = "2000000000000000001"'),
    "gas limit cap under 21000": broken("gas_limit_cap = 150000", "gas_limit_cap = 20999"),
    "name with a newline at the end": broken("[chains.polygon]", '[chains."polygon\\n"]'),
    "amount with a newline at the end": broken(MAX_VALUE_LINE, 'max_fund_value_wei = "300000000000000000\\n"'),
    "address with a newline at the end": broken(f'"{DEST_SHOP_ANVIL}"', f'"{DEST_SHOP_ANVIL}\\n"'),
    "family tron": broken('family = "evm"', 'family = "tron"'),
    "listen without port": broken('"127.0.0.1:7070"', '"127.0.0.1"'),
    "listen port 0": broken('"127.0.0.1:7070"', '"127.0.0.1:0"'),
    "listen port too high": broken('"127.0.0.1:7070"', '"127.0.0.1:70000"'),
    "listen host name": broken('"127.0.0.1:7070"', '"signer:7070"'),
    "address in lower case": broken(USDT_ANVIL, USDT_ANVIL.lower()),
    "address with wrong checksum": broken(DEST_SHOP_ANVIL, DEST_SHOP_ANVIL.swapcase().replace("0X", "0x")),
    "address too short": broken(USDT_ANVIL, USDT_ANVIL[:-2]),
    "address without 0x": broken(USDT_ANVIL, USDT_ANVIL[2:]),
    "usdt is the zero address": broken(USDT_ANVIL, "0x" + "00" * 20),
    "chain id used two times": broken("chain_id = 137", "chain_id = 31337"),
    "chain id 0": broken("chain_id = 137", "chain_id = 0"),
    "chain id is a string": broken("chain_id = 137", 'chain_id = "137"'),
    "chain id is true": broken("chain_id = 137", "chain_id = true"),
    "account used two times": broken("account = 2", "account = 1"),
    "account 9000": broken("account = 2", "account = 9000"),
    "account negative": broken("account = 2", "account = -1"),
    "account 2^31": broken("account = 2", f"account = {2**31}"),
    "destination chain not in chains": broken("[stores.other.destinations]\nanvil", "[stores.other.destinations]\nbase"),
    "destination is the zero address": broken(DEST_SHOP_ANVIL, "0x" + "00" * 20),
    "destination is the USDT contract": broken(DEST_SHOP_ANVIL, USDT_ANVIL),
    "destination is a recovery token": broken(DEST_SHOP_ANVIL, RECOVERY_TOKEN),
    "cap is missing": broken('max_fund_value_wei = "300000000000000000"\n', ""),
    "cap has a wrong name": broken("fee_wallet_daily_cap_wei", "fee_wallet_daily_cap"),
    "cap is an integer": broken('max_fund_value_wei = "300000000000000000"', "max_fund_value_wei = 300000000000000000"),
    "cap is negative": broken('max_fund_value_wei = "300000000000000000"', 'max_fund_value_wei = "-1"'),
    "cap is a float": broken('max_fund_value_wei = "300000000000000000"', 'max_fund_value_wei = "0.3"'),
    "cap is hex": broken('max_fund_value_wei = "300000000000000000"', 'max_fund_value_wei = "0x10"'),
    "gas limit cap is a string": broken("gas_limit_cap = 150000", 'gas_limit_cap = "150000"'),
    "unknown key in chain": broken('family = "evm"', 'family = "evm"\ndestination = "x"'),
    "unknown key in store": broken("account = 2", "account = 2\nfee = 1"),
    "unknown top-level table": broken("[recovery_tokens]", "[debug]\non = true\n[recovery_tokens]"),
    "recovery token chain not in chains": broken("[recovery_tokens]\nanvil", "[recovery_tokens]\nbase"),
    "recovery token in lower case": broken(RECOVERY_TOKEN, RECOVERY_TOKEN.lower()),
    "recovery tokens not a list": broken(f'anvil = ["{RECOVERY_TOKEN}"]', f'anvil = "{RECOVERY_TOKEN}"'),
    "chain name with upper case": broken("[chains.polygon]", "[chains.Polygon]"),
    "no stores": GOOD.split("[stores.shop]")[0].encode(),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_bad_config_is_refused(case):
    with pytest.raises(ConfigError):
        parse_config(BAD[case])


def test_lowest_address_that_is_accepted():
    lowest = helpers.checksum_address((0x10000).to_bytes(20, "big"))
    config = parse_config(GOOD.replace(DEST_SHOP_ANVIL, lowest).encode())
    assert config.stores["shop"].destinations["anvil"] == lowest


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "none.toml"))


def test_checksum_changes_with_every_byte():
    assert parse_config(GOOD.encode()).sha256 != parse_config((GOOD + "\n").encode()).sha256
