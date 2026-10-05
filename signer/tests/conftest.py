import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import helpers

from acctpool_signer import keystore as keystore_mod
from acctpool_signer.__main__ import Paths, build_state
from acctpool_signer.api import create_app

GWEI = 10**9
ETHER = 10**18

USDT_ANVIL = helpers.checksum_address(bytes([0xA1]) * 20)
USDT_POLYGON = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
USDT_BNB = "0x55d398326f99059fF775485246999027B3197955"
RECOVERY_TOKEN = helpers.checksum_address(bytes([0xB2]) * 20)
OTHER_TOKEN = helpers.checksum_address(bytes([0xB3]) * 20)
DEST_SHOP_ANVIL = helpers.checksum_address(bytes([0xD1]) * 20)
DEST_SHOP_POLYGON = helpers.checksum_address(bytes([0xD2]) * 20)
DEST_OTHER_ANVIL = helpers.checksum_address(bytes([0xD3]) * 20)
DEST_SHOP_BNB = helpers.checksum_address(bytes([0xD4]) * 20)
NATIVE_SHOP_ANVIL = helpers.checksum_address(bytes([0xC1]) * 20)
NATIVE_SHOP_BNB = helpers.checksum_address(bytes([0xC2]) * 20)
NATIVE_OTHER_ANVIL = helpers.checksum_address(bytes([0xC3]) * 20)
NATIVE_OTHER_POLYGON = helpers.checksum_address(bytes([0xC4]) * 20)
ATTACKER = helpers.checksum_address(bytes([0xEE]) * 20)

CAPS = {
    "gas_limit_cap": 150000,
    "max_fee_per_gas_cap_wei": 1000 * GWEI,
    "max_fund_value_wei": 3 * ETHER // 10,
    "max_fund_total_per_address_wei": 9 * ETHER // 10,
    "fee_wallet_daily_cap_wei": 2 * ETHER,
}
# polygon has its own values for the two optional keys; the other chains have the defaults (21000 and 10)
NATIVE_GAS_CAP_POLYGON = 60000
NATIVE_SHARE_POLYGON = 25
# The default limits (30 and 10 per minute) are too low for tests that send hundreds of calls.
NO_LIMITS = "max_signatures_per_minute = 1000000\nmax_derive_per_minute = 1000000\nmax_other_per_minute = 1000000"
PREFUND = CAPS["max_fund_value_wei"]

CONFIG = f"""
[signer]
listen = "127.0.0.1:{{port}}"
{NO_LIMITS}

[chains.anvil]
family = "evm"
chain_id = 31337
usdt = "{USDT_ANVIL}"
gas_limit_cap = {CAPS["gas_limit_cap"]}
max_fee_per_gas_cap_wei = "{CAPS["max_fee_per_gas_cap_wei"]}"
max_fund_value_wei = "{CAPS["max_fund_value_wei"]}"
max_fund_total_per_address_wei = "{CAPS["max_fund_total_per_address_wei"]}"
fee_wallet_daily_cap_wei = "{CAPS["fee_wallet_daily_cap_wei"]}"

[chains.polygon]
family = "evm"
chain_id = 137
usdt = "{USDT_POLYGON}"
gas_limit_cap = {CAPS["gas_limit_cap"]}
max_fee_per_gas_cap_wei = "{CAPS["max_fee_per_gas_cap_wei"]}"
max_fund_value_wei = "{CAPS["max_fund_value_wei"]}"
max_fund_total_per_address_wei = "{CAPS["max_fund_total_per_address_wei"]}"
fee_wallet_daily_cap_wei = "{CAPS["fee_wallet_daily_cap_wei"]}"
native_gas_limit_cap = {NATIVE_GAS_CAP_POLYGON}
native_max_fee_share_percent = {NATIVE_SHARE_POLYGON}

[chains.bnb]
family = "evm"
chain_id = 56
usdt = "{USDT_BNB}"
gas_limit_cap = {CAPS["gas_limit_cap"]}
max_fee_per_gas_cap_wei = "{CAPS["max_fee_per_gas_cap_wei"]}"
max_fund_value_wei = "{CAPS["max_fund_value_wei"]}"
max_fund_total_per_address_wei = "{CAPS["max_fund_total_per_address_wei"]}"
fee_wallet_daily_cap_wei = "{CAPS["fee_wallet_daily_cap_wei"]}"

[stores.shop]
account = 1
[stores.shop.destinations]
anvil = "{DEST_SHOP_ANVIL}"
polygon = "{DEST_SHOP_POLYGON}"
bnb = "{DEST_SHOP_BNB}"
[stores.shop.native_destinations]
anvil = "{NATIVE_SHOP_ANVIL}"
bnb = "{NATIVE_SHOP_BNB}"

[stores.other]
account = 2
[stores.other.destinations]
anvil = "{DEST_OTHER_ANVIL}"
[stores.other.native_destinations]
anvil = "{NATIVE_OTHER_ANVIL}"
polygon = "{NATIVE_OTHER_POLYGON}"

[recovery_tokens]
anvil = ["{RECOVERY_TOKEN}"]
"""

