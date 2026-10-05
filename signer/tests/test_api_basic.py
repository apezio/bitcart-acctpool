"""Authentication, status, derive, unknown paths, no keystore."""

import hashlib
import json
import logging

import pytest
from conftest import (
    DEST_OTHER_ANVIL,
    DEST_SHOP_ANVIL,
    DEST_SHOP_BNB,
    DEST_SHOP_POLYGON,
    NATIVE_OTHER_ANVIL,
    NATIVE_OTHER_POLYGON,
    NATIVE_SHOP_ANVIL,
    NATIVE_SHOP_BNB,
    USDT_ANVIL,
    USDT_BNB,
    USDT_POLYGON,
    check_audit,
    fund_request,
    make_api,
    sweep_native_request,
    sweep_request,
)

from acctpool_signer import api as api_mod

CALLS = [
    ("GET", "/v1/status", None),
    ("POST", "/v1/derive", {"store": "shop", "family": "evm", "first_index": 0, "count": 1}),
    ("POST", "/v1/sign/fund", fund_request("auth-fund-1")),
    ("POST", "/v1/sign/sweep", sweep_request("auth-sweep-1")),
    ("POST", "/v1/sign/sweep_native", sweep_native_request("auth-native-1")),
    ("GET", "/v1/nothing", None),
]


async def test_status(api, env):
    status, body = await api.get("/v1/status")
    assert status == 200
    assert body == {
        "keystore": True,
        "seed_id": hashlib.sha256(env.fee_address.encode()).hexdigest()[:16],
        "config_sha256": hashlib.sha256(open(env.paths.config, "rb").read()).hexdigest(),
        "fee_wallets": {"evm": env.fee_address},
        "chains": {
            "anvil": {"family": "evm", "chain_id": 31337, "usdt": USDT_ANVIL},
            "polygon": {"family": "evm", "chain_id": 137, "usdt": USDT_POLYGON},
            "bnb": {"family": "evm", "chain_id": 56, "usdt": USDT_BNB},
        },
        "stores": {
            "shop": {
                "destinations": {"anvil": DEST_SHOP_ANVIL, "polygon": DEST_SHOP_POLYGON, "bnb": DEST_SHOP_BNB},
                "native_destinations": {"anvil": NATIVE_SHOP_ANVIL, "bnb": NATIVE_SHOP_BNB},
                "highest_index": 19,
            },
            "other": {
                "destinations": {"anvil": DEST_OTHER_ANVIL},
                "native_destinations": {"anvil": NATIVE_OTHER_ANVIL, "polygon": NATIVE_OTHER_POLYGON},
                "highest_index": None,
            },
        },
    }


async def test_status_has_the_highest_index_given_out(api, env):
    """The backend derives ahead from max(its own highest index, this one) + 1: a database restored from an older
    backup must not give out an index again."""
    assert (await api.derive("shop", 20, 15))[0] == 200
    assert (await api.derive("other", 0, 3))[0] == 200
    _, body = await api.get("/v1/status")
    assert (body["stores"]["shop"]["highest_index"], body["stores"]["other"]["highest_index"]) == (34, 2)


async def test_status_with_the_token_writes_no_audit_line(api, env):
    lines = len(api.audit_lines())
    for _ in range(5):
        status, _ = await api.get("/v1/status")
        assert status == 200
    assert len(api.audit_lines()) == lines


@pytest.mark.parametrize(("method", "path", "body"), CALLS)
async def test_missing_or_wrong_token_is_401(api, env, method, path, body):
    rows = api.journal_rows()
    wrong_tokens = [
        None,
        "",
        "wrong-token-wrong-token-wrong-token-xx",
        env.token[:-1],
        env.token + "0",
        env.token.upper(),
        " " + env.token,
        env.token + " ",
    ]
    for token in wrong_tokens:
        status, result = await api.call(method, path, body, token=token)
        assert (status, result["error"]) == (401, "unauthorized"), token
    # a right token with a wrong scheme is refused too
    for header in (env.token, f"Basic {env.token}", f"bearer {env.token}", f"Bearer  {env.token}", f"Token {env.token}"):
        response = await api.client.request(method, path, json=body, headers={"Authorization": header})
        assert response.status == 401, header
        assert (await response.json())["error"] == "unauthorized"
    assert api.journal_rows() == rows
    assert api.transactions == []


