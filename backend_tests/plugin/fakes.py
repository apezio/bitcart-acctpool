"""Fakes for what the unit tests must not reach for real: the stock coin daemon (with a small in-memory chain
behind it), the signer and the second-opinion provider. All are small aiohttp servers inside the test process.
No key and no seed is in this file: the fake signer makes addresses and "signatures" from hashes."""

import asyncio
import hashlib
import hmac
import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from aiohttp import web

USDT = {
    "matic": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    "eth": "0x1111111111111111111111111111111111111111",  # the anvil test chain (ACCTPOOL_TEST_CHAIN)
    "bnb": "0x55d398326f99059fF775485246999027B3197955",
}
CHAIN_IDS = {"matic": 137, "eth": 31337, "bnb": 56}
TOKEN_DECIMALS = {"matic": 6, "eth": 6, "bnb": 18}
GWEI = 10**9
TOKEN_GAS_USED = 50_000


async def start(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def coins(units: int, decimals: int) -> str:
    return format(Decimal(units).scaleb(-decimals), "f")


class Chain:
    """Balances, nonces, a mempool and receipts. Transactions are the effects that the fake signer registers."""

    def __init__(self, coin: str) -> None:
        self.coin = coin
        self.usdt = USDT[coin]
        self.decimals = TOKEN_DECIMALS[coin]
        self.native: dict[str, int] = defaultdict(int)
        self.token: dict[str, int] = defaultdict(int)
        self.nonces: dict[str, int] = defaultdict(int)
        self.height = 1000
        self.gas_price = 30 * GWEI
        self.mempool: dict[str, dict[str, Any]] = {}  # hash -> effect
        self.effects: dict[str, dict[str, Any]] = {}  # raw -> effect
        self.mined: dict[str, int] = {}  # hash -> block
        self.failed: set[str] = set()  # mined with status 0
        self.broadcasts: list[str] = []
        self.broadcast_error: str | None = None
        self.min_fee = 0  # a transaction with a lower max fee stays in the mempool
        self.lag = 0  # blocks that the daemon has not processed yet (getinfo blockchain_height)
        self.stale: dict[str, int] = {}  # address -> a pending nonce that the daemon gives one time (a stale provider)
        self.receipts: dict[str, tuple[int, int]] = {}  # hash -> (gas used, effective gas price)

    def pay(self, to: str, native: int = 0, token: int = 0, tx_hash: str | None = None) -> str:
        """A customer payment, mined at once."""
        tx_hash = tx_hash or "0x" + hashlib.sha256(f"{to}{native}{token}{self.height}{len(self.mined)}".encode()).hexdigest()
        self.native[to.lower()] += native
        self.token[to.lower()] += token
        self.height += 1
        self.mined[tx_hash] = self.height
        return tx_hash

    def mine(self, blocks: int = 1) -> None:
        for effect in sorted(self.mempool.values(), key=lambda e: (e["from"], e["nonce"], -e["max_fee"])):
            sender = effect["from"].lower()
            if (
                effect["hash"] not in self.mempool
                or effect["nonce"] != self.nonces[sender]
                or effect["max_fee"] < self.min_fee
            ):
                continue
            used = TOKEN_GAS_USED if effect["kind"] == "sweep" else 21000
            if self.native[sender] < effect["gas"] * effect["max_fee"] + effect["value"]:
                continue
            self.native[sender] -= used * effect["max_fee"] + effect["value"]
            self.native[effect["to"].lower()] += effect["value"]
            if effect["kind"] == "sweep":
                self.token[sender] -= effect["amount"]
                self.token[effect["dest"].lower()] += effect["amount"]
            self.nonces[sender] += 1
            self.mined[effect["hash"]] = self.height + 1
            self.receipts[effect["hash"]] = (used, effect["max_fee"])
            for other in [h for h, e in self.mempool.items() if e["from"].lower() == sender and e["nonce"] == effect["nonce"]]:
                del self.mempool[other]
        self.height += blocks

    def pending_nonce(self, address: str) -> int:
        mine = [e["nonce"] + 1 for e in self.mempool.values() if e["from"].lower() == address.lower()]
        return max([self.nonces[address.lower()], *mine])


class FakeDaemon:
    """The JSON-RPC calls that Bitcart 0.10.3.0 and the plugin send to an eth-based daemon."""

    def __init__(self, coin: str) -> None:
        self.coin = coin
        self.chain = Chain(coin)
        self.decimals = TOKEN_DECIMALS[coin]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: set[str] = set()
        self.loaded: set[str] = set()
        self.delay: dict[str, float] = {}  # method -> seconds before the answer (a slow provider)
        self.app = web.Application()
        self.app.router.add_post("/", self.handle)
        self.app.router.add_get("/spec", self.spec)

    def called(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    async def spec(self, request: web.Request) -> web.Response:
        return web.json_response({"version": "4.5.0", "exceptions": {}})

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        method, params = body["method"], body.get("params") or {}
        if isinstance(params, list):
            params = {**(params.pop() if params and isinstance(params[-1], dict) else {}), "_args": params}
        xpub = params.pop("xpub", None)
        self.calls.append((method, params))
        await asyncio.sleep(self.delay.get(method, 0))
        handler = getattr(self, f"rpc_{method}", None)
        try:
            if method in self.fail or handler is None:
                raise RuntimeError(f"fake daemon: no answer for {method}")
            result = handler(xpub, *params.pop("_args", []), **params)
        except Exception as e:
            return web.json_response({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32603, "message": str(e)}})
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})

    # -- the chain layer that the plugin uses

    def rpc_batch_load(self, xpub: Any, wallets: list[dict[str, Any]], **kw: Any) -> bool:
        self.loaded.update(w["xpub"].lower() for w in wallets)
        return True

    def rpc_getaddressbalance(self, xpub: Any, address: str) -> str:
        return coins(self.chain.native[address.lower()], 18)

    def rpc_getinfo(self, xpub: Any) -> dict[str, Any]:
        height = self.chain.height
        return {"blockchain_height": height - self.chain.lag, "server_height": height, "connected": True}

    def rpc_gettransaction(self, xpub: Any, tx: str) -> dict[str, Any]:
        if tx not in self.chain.mined:
            raise RuntimeError("TransactionNotFound")
        block = self.chain.mined[tx]
        return {"hash": tx, "blockNumber": block, "confirmations": self.chain.height - block + 1}

    def rpc_get_tx_status(self, xpub: Any, tx: str) -> dict[str, Any]:
        if tx not in self.chain.mined:
            raise RuntimeError("TransactionNotFound")
        status = 0 if tx in self.chain.failed else 1
        used, price = self.chain.receipts.get(tx, (21000, 0))
        return {
            "status": status,
            "blockNumber": self.chain.mined[tx],
            "confirmations": self.chain.height - self.chain.mined[tx] + 1,
            "gasUsed": used,
            "effectiveGasPrice": price + 7,  # as anvil: not always what was charged
        }

    def rpc_getfeerate(self, xpub: Any, multiplier: Any = None) -> int:
        return int(self.chain.gas_price * float(multiplier or 1))

    def rpc_getnonce(self, xpub: Any, address: str, pending: Any = True) -> int:
        stale = self.chain.stale.pop(address.lower(), None)
        return self.chain.pending_nonce(address) if stale is None else stale

    def rpc_get_default_gas(self, xpub: Any, tx: Any) -> int:
        return TOKEN_GAS_USED

    def rpc_broadcast(self, xpub: Any, tx: str) -> str:
        self.chain.broadcasts.append(tx)
        if self.chain.broadcast_error:
            raise RuntimeError(self.chain.broadcast_error)
        effect = self.chain.effects[tx]
        if effect["hash"] in self.chain.mined or effect["nonce"] < self.chain.nonces[effect["from"].lower()]:
            raise RuntimeError("nonce too low")
        if effect["hash"] in self.chain.mempool:
            raise RuntimeError("already known")
        self.chain.mempool[effect["hash"]] = effect
        return effect["hash"]

    # -- what stock Bitcart asks when wallets and stock invoices are made

    def rpc_validatekey(self, xpub: Any, *args: Any, **kwargs: Any) -> bool:
        return True

    def rpc_get_tokens(self, xpub: Any) -> dict[str, str]:
        return {"USDT": USDT[self.coin]}

    def rpc_validatecontract(self, xpub: Any, contract: str) -> bool:
        return True

    def rpc_normalizeaddress(self, xpub: Any, address: str) -> str:
        return address

    def rpc_readcontract(self, xpub: Any, contract: str, function: str, *args: Any) -> Any:
        return {"symbol": "USDT", "decimals": self.decimals, "name": "Tether USD"}[function]

    def rpc_recommended_fee(self, xpub: Any, *args: Any) -> int:
        return 0

    def rpc_getbalance(self, xpub: Any) -> dict[str, str]:
        # the plugin's USDT reading: the diskless watch-only token wallet of an address (as the stock daemon: only
        # "confirmed"). Not rpc_getaddressbalance_contract: that call leaks in the stock daemon, the plugin must
        # never send it (no handler here: a call fails the test)
        if isinstance(xpub, dict) and xpub.get("diskless") and xpub.get("contract"):
            assert xpub["contract"].lower() == self.chain.usdt.lower()
            return {"confirmed": coins(self.chain.token[xpub["xpub"].lower()], self.decimals)}
        return {"confirmed": "0", "unconfirmed": "0", "unmatured": "0"}

    def rpc_setrequestaddress(self, xpub: Any, *args: Any) -> bool:
        return True

    def rpc_modifypaymenturl(self, xpub: Any, url: str, amount: Any, divisibility: Any = None) -> str:
        units = int(Decimal(str(amount)) * 10 ** (self.decimals if "/transfer" in url else 18))
        return re.sub(r"(uint256|value)=\d+", lambda m: f"{m.group(1)}={units}", url)

    def rpc_add_request(self, xpub: Any, amount: Any = None, memo: str = "", **kwargs: Any) -> dict[str, Any]:
        """The stock flow of a wallet that is not in pool mode."""
        address = xpub["xpub"] if isinstance(xpub, dict) else xpub
        amount = str(amount or 0)
        request_id = hashlib.sha256(f"{self.coin}|{address}|{amount}|{memo}".encode()).hexdigest()[:32]
        return {
            "request_id": request_id, "id": request_id, "address": address, "URI": f"ethereum:{address}",
            "status": 0, "status_str": "Pending", "amount_USDT": amount, f"amount_{self.coin.upper()}": amount,
            "message": memo, "tx_hashes": [], "sent_amount": "0", "confirmations": 0, "payment_address": None,
        }  # fmt: skip


