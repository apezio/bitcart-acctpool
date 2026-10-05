"""The signer as a real process against a caller WITHOUT the token (review A: F02 part 2, F12, F13, the HTTP items).

Such a caller must not be able to fill its volume or to get it killed. v4 has stock aiohttp: the connection-flood
layer of review A F02 part 1 is deleted (SPEC-v4 3); its only peer is the worker on an internal network (THREATS.md).
"""

import gzip
import json
import os
import shutil
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from conftest import CONFIG, NO_LIMITS, check_audit, fund_request, make_env
from procs import http, running

from acctpool_signer.__main__ import Paths

DERIVE = {"store": "shop", "family": "evm", "first_index": 0, "count": 5}
SMALL = os.environ.get("SGN_SMALL_DISK", "/small")


def call(port, token, method, path, body=None):
    status, text, headers = http(port, token, method, path, body)
    return status, json.loads(text), headers


def raw(port: int, data: bytes, timeout: float = 5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        try:
            while chunk := s.recv(65536):
                chunks.append(chunk)
        except (TimeoutError, ConnectionError):
            pass
    return b"".join(chunks)


def audit_lines(env) -> list[dict]:
    return [json.loads(line) for line in open(env.paths.audit, "rb").read().splitlines()]


def memory_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise AssertionError("no VmRSS")


def fill(path: Path) -> None:
    """Write to the file until the file system is full."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        for size in (4096, 512, 16, 1):
            while True:
                try:
                    os.write(fd, b"x" * size)
                except OSError:
                    break
    finally:
        os.close(fd)


def test_program_that_reads_the_journal_does_not_stop_the_signer(env):
    """The proof test of F02, second part. No attacker: a backup tool or the sqlite3 shell of the operator."""
    with running(env) as (port, process):
        assert call(port, env.token, "POST", "/v1/derive", DERIVE)[0] == 200
        reader = sqlite3.connect(env.paths.journal, isolation_level=None)
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM signatures").fetchone()[0] == 0  # a read transaction
        during, body, _ = call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-read-1", nonce=0))
        assert during == 200, body
        assert reader.execute("SELECT count(*) FROM signatures").fetchone()[0] == 0  # the reader has its view
        reader.execute("COMMIT")
        assert reader.execute("SELECT count(*) FROM signatures").fetchone()[0] == 1
        reader.close()
        assert call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-read-2", nonce=1))[0] == 200
        assert process.poll() is None
    assert check_audit(env) == len(audit_lines(env))


def test_signer_works_again_after_another_program_held_a_write_lock_on_the_journal(env):
    """A program that WRITES to the journal file stops the writes of the signer for that time, and only for that time."""
    with running(env) as (port, process):
        assert call(port, env.token, "POST", "/v1/derive", DERIVE)[0] == 200
        other = sqlite3.connect(env.paths.journal, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        started = time.time()
        during, body, _ = call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-lock-1", nonce=0))
        waited = time.time() - started
        other.execute("ROLLBACK")
        other.close()
        # the signer waited for the lock (5 seconds) and then gave 500; nothing was signed
        assert (during, body["error"]) == (500, "internal")
        assert 4 < waited < 9
        after, body, _ = call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-lock-2", nonce=0))
        assert after == 200, body
        assert process.poll() is None
        assert "Traceback" not in (env.root / "stderr.txt").read_text()
    assert check_audit(env) == len(audit_lines(env))


def test_calls_without_the_token_do_not_fill_the_volume(env):
    """F12: 2000 calls without the token. The audit log gets no line (the log of stderr gets one line each; the
    container log driver has a size limit). A refused call closes its connection."""
    with running(env) as (port, _):
        assert call(port, env.token, "POST", "/v1/derive", DERIVE)[0] == 200
        before = os.path.getsize(env.paths.audit)
        for _ in range(2000):
            answer = raw(port, b"GET /v1/status HTTP/1.1\r\nHost: signer\r\n\r\n")
            assert answer.startswith(b"HTTP/1.1 401")
        assert os.path.getsize(env.paths.audit) == before
        assert call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-fill-1", nonce=0))[0] == 200
    lines = audit_lines(env)
    assert not [line for line in lines if line["result"] == "unauthorized"]
    assert check_audit(env) == len(lines)
    logged = [
        json.loads(line.split(": ", 1)[1])
        for line in (env.root / "stderr.txt").read_text().splitlines()
        if '"status": 401' in line
    ]
    assert len(logged) == 2000 and logged[0] == {
        "method": "GET",
        "path": "/v1/status",
        "status": 401,
        "remote": "127.0.0.1",
        "ms": logged[0]["ms"],
    }


@pytest.mark.skipif(not os.path.ismount(SMALL), reason=f"{SMALL} is not a mounted small file system")
def test_full_volume_stops_the_signatures_and_the_signer_works_again_when_there_is_space(tmp_path):
    """A volume that is full (by another cause): no signature leaves, the process stays, and no repair is necessary."""
    data = Path(SMALL) / "data"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir()
    env = make_env(tmp_path / "signer", keystore=False)
    env.paths = Paths(
        config=env.paths.config,
        master_key=env.paths.master_key,
        token=env.paths.token,
        data=str(data),
        highwater=env.paths.highwater,
    )
    env.create_keystore()
    ballast = Path(SMALL) / "ballast"
    try:
        with running(env) as (port, process):
            assert call(port, env.token, "POST", "/v1/derive", DERIVE)[0] == 200
            assert call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-full-0", nonce=0))[0] == 200
            fill(ballast)
            statuses = []
            for nonce in range(1, 6):
                status, body, _ = call(
                    port, env.token, "POST", "/v1/sign/fund", fund_request(f"srv-full-{nonce}", nonce=nonce)
                )
                statuses.append(status)
                assert status in (200, 500)
                assert status == 200 or body == {"error": "internal", "detail": "internal error"}
            assert 500 in statuses
            assert process.poll() is None
            ballast.unlink()
            status, body, _ = call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-full-9", nonce=9))
            assert status == 200, body
        assert check_audit(env) == len(audit_lines(env))
        # and a start on a full volume is refused with a message, and works when there is space
        fill(ballast)
        from procs import run_cli

        done = run_cli(env, "serve")
        assert done.returncode == 1
        assert "Traceback" not in done.stderr
        ballast.unlink()
        with running(env) as (port, _):
            assert call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-full-10", nonce=10))[0] == 200
    finally:
        ballast.unlink(missing_ok=True)
        shutil.rmtree(data, ignore_errors=True)


def test_packed_body_is_not_unpacked(env):
    """F13: 30 kB of gzip are 30 MB. The signer does not decompress, with or without the token."""
    packed = gzip.compress(b" " * (30 * 1024 * 1024), 9)
    assert len(packed) < 40000
    head = f"POST /v1/derive HTTP/1.1\r\nHost: x\r\nContent-Encoding: gzip\r\nContent-Length: {len(packed)}\r\n"
    with running(env) as (port, process):
        assert call(port, env.token, "POST", "/v1/derive", DERIVE)[0] == 200
        before = memory_kb(process.pid)

        def attack(extra: str, answers: list) -> None:
            try:
                answers.append(raw(port, (head + extra + "\r\n").encode() + packed, timeout=20))
            except OSError:
                answers.append(b"")

        answers: list[bytes] = []
        threads = [threading.Thread(target=attack, args=("", answers)) for _ in range(40)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert all(a == b"" or a.startswith(b"HTTP/1.1 401") for a in answers)
        assert sum(a.startswith(b"HTTP/1.1 401") for a in answers) > 0
        with_token: list[bytes] = []
        attack(f"Authorization: Bearer {env.token}\r\n", with_token)
        assert with_token[0].startswith(b"HTTP/1.1 400")
        assert b"Content-Encoding is not accepted" in with_token[0]
        after = memory_kb(process.pid)
        assert after - before < 30_000, (before, after)
        assert process.poll() is None
        assert call(port, env.token, "POST", "/v1/sign/fund", fund_request("srv-gzip-1", nonce=0))[0] == 200


def test_bytes_that_are_not_http(env):
    """Stock aiohttp answers 400 (or closes). The signer stays up, and its log has no traceback and none of the bytes."""
    bad = [
        b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03SECRET-LOOKING-BYTES\r\n\r\n",
        b"GET /v1/status HTTP/9.9\r\nHost: x\r\n\r\n",
        b"NOT-A-METHOD-WITH-MARKER-abc123 / HTTP/1.1\r\n\r\n",
        b"GET /v1/status HTTP/1.1\r\n" + b"X-Long: " + b"a" * 20000 + b"\r\n\r\n",
        b"GET /" + b"p" * 20000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
    ]
    with running(env) as (port, process):
        for data in bad:
            answer = raw(port, data)
            assert answer == b"" or answer.split(b" ")[1] in (b"400", b"505"), answer[:80]
        assert call(port, env.token, "GET", "/v1/status")[0] == 200
        assert process.poll() is None
    errors = (env.root / "stderr.txt").read_text()
    for marker in ("Traceback", "SECRET-LOOKING", "abc123", "BadStatusLine", "aaaa"):
        assert marker not in errors
    assert all(line["call"] in ("start", "stop") for line in audit_lines(env))


def test_limits_of_a_request_with_the_token(env):
    with running(env) as (port, _):
        status, body, _ = call(port, env.token, "POST", "/v1/derive", {"store": "shop", "pad": "x" * 20000})
        assert (status, body["error"]) == (413, "invalid_request")
        head = f"POST /v1/derive HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {env.token}\r\n".encode()
        chunks = b"1000\r\n" + b"y" * 4096 + b"\r\n0\r\n\r\n"
        answer = raw(port, head + b"Transfer-Encoding: chunked\r\n\r\n" + chunks)
        assert answer.startswith(b"HTTP/1.1 400")


def test_default_limits_config_runs(env):
    """The signer with the limits of a config that has no limit keys."""
    with running(env, config=CONFIG.replace(NO_LIMITS, "")) as (port, _):
        statuses = [call(port, env.token, "POST", "/v1/derive", DERIVE)[0] for _ in range(12)]
        assert statuses == [200] * 10 + [429] * 2