async def test_failed_authentication_is_logged_without_the_token_and_writes_no_audit_line(api, env, caplog):
    """SPEC-v4 3: a call without the token gets the request log line only, so such calls cannot fill the volume."""
    caplog.set_level(logging.INFO, logger="acctpool_signer")
    sent = "guess-" + "a1b2c3d4" * 5
    before = open(env.paths.audit).read()
    await api.post("/v1/sign/fund", fund_request("auth-fund-2"), token=sent)
    for number in range(1000):
        status, result = await api.get("/v1/status", token=None if number % 2 else f"wrong-token-{number:030d}")
        assert (status, result["error"]) == (401, "unauthorized")
    assert open(env.paths.audit).read() == before
    assert check_audit(env) == len(api.audit_lines())
    logged = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    assert logged[0] == {
        "method": "POST",
        "path": "/v1/sign/fund",
        "status": 401,
        "remote": "127.0.0.1",
        "ms": logged[0]["ms"],
    }
    assert len(logged) == 1001
    # the token that was sent and the body of a request without authentication (it is not read) are not in the log
    assert sent not in caplog.text and env.token not in caplog.text and "auth-fund-2" not in caplog.text


async def test_token_compare_uses_compare_digest(api, env, monkeypatch):
    """The compare function of the standard library for secrets is used, with the token of the file as one side.

    This test does not measure time.
    """
    seen = []
    real = api_mod.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(api_mod.hmac, "compare_digest", spy)
    for token in ("x" * 40, None, env.token):
        await api.get("/v1/status", token=token)
    assert len(seen) == 3
    assert all(b == env.token.encode() for _, b in seen)


UNKNOWN = [
    ("GET", "/"),
    ("GET", "/v1"),
    ("GET", "/v1/"),
    ("GET", "/v1/status/"),
    ("GET", "/v2/status"),
    ("GET", "/status"),
    ("GET", "/v1/export"),
    ("POST", "/v1/export"),
    ("GET", "/v1/keys"),
    ("GET", "/v1/seed"),
    ("GET", "/v1/config"),
    ("POST", "/v1/config"),
    ("POST", "/v1/sign"),
    ("POST", "/v1/sign/raw"),
    ("POST", "/v1/sign/message"),
    ("POST", "/v1/sign/transaction"),
    ("POST", "/v1/sign/fund/"),
    ("POST", "/v1/sign/sweep/raw"),
    ("POST", "/v1/sign/sweep_native/"),
    ("POST", "/v1/sign/sweep-native"),
    ("POST", "/v1/sign/native"),
    ("POST", "/v1/sign/transfer"),
    ("POST", "/v1/broadcast"),
    ("GET", "/debug"),
    ("GET", "/metrics"),
    ("GET", "/health"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("GET", "/v1/journal"),
    ("GET", "/v1/audit"),
    ("GET", "/audit.log"),
    ("GET", "/keystore.json"),
    ("GET", "/../data/keystore.json"),
]


@pytest.mark.parametrize(("method", "path"), UNKNOWN)
async def test_unknown_paths_are_404(api, method, path):
    status, result = await api.call(method, path, {"x": 1} if method == "POST" else None)
    assert (status, result["error"]) == (404, "not_found")
    assert len(api.audit_lines()) == 1  # the request log line only (the line of the fixture)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/status"),
        ("PUT", "/v1/status"),
        ("DELETE", "/v1/status"),
        ("GET", "/v1/derive"),
        ("GET", "/v1/sign/fund"),
        ("GET", "/v1/sign/sweep"),
        ("GET", "/v1/sign/sweep_native"),
        ("PUT", "/v1/sign/fund"),
        ("PATCH", "/v1/sign/sweep"),
        ("DELETE", "/v1/sign/sweep"),
    ],
)
async def test_wrong_method_is_refused(api, method, path):
    rows = api.journal_rows()
    status, result = await api.call(method, path, fund_request("method-test-1") if method != "GET" else None)
    assert status == 405
    assert result["error"] == "invalid_request"
    assert api.journal_rows() == rows


