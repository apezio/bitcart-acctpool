"""Client of the signer API (SPEC 3.4, SPEC-v4 3). The worker only: the backend has no route to the signer.

The token is read from its file for every call. The token, and any text that the signer did not send as
"detail", never go into an exception text or a log line.
"""

import asyncio
import json
import os
import re
from typing import Any

import aiohttp

URL_ENV = "ACCTPOOL_SIGNER_URL"
TOKEN_FILE_ENV = "ACCTPOOL_TOKEN_FILE"  # noqa: S105  (a name, not a token)
DEFAULT_TOKEN_FILE = "/run/acctpool/signer.token"  # noqa: S105  (a path)
MAX_DERIVE = 200
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
RAW_RE = re.compile(r"^0x[0-9a-f]+$")


class SignerError(Exception):
    """code: the error code of the signer, or unavailable / protocol / config. status: HTTP status or None."""

    def __init__(self, code: str, detail: str = "", status: int | None = None) -> None:
        self.code, self.detail, self.status = code, str(detail)[:300], status
        super().__init__(f"{code}: {self.detail}" if self.detail else code)

    @property
    def refused(self) -> bool:
        """A clear answer that the same request will not change: do not retry it."""
        return self.status is not None and 400 <= self.status < 500 and self.code not in ("rate_limited", "unauthorized")


def need(condition: bool, what: str) -> None:
    if not condition:
        raise SignerError("protocol", what)


class SignerClient:
    def __init__(self, url: str | None = None, token_file: str | None = None, timeout: float = 15.0) -> None:
        self.url = (url if url is not None else os.environ.get(URL_ENV, "")).rstrip("/")
        self.token_file = token_file or os.environ.get(TOKEN_FILE_ENV, DEFAULT_TOKEN_FILE)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def read_token(self) -> str:
        try:
            with open(self.token_file, encoding="ascii") as f:
                token = f.read().strip()
        except (OSError, UnicodeError):
            raise SignerError("config", "the token file cannot be read") from None
        if len(token) < 32 or any(c.isspace() for c in token):
            raise SignerError("config", "the token in the token file is not usable")
        return token

    async def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.url.startswith(("http://", "https://")):
            raise SignerError("config", f"{URL_ENV} is not set")
        token = await asyncio.to_thread(self.read_token)
        if self.session is None:
            # trust_env off: a proxy from the environment must never see a signer call
            self.session = aiohttp.ClientSession(timeout=self.timeout, trust_env=False)
        try:
            async with self.session.request(
                method, self.url + path, json=payload, headers={"Authorization": f"Bearer {token}"}, allow_redirects=False
            ) as response:
                status, body = response.status, await response.read()
        except (TimeoutError, aiohttp.ClientError, OSError) as e:
            raise SignerError("unavailable", type(e).__name__) from None
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if status >= 400:
            if isinstance(data, dict) and isinstance(data.get("error"), str):
                raise SignerError(data["error"], str(data.get("detail", "")), status)
            raise SignerError("unavailable" if status >= 500 else "protocol", f"HTTP {status}", status)
        need(status == 200 and isinstance(data, dict), "no JSON object")
        return data  # type: ignore[return-value]

    async def status(self) -> dict[str, Any]:
        data = await self.call("GET", "/v1/status")
        need(isinstance(data.get("keystore"), bool), "status: keystore")
        need(not data["keystore"] or bool(ADDRESS_RE.match(str((data.get("fee_wallets") or {}).get("evm")))), "status: fee")
        return data

    async def derive(self, store: str, first: int, count: int) -> list[str]:
        data = await self.call("POST", "/v1/derive", {"store": store, "family": "evm", "first_index": first, "count": count})
        rows = data.get("addresses")
        need(isinstance(rows, list) and len(rows) == count, "derive: number of addresses")
        for offset, row in enumerate(rows):  # type: ignore[arg-type]
            need(isinstance(row, dict) and row.get("index") == first + offset, "derive: index")
            need(bool(ADDRESS_RE.match(str(row.get("address")))), "derive: address")
        return [row["address"] for row in rows]  # type: ignore[union-attr]

    async def sign(self, kind: str, fields: dict[str, Any], sender: str, chain_id: int) -> tuple[str, str]:
        """kind: fund, sweep or sweep_native. Returns (raw_tx, tx_hash). The signed sender and chain id must be the
        ones the second opinion checked (the signer signs for the index)."""
        data = await self.call("POST", f"/v1/sign/{kind}", fields)
        raw, tx_hash = data.get("raw_tx"), data.get("tx_hash")
        need(isinstance(raw, str) and bool(RAW_RE.match(raw)), "sign: raw_tx")
        need(isinstance(tx_hash, str) and bool(HASH_RE.match(tx_hash)), "sign: tx_hash")
        need(str(data.get("from")).lower() == sender.lower(), "sign: from is not the expected sender")
        need(data.get("chain_id") == chain_id, "sign: chain_id is not the chain's")
        return raw, tx_hash  # type: ignore[return-value]
