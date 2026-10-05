"""No key material outside the process (SPEC 0).

A real signer process gets many requests, good and bad. Then everything that it wrote is searched:
stdout, stderr, the audit log, the journal database (file bytes and every value of every row),
every HTTP response (status, headers, body), and the keystore (for plaintext).
"""

import base64
import json
import os
import sqlite3
from pathlib import Path

import helpers
import pytest
from conftest import ACCOUNTS, RECOVERY_TOKEN, fund_request, sweep_native_request, sweep_request
from procs import http, run_cli, run_in_terminal, running

from acctpool_signer import api as api_mod
from acctpool_signer import tx as tx_mod

INDEXES = 12
# value of each funding: the budget for the fees of the token sweeps of that address (SPEC 7.8)
BUDGET = str(2 * 10**16)


def encodings(secret: bytes) -> list[bytes]:
    number = int.from_bytes(secret, "big")
    return [
        secret,
        secret.hex().encode(),
        secret.hex().upper().encode(),
        base64.b64encode(secret),
        base64.urlsafe_b64encode(secret),
        str(number).encode(),
        f"{number:x}".encode(),  # hex without leading zeros
        repr(secret).encode(),
        secret[::-1].hex().encode(),
    ]


def needles(env) -> dict[str, list[bytes]]:
    found: dict[str, list[bytes]] = {
        "entropy": encodings(env.entropy),
        "bip39 seed": encodings(env.seed) + encodings(env.seed[:32]) + encodings(env.seed[32:]),
        "master key": encodings(env.master_key),
        "fee wallet key": encodings(env.fee_key),
    }
    for store in ACCOUNTS:
        for index in range(INDEXES):
            found[f"key of {store}/{index}"] = encodings(env.key(store, index))
    # the keys of the levels above the address keys (m/44', m/44'/60', ...)
    for account in (*ACCOUNTS.values(), 9000):
        for depth in range(1, 5):
            path = helpers.deposit_path(account, 0)[:depth]
            found[f"key of level {depth}, account {account}"] = encodings(helpers.bip32_derive(env.seed, path))
    words = env.words.split()
    phrases = []
    for separator in (" ", ",", ", ", '","', '", "', "\n", "\\n", "-", "_", "+", "%20", ""):
        phrases += [separator.join(words[i : i + 3]).encode() for i in range(len(words) - 2)]
    found["seed words"] = phrases + [p.upper() for p in phrases]
    return found


def search(name: str, haystack: bytes, env, extra: dict[str, list[bytes]] | None = None) -> None:
    assert haystack, f"{name} is empty: the search would prove nothing"
    for what, values in {**needles(env), **(extra or {})}.items():
        for value in values:
            assert value not in haystack, f"{what} is in {name}"


def database_values(path: str) -> bytes:
    db = sqlite3.connect(path)
    out = []
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert set(tables) == {"meta", "derived", "signatures"}
    for table in tables:
        for row in db.execute(f"SELECT * FROM {table}"):
            out += [value if isinstance(value, bytes) else str(value).encode() for value in row]
    db.close()
    return b"\n".join(out)