ACCOUNTS = {"shop": 1, "other": 2}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
        self.seconds = 1000.0  # the clock of the rate limits

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.seconds += seconds


@dataclass
class Env:
    """One signer installation in a temporary folder. The seed is made here, never read from a file."""

    root: Path
    paths: Paths
    master_key: bytes = field(repr=False)
    token: str = field(repr=False)
    entropy: bytes = field(repr=False)
    words: str = field(repr=False)
    seed: bytes = field(repr=False)
    clock: Clock = field(default_factory=Clock, repr=False)

    def key(self, store: str, index: int) -> bytes:
        return helpers.bip32_derive(self.seed, helpers.deposit_path(ACCOUNTS[store], index))

    def address(self, store: str, index: int) -> str:
        return helpers.address_from_key(self.key(store, index))

    @property
    def fee_key(self) -> bytes:
        return helpers.bip32_derive(self.seed, helpers.FEE_PATH)

    @property
    def fee_address(self) -> str:
        return helpers.address_from_key(self.fee_key)

    def write_config(self, text: str, port: int = 7070) -> None:
        Path(self.paths.config).write_text(text.replace("{port}", str(port)))

    def create_keystore(self) -> None:
        keystore_mod.create(self.paths.keystore, self.master_key, self.entropy)

    def add_second_seed(self, active: int = 0) -> None:
        """Put a second seed into the keystore. It is a correct entry: same master key, its own nonce and tag."""
        other = self.root / "second-keystore.json"
        keystore_mod.create(str(other), self.master_key, os.urandom(32))
        doc = json.loads(Path(self.paths.keystore).read_text())
        doc["seeds"].append(json.loads(other.read_text())["seeds"][0])
        doc["active"] = doc["seeds"][active]["seed_id"]
        Path(self.paths.keystore).write_text(json.dumps(doc))
        other.unlink()


def make_env(root: Path, keystore: bool = True) -> Env:
    from mnemonic import Mnemonic  # the reference implementation of BIP39, not the one that the signer uses

    data = root / "data"
    data.mkdir(parents=True)
    paths = Paths(
        config=str(root / "pools.toml"),
        master_key=str(root / "master.key"),
        token=str(root / "signer.token"),
        data=str(data),
        highwater=str(root / "audit.highwater"),
    )
    master_key = os.urandom(32)
    token = os.urandom(24).hex()
    entropy = os.urandom(32)
    words = Mnemonic("english").to_mnemonic(entropy)
    Path(paths.master_key).write_bytes(master_key)
    Path(paths.token).write_text(token + "\n")
    env = Env(
        root=root,
        paths=paths,
        master_key=master_key,
        token=token,
        entropy=entropy,
        words=words,
        seed=helpers.bip39_seed(words),
    )
    env.write_config(CONFIG)
    if keystore:
        env.create_keystore()
    return env


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path / "signer")


@pytest.fixture
def env_no_keystore(tmp_path: Path) -> Env:
    return make_env(tmp_path / "signer-empty", keystore=False)


