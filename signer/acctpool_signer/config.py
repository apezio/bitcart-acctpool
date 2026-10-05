"""pools.toml loading and validation (SPEC 3.2, 7.6, 8.3). The config is root-owned; the API cannot change it."""

import hashlib
import ipaddress
import re
import tomllib
from dataclasses import dataclass
from typing import Any

from eth_utils import is_checksum_address

from .errors import ConfigError

FEE_ACCOUNT = 9000
HARDENED_LIMIT = 2**31
MAX_INT64 = 2**63 - 1
# used with fullmatch(): "$" with match() accepts a newline at the end
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
AMOUNT_RE = re.compile(r"0|[1-9][0-9]{0,77}")
LOWEST_ADDRESS = 0x10000  # below: precompiled contracts and other reserved addresses; value sent there is lost
AMOUNT_KEYS = ("max_fee_per_gas_cap_wei", "max_fund_value_wei", "max_fund_total_per_address_wei", "fee_wallet_daily_cap_wei")
CHAIN_KEYS = {"family", "chain_id", "usdt", "gas_limit_cap", *AMOUNT_KEYS}
CHAIN_DEFAULTS = {"native_gas_limit_cap": 21000, "native_max_fee_share_percent": 10}
INT_KEYS = {"chain_id": (1, MAX_INT64), "gas_limit_cap": (21000, MAX_INT64), "native_gas_limit_cap": (21000, MAX_INT64),
            "native_max_fee_share_percent": (1, 100)}  # fmt: skip
SIGNER_DEFAULTS = {"max_signatures_per_minute": 30, "max_derive_per_minute": 10, "max_other_per_minute": 120}


@dataclass(frozen=True)
class ChainConfig:
    name: str
    chain_id: int
    usdt: str
    gas_limit_cap: int
    max_fee_per_gas_cap_wei: int
    max_fund_value_wei: int
    max_fund_total_per_address_wei: int
    fee_wallet_daily_cap_wei: int
    native_gas_limit_cap: int
    native_max_fee_share_percent: int
    recovery_tokens: tuple[str, ...]


@dataclass(frozen=True)
class StoreConfig:
    name: str
    account: int
    destinations: dict[str, str]
    native_destinations: dict[str, str]


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    max_signatures_per_minute: int
    max_derive_per_minute: int
    max_other_per_minute: int
    chains: dict[str, ChainConfig]
    stores: dict[str, StoreConfig]
    sha256: str


def is_address(value: Any) -> bool:
    """True for an EIP-55 checksummed address string. Lower-case and upper-case forms are refused."""
    return isinstance(value, str) and ADDRESS_RE.fullmatch(value) is not None and is_checksum_address(value)