class FakeSigner:
    """SPEC 3.4 as far as the client needs it: token check, derive, idempotent sign with the stored answer, one
    signature per (chain, sender, nonce) unless `replaces`. It registers each transaction on the fake chain."""

    def __init__(self, token: str, secret: bytes, chains: dict[str, Chain], stores: tuple[str, ...] = ("teststore",)) -> None:
        self.token, self.secret, self.chains, self.stores = token, secret, chains, stores
        self.fee_wallet = self.address("fee", -1)
        self.journal: dict[str, tuple[str, dict[str, Any]]] = {}
        self.nonces: dict[tuple[str, str, int], str] = {}
        self.top_fee: dict[tuple[str, str, int], int] = {}  # the highest max fee signed for a (chain, sender, nonce)
        self.calls: list[tuple[str, Any]] = []
        self.next_error: tuple[int, dict[str, Any]] | None = None
        self.highest: dict[str, int] = {}  # store -> highest index given out by derive
        self.answer_changes: dict[str, Any] = {}  # fields of the next sign answer to change (a wrong signer)
        self.app = web.Application()
        self.app.router.add_get("/v1/status", self.status)
        self.app.router.add_post("/v1/derive", self.derive)
        self.app.router.add_post("/v1/sign/{kind}", self.sign)

    def address(self, store: str, index: int) -> str:
        return "0x" + hashlib.sha256(self.secret + f"|{store}|{index}".encode()).hexdigest()[:40]

    def destination(self, store: str, kind: str) -> str:
        return self.address(f"{store}-{kind}", 0)

    def signed(self, kind: str | None = None) -> list[Any]:
        return [body for path, body in self.calls if path.startswith("/v1/sign/") and (kind is None or path.endswith(kind))]

    def refuse(self, request: web.Request) -> web.Response | None:
        if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {self.token}"):
            return web.json_response({"error": "unauthorized", "detail": "bad token"}, status=401)
        if self.next_error is not None:
            status, body = self.next_error
            self.next_error = None
            return web.json_response(body, status=status)
        return None

    async def status(self, request: web.Request) -> web.Response:
        if (refusal := self.refuse(request)) is not None:
            return refusal
        stores = {
            s: {"destinations": {c: self.destination(s, "usdt") for c in ("polygon", "anvil", "bnb")},
                "highest_index": self.highest.get(s)}
            for s in self.stores
        }  # fmt: skip
        return web.json_response({"keystore": True, "seed_id": "fakeseed", "config_sha256": "0" * 64,
                                  "fee_wallets": {"evm": self.fee_wallet}, "chains": {}, "stores": stores})  # fmt: skip

    async def derive(self, request: web.Request) -> web.Response:
        if (refusal := self.refuse(request)) is not None:
            return refusal
        body = await request.json()
        self.calls.append(("derive", body))
        first = body["first_index"]
        rows = [{"index": i, "address": self.address(body["store"], i)} for i in range(first, first + body["count"])]
        self.highest[body["store"]] = max(self.highest.get(body["store"], -1), first + body["count"] - 1)
        return web.json_response({"seed_id": "fakeseed", "addresses": rows})

    async def sign(self, request: web.Request) -> web.Response:
        if (refusal := self.refuse(request)) is not None:
            return refusal
        body = await request.json()
        kind = request.match_info["kind"]
        self.calls.append((request.path, body))
        fingerprint = hashlib.sha256(repr((kind, sorted(body.items()))).encode()).hexdigest()
        known = self.journal.get(body["idempotency_key"])
        if known is not None:
            if known[0] != fingerprint:
                return web.json_response({"error": "idempotency_conflict", "detail": "other request"}, status=409)
            return web.json_response(known[1])
        chain = self.chains[{"polygon": "matic", "anvil": "eth", "bnb": "bnb"}[body["chain"]]]
        sender = self.fee_wallet if kind == "fund" else self.address(body["store"], body["index"])
        slot = (body["chain"], sender, body["nonce"])
        if slot in self.nonces and not body.get("replaces"):
            return web.json_response({"error": "idempotency_conflict", "detail": "nonce signed"}, status=409)
        fee, top = int(body["max_fee_per_gas_wei"]), self.top_fee.get(slot, 0)
        if body.get("replaces") and (fee * 10 < top * 11 or fee <= top):  # the real signer's replacement rule
            return web.json_response({"error": "invalid_request", "detail": "replaces: fee not 10% above"}, status=400)
        self.top_fee[slot] = max(top, fee)
        raw = "0x02" + fingerprint
        tx_hash = "0x" + hashlib.sha256(raw.encode()).hexdigest()
        self.nonces[slot] = tx_hash
        to = {"fund": self.address(body["store"], body["index"]), "sweep": chain.usdt}.get(kind) or self.destination(
            body["store"], "native"
        )
        chain.effects[raw] = {
            "kind": kind, "hash": tx_hash, "from": sender, "to": to, "nonce": body["nonce"],
            "max_fee": int(body["max_fee_per_gas_wei"]), "gas": int(body.get("gas_limit", 21000)),
            "value": int(body.get("value_wei", 0)), "amount": int(body.get("amount", 0)),
            "dest": self.destination(body["store"], "usdt"),
        }  # fmt: skip
        answer = {"raw_tx": raw, "tx_hash": tx_hash, "from": sender, "to": to, "chain_id": CHAIN_IDS[chain.coin]}
        answer.update(self.answer_changes)
        self.answer_changes = {}
        self.journal[body["idempotency_key"]] = (fingerprint, answer)
        return web.json_response(answer)


class SecondOpinion:
    """A JSON-RPC provider that reads the fake chains; `lie` adds 1 to every balance, `down` answers 500."""

    def __init__(self, chains: dict[int, Chain]) -> None:
        self.chains = chains  # chain id -> chain
        self.lie = False
        self.down = False
        self.app = web.Application()
        self.app.router.add_post("/{chain_id}", self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        if self.down:
            return web.Response(status=500)
        chain_id = int(request.match_info["chain_id"])
        chain = self.chains[chain_id]
        body = await request.json()
        method, params = body["method"], body["params"]
        if method == "eth_chainId":
            value = chain_id
        elif method == "eth_blockNumber":
            value = chain.height
        elif method == "eth_getBalance":
            value = chain.native[params[0].lower()]
        else:  # eth_call balanceOf
            value = chain.token["0x" + params[0]["data"][-40:]]
        if self.lie and method in ("eth_getBalance", "eth_call"):
            value += 1
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": hex(value)})