def test_the_api_has_five_routes_and_no_more(env):
    from acctpool_signer.__main__ import build_state

    app = api_mod.create_app(build_state(env.paths))
    routes = sorted((route.method, route.resource.canonical) for route in app.router.routes())
    assert routes == [
        ("GET", "/v1/status"),
        ("POST", "/v1/derive"),
        ("POST", "/v1/sign/fund"),
        ("POST", "/v1/sign/sweep"),
        ("POST", "/v1/sign/sweep_native"),
    ]


async def test_derive(api, env):
    status, body = await api.derive("shop", 0, 50)
    assert status == 200
    assert body["seed_id"] == hashlib.sha256(env.fee_address.encode()).hexdigest()[:16]
    assert body["addresses"] == [{"index": i, "address": env.address("shop", i)} for i in range(50)]
    status, body = await api.derive("other", 0, 10)
    assert status == 200
    assert body["addresses"] == [{"index": i, "address": env.address("other", i)} for i in range(10)]
    # a second call for a part of the range that was given out (the daily compare job of the plugin)
    status, body = await api.derive("other", 7, 3)
    assert status == 200
    assert body["addresses"] == [{"index": i, "address": env.address("other", i)} for i in (7, 8, 9)]
    addresses = {a["address"] for a in body["addresses"]}
    assert env.fee_address not in addresses
    assert len(addresses) == 3


async def test_derive_count_limits(api, env):
    status, body = await api.derive("shop", 20, 200)
    assert status == 200
    assert len(body["addresses"]) == 200
    assert body["addresses"][-1] == {"index": 219, "address": env.address("shop", 219)}
    status, body = await api.derive("shop", 220, 201)
    assert (status, body["error"]) == (400, "invalid_request")


async def test_derive_cannot_make_a_gap(api, env):
    journal = api.state.journal
    assert journal.highest_index("shop") == 19  # the fixture gave out 0..19

    for first in (21, 22, 100, 2**31 - 200, 2**31 - 1):
        status, body = await api.derive("shop", first, 1)
        assert (status, body["error"]) == (400, "invalid_request"), first
        assert "gap" in body["detail"]
        assert "addresses" not in body
        assert api.audit_lines()[-1]["result"] == "invalid_request"
    assert journal.highest_index("shop") == 19

    # a store without a call: only first_index 0 is possible
    for first in (1, 5):
        status, body = await api.derive("other", first, 1)
        assert (status, body["error"]) == (400, "invalid_request")
    assert journal.highest_index("other") is None
    status, body = await api.derive("other", 0, 1)
    assert status == 200

    # highest + 1 is the next address; each call can add 200 at most
    status, body = await api.derive("shop", 20, 200)
    assert status == 200
    assert [a["index"] for a in body["addresses"]] == list(range(20, 220))
    assert journal.highest_index("shop") == 219
    status, body = await api.derive("shop", 221, 1)
    assert (status, body["error"]) == (400, "invalid_request")
    # a range that starts inside and ends outside is no gap
    status, body = await api.derive("shop", 210, 20)
    assert status == 200
    assert journal.highest_index("shop") == 229


async def test_journal_has_the_highest_index_given_out(api, env):
    journal = api.state.journal
    assert journal.highest_index("shop") == 19  # from the fixture
    assert journal.highest_index("other") is None
    await api.derive("shop", 20, 15)
    assert journal.highest_index("shop") == 34
    await api.derive("shop", 0, 3)
    assert journal.highest_index("shop") == 34  # never goes down
    await api.derive("other", 0, 1)
    assert journal.highest_index("other") == 0
    assert journal.highest_index("shop") == 34