def requests(env) -> list[tuple[str | None, str, str, object]]:
    """(token, method, path, body): every call, every kind of refusal, and some attacks."""
    wrong = "wrong-" + os.urandom(20).hex()
    calls: list[tuple[str | None, str, str, object]] = [
        (env.token, "GET", "/v1/status", None),
        (wrong, "GET", "/v1/status", None),
        (None, "POST", "/v1/sign/fund", fund_request("secret-fund-0")),
        (env.token, "GET", "/v1/export", None),
        (env.token, "GET", "/v1/keys", None),
        (env.token, "POST", "/v1/sign/raw", {"data": "0x00"}),
        (env.token, "POST", "/v1/status", None),
    ]
    for store in ACCOUNTS:
        calls.append((env.token, "POST", "/v1/derive", {"store": store, "family": "evm", "first_index": 0, "count": INDEXES}))
    calls.append((env.token, "POST", "/v1/derive", {"store": "shop", "family": "evm", "first_index": 0, "count": 0}))
    calls.append(
        (env.token, "POST", "/v1/derive", {"store": "shop", "family": "evm", "first_index": 0, "count": 1, "private": True})
    )
    nonce = 0
    for store in ACCOUNTS:
        for index in range(INDEXES):
            key = f"secret-{store}-{index}"
            calls.append(
                (
                    env.token,
                    "POST",
                    "/v1/sign/fund",
                    fund_request(f"{key}-fund", store=store, index=index, nonce=nonce, value_wei=BUDGET),
                )
            )
            calls.append((env.token, "POST", "/v1/sign/sweep", sweep_request(f"{key}-sweep", store=store, index=index)))
            native = sweep_native_request(f"{key}-native", store=store, index=index, nonce=5)
            calls.append((env.token, "POST", "/v1/sign/sweep_native", native))
            nonce += 1
    bump = str(200 * 10**9)
    calls += [
        (env.token, "POST", "/v1/sign/fund", fund_request("secret-shop-0-fund")),  # other request, same key
        # a replay
        (env.token, "POST", "/v1/sign/fund", fund_request("secret-shop-1-fund", index=1, nonce=1, value_wei=BUDGET)),
        (
            env.token,
            "POST",
            "/v1/sign/fund",
            fund_request(
                "secret-repl-1", index=0, nonce=0, value_wei=BUDGET, max_fee_per_gas_wei=bump, replaces="secret-shop-0-fund"
            ),
        ),
        (
            env.token,
            "POST",
            "/v1/sign/sweep",
            sweep_request("secret-repl-2", index=0, max_fee_per_gas_wei=bump, amount="5", replaces="secret-shop-0-sweep"),
        ),
        (env.token, "POST", "/v1/sign/sweep", sweep_request("secret-token-1", index=1, nonce=1, token=RECOVERY_TOKEN)),
        (env.token, "POST", "/v1/sign/sweep", sweep_request("secret-token-2", index=1, nonce=2, token="0x" + "11" * 20)),
        (env.token, "POST", "/v1/sign/fund", fund_request("secret-cap-1", nonce=100, value_wei=str(10**19))),
        (env.token, "POST", "/v1/sign/fund", fund_request("secret-cap-2", nonce=100, max_fee_per_gas_wei=str(10**15))),
        (env.token, "POST", "/v1/sign/fund", fund_request("secret-index-1", nonce=100, index=5000)),
        (env.token, "POST", "/v1/sign/fund", fund_request("secret-chain-1", nonce=100, chain="ethereum")),
        (env.token, "POST", "/v1/sign/fund", fund_request("secret-store-1", nonce=100, store="nostore")),
        (env.token, "POST", "/v1/sign/sweep", sweep_request("secret-zero-1", nonce=9, amount="0")),
        (env.token, "POST", "/v1/sign/sweep", sweep_request("secret-to-1", nonce=9, to="0x" + "ee" * 20)),
        (env.token, "POST", "/v1/sign/sweep", sweep_request("secret-path-1", nonce=9, path="m/44'/60'/9000'/0'/0'")),
        (env.token, "POST", "/v1/sign/sweep", sweep_request("secret-type-1", nonce="9", index=None)),
        (env.token, "POST", "/v1/sign/sweep", ["not", "an", "object"]),
        (env.token, "POST", "/v1/sign/sweep_native", sweep_native_request("secret-native-1", nonce=9, value_wei="1")),
        (env.token, "POST", "/v1/sign/sweep_native", sweep_native_request("secret-native-2", nonce=9, to="0x" + "ee" * 20)),
        (env.token, "POST", "/v1/sign/sweep_native", sweep_native_request("secret-native-3", nonce=9, chain="polygon")),
        (env.token, "POST", "/v1/sign/sweep_native", sweep_native_request("secret-native-4", nonce=0)),  # nonce of a sweep
    ]
    return calls


def test_nothing_that_the_signer_writes_has_key_material(env):
    responses = []
    sent_tokens = set()
    signed = 0
    with running(env) as (port, _):
        for token, method, path, body in requests(env):
            status, text, headers = http(port, token, method, path, body)
            responses.append(f"{status}\n{headers}\n{text}")
            if token:
                sent_tokens.add(token)
            if status == 200 and "raw_tx" in text:
                signed += 1
                # the test must look for the key that really signed: recover the signer, compare with the test's key
                decoded = helpers.decode_tx(json.loads(text)["raw_tx"])
                if path.endswith("fund"):
                    assert decoded["signer"] == env.fee_address
                else:
                    assert decoded["signer"] == env.address(body["store"], body["index"])
    statuses = sorted({r.split("\n", 1)[0] for r in responses})
    assert statuses == ["200", "400", "401", "403", "404", "405", "409"]
    assert signed >= 6 * INDEXES

    done = run_cli(env, "audit-verify")
    assert done.returncode == 0, done.stderr
    verify = run_cli(env, "verify")
    assert verify.returncode == 0

    tokens = {"bearer token": [t.encode() for t in sent_tokens]}
    data = Path(env.paths.data)
    assert sorted(os.listdir(data)) == ["audit.log", "journal.lock", "journal.sqlite3", "keystore.json"]
    search("stdout", (env.root / "stdout.txt").read_bytes(), env, tokens)
    search("stderr", (env.root / "stderr.txt").read_bytes(), env, tokens)
    search("audit.log", (data / "audit.log").read_bytes(), env, tokens)
    search("journal file", (data / "journal.sqlite3").read_bytes(), env, tokens)
    search("journal rows", database_values(env.paths.journal), env, tokens)
    search("HTTP responses", "\n".join(responses).encode(), env, tokens)
    search("keystore.json", (data / "keystore.json").read_bytes(), env, tokens)
    search(
        "output of audit-verify and verify", (done.stdout + done.stderr + verify.stdout + verify.stderr).encode(), env, tokens
    )
    # the audit log has many lines: the search above was not a search in an empty file
    # lines: the start and the stop, and one for each request but the first 7: the status call with the token, the two
    # calls without the token, the three unknown paths and the wrong method (the request log line only)
    lines = [json.loads(line) for line in (data / "audit.log").read_bytes().splitlines()]
    assert len(lines) > 80
    assert len([line for line in lines if line["call"] not in ("start", "stop")]) == len(requests(env)) - 7


