"""HTTP API and signing rules (SPEC 3.4, 7.6, 7.8, 8.3). No await between a journal read and the commit."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
import traceback
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from . import tx
from .audit import EXTRA_FIELDS, FIELDS, AuditLog
from .config import AMOUNT_RE, HARDENED_LIMIT, Config, is_address
from .errors import ApiError
from .hd import address_of
from .journal import Journal
from .keystore import Keystore

log = logging.getLogger("acctpool_signer")
MAX_BODY_BYTES = 16 * 1024
BODY_SECONDS = 10.0  # a body that does not come in this time after the headers is refused
KEY_RE = re.compile(r"[A-Za-z0-9:._-]{8,64}")  # fullmatch(): "$" with match() accepts a newline at the end
LIMITED = {"sign/fund": "sign", "sign/sweep": "sign", "sign/sweep_native": "sign", "derive": "derive"}


def _int(low: int, high: int) -> tuple[Callable[[Any], bool], str]:
    return lambda v: type(v) is int and low <= v <= high, f"must be an integer from {low} to {high}"


_AMOUNT = (lambda v: isinstance(v, str) and AMOUNT_RE.fullmatch(v) is not None and int(v) < tx.UINT256_LIMIT,
           "must be a string of decimal digits (base units), without a sign or leading zeros")  # fmt: skip
_KEY = (lambda v: isinstance(v, str) and KEY_RE.fullmatch(v) is not None,
        "must be a string of 8 to 64 characters from A-Z a-z 0-9 : . _ -")  # fmt: skip
_NAME = (lambda v: isinstance(v, str) and 1 <= len(v) <= 32, "must be a string of 1 to 32 characters")
# field -> (test, text of the refusal). Amounts are strings in the request and integers after validate().
CHECKS = {"idempotency_key": _KEY, "replaces": _KEY, "chain": _NAME, "store": _NAME, "family": _NAME,
          "index": _int(0, HARDENED_LIMIT - 1), "first_index": _int(0, HARDENED_LIMIT - 1), "count": _int(1, 200),
          "nonce": _int(0, 2**63 - 1), "gas_limit": _int(tx.FUND_GAS, 2**63 - 1), "max_fee_per_gas_wei": _AMOUNT,
          "max_priority_fee_per_gas_wei": _AMOUNT, "value_wei": _AMOUNT, "amount": _AMOUNT,
          "token": (is_address, "must be an EIP-55 checksummed address or null")}  # fmt: skip
OPTIONAL = ("replaces", "token")
COMMON = ("idempotency_key", "chain", "store", "index", "nonce", "max_fee_per_gas_wei", "max_priority_fee_per_gas_wei",
          "replaces")  # fmt: skip
SCHEMAS = {"derive": ("store", "family", "first_index", "count"), "fund": (*COMMON, "value_wei"),
           "sweep": (*COMMON, "gas_limit", "amount", "token"),
           "sweep_native": (*COMMON, "gas_limit", "value_wei")}  # fmt: skip


def validate(body: Any, kind: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError("invalid_request", "the body must be a JSON object")
    for name in body:
        if name not in SCHEMAS[kind]:
            shown = repr(name) if re.fullmatch(r"[A-Za-z0-9_]{1,32}", name) else "(name not shown)"
            raise ApiError("invalid_request", f"unknown field {shown}; the signer takes no other input")
    out: dict[str, Any] = {}
    for name in SCHEMAS[kind]:
        if name not in body and name not in OPTIONAL:
            raise ApiError("invalid_request", f"missing field {name!r}")
        value = out[name] = body.get(name)
        if not (value is None and name in OPTIONAL) and not CHECKS[name][0](value):
            raise ApiError("invalid_request", f"{name}: {CHECKS[name][1]}")
        if CHECKS[name] is _AMOUNT:
            out[name] = int(value)
    return out


def audit_fields(body: Any) -> dict[str, Any]:
    """The request values for the audit line: only a value that passes the check of its field."""
    items = body.items() if isinstance(body, dict) else ()
    fields = {"index" if n == "first_index" else n: v for n, v in items if n in CHECKS and v is not None and CHECKS[n][0](v)}
    return {name: value for name, value in fields.items() if name in FIELDS or name in EXTRA_FIELDS}


class RateLimit:
    """At most `limit` calls in 60 seconds; a refused call uses no place and only the first refusal of 60 s is audited."""

    def __init__(self, limit: int) -> None:
        self.limit, self.times, self.noted, self.unnoted = limit, deque(), None, 0

    def note(self, now: float) -> bool:
        if self.noted is not None and now - self.noted < 60:
            self.unnoted += 1
            return False
        self.noted = now
        return True

    def report(self, now: float, stop: bool = False) -> None:
        """The refusals without a line, on stderr: when the 60 s of the audited one are over (any call), or at the stop."""
        if self.unnoted and (stop or now - self.noted >= 60):
            log.warning("%d more rate_limited calls in the 60 s of the audited one, %d s ago", self.unnoted, now - self.noted)
            self.unnoted = 0

    def allow(self, now: float) -> bool:
        while self.times and now - self.times[0] >= 60:
            self.times.popleft()
        if len(self.times) >= self.limit:
            return False
        self.times.append(now)
        return True


@dataclass
class State:
    config: Config
    token: str = field(repr=False)
    keystore: Keystore | None = field(repr=False)
    journal: Journal
    audit: AuditLog
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic  # the limits use a clock that cannot go back
    limits: dict[str, RateLimit] = field(init=False)

    def __post_init__(self) -> None:
        c = self.config
        limits = {"sign": c.max_signatures_per_minute, "derive": c.max_derive_per_minute, "other": c.max_other_per_minute}
        self.limits = {name: RateLimit(limit) for name, limit in limits.items()}

    def close(self) -> None:
        self.audit.close()
        self.journal.close()


STATE: web.AppKey[State] = web.AppKey("state", State)
Result = tuple[dict[str, Any], dict[str, Any]]  # (response body, extra audit fields)


def do_status(state: State, kind: str, body: Any, now: datetime) -> Result:
    keystore, config = state.keystore, state.config
    return {
        "keystore": keystore is not None,
        "seed_id": keystore.active if keystore else None,
        "config_sha256": config.sha256,
        "fee_wallets": {"evm": keystore.wallet.fee_address} if keystore else {},
        "chains": {name: {"family": "evm", "chain_id": c.chain_id, "usdt": c.usdt} for name, c in config.chains.items()},
        "stores": {
            name: {
                "destinations": dict(s.destinations),
                "native_destinations": dict(s.native_destinations),
                "highest_index": state.journal.highest_index(name),
            }
            for name, s in config.stores.items()
        },
    }, {}


def _store(state: State, name: str) -> Any:
    if name not in state.config.stores:
        raise ApiError("unknown_store", "the store is not in the signer config")
    return state.config.stores[name]


def do_derive(state: State, kind: str, body: Any, now: datetime) -> Result:
    fields = validate(body, "derive")
    store = _store(state, fields["store"])
    if fields["family"] != "evm":
        raise ApiError("invalid_request", "family: must be 'evm'")
    first, highest = fields["first_index"], fields["first_index"] + fields["count"] - 1
    if highest >= HARDENED_LIMIT:
        raise ApiError("invalid_request", "first_index + count is over the index range")
    given = state.journal.highest_index(store.name)
    if first > (0 if given is None else given + 1):
        raise ApiError("invalid_request", "first_index: at most (highest index given out + 1); a call cannot make a gap")
    wallet = state.keystore.wallet  # type: ignore[union-attr]
    addresses = [{"index": i, "address": wallet.deposit_address(store.account, i)} for i in range(first, highest + 1)]
    state.journal.record_derived(store.name, highest)
    return {"seed_id": state.keystore.active, "addresses": addresses}, {}  # type: ignore[union-attr]


def _transaction(state: State, kind: str, fields: dict[str, Any]) -> tuple[Any, bytes, str, int, bytes, int]:
    """(chain, key, to, value, data, gas) of a request, after the per-transaction caps (the three shapes: README)."""
    if fields["chain"] not in state.config.chains:
        raise ApiError("unknown_chain", "the chain is not in the signer config")
    chain, store, wallet = state.config.chains[fields["chain"]], _store(state, fields["store"]), state.keystore.wallet  # type: ignore[union-attr]
    native = kind == "sweep_native"
    destination = (store.native_destinations if native else store.destinations).get(chain.name)
    if destination is None:  # also for a funding: no gas for an address that cannot be swept on this chain
        raise ApiError("unknown_store", f"the store has no {'native ' * native}destination on this chain in the signer config")
    highest = state.journal.highest_index(store.name)
    if highest is None or fields["index"] > highest:
        raise ApiError("invalid_request", "index: higher than the highest index given out by /v1/derive")
    gas, max_fee, value = fields.get("gas_limit", tx.FUND_GAS), fields["max_fee_per_gas_wei"], fields.get("value_wei", 0)
    cap, token, fee = "native_gas_limit_cap" if native else "gas_limit_cap", fields.get("token"), gas * max_fee
    share = chain.native_max_fee_share_percent
    refusals = (
        (gas > getattr(chain, cap), "cap_exceeded", f"gas_limit is over {cap}"),
        (max_fee > chain.max_fee_per_gas_cap_wei, "cap_exceeded", "max_fee_per_gas_wei is over max_fee_per_gas_cap_wei"),
        (fields["max_priority_fee_per_gas_wei"] > max_fee, "invalid_request", "max_priority_fee_per_gas_wei is over max_fee"),
        (kind == "fund" and value > chain.max_fund_value_wei, "cap_exceeded", "value_wei is over max_fund_value_wei"),
        (kind == "sweep" and fields.get("amount") == 0, "invalid_request", "amount: must be more than 0"),
        (native and value == 0, "invalid_request", "value_wei: must be more than 0"),
        (token not in (None, *chain.recovery_tokens), "token_not_allowed", "token: not in recovery_tokens of this chain"),
        # The fee of a native sweep goes to the block producer: without this rule a deposit could be signed away as fee.
        (native and fee * 100 > share * (value + fee), "cap_exceeded", "fee is over native_max_fee_share_percent"),
    )
    for refused, code, text in refusals:
        if refused:
            raise ApiError(code, text)
    if kind == "fund":
        return chain, wallet.fee_key(), wallet.deposit_address(store.account, fields["index"]), value, b"", gas
    key = wallet.deposit_key(store.account, fields["index"])
    if kind == "sweep":
        return chain, key, fields["token"] or chain.usdt, 0, tx.transfer_data(destination, fields["amount"]), gas
    return chain, key, destination, value, b"", gas


def _check_replacement(state: State, kind: str, fields: dict[str, Any], sender: str, group: list[Any]) -> None:
    """One signature per (chain, sender, nonce), all kinds, unless it names what it replaces."""
    if fields["replaces"] is None:
        if group:
            raise ApiError("idempotency_conflict", "this chain, sender and nonce is signed already; name it in 'replaces'")
        return
    old = state.journal.get_signature(fields["replaces"])
    if old is None:
        raise ApiError("invalid_request", "replaces: no signature has this key")
    before = {**json.loads(old["request"]), "kind": old["kind"], "sender": old["from_address"]}
    after = {**fields, "kind": kind, "sender": sender}
    # "kind" is first; a sweep can change its amount and a native sweep its value, a funding cannot
    for name in ("kind", "chain", "store", "index", "nonce", "sender", "token", *(("value_wei",) * (kind == "fund"))):
        if before.get(name) != after.get(name):
            raise ApiError("invalid_request", f"replaces: the replacement must have the same {name}")
    # a node replaces only with 10% more than the highest fee it has
    top, max_fee = max(int(r["max_fee_per_gas_wei"]) for r in group), fields["max_fee_per_gas_wei"]
    if max_fee * 10 < top * 11 or max_fee <= top:
        raise ApiError("invalid_request", "replaces: max_fee_per_gas_wei must be 10% above the highest one of the nonce")


def do_sign(state: State, kind: str, body: Any, now: datetime) -> Result:
    fields = validate(body, kind)
    digest = hashlib.sha256(json.dumps({"kind": kind, **fields}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    row = state.journal.get_signature(fields["idempotency_key"])
    if row is not None:  # same key + same request: the stored answer; same key + another request: 409
        if not hmac.compare_digest(row["request_hash"], digest):
            raise ApiError("idempotency_conflict", "this idempotency_key was used with a different request")
        stored = json.loads(row["response"])
        return stored, {"replay": True, "tx_hash": stored["tx_hash"]}
    chain, key, to, value, data, gas = _transaction(state, kind, fields)
    sender, max_fee, day = address_of(key), fields["max_fee_per_gas_wei"], now.strftime("%Y-%m-%d")
    group = state.journal.nonce_group(chain.name, sender, fields["nonce"])
    _check_replacement(state, kind, fields, sender, group)
    # A replacement excludes its original on chain: caps count only its difference to the highest one of its nonce.
    delta = max(0, value + gas * max_fee - max((int(r["value_wei"]) + int(r["fee_wei"]) for r in group), default=0))
    total = state.journal.fund_total(chain.name, to) + (0 if group else value)
    if kind == "fund" and total > chain.max_fund_total_per_address_wei:
        raise ApiError("cap_exceeded", "the funding total of this address is over max_fund_total_per_address_wei")
    if kind == "fund" and state.journal.daily_spend(chain.name, day) + delta > chain.fee_wallet_daily_cap_wei:
        raise ApiError("daily_cap_exceeded", "the fee wallet spend of this UTC day is over fee_wallet_daily_cap_wei")
    # fee budget (SPEC 7.8): a token sweep may pay its fee only from funded coin, never from a customer's native coin
    if kind == "sweep" and delta > state.journal.sweep_budget(chain.name, sender):
        raise ApiError("cap_exceeded", "the fees of the token sweeps of this address are over its signed funding")
    raw_tx, tx_hash = tx.sign(
        key, sender, chain.chain_id, fields["nonce"], to, value, data, gas, max_fee, fields["max_priority_fee_per_gas_wei"]
    )
    response = {"raw_tx": raw_tx, "tx_hash": tx_hash, "from": sender, "to": to, "chain_id": chain.chain_id}
    columns = (str(value), str(max_fee), str(gas * max_fee), str(delta if kind == "fund" else 0), day)
    request, answer = json.dumps(fields, separators=(",", ":")), json.dumps(response, separators=(",", ":"))
    state.journal.insert_signature(
        fields["idempotency_key"], digest, kind, chain.name, fields["nonce"], sender, to, *columns, request, answer
    )
    return response, {"tx_hash": tx_hash}


def _error(code: str, detail: str, status: int) -> web.Response:
    response = web.json_response({"error": code, "detail": detail}, status=status)
    response.force_close()  # no second request on the connection of a refused one (a body that did not come)
    return response


def _handler(call: str, action: Callable[[State, str, Any, datetime], Result]) -> Callable[..., Any]:
    async def handle(request: web.Request) -> web.Response:
        state, now, fields, noted = request.app[STATE], None, {}, True
        try:
            limit, moment = state.limits[LIMITED.get(call, "other")], state.monotonic()
            for each in state.limits.values():
                each.report(moment)
            if not limit.allow(moment):
                noted = limit.note(moment)
                raise ApiError("rate_limited", f"more than {limit.limit} '{LIMITED.get(call, 'other')}' calls in 60 seconds")
            body = None
            if request.method == "POST":
                try:
                    body = json.loads(await asyncio.wait_for(request.read(), BODY_SECONDS))
                except TimeoutError:
                    raise ApiError("invalid_request", f"the body did not come in {BODY_SECONDS:g} seconds") from None
                except (ValueError, RecursionError):
                    raise ApiError("invalid_request", "the body is not valid JSON") from None
            now, fields = state.now(), audit_fields(body)
            if call != "status" and state.keystore is None:
                raise ApiError("keystore_missing", "the signer has no keystore")
            with state.journal.transaction():
                response, extra = action(state, call.removeprefix("sign/"), body, now)
                if call != "status":  # a status call with the token writes no audit line (SPEC 7.6): the worker polls it
                    state.audit.write(call, "ok", at=now, **{**fields, **extra})
        except (web.HTTPException, sqlite3.Error):  # body too large; a journal that cannot be written (it would wait
            raise  # for the same lock): the request log line only
        except Exception as e:
            code, detail = (e.code, e.detail) if isinstance(e, ApiError) else ("internal", "internal error")
            if noted:
                state.audit.write(call, code, at=now, **fields, detail=detail)
            raise
        state.audit.flush()  # the line is in audit.log before the answer goes out
        return web.json_response(response)

    return handle


@web.middleware
async def guard(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
    """Token check first (constant time), errors as JSON, and one JSON log line per request (no body, no token)."""
    state, start = request.app[STATE], time.monotonic()
    try:
        scheme, _, sent = request.headers.get("Authorization", "").partition(" ")
        if not hmac.compare_digest(sent.encode("utf-8", "replace"), state.token.encode()) or scheme != "Bearer":
            raise ApiError("unauthorized", "missing or wrong bearer token")
        if "Content-Encoding" in request.headers:  # no decompression: 16 kB of gzip can be 16 MB
            raise ApiError("invalid_request", "a request with Content-Encoding is not accepted")
        response = await handler(request)
    except ApiError as e:
        response = _error(e.code, e.detail, e.status)
    except web.HTTPException as e:  # unknown path, wrong method, body too large
        status = e.status if 400 <= e.status < 500 else 500
        code = "not_found" if status == 404 else "invalid_request" if status < 500 else "internal"
        response = _error(code, "no such call" if status == 404 else f"HTTP {e.status}", status)
    except Exception as e:  # a crypto library's text can have key material: type and place only
        place = traceback.extract_tb(e.__traceback__)[-1]
        log.error("internal error in %s: %s at %s:%s", request.path[:64], type(e).__name__, place.filename, place.lineno)
        response = _error("internal", "internal error", 500)
    entry = {"method": request.method, "path": request.path[:64], "status": response.status, "remote": request.remote}
    log.info(json.dumps({**entry, "ms": round((time.monotonic() - start) * 1000)}))
    return response


def create_app(state: State) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY_BYTES)
    app[STATE] = state
    app.router.add_get("/v1/status", _handler("status", do_status), allow_head=False)
    app.router.add_post("/v1/derive", _handler("derive", do_derive))
    for kind in ("fund", "sweep", "sweep_native"):
        app.router.add_post(f"/v1/sign/{kind}", _handler(f"sign/{kind}", do_sign))
    return app
