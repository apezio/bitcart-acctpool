"""Second opinion before every signature (SPEC-v4 4.7): one independent JSON-RPC provider per chain, from a
root-owned file (`[polygon] url = "https://..."`). The URL is never logged. No file or no entry for a chain =
the check fails: payouts on that chain wait."""

import os
import tomllib
from typing import Any

import aiohttp

from .constants import NATIVE, Chain

PATH_ENV = "ACCTPOOL_SECOND_OPINION"
DEFAULT_PATH = "/run/acctpool/second.toml"


class Down(Exception):
    pass


class Mismatch(Exception):
    pass


def url_of(chain: Chain) -> str:
    try:
        with open(os.environ.get(PATH_ENV, DEFAULT_PATH), "rb") as f:
            url = tomllib.load(f)[chain.name]["url"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Down(f"no second-opinion provider for {chain.name}") from None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise Down(f"the second-opinion URL of {chain.name} is not usable")
    return url


async def rpc(session: aiohttp.ClientSession, url: str, method: str, *params: Any) -> Any:
    try:
        async with session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}) as r:
            data = await r.json(content_type=None)
        return int(data["result"], 16)
    except Exception as e:  # the text of the exception can hold the URL
        raise Down(f"second opinion: {method} failed ({type(e).__name__})") from None


async def check(chain: Chain, address: str, asset: str, need: int, daemon_read: Any) -> None:
    """Raises Down or Mismatch unless the second provider sees the chain id and a balance of `address` that is at
    least `need` and equal to the daemon's. daemon_read() -> (balance, height) of the daemon, base units."""
    url = url_of(chain)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as session:
        if await rpc(session, url, "eth_chainId") != chain.chain_id:
            raise Mismatch(f"second opinion: wrong chain id on {chain.name}")

        async def read() -> tuple[int, int]:
            head = await rpc(session, url, "eth_blockNumber")
            if asset == NATIVE:
                return await rpc(session, url, "eth_getBalance", address, "latest"), head
            data = "0x70a08231" + address[2:].lower().rjust(64, "0")
            return await rpc(session, url, "eth_call", {"to": chain.usdt, "data": data}, "latest"), head

        mine, (theirs, their_head) = await daemon_read(), await read()
        if theirs != mine[0] and their_head > mine[1]:
            # the second provider is ahead: read both again, once
            mine, (theirs, their_head) = await daemon_read(), await read()
    if theirs != mine[0]:
        raise Mismatch(f"second opinion: {asset} balance differs on {chain.name} ({mine[0]} vs {theirs})")
    if theirs < need:
        raise Mismatch(f"second opinion: {asset} balance {theirs} is below {need} on {chain.name}")
