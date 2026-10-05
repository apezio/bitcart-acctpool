import os
from dataclasses import dataclass

PLUGIN_NAME = "acctpool"
LOOKUP_PREFIX = "acctpool:"
META_KEY = "acctpool"


@dataclass(frozen=True)
class Chain:
    name: str
    currency: str  # Bitcart currency (= the stock daemon) of this chain
    chain_id: int
    usdt: str
    usdt_decimals: int
    payout_confirmations: int
    pay_at_once: bool  # fee rule: no fee limit on this chain (SPEC-v4 4.6.2)
    native_gas_limit: int = 21000
    token_gas_limit: int = 100000  # fallback when the daemon cannot estimate


CHAINS: dict[str, Chain] = {
    chain.name: chain
    for chain in (
        Chain("polygon", "matic", 137, "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6, 16, True),
        Chain("ethereum", "eth", 1, "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6, 64, False),
        Chain("bnb", "bnb", 56, "0x55d398326f99059fF775485246999027B3197955", 18, 15, False),
    )
}
# Tests only: "<chain id>:<usdt contract>:<decimals>". The anvil chain then takes the place of ethereum on the
# eth daemon. Never set in production.
TEST_CHAIN_ENV = "ACCTPOOL_TEST_CHAIN"
if os.environ.get(TEST_CHAIN_ENV):
    _id, _usdt, _decimals = os.environ[TEST_CHAIN_ENV].split(":")
    del CHAINS["ethereum"]
    CHAINS["anvil"] = Chain("anvil", "eth", int(_id), _usdt, int(_decimals), 2, True)
BY_CURRENCY = {chain.currency: chain for chain in CHAINS.values()}

NATIVE = "native"
USDT = "usdt"
NATIVE_DECIMALS = 18


class Status:
    READY = "ready"
    IN_INVOICE = "in_invoice"
    PENDING_PAYOUT = "pending_payout"
    IN_PAYOUT = "in_payout"
    RETIRED = "retired"


class State:
    PLANNED = "planned"
    SIGNED = "signed"
    BROADCAST = "broadcast"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    OPEN = (PLANNED, SIGNED, BROADCAST)


class Event:
    """Kinds of plugin_acctpool_events. The first ten are alerts of the probe."""

    FEE_WALLET_LOW = "fee_wallet_low"
    PAYOUT_WAITING_24H = "payout_waiting_24h"
    LATE_PAYMENT = "late_payment"
    SECOND_OPINION_MISMATCH = "second_opinion_mismatch"
    SECOND_OPINION_DOWN = "second_opinion_down"
    READY_LOW = "ready_low"
    UNSWEPT_HIGH = "unswept_high"
    SIGNER_DOWN = "signer_down"
    PAYOUT_FAILED = "payout_failed"
    STOCK_FALLBACK = "stock_fallback"
    ADDRESS_MISMATCH = "address_mismatch"  # alert too: the table has an address that the signer does not derive
    ADDRESS_IN_USE = "address_in_use"  # alert too: a ready address has a nonce or a balance (a restored database)
    DEPOSIT_DUPLICATE = "deposit_duplicate"  # an event of a native transfer that a balance row counted already
    ERROR = "error"
    CREDIT = "credit"
    PAYOUT = "payout"


# plugin_acctpool_state keys; the worker writes them
STATE_LEADER = "leader"
STATE_SIGNER = "signer"
STATE_SETTINGS = "settings"
ALIVE_SECONDS = 300
DEFAULT_SETTINGS = {"ready_target": 50, "max_invoice_usd": "500", "speed_factor": "1.25"}
WATCH_DAYS = 30
