"""Driver of the v4 integration test. It runs inside the Bitcart container (v4i-bitcart), one step per call:

    python /plug/integration/evm/driver.py <step>

What is real: the Bitcart 0.10.3.0 backend and worker with the plugin (stock plugin loader, production mode),
the STOCK bitcart-eth:0.10.3.0 daemon, anvil, postgres, redis, the signer image. What is a test part: the proxy
(v4i-proxy) in front of anvil, which can block broadcasts of the daemon and be a lying or dead second opinion.
The driver is the customer, the admin and the block producer: it calls the Bitcart API over HTTP, sends payments
on anvil (impersonated accounts, no private key here), and reads the tables to check the result.
"""

import asyncio
import json
import os
import secrets
import signal
import sys
import time
from decimal import Decimal
from typing import Any

import aiohttp
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SHARED = "/shared"
STATE = f"{SHARED}/state.json"
API = "http://127.0.0.1:8000"
NODE = "http://v4i-anvil:8545"
DAEMON = "http://v4i-eth:5002"
PROXY = "http://v4i-proxy:8548"
SIGNER = os.environ.get("ACCTPOOL_SIGNER_URL", "")
CHAIN_ID = 31337
STORE = "teststore"
ETH = 10**18
USDT = 10**6
PASSWORD = "integration-test-1"  # the admin of a database that lives for one run


class Failed(AssertionError):
    pass


def check(condition: Any, what: str) -> None:
    if not condition:
        raise Failed(what)
    print(f"    ok   {what}", flush=True)  # noqa: T201


def same(got: Any, wanted: Any, what: str) -> None:
    if got != wanted:
        raise Failed(f"{what}: got {got!r}, wanted {wanted!r}")
    print(f"    ok   {what}: {got!r}", flush=True)  # noqa: T201


def load() -> dict[str, Any]:
    with open(STATE) as f:
        return json.load(f)


def save(data: dict[str, Any]) -> None:
    with open(STATE + ".new", "w") as f:
        json.dump(data, f, indent=1)
    os.replace(STATE + ".new", STATE)