DERIVE_OK = {"store": "shop", "family": "evm", "first_index": 0, "count": 1}
BAD_DERIVE = {
    "count 0": ({**DERIVE_OK, "count": 0}, "invalid_request"),
    "count 201": ({**DERIVE_OK, "count": 201}, "invalid_request"),
    "count negative": ({**DERIVE_OK, "count": -1}, "invalid_request"),
    "count is a string": ({**DERIVE_OK, "count": "5"}, "invalid_request"),
    "count is a float": ({**DERIVE_OK, "count": 5.0}, "invalid_request"),
    "count is true": ({**DERIVE_OK, "count": True}, "invalid_request"),
    "first_index negative": ({**DERIVE_OK, "first_index": -1}, "invalid_request"),
    "first_index 2^31": ({**DERIVE_OK, "first_index": 2**31}, "invalid_request"),
    "range over 2^31": ({**DERIVE_OK, "first_index": 2**31 - 1, "count": 2}, "invalid_request"),
    "first_index is a string": ({**DERIVE_OK, "first_index": "0"}, "invalid_request"),
    "family tron": ({**DERIVE_OK, "family": "tron"}, "invalid_request"),
    "family missing": ({k: v for k, v in DERIVE_OK.items() if k != "family"}, "invalid_request"),
    "store missing": ({k: v for k, v in DERIVE_OK.items() if k != "store"}, "invalid_request"),
    "store unknown": ({**DERIVE_OK, "store": "nostore"}, "unknown_store"),
    "store is a number": ({**DERIVE_OK, "store": 1}, "invalid_request"),
    "store is empty": ({**DERIVE_OK, "store": ""}, "invalid_request"),
    "store name too long": ({**DERIVE_OK, "store": "s" * 33}, "invalid_request"),
    "field account": ({**DERIVE_OK, "account": 9000}, "invalid_request"),
    "field path": ({**DERIVE_OK, "path": "m/44'/60'/9000'/0'/0'"}, "invalid_request"),
    "field private": ({**DERIVE_OK, "private": True}, "invalid_request"),
    "field seed_id": ({**DERIVE_OK, "seed_id": "0" * 16}, "invalid_request"),
    "body is a list": ([DERIVE_OK], "invalid_request"),
    "body is a string": ("derive", "invalid_request"),
    "body is null": (None, "invalid_request"),
}


@pytest.mark.parametrize("case", sorted(BAD_DERIVE))
async def test_bad_derive_is_refused(api, case):
    body, code = BAD_DERIVE[case]
    raw = json.dumps(body).encode()
    status, result = await api.post("/v1/derive", raw=raw)
    assert (status, result["error"]) == (400, code)
    assert "addresses" not in result
    assert api.audit_lines()[-1]["result"] == code


@pytest.mark.parametrize("path", ["/v1/derive", "/v1/sign/fund", "/v1/sign/sweep", "/v1/sign/sweep_native"])
async def test_body_that_is_not_json_is_refused(api, path):
    for raw in (b"", b"{", b"store=shop", b'{"store": "shop",}', b"\xff\xfe", b"[" * 5000, b'{"count": ' + b"9" * 5000 + b"}"):
        status, result = await api.post(path, raw=raw)
        assert (status, result["error"]) == (400, "invalid_request"), raw[:20]
    status, result = await api.post(path, raw=b'{"pad": "' + b"x" * 20000 + b'"}')
    assert (status, result["error"]) == (413, "invalid_request")
    assert api.journal_rows() == 0


async def test_without_keystore_only_status_works(env_no_keystore, aiohttp_client):
    env = env_no_keystore
    api = await make_api(env, aiohttp_client)
    status, body = await api.get("/v1/status")
    assert status == 200
    assert body["keystore"] is False
    assert body["seed_id"] is None
    assert body["fee_wallets"] == {}
    assert body["config_sha256"] == hashlib.sha256(open(env.paths.config, "rb").read()).hexdigest()
    assert set(body["chains"]) == {"anvil", "polygon", "bnb"}
    for method, path, request in CALLS[1:5]:
        status, result = await api.call(method, path, request)
        assert (status, result["error"]) == (503, "keystore_missing")
        status, result = await api.call(method, path, request, token="wrong-token-wrong-token-wrong-token-xx")
        assert (status, result["error"]) == (401, "unauthorized")
    status, result = await api.get("/v1/export")
    assert status == 404
    assert api.journal_rows() == 0
    assert [line["result"] for line in api.audit_lines()].count("keystore_missing") == 4