def _table(value: Any, where: str, necessary: set[str] | None = None, optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: must be a table")
    if necessary is not None:
        # An unknown key is refused: a misspelled cap must not become a missing cap or a default.
        for name in sorted(set(value) - necessary - (optional or set())):
            raise ConfigError(f"{where}: unknown key {name!r}")
        for name in sorted(necessary - set(value)):
            raise ConfigError(f"{where}: missing key {name!r}")
    return value


def _int(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where}: must be an integer from {low} to {high}")
    return value


def _amount(value: Any, where: str) -> int:
    if not isinstance(value, str) or AMOUNT_RE.fullmatch(value) is None:
        raise ConfigError(f"{where}: must be a string of decimal digits (base units)")
    return int(value)


def _address(value: Any, where: str) -> str:
    if not is_address(value):
        raise ConfigError(f"{where}: must be an EIP-55 checksummed address")
    if int(value, 16) < LOWEST_ADDRESS:
        raise ConfigError(f"{where}: the zero address and addresses below 0x10000 (precompiled contracts) lose the value")
    return value


def _name(value: str, where: str) -> str:
    if NAME_RE.fullmatch(value) is None:
        raise ConfigError(f"{where}: name must match {NAME_RE.pattern}")
    return f"{where}.{value}"


def _listen(value: Any) -> tuple[str, int]:
    host, _, port = value.rpartition(":") if isinstance(value, str) else ("", "", "")
    try:
        ipaddress.IPv4Address(host)
        if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError
    except ValueError:
        raise ConfigError('signer.listen: must be "<ipv4>:<port>", port 1 to 65535') from None
    return host, int(port)


def _chain(name: str, body: Any, tokens: Any) -> ChainConfig:
    where = _name(name, "chains")
    body = {**CHAIN_DEFAULTS, **_table(body, where, CHAIN_KEYS, set(CHAIN_DEFAULTS))}
    if body["family"] != "evm":
        raise ConfigError(f"{where}.family: must be 'evm'")
    amounts = {key: _amount(body[key], f"{where}.{key}") for key in AMOUNT_KEYS}
    # caps that cannot be true together are a fault of the file, for example one digit too many
    if not amounts["max_fund_value_wei"] <= amounts["max_fund_total_per_address_wei"] <= amounts["fee_wallet_daily_cap_wei"]:
        raise ConfigError(
            f"{where}: max_fund_value_wei <= max_fund_total_per_address_wei <= fee_wallet_daily_cap_wei is necessary"
        )
    if not isinstance(tokens, list):
        raise ConfigError(f"recovery_tokens.{name}: must be a list")
    usdt, ints = _address(body["usdt"], f"{where}.usdt"), {k: _int(body[k], f"{where}.{k}", *r) for k, r in INT_KEYS.items()}
    recovery = tuple(_address(t, f"recovery_tokens.{name}[{i}]") for i, t in enumerate(tokens))
    return ChainConfig(name=name, usdt=usdt, recovery_tokens=recovery, **ints, **amounts)


def _destinations(table: Any, where: str, chains: dict[str, ChainConfig]) -> dict[str, str]:
    out: dict[str, str] = {}
    for chain, address in _table(table, where).items():
        if chain not in chains:
            raise ConfigError(f"{where}.{chain}: chain is not in [chains]")
        out[chain] = _address(address, f"{where}.{chain}")
        # A transfer to the token contract itself burns the tokens; native coin sent there is lost too.
        if out[chain] == chains[chain].usdt or out[chain] in chains[chain].recovery_tokens:
            raise ConfigError(f"{where}.{chain}: must not be a token contract")
    return out


def parse_config(raw: bytes) -> Config:
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"config is not valid TOML: {e}") from None
    for name in sorted(set(doc) - {"signer", "chains", "stores", "recovery_tokens"}):
        raise ConfigError(f"unknown top-level key {name!r}")
    signer = {**SIGNER_DEFAULTS, **_table(doc.get("signer"), "signer", {"listen"}, set(SIGNER_DEFAULTS))}
    limits = {key: _int(signer[key], f"signer.{key}", 1, 1_000_000) for key in SIGNER_DEFAULTS}
    chains_doc, stores_doc = _table(doc.get("chains"), "chains"), _table(doc.get("stores"), "stores")
    recovery_doc = _table(doc.get("recovery_tokens", {}), "recovery_tokens")
    if not chains_doc or not stores_doc:
        raise ConfigError("chains, stores: at least one chain and one store are necessary")
    for name in recovery_doc:
        if name not in chains_doc:
            raise ConfigError(f"recovery_tokens.{name}: chain is not in [chains]")
    chains = {name: _chain(name, body, recovery_doc.get(name, [])) for name, body in chains_doc.items()}
    ids = [chain.chain_id for chain in chains.values()]
    if len(set(ids)) != len(ids):
        raise ConfigError("chains: a chain_id is in the config two times")
    stores: dict[str, StoreConfig] = {}
    for name, body in stores_doc.items():
        where = _name(name, "stores")
        body = _table(body, where, {"account", "destinations"}, {"native_destinations"})
        account = _int(body["account"], f"{where}.account", 0, HARDENED_LIMIT - 1)
        if account == FEE_ACCOUNT or account in [store.account for store in stores.values()]:
            raise ConfigError(f"{where}.account: {account} is the fee wallet account or the account of another store")
        destinations = _destinations(body["destinations"], f"{where}.destinations", chains)
        native = _destinations(body.get("native_destinations", {}), f"{where}.native_destinations", chains)
        stores[name] = StoreConfig(name=name, account=account, destinations=destinations, native_destinations=native)
    host, port = _listen(signer["listen"])
    return Config(host, port, **limits, chains=chains, stores=stores, sha256=hashlib.sha256(raw).hexdigest())


def load_config(path: str) -> Config:
    try:
        with open(path, "rb") as f:
            return parse_config(f.read())
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e.strerror}") from None