def read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def write(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


def kill_worker() -> list[str]:
    """SIGKILL to every process of the worker (as the kernel OOM killer or `docker kill` does it)."""
    killed = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == str(os.getpid()):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"worker.py" in f.read():
                    os.kill(int(pid), signal.SIGKILL)
                    killed.append(pid)
        except OSError:
            continue
    return killed


def pad(value: int | str) -> str:
    return f"{value:064x}" if isinstance(value, int) else value.lower().removeprefix("0x").rjust(64, "0")


class Test:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.state: dict[str, Any] = load() if os.path.exists(STATE) else {}
        dsn = f"postgresql+asyncpg://postgres:{os.environ['DB_PASSWORD']}@{os.environ['DB_HOST']}/bitcart"
        self.db = create_async_engine(dsn, pool_size=2)

    # -- anvil, the daemon, the proxy

    async def rpc(self, url: str, method: str, *params: Any, auth: Any = None, named: dict[str, Any] | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": named if named is not None else list(params)}
        async with self.session.post(url, json=body, auth=auth) as response:
            answer = await response.json(content_type=None)
        if answer.get("error"):
            raise RuntimeError(f"{method}: {answer['error']}")
        return answer["result"]

    async def node(self, method: str, *params: Any) -> Any:
        return await self.rpc(NODE, method, *params)

    async def daemon(self, method: str, **params: Any) -> Any:
        return await self.rpc(DAEMON, method, auth=aiohttp.BasicAuth("electrum", "electrumz"), named=params)

    async def checksum(self, address: str) -> str:
        return await self.daemon("normalizeaddress", address=address)

    async def proxy(self, **mode: Any) -> None:
        async with self.session.post(PROXY + "/mode", json=mode) as response:
            same((await response.json())[next(iter(mode))], next(iter(mode.values())), f"proxy mode {mode}")

    async def send(self, sender: str, to: str | None, value: int = 0, data: str = "0x") -> str:
        """A transaction of an account that is not ours (impersonated), waited for until mined."""
        await self.node("anvil_impersonateAccount", sender)
        await self.node("anvil_setBalance", sender, hex(1000 * ETH))
        tx = {"from": sender, "value": hex(value), "data": data}
        if to is not None:
            tx["to"] = to
        tx_hash = await self.node("eth_sendTransaction", tx)
        await self.node("anvil_stopImpersonatingAccount", sender)
        for _ in range(60):
            receipt = await self.node("eth_getTransactionReceipt", tx_hash)
            if receipt:
                check(receipt["status"] == "0x1", f"test transaction {tx_hash[:12]}.. is mined")
                return receipt
            await asyncio.sleep(0.5)
        raise Failed(f"no receipt for {tx_hash}")

    async def pay_usdt(self, to: str, units: int) -> str:
        data = "0xa9059cbb" + pad(to) + pad(units)
        return (await self.send(self.state["holder"], self.state["usdt"], 0, data))["transactionHash"]

    async def pay_native(self, to: str, wei: int) -> str:
        return (await self.send("0x" + secrets.token_hex(20), to, wei))["transactionHash"]

    async def token(self, address: str) -> int:
        data = "0x70a08231" + pad(address)
        return int(await self.node("eth_call", {"to": self.state["usdt"], "data": data}, "latest"), 16)

    async def native(self, address: str) -> int:
        return int(await self.node("eth_getBalance", address, "latest"), 16)

    # -- Bitcart API

    async def api(self, method: str, path: str, body: Any = None, expect: int = 200) -> Any:
        headers = {"Authorization": f"Bearer {self.state['token']}"} if self.state.get("token") else {}
        async with self.session.request(method, API + path, json=body, headers=headers) as response:
            answer = await response.json(content_type=None)
            if response.status != expect:
                raise Failed(f"{method} {path}: HTTP {response.status}: {answer}")
            return answer

    async def invoice(self, price: Any = 5, **extra: Any) -> dict[str, Any]:
        wallets = [self.state["wallets"]["usdt"], self.state["wallets"]["native"]]
        body = {"store_id": self.state["store"], "price": price, "currency": "USD", "payment_methods": wallets, **extra}
        invoice = await self.api("POST", "/invoices", body)
        usdt, native = self.methods(invoice)
        same(native["payment_address"], usdt["payment_address"], "USDT and native coin of the invoice: ONE address")
        same(usdt["user_address"], usdt["payment_address"], "no sender-address step (user_address = deposit address)")
        check(usdt["metadata"]["acctpool"]["chain"] == "anvil", "the metadata.acctpool marker is there")
        return invoice

    def methods(self, invoice: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        by_wallet = {payment["wallet_id"]: payment for payment in invoice["payments"]}
        return by_wallet[self.state["wallets"]["usdt"]], by_wallet[self.state["wallets"]["native"]]

    # -- database (read only)

    async def rows(self, query: str, **params: Any) -> list[dict[str, Any]]:
        async with self.db.connect() as connection:
            return [dict(row) for row in (await connection.execute(text(query), params)).mappings().all()]

    async def address(self, invoice: dict[str, Any]) -> dict[str, Any]:
        (row,) = await self.rows("SELECT * FROM plugin_acctpool_addresses WHERE invoice_id = :id", id=invoice["id"])
        return row

    async def deposits(self, invoice: dict[str, Any]) -> list[dict[str, Any]]:
        query = "SELECT d.* FROM plugin_acctpool_deposits d JOIN plugin_acctpool_addresses a ON a.id = d.address_id"
        return await self.rows(query + " WHERE a.invoice_id = :id ORDER BY d.id", id=invoice["id"])

    async def payouts(self, invoice: dict[str, Any]) -> list[dict[str, Any]]:
        query = "SELECT p.* FROM plugin_acctpool_payouts p JOIN plugin_acctpool_addresses a ON a.id = p.address_id"
        return await self.rows(query + " WHERE a.invoice_id = :id ORDER BY p.id", id=invoice["id"])

    async def events(self, kind: str, since: int = 0) -> list[dict[str, Any]]:
        return await self.rows("SELECT * FROM plugin_acctpool_events WHERE kind = :k AND id > :s ORDER BY id", k=kind, s=since)

    async def last_event(self) -> int:
        return (await self.rows("SELECT coalesce(max(id), 0) AS n FROM plugin_acctpool_events"))[0]["n"]

    # -- waiting

    async def until(self, what: str, probe: Any, seconds: float = 120) -> Any:
        end = time.monotonic() + seconds
        while True:
            result = await probe()
            if result:
                print(f"    ok   {what}", flush=True)  # noqa: T201
                return result
            if time.monotonic() > end:
                raise Failed(f"not after {seconds:.0f} s: {what}")
            await asyncio.sleep(1)

    async def status(self, invoice: dict[str, Any], wanted: str, seconds: float = 90) -> dict[str, Any]:
        async def now() -> Any:
            current = await self.api("GET", f"/invoices/{invoice['id']}")
            return current if current["status"] == wanted else None

        return await self.until(f"invoice {invoice['id']} is {wanted}", now, seconds)

    async def swept(self, invoice: dict[str, Any], kinds: list[str], seconds: float = 180) -> list[dict[str, Any]]:
        """The confirmed payouts of the invoice's address, in order, are `kinds`, and the address is retired."""

        async def now() -> Any:
            rows = await self.payouts(invoice)
            hard = [row for row in rows if row["state"] == "failed" and row["error"] != "replaced"]
            if hard:
                raise Failed(f"a payout failed: {hard[0]['error']}")
            done = [row for row in rows if row["state"] == "confirmed"]
            retired = (await self.address(invoice))["status"] == "retired"
            return done if [row["kind"] for row in done] == kinds and retired else None

        return await self.until(f"payouts {kinds} confirmed and the address retired", now, seconds)

    async def settled(self, invoice: dict[str, Any], seconds: float = 180) -> None:
        async def now() -> Any:
            rows = await self.payouts(invoice)
            if [row for row in rows if row["state"] == "failed" and row["error"] != "replaced"]:
                raise Failed("a payout failed")
            idle = all(row["state"] in ("confirmed", "failed") for row in rows)
            return rows and idle and (await self.address(invoice))["status"] == "retired"

        await self.until("all payouts of the address confirmed, the address retired", now, seconds)


# ---------------------------------------------------------------- steps before the flows


async def chain_setup(test: Test) -> None:
    """The test token on anvil, the destinations, and the pools.toml of the signer."""
    same(int(await test.node("eth_chainId"), 16), CHAIN_ID, "chain id of anvil")
    holder = await test.checksum("0x" + secrets.token_hex(20))
    receipt = await test.send(holder, None, 0, read(f"{SHARED}/token.bytecode"))
    usdt = await test.checksum(receipt["contractAddress"])
    test.state.update(holder=holder, usdt=usdt)
    await test.send(holder, usdt, 0, "0x40c10f19" + pad(holder) + pad(10**9 * USDT))
    same(await test.token(holder), 10**9 * USDT, "token balance of the payer")
    test.state["destination"] = await test.checksum("0x" + secrets.token_hex(20))
    test.state["native_destination"] = await test.checksum("0x" + secrets.token_hex(20))
    caps = (
        'gas_limit_cap = 150000\nmax_fee_per_gas_cap_wei = "2000000000000"\nmax_fund_value_wei = "300000000000000000"\n'
        'max_fund_total_per_address_wei = "900000000000000000"\nfee_wallet_daily_cap_wei = "20000000000000000000"\n'
    )
    toml = (
        f'[signer]\nlisten = "0.0.0.0:7070"\n\n'
        f'[chains.anvil]\nfamily = "evm"\nchain_id = {CHAIN_ID}\nusdt = "{usdt}"\n{caps}\n'
        f'[stores.{STORE}]\naccount = 1\n[stores.{STORE}.destinations]\nanvil = "{test.state["destination"]}"\n'
        f'[stores.{STORE}.native_destinations]\nanvil = "{test.state["native_destination"]}"\n\n'
        "[recovery_tokens]\nanvil = []\n"
    )
    write(f"{SHARED}/pools.toml", toml)
    write(f"{SHARED}/test-chain", f"{CHAIN_ID}:{usdt}:6")
    save(test.state)


async def fee_wallet(test: Test) -> None:
    headers = {"Authorization": f"Bearer {read('/run/acctpool/signer.token')}"}
    async with test.session.get(f"{SIGNER}/v1/status", headers=headers) as response:
        status = await response.json()
    check(status["keystore"] is True, "the real signer has its keystore")
    same(status["chains"]["anvil"]["usdt"], test.state["usdt"], "USDT contract pinned in the signer")
    wallet = status["fee_wallets"]["evm"]
    await test.node("anvil_setBalance", wallet, hex(10 * ETH))
    test.state["fee_wallet"] = wallet
    save(test.state)


async def setup(test: Test) -> None:
    """The shop, through the Bitcart API as an admin does it."""

    async def backend_up() -> Any:
        try:
            async with test.session.get(API + "/plugins/acctpool/ui") as response:
                return response.status == 200
        except aiohttp.ClientError:
            return False

    await test.until("the backend answers with the plugin routes (stock plugin loader)", backend_up, 180)
    email = "admin@example.com"
    await test.api("POST", "/users", {"email": email, "password": PASSWORD, "is_superuser": True})
    token = await test.api("POST", "/token", {"email": email, "password": PASSWORD, "permissions": ["full_control"]})
    test.state["token"] = token["access_token"]

    async def worker() -> Any:
        rows = await test.rows("SELECT key FROM plugin_acctpool_state WHERE key IN ('leader', 'signer')")
        return len(rows) == 2

    await test.until("the worker leads and has the signer status", worker, 180)
    merchant = await test.checksum("0x" + secrets.token_hex(20))
    usdt = await test.api(
        "POST", "/wallets", {"name": "usdt", "xpub": merchant, "currency": "eth", "contract": test.state["usdt"]}
    )
    native = await test.api("POST", "/wallets", {"name": "native", "xpub": merchant, "currency": "eth"})
    store = await test.api("POST", "/stores", {"name": "integration", "wallets": [usdt["id"], native["id"]]})
    # the rate source of the test: fixed rules of the store (a stock setting); no exchange is asked
    await test.api("PATCH", f"/stores/{store['id']}/rate_rules", "USDT_USD = 1\nETH_USD = 2000\nX_X = 1")
    test.state.update(store=store["id"], wallets={"usdt": usdt["id"], "native": native["id"]})
    for wallet in (usdt, native):
        pool = await test.api(
            "POST", "/plugins/acctpool/pools", {"wallet_id": wallet["id"], "chain": "anvil", "store": STORE, "enabled": True}
        )
        check(pool["asset"] in ("usdt", "native"), f"pool of the {pool['asset']} wallet")
    await test.api("PUT", "/plugins/acctpool/settings", {"ready_target": 12})
    save(test.state)

    async def ready() -> Any:
        return (await test.rows("SELECT count(*) AS n FROM plugin_acctpool_addresses WHERE status = 'ready'"))[0]["n"] >= 12

    await test.until("the worker derived 12 ready addresses from the real signer", ready, 120)


# ---------------------------------------------------------------- flows


async def usdt_flow(test: Test, units: int = 5 * USDT) -> dict[str, Any]:
    """Exact USDT -> credit by the plugin's own new_transaction handler -> complete -> fund -> sweep."""
    before = await test.token(test.state["destination"])
    invoice = await test.invoice(5)
    address = test.methods(invoice)[0]["payment_address"]
    tx = await test.pay_usdt(address, units)
    done = await test.status(invoice, "complete", 60)
    same((Decimal(done["sent_amount"]), done["tx_hashes"]), (Decimal(5), [tx]), "sent amount and hash of the invoice")
    (deposit,) = await test.deposits(invoice)
    same((deposit["source"], deposit["tx_hash"], deposit["credited"]), ("event", tx, True), "deposit from the daemon event")
    fund, sweep = await test.swept(invoice, ["fund", "sweep"])
    same(await test.token(test.state["destination"]) - before, units, "the destination got the amount, once")
    same(await test.token(address), 0, "no USDT left on the deposit address")
    funding = await test.node("eth_getTransactionByHash", fund["tx_hash"])
    same((funding["from"].lower(), funding["to"].lower()), (test.state["fee_wallet"].lower(), address.lower()), "funding tx")
    moved = await test.node("eth_getTransactionByHash", sweep["tx_hash"])
    same((moved["from"].lower(), moved["to"].lower()), (address.lower(), test.state["usdt"].lower()), "sweep tx")
    return invoice


async def native_flow(test: Test) -> None:
    before = await test.native(test.state["native_destination"])
    invoice = await test.invoice(5)
    _, native = test.methods(invoice)
    wei = int(Decimal(native["amount"]) * ETH)
    same(wei, 25 * 10**14, "native amount of 5 USD at 2000 USD")
    await test.pay_native(native["payment_address"], wei)
    await test.status(invoice, "complete", 60)
    (sweep,) = await test.swept(invoice, ["sweep_native"])
    fee = int(sweep["gas_limit"]) * int(sweep["max_fee_wei"])
    same(await test.native(test.state["native_destination"]) - before, wei - fee, "native destination got balance - fee")
    same(await test.native(native["payment_address"]), 0, "nothing left on the address (priority fee = max fee)")


async def expiry_flow(test: Test) -> None:
    """Underpayment (part payment, invoice expires, the part is swept) and a payment after expiry (late)."""
    before = await test.token(test.state["destination"])
    first_event = await test.last_event()
    under = await test.invoice(5, expiration=1)
    unpaid = await test.invoice(5, expiration=1)
    under_address = test.methods(under)[0]["payment_address"]
    await test.pay_usdt(under_address, 3 * USDT)

    async def partial() -> Any:
        now = await test.api("GET", f"/invoices/{under['id']}")
        return (
            now["status"] == "pending" and Decimal(now["sent_amount"] or 0) == 3 and now["exception_status"] == "paid_partial"
        )

    await test.until("underpaid invoice: pending, sent 3, paid_partial", partial, 60)
    await test.status(under, "expired", 150)
    await test.status(unpaid, "expired", 30)
    same((await test.address(unpaid))["status"], "retired", "unpaid expired invoice: address retired")
    late_address = test.methods(unpaid)[0]["payment_address"]
    tx = await test.pay_usdt(late_address, 5 * USDT)

    async def late() -> Any:
        rows = await test.deposits(unpaid)
        return rows and rows[0]["late"] and rows[0]["tx_hash"] == tx

    await test.until("the payment after expiry is a late deposit", late, 60)
    check(len(await test.events("late_payment", first_event)) == 1, "one late_payment event")
    same(
        (await test.api("GET", f"/invoices/{unpaid['id']}"))["status"],
        "expired",
        "the late payment did not reopen the invoice",
    )
    await test.swept(under, ["fund", "sweep"])
    await test.swept(unpaid, ["fund", "sweep"])
    same(await test.token(test.state["destination"]) - before, 8 * USDT, "destination got the part payment and the late one")


async def two_at_once(test: Test) -> None:
    invoices = await asyncio.gather(test.invoice(5), test.invoice(7))
    addresses = [test.methods(invoice)[0]["payment_address"] for invoice in invoices]
    check(addresses[0] != addresses[1], "two invoices made at the same time: two addresses")
    # the payer is one account: its two payments one after the other (one nonce each), both invoices open
    await test.pay_usdt(addresses[0], 5 * USDT)
    await test.pay_usdt(addresses[1], 7 * USDT)
    for invoice in invoices:
        await test.status(invoice, "complete", 60)
    for invoice in invoices:
        await test.swept(invoice, ["fund", "sweep"])


async def restart_a(test: Test) -> None:
    invoice = await test.invoice(5)
    test.state["restart"] = invoice
    save(test.state)
    await asyncio.sleep(3)


async def restart_pay(test: Test) -> None:
    """While the daemon is stopped: the payment gives no event, ever (the wallets were diskless)."""
    address = test.methods(test.state["restart"])[0]["payment_address"]
    test.state["restart_tx"] = await test.pay_usdt(address, 5 * USDT)
    save(test.state)


async def restart_b(test: Test) -> None:
    invoice = test.state["restart"]
    before = await test.token(test.state["destination"])
    await test.status(invoice, "complete", 120)
    (deposit,) = await test.deposits(invoice)
    same(
        (deposit["source"], deposit["credited"]),
        ("balance", True),
        "credited by the reconcile (paid while the daemon was down)",
    )
    await asyncio.sleep(10)  # the worker's websocket is back; Bitcart ran check_pending
    # The address was loaded before the restart only. An event for a new payment proves the reload. (If the
    # reconcile counts it first, the event still gives that row its real hash: source "event" either way.)
    address = test.methods(invoice)[0]["payment_address"]
    tx = await test.pay_usdt(address, 1 * USDT)

    async def event() -> Any:
        return [row for row in await test.deposits(invoice) if row["tx_hash"] == tx and row["source"] == "event"]

    await test.until("a new payment to that address comes as an event: the wallets were loaded again", event, 60)
    await test.settled(invoice)
    same(await test.token(test.state["destination"]) - before, 6 * USDT, "the destination got both payments, once")


async def kill_a(test: Test) -> None:
    """The daemon cannot broadcast: the funding stays signed; then the script kills the worker (SIGKILL)."""
    await test.proxy(block_send=True)
    invoice = await test.invoice(5)
    await test.pay_usdt(test.methods(invoice)[0]["payment_address"], 5 * USDT)
    await test.status(invoice, "complete", 60)

    async def signed() -> Any:
        rows = await test.payouts(invoice)
        return rows[0] if rows and rows[0]["state"] == "signed" and rows[0]["raw_tx"] else None

    row = await test.until("the funding is signed and its broadcast failed", signed, 90)
    test.state["kill"] = {
        "invoice": invoice,
        "tx_hash": row["tx_hash"],
        "raw_tx": row["raw_tx"],
        "key": row["idempotency_key"],
    }
    save(test.state)


async def kill_b(test: Test) -> None:
    kill = test.state["kill"]
    before = await test.token(test.state["destination"])
    await test.proxy(block_send=False)
    fund, _ = await test.swept(kill["invoice"], ["fund", "sweep"])
    same(
        (fund["tx_hash"], fund["raw_tx"], fund["idempotency_key"]),
        (kill["tx_hash"], kill["raw_tx"], kill["key"]),
        "same bytes",
    )
    rows = await test.payouts(kill["invoice"])
    same([row["kind"] for row in rows], ["fund", "sweep"], "no second funding and no replacement")
    mined = await test.node("eth_getTransactionByHash", kill["tx_hash"])
    check(mined and mined["blockNumber"], "the funding that the dead worker signed is the one in the chain")
    same(await test.token(test.state["destination"]) - before, 5 * USDT, "the destination got the amount once")


async def second_opinion(test: Test) -> None:
    first_event = await test.last_event()
    for mode, kind in (("lie", "second_opinion_mismatch"), ("down", "second_opinion_down")):
        await test.proxy(second=mode)
        invoice = await test.invoice(5)
        await test.pay_usdt(test.methods(invoice)[0]["payment_address"], 5 * USDT)
        await test.status(invoice, "complete", 60)

        async def held(kind: str = kind) -> Any:
            return await test.events(kind, first_event)

        await test.until(f"second opinion {mode}: event {kind}", held, 60)
        await asyncio.sleep(6)
        rows = await test.payouts(invoice)
        same(
            [(row["kind"], row["state"], row["raw_tx"]) for row in rows],
            [("fund", "planned", None)],
            "no signature while it disagrees",
        )
        await test.proxy(second="ok")
        await test.swept(invoice, ["fund", "sweep"])


async def replace_flow(test: Test) -> None:
    """H1 on the real stack (the worker runs with replace_after 8 s): the native sweep stays in the mempool (no
    blocks), it is replaced, the second opinion is down so the replacement is not signed, then one block mines the
    original. The receipt finishes the nonce group: the replacement is never signed, no mismatch, no failure."""
    await worker_listens(test)  # the script started that worker just now
    first_event = await test.last_event()
    before = await test.native(test.state["native_destination"])
    invoice = await test.invoice(5)
    _, native = test.methods(invoice)
    wei = int(Decimal(native["amount"]) * ETH)
    await test.node("anvil_impersonateAccount", sender := "0x" + secrets.token_hex(20))
    await test.node("anvil_setBalance", sender, hex(ETH))
    await test.node("eth_sendTransaction", {"from": sender, "to": native["payment_address"], "value": hex(wei)})
    await test.status(invoice, "complete", 60)

    async def deep() -> Any:
        (deposit,) = await test.deposits(invoice)
        receipt = await test.node("eth_getTransactionReceipt", deposit["tx_hash"])
        return int(await test.node("eth_blockNumber"), 16) >= int(receipt["blockNumber"], 16) + 1

    await test.until("the payment has 2 confirmations", deep, 30)
    await test.node("evm_setIntervalMining", 0)  # no more blocks: the sweep stays in the mempool
    try:

        async def row_in(state: str) -> Any:
            rows = await test.payouts(invoice)
            return rows if rows and rows[0]["state"] == state else None

        await test.until("the native sweep is broadcast, not mined", lambda: row_in("broadcast"), 60)
        await test.proxy(second="down")

        async def waiting() -> Any:
            rows = await test.payouts(invoice)
            return len(rows) == 2 and rows[1]["state"] == "planned" and rows[1]["error"] == "second_opinion_down"

        await test.until("its replacement is planned and waits for the second opinion", waiting, 60)
        same((await test.payouts(invoice))[0]["state"], "broadcast", "the original stays broadcast")
        await test.node("evm_mine")  # the original is mined
        await test.proxy(second="ok")
    finally:
        await test.node("evm_setIntervalMining", 1)
    (sweep,) = await test.swept(invoice, ["sweep_native"])
    rows = await test.payouts(invoice)
    same([(row["state"], row["error"], row["raw_tx"] is None) for row in rows], [("confirmed", None, False),
         ("failed", "replaced", True)], "the original confirmed, the replacement never signed")  # fmt: skip
    fee = int(sweep["gas_limit"]) * int(sweep["max_fee_wei"])
    same(await test.native(test.state["native_destination"]) - before, wei - fee, "the destination got the sweep once")
    same(await test.events("second_opinion_mismatch", first_event), [], "no mismatch from the balance that moved")


async def reuse_flow(test: Test) -> None:
    """M6: after a restore of an older database, ready rows can be addresses that were given out since. One with a
    balance and one with a nonce are retired (event address_in_use); the money on the first is a late payment."""
    first_event = await test.last_event()
    ready = [row["address"] for row in await test.rows(
        "SELECT address FROM plugin_acctpool_addresses WHERE status = 'ready' ORDER BY id LIMIT 3")]  # fmt: skip
    paid, used, free = [await test.checksum(address) for address in ready]
    before = await test.native(test.state["native_destination"])
    await test.pay_native(paid, 10**15)  # a customer of a lost invoice paid here
    await test.send(used, used, 0)  # a nonce: the address sent a transaction
    await test.node("anvil_setBalance", used, "0x0")
    invoice = await test.invoice(5)
    same(test.methods(invoice)[0]["payment_address"].lower(), free.lower(), "the invoice got the next ready address")
    reused = await test.rows("SELECT address, status FROM plugin_acctpool_addresses WHERE address IN (:a, :b) ORDER BY id",
                             a=ready[0], b=ready[1])  # fmt: skip
    same([row["status"] for row in reused], ["retired", "retired"], "the used addresses are retired")
    events = {row["address"] for row in await test.events("address_in_use", first_event)}
    same(events, {ready[0], ready[1]}, "an address_in_use event for each")

    async def swept() -> Any:
        deposits = await test.rows("SELECT d.* FROM plugin_acctpool_deposits d JOIN plugin_acctpool_addresses a ON a.id ="
                                   " d.address_id WHERE a.address = :a", a=ready[0])  # fmt: skip
        payouts = await test.rows("SELECT p.state FROM plugin_acctpool_payouts p JOIN plugin_acctpool_addresses a ON"
                                  " a.id = p.address_id WHERE a.address = :a", a=ready[0])  # fmt: skip
        return deposits and deposits[0]["late"] and [row["state"] for row in payouts] == ["confirmed"]

    await test.until("the money on the retired address is a late payment and is swept", swept, 120)
    check(await test.native(test.state["native_destination"]) > before, "the native destination got it")


async def worker_listens(test: Test) -> float:
    """After a worker start: wait until it is subscribed to Bitcart's task channel. Stock Bitcart 0.10.3.0 (with or
    without this plugin): the backend asks the worker over Redis PUB/SUB (ExchangeRateService.add_contract, for every
    token payment method) and waits for the answer without a timeout. A message sent while no worker is subscribed is
    lost, and that invoice request never answers. This waits for that condition, not for a time."""
    start, redis = time.monotonic(), Redis(host=os.environ["REDIS_HOST"])
    try:
        while (await redis.pubsub_numsub("taskiq"))[0][1] < 1:
            if time.monotonic() - start > 120:
                raise Failed("the new worker does not listen on the task channel after 120 s")
            await asyncio.sleep(0.05)
    finally:
        await redis.aclose()
    seconds = time.monotonic() - start
    print(f"    ok   the new worker listens on Bitcart's task channel after {seconds:.2f} s", flush=True)  # noqa: T201
    return seconds


async def worker_restart(test: Test) -> None:
    """The script killed the worker and started a new one: at once (only Bitcart's own condition above), a pool
    invoice is made and paid; the new worker credits it and pays it out."""
    await worker_listens(test)
    start = time.monotonic()
    invoice = await test.invoice(5)
    made = time.monotonic() - start
    tx = await test.pay_usdt(test.methods(invoice)[0]["payment_address"], 5 * USDT)
    mined = time.monotonic() - start
    await test.status(invoice, "complete", 60)
    (deposit,) = await test.deposits(invoice)
    same((deposit["tx_hash"], deposit["credited"]), (tx, True), "the payment is credited")
    print(  # noqa: T201
        f"    timings: invoice answered {made:.2f} s, payment mined {mined:.2f} s, complete {time.monotonic() - start:.2f} s"
        f" after the worker listened; found by {deposit['source']}",
        flush=True,
    )
    await test.swept(invoice, ["fund", "sweep"])


async def stop_worker(test: Test) -> None:
    check(kill_worker(), "the worker processes are killed with SIGKILL")


async def summary(test: Test) -> None:
    rows = await test.rows("SELECT kind, count(*) AS n FROM plugin_acctpool_events GROUP BY kind ORDER BY kind")
    print("    events: " + ", ".join(f"{row['kind']}={row['n']}" for row in rows), flush=True)  # noqa: T201
    bad = await test.rows(
        "SELECT kind, detail FROM plugin_acctpool_events WHERE kind IN ('payout_failed', 'address_mismatch')"
    )
    same(bad, [], "no payout_failed or address_mismatch event")
    open_rows = await test.rows(
        "SELECT id, kind, state FROM plugin_acctpool_payouts WHERE state IN ('planned', 'signed', 'broadcast')"
    )
    same(open_rows, [], "no payout left open")
    dust = await test.rows(
        "SELECT id, amount FROM plugin_acctpool_deposits WHERE source = 'balance' AND asset = 'native' AND amount < 0.0001"
    )
    same(dust, [], "no native dust found by balance (the baseline of our own transactions is exact)")


STEPS = {
    "chain-setup": chain_setup,
    "fee-wallet": fee_wallet,
    "setup": setup,
    "usdt": usdt_flow,
    "native": native_flow,
    "expiry": expiry_flow,
    "two-at-once": two_at_once,
    "restart-a": restart_a,
    "restart-pay": restart_pay,
    "restart-b": restart_b,
    "kill-a": kill_a,
    "kill-b": kill_b,
    "second-opinion": second_opinion,
    "replace": replace_flow,
    "reuse": reuse_flow,
    "worker-listens": worker_listens,
    "worker-restart": worker_restart,
    "stop-worker": stop_worker,
    "summary": summary,
}


async def main(step: str) -> int:
    print(f"--- {step}", flush=True)  # noqa: T201
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        test = Test(session)
        try:
            await STEPS[step](test)
        except Failed as e:
            print(f"    FAIL {e}", flush=True)  # noqa: T201
            return 1
        finally:
            await test.db.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
