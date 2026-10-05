"""The stock Bitcart coin daemon of a chain, through Bitcart's own coin objects (SPEC-v4 2.A). No own RPC here."""

from decimal import Decimal
from typing import Any

from bitcart.providers.jsonrpcrequests import RPCProxy

from api.services.coins import CoinService

from .constants import NATIVE, NATIVE_DECIMALS, Chain


async def server(container: Any, chain: Chain) -> Any:
    """`.server.<method>(...)` of the base coin object (no wallet) of the chain's currency."""
    coins = await container.get(CoinService)
    return (await coins.get_coin(chain.currency)).server


def units(amount: Decimal | str, decimals: int) -> int:
    # the same rounding as the stock daemon (to_wei): cut, do not round
    return int(Decimal(amount).scaleb(decimals))


def coins(value: int | Decimal, decimals: int) -> Decimal:
    return Decimal(int(value)).scaleb(-decimals)


def decimals(chain: Chain, asset: str) -> int:
    return NATIVE_DECIMALS if asset == NATIVE else chain.usdt_decimals


def token_wallet(srv: Any, chain: Chain, address: str) -> RPCProxy:
    """The daemon's diskless watch-only USDT wallet of an address (the one that detect.load_addresses loads), on an
    own connection: the caller closes it."""
    xpub = {"xpub": address, "contract": chain.usdt, "diskless": True}
    return RPCProxy(srv.url, srv.username, srv.password, xpub=xpub, proxy=srv.proxy, verify=srv.verify)  # type: ignore[arg-type]


async def balance(srv: Any, chain: Chain, address: str, asset: str) -> int:
    """Balance in base units (wei or token units).

    USDT is read through the wallet (`getbalance`), not with `getaddressbalance_contract`: that call of the stock
    daemon (0.10.3.0) makes a new web3 contract object on each call and keeps it as a key of its decimals cache
    forever, about 80 KB per call. One reading per watched address per minute filled the host memory in 3 days
    (2026-10-04). The wallet has the daemon's one cached contract object. `readcontract(<address>, "decimals")`
    leaks one object only, on the first call after a daemon start: after that its string key is in the cache."""
    if asset == NATIVE:
        return units(await srv.getaddressbalance(address=address), NATIVE_DECIMALS)
    divisibility = int(await srv.readcontract(chain.usdt, "decimals"))
    if divisibility != chain.usdt_decimals:
        raise ValueError(f"the daemon says {divisibility} decimals for the USDT of {chain.name}")
    wallet = token_wallet(srv, chain, address)
    try:
        return units((await wallet.getbalance())["confirmed"], chain.usdt_decimals)
    finally:
        await wallet.close()


async def height(srv: Any) -> int:
    """The last block that the daemon has processed (its events are out)."""
    return int((await srv.getinfo())["blockchain_height"])


async def head(srv: Any) -> int:
    """The head block of the daemon's provider: balances are read there."""
    return int((await srv.getinfo())["server_height"])


async def confirmations(srv: Any, deposit: Any) -> int:
    """Of a deposit row: from its tx, or for one found by balance from the provider head of its reading."""
    if deposit.source == "balance":
        return max(0, await head(srv) - int(deposit.height or 0) + 1)
    return int((await srv.gettransaction(tx=deposit.tx_hash)).get("confirmations") or 0)


def transfer_data(to: str, amount: int) -> str:
    """Call data of ERC-20 transfer(to, amount), for the gas estimate of the daemon."""
    return "0xa9059cbb" + to[2:].lower().rjust(64, "0") + f"{amount:064x}"