class Api:
    """Test client. Keeps every response text, and decodes every signed transaction that comes back."""

    def __init__(self, client: Any, env: Env, state: Any) -> None:
        self.client = client
        self.env = env
        self.state = state
        self.responses: list[str] = []
        self.transactions: list[dict[str, Any]] = []

    async def call(self, method: str, path: str, body: Any = None, token: str | None = "default", raw: bytes | None = None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {self.env.token if token == 'default' else token}"
        kwargs: dict[str, Any] = {"headers": headers}
        if raw is not None:
            kwargs["data"] = raw
        elif body is not None:
            kwargs["json"] = body
        response = await self.client.request(method, path, **kwargs)
        text = await response.text()
        self.responses.append(f"{response.status} {dict(response.headers)} {text}")
        assert response.content_type == "application/json", text
        result = json.loads(text)
        if response.status == 200 and "raw_tx" in result:
            result["decoded"] = self.check_tx(result)
        elif response.status != 200:
            assert set(result) == {"error", "detail"}
        return response.status, result

    def check_tx(self, result: dict[str, Any]) -> dict[str, Any]:
        """The response must describe the transaction that is really in raw_tx."""
        assert set(result) == {"raw_tx", "tx_hash", "from", "to", "chain_id"}
        decoded = helpers.decode_tx(result["raw_tx"])
        assert decoded["signer"] == result["from"]
        assert decoded["to"] == result["to"]
        assert decoded["chain_id"] == result["chain_id"]
        assert decoded["hash"] == result["tx_hash"]
        assert decoded["access_list"] == []
        self.transactions.append(decoded)
        return decoded

    async def get(self, path: str, **kwargs: Any):
        return await self.call("GET", path, **kwargs)

    async def post(self, path: str, body: Any = None, **kwargs: Any):
        return await self.call("POST", path, body, **kwargs)

    async def derive(self, store: str = "shop", first_index: int = 0, count: int = 20, family: str = "evm"):
        return await self.post("/v1/derive", {"store": store, "family": family, "first_index": first_index, "count": count})

    async def fund(self, key: str, **changes: Any):
        return await self.post("/v1/sign/fund", fund_request(key, **changes))

    async def sweep(self, key: str, **changes: Any):
        return await self.post("/v1/sign/sweep", sweep_request(key, **changes))

    async def sweep_native(self, key: str, **changes: Any):
        return await self.post("/v1/sign/sweep_native", sweep_native_request(key, **changes))

    def close(self) -> None:
        """Stop this signer, as the end of the process does: the files are closed and the lock is free."""
        self.state.close()

    def journal_rows(self) -> int:
        """The signatures that the calls of the test made (without the rows of prefund)."""
        query = "SELECT count(*) FROM signatures WHERE idempotency_key NOT LIKE 'prefund:%'"
        return self.state.journal._db.execute(query).fetchone()[0]

    def prefund(self, chain: str = "anvil", store: str = "shop", indexes=range(20), value: int = PREFUND):
        """Funding rows in the journal, as if the signer signed them on an earlier day.

        A token sweep needs a signed funding of its address (SPEC 7.8). Tests of OTHER rules use this, so that
        they do not make 20 signatures first. The tests of the budget rule use the real funding call.
        """
        journal = self.state.journal
        wallet = self.state.keystore.wallet
        with journal.transaction():
            for index in indexes:
                to = wallet.deposit_address(ACCOUNTS[store], index)
                number = journal._db.execute("SELECT count(*) FROM signatures").fetchone()[0]
                journal.insert_signature(
                    f"prefund:{chain}:{store}:{index}:{number}", "prefund", "fund", chain, 10**9 + number,
                    wallet.fee_address, to, str(value), "0", "0", str(value), "2026-01-01", "{}", "{}",
                )  # fmt: skip

    def audit_lines(self) -> list[dict[str, Any]]:
        with open(self.env.paths.audit) as f:
            return [json.loads(line) for line in f]


def fund_request(key: str, **changes: Any) -> dict[str, Any]:
    body = {
        "idempotency_key": key,
        "chain": "anvil",
        "store": "shop",
        "index": 3,
        "nonce": 0,
        "max_fee_per_gas_wei": str(60 * GWEI),
        "max_priority_fee_per_gas_wei": str(30 * GWEI),
        "value_wei": str(5 * 10**15),
        "replaces": None,
    }
    return _changed(body, changes)


def sweep_request(key: str, **changes: Any) -> dict[str, Any]:
    body = {
        "idempotency_key": key,
        "chain": "anvil",
        "store": "shop",
        "index": 3,
        "nonce": 0,
        "gas_limit": 70000,
        "max_fee_per_gas_wei": str(60 * GWEI),
        "max_priority_fee_per_gas_wei": str(30 * GWEI),
        "amount": "2000000",
        "token": None,
        "replaces": None,
    }
    return _changed(body, changes)


def sweep_native_request(key: str, **changes: Any) -> dict[str, Any]:
    # as the engine makes it: priority fee = max fee, so the fee is exactly gas_limit x max_fee
    body = {
        "idempotency_key": key,
        "chain": "anvil",
        "store": "shop",
        "index": 3,
        "nonce": 0,
        "gas_limit": 21000,
        "max_fee_per_gas_wei": str(60 * GWEI),
        "max_priority_fee_per_gas_wei": str(60 * GWEI),
        "value_wei": str(5 * 10**16),
        "replaces": None,
    }
    return _changed(body, changes)


REMOVE = object()


def _changed(body: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    for name, value in changes.items():
        if value is REMOVE:
            body.pop(name, None)
        else:
            body[name] = value
    return body


async def make_api(env: Env, aiohttp_client: Any) -> Api:
    state = build_state(env.paths, now=env.clock, monotonic=env.clock.monotonic)
    client = await aiohttp_client(create_app(state))
    return Api(client, env, state)


@pytest.fixture
async def bare(env: Env, aiohttp_client: Any) -> Api:
    """Signer with a keystore, and indexes 0..19 given out for store 'shop'. No funding: no fee budget."""
    api = await make_api(env, aiohttp_client)
    status, _ = await api.derive()
    assert status == 200
    return api


@pytest.fixture
async def api(bare: Api) -> Api:
    """The signer of 'bare', with a funding in the journal for each address but index 5 (the fee budget, SPEC 7.8).

    Index 5 has no funding: the tests of the funding total use it.
    """
    return prefund_all(bare, skip=(5,))


def check_audit(env: Env) -> int:
    """The check of the command audit-verify: the chain of audit.log, and each line against the journal."""
    from acctpool_signer import audit
    from acctpool_signer.journal import Journal

    journal = Journal(env.paths.journal, readonly=True)
    try:
        return audit.verify(env.paths.audit, journal).seq
    finally:
        journal.close()


def prefund_all(api: Api, skip: tuple[int, ...] = ()) -> Api:
    """Funding rows for the addresses that the tests of a file sweep: 'shop' on all chains, 'other' on anvil."""
    indexes = [index for index in range(20) if index not in skip]
    for chain in ("anvil", "polygon", "bnb"):
        api.prefund(chain, "shop", indexes)
    api.prefund("anvil", "other", indexes)
    return api