def test_restore_writes_and_shows_no_key_material(env_no_keystore):
    """The output of restore on the terminal: the words are typed without echo, and only public values are shown."""
    env = env_no_keystore
    code, shown = run_in_terminal(env, "restore", typed=env.words)
    assert code == 0, shown
    assert f"fee wallet (evm): {env.fee_address}" in shown
    search("terminal of restore", shown.encode(), env)
    search("keystore.json", Path(env.paths.keystore).read_bytes(), env)


def test_init_writes_no_key_material_but_the_keystore(env_no_keystore):
    """The command init: what it shows on the terminal is the seed. All other places are searched."""
    import helpers
    from mnemonic import Mnemonic
    from procs import run_in_terminal
    from test_cli import type_back, words_in

    env = env_no_keystore
    code, shown = run_in_terminal(env, "init", answer=type_back)
    assert code == 0
    env.words = " ".join(words_in(shown))
    env.entropy = bytes(Mnemonic("english").to_entropy(env.words))
    env.seed = helpers.bip39_seed(env.words)
    assert os.listdir(env.paths.data) == ["keystore.json"]
    search("keystore.json", Path(env.paths.keystore).read_bytes(), env)
    # the start after it, and the files of the service
    with running(env) as (port, _):
        status, text, _ = http(port, env.token, "GET", "/v1/status")
        assert json.loads(text)["keystore"] is True
    for name in os.listdir(env.paths.data):
        search(name, (Path(env.paths.data) / name).read_bytes() + b"x", env)
    search("stdout and stderr", (env.root / "stdout.txt").read_bytes() + (env.root / "stderr.txt").read_bytes(), env)


def test_search_finds_a_secret_when_there_is_one(env):
    """The test of the test: every kind of leak must make the search fail."""
    leaks = [
        env.entropy.hex(),
        env.entropy.hex().upper(),
        "0x" + env.fee_key.hex(),
        env.key("shop", 3).hex(),
        env.key("other", 11).hex(),
        str(int.from_bytes(env.key("shop", 0), "big")),
        base64.b64encode(env.master_key).decode(),
        env.seed.hex(),
        env.words,
        json.dumps(env.words.split()),
        env.words.replace(" ", "\n"),
        " ".join(env.words.split()[5:9]),
    ]
    for leak in leaks:
        with pytest.raises(AssertionError):
            search("test", f'{{"detail": "error with {leak} in it"}}'.encode(), env)
    with pytest.raises(AssertionError):
        search("test", b"\x00\x01" + env.key("shop", 2) + b"\x02", env)
    search("test", b'{"detail": "no secret here", "index": 3}', env)


async def test_text_of_an_internal_error_is_not_shown(api, env, monkeypatch, caplog, capsys):
    """An exception of a library can have a key in its text. The signer shows and logs the type only."""
    caplog.set_level(0)

    def broken(key, *args):
        raise ValueError(f"cannot sign with key {key.hex()} and seed words {env.words}")

    monkeypatch.setattr(tx_mod, "sign", broken)
    assert api_mod.tx is tx_mod
    api.prefund("anvil", "shop", [3])  # the sweep must come to the signature: it needs a funding in the journal
    rows = api.journal_rows()
    for call, request in (
        ("/v1/sign/fund", fund_request("secret-error-1")),
        ("/v1/sign/sweep", sweep_request("secret-error-2")),
    ):
        status, result = await api.post(call, request)
        assert (status, result) == (500, {"error": "internal", "detail": "internal error"})
    assert api.journal_rows() == rows
    assert [line["result"] for line in api.audit_lines()[-2:]] == ["internal", "internal"]

    captured = capsys.readouterr()
    logged = "\n".join(f"{r.getMessage()} {r.exc_info} {r.exc_text} {r.args}" for r in caplog.records)
    assert "ValueError" in logged
    assert "internal" in captured.out  # the audit lines
    for name, text in {
        "log": logged,
        "stdout and stderr": captured.out + captured.err,
        "responses": "\n".join(api.responses),
    }.items():
        search(name, text.encode(), env)
    search("audit.log", Path(env.paths.audit).read_bytes(), env)


async def test_in_process_responses_and_logs_have_no_key_material(api, env, caplog, capsys):
    """The same search for the in-process client, with all loggers at the lowest level (aiohttp, asyncio, eth)."""
    caplog.set_level(0)
    for token, method, path, body in requests(env):
        await api.call(method, path, body, token=token if token != env.token else "default")
    assert len(api.transactions) >= 6 * INDEXES
    captured = capsys.readouterr()
    logged = "\n".join(f"{r.name} {r.getMessage()} {r.args}" for r in caplog.records)
    search("responses", "\n".join(api.responses).encode(), env, {"bearer token": [env.token.encode()]})
    search("stdout", captured.out.encode(), env, {"bearer token": [env.token.encode()]})
    search("log and stderr", (logged + captured.err + "x").encode(), env, {"bearer token": [env.token.encode()]})
