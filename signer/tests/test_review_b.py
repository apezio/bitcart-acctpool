"""Regression tests for the findings of review B (review/signer-b-REPORT.md) and the decisions of SPEC 7.10, 7.11.

Each test asserts the SAFE behaviour. Where a proof test of the review (review/signer-b-proofs/) is the base, the
text names it and says what is different and why. The proofs about Tron (b01, b03, b05, b06, b08, b12) and about
journal-rebuild (b09) have no subject in v4 (SPEC-v4 3): those parts are removed.
"""

import asyncio
import hashlib
import json
import os
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path

import procs
import pytest
from conftest import CAPS, CONFIG, GWEI, NO_LIMITS, fund_request, make_api, make_env
from procs import run_cli
from test_recovery import traffic

from acctpool_signer import api as api_mod
from acctpool_signer.__main__ import build_state
from acctpool_signer.audit import GENESIS
from acctpool_signer.errors import StartError

VALUE = str(CAPS["max_fund_value_wei"])


def start(env):
    return build_state(env.paths)


def last_line(env) -> tuple[int, str]:
    """(seq, hash) of the last complete line of audit.log: what the probe writes into the high-water file."""
    line = Path(env.paths.audit).read_bytes().splitlines()[-1]
    return json.loads(line)["seq"], hashlib.sha256(line).hexdigest()


def write_mark(env, seq: int, digest: str) -> None:
    Path(env.paths.highwater).write_text(f"{seq} {digest}\n")


def remove_journal(env) -> None:
    for extra in ("", "-wal", "-shm"):
        if os.path.exists(env.paths.journal + extra):
            os.unlink(env.paths.journal + extra)


# ---------------------------------------------------------------- M1: a restore of the full volume


async def test_restore_of_a_full_older_volume_copy_is_refused_with_the_highwater_file(env, aiohttp_client):
    """Proof b04 test_f04_added: the backup has the journal AND audit.log of one time; they agree with each other.
    Different from the proof: the probe wrote the high-water file after the traffic (the proof has no such file)."""
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    api.close()
    backup = env.root / "backup"
    shutil.copytree(env.paths.data, backup)

    api = await traffic(env, aiohttp_client)
    first = api.transactions[0]
    api.close()
    write_mark(env, *last_line(env))
    shutil.rmtree(env.paths.data)
    shutil.copytree(backup, env.paths.data)  # restore of the full volume
    before = {name: (Path(env.paths.data) / name).read_bytes() for name in ("audit.log", "journal.sqlite3")}

    with pytest.raises(StartError) as info:
        start(env)
    text = str(info.value)
    assert "high-water file" in text and "README 'Recovery'" in text
    # the refused start changed no line: the operator can add the lines that are missing
    assert (Path(env.paths.data) / "audit.log").read_bytes() == before["audit.log"]
    # and without the high-water file the start takes the old volume (the reason for the file)
    os.unlink(env.paths.highwater)
    api = await make_api(env, aiohttp_client)
    status, again = await api.fund("rb-other-key", index=6, nonce=0, value_wei=VALUE)
    assert status == 200 and again["to"] != first["to"]
    api.close()


def test_normal_restarts_pass_with_the_highwater_file(env):
    """The probe writes the file while the signer runs, and it is behind the journal most of the time."""
    from procs import http, running

    for run in range(3):
        with running(env) as (port, _):
            assert (
                http(port, env.token, "POST", "/v1/derive", {"store": "shop", "family": "evm", "first_index": 0, "count": 2})[
                    0
                ]
                == 200
            )
            assert (
                http(port, env.token, "POST", "/v1/sign/fund", fund_request(f"rb-restart-{run}", index=1, nonce=run))[0] == 200
            )
            if run == 1:
                write_mark(env, *last_line(env))  # the probe, while the signer runs
        if run == 0:
            write_mark(env, *last_line(env))  # the probe, after the stop: the mark is the last line
    lines = Path(env.paths.audit).read_bytes().splitlines()
    assert [json.loads(line)["call"] for line in lines].count("start") == 3
    done = run_cli(env, "audit-verify")
    assert done.returncode == 0, done.stderr
    seq = int(Path(env.paths.highwater).read_text().split()[0])
    assert f"the log has line {seq} of the high-water file" in done.stdout


async def test_highwater_seq_higher_than_every_line_is_refused(env, aiohttp_client):
    api = await traffic(env, aiohttp_client)
    api.close()
    seq, digest = last_line(env)
    write_mark(env, seq + 1, digest)
    with pytest.raises(StartError) as info:
        start(env)
    assert f"audit.log ends with line {seq}, and the high-water file" in str(info.value)
    write_mark(env, seq, digest)
    start(env).close()


async def test_highwater_hash_of_another_line_is_refused(env, aiohttp_client):
    api = await traffic(env, aiohttp_client)
    api.close()
    seq, _ = last_line(env)
    write_mark(env, seq - 1, last_line(env)[1])  # the hash of line seq, with the number seq - 1
    with pytest.raises(StartError) as info:
        start(env)
    assert f"audit line {seq - 1} does not agree with the high-water file" in str(info.value)


MALFORMED = {
    "empty": b"",
    "a newline only": b"\n",
    "seq only": b"12\n",
    "upper-case hash": b"3 " + b"AB" * 32 + b"\n",
    "short hash": b"3 " + b"ab" * 31 + b"\n",
    "leading zero": b"03 " + b"ab" * 32 + b"\n",
    "negative": b"-3 " + b"ab" * 32 + b"\n",
    "two spaces": b"3  " + b"ab" * 32 + b"\n",
    "two lines": b"3 " + b"ab" * 32 + b"\n4 " + b"cd" * 32 + b"\n",
    "a second newline": b"3 " + b"ab" * 32 + b"\n\n",
    "a carriage return": b"3 " + b"ab" * 32 + b"\r\n",
    "seq 0 with a hash that is not zero": b"0 " + b"ab" * 32 + b"\n",
    "JSON": b'{"seq": 3, "hash": "' + b"ab" * 32 + b'"}\n',
    "long": b"3 " + b"ab" * 32 + b" " * 200,
}


@pytest.mark.parametrize("case", list(MALFORMED))
async def test_malformed_highwater_file_is_refused(env, aiohttp_client, case):
    api = await traffic(env, aiohttp_client)
    api.close()
    Path(env.paths.highwater).write_bytes(MALFORMED[case])
    with pytest.raises(StartError) as info:
        start(env)
    assert "high-water file" in str(info.value)


def test_highwater_path_that_is_a_folder_is_refused(env):
    # docker makes a folder for a bind mount whose host file is not there: that is not "no file"
    os.mkdir(env.paths.highwater)
    with pytest.raises(StartError) as info:
        start(env)
    assert "cannot be read (IsADirectoryError" in str(info.value)


async def test_missing_highwater_file_and_seq_0_are_no_check(env, aiohttp_client):
    assert not os.path.exists(env.paths.highwater)
    start(env).close()  # first start: no file
    write_mark(env, 0, GENESIS)
    start(env).close()  # the file of the deploy step before the first line
    api = await traffic(env, aiohttp_client)
    api.close()
    start(env).close()


async def test_new_empty_volume_is_refused_with_the_highwater_file(env, aiohttp_client):
    """THREATS.md, a party that writes the volume: removes the journal AND all files of the audit log."""
    api = await traffic(env, aiohttp_client)
    api.close()
    write_mark(env, *last_line(env))
    remove_journal(env)
    os.unlink(env.paths.audit)
    with pytest.raises(StartError) as info:
        start(env)
    assert "audit.log ends with line 0" in str(info.value)


async def test_restored_volume_starts_only_after_root_resets_the_highwater_file(env, aiohttp_client):
    """README 'Recovery': v4 has no journal-rebuild. A restored volume is refused until root, after the checks of the
    README, writes "0 <64 zeros>" into the high-water file; the probe then writes the new mark."""
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    api.close()
    backup = env.root / "backup"
    shutil.copytree(env.paths.data, backup)
    api = await traffic(env, aiohttp_client)
    api.close()
    write_mark(env, *last_line(env))
    shutil.rmtree(env.paths.data)
    shutil.copytree(backup, env.paths.data)
    with pytest.raises(StartError):
        start(env)
    assert run_cli(env, "audit-verify").returncode == 1  # the probe alerts
    write_mark(env, 0, GENESIS)
    start(env).close()


async def test_audit_verify_compares_with_the_highwater_file(env, aiohttp_client):
    """The probe runs audit-verify: a journal below the mark must fail it, not only the next start."""
    api = await traffic(env, aiohttp_client)
    api.close()
    seq, digest = last_line(env)
    write_mark(env, seq + 5, digest)
    done = run_cli(env, "audit-verify")
    assert done.returncode == 1 and "high-water file" in done.stderr
    write_mark(env, seq, digest)
    done = run_cli(env, "audit-verify")
    assert done.returncode == 0 and f"the log has line {seq} of the high-water file" in done.stdout


# ---------------------------------------------------------------- L2: the journal of another seed


async def test_journal_of_another_seed_without_audit_log_is_refused(tmp_path, aiohttp_client):
    """Proof b13 test 1."""
    first = make_env(tmp_path / "first")
    api = await make_api(first, aiohttp_client)
    assert (await api.derive())[0] == 200
    assert (await api.fund("rb-first-a", index=3, nonce=0, value_wei=VALUE))[0] == 200
    api.close()
    second = make_env(tmp_path / "second")  # another seed
    shutil.copy(first.paths.journal, second.paths.journal)
    assert not os.path.exists(second.paths.audit)
    with pytest.raises(StartError) as info:
        start(second)
    assert "the journal is of the seed" in str(info.value) and "README 'Recovery'" in str(info.value)
    assert not os.path.exists(second.paths.audit)


async def test_journal_and_audit_log_of_another_seed_are_refused(tmp_path, aiohttp_client):
    first = make_env(tmp_path / "first")
    api = await make_api(first, aiohttp_client)
    assert (await api.derive())[0] == 200
    api.close()
    second = make_env(tmp_path / "second")
    shutil.rmtree(second.paths.data)
    shutil.copytree(first.paths.data, second.paths.data)
    os.unlink(Path(second.paths.data) / "keystore.json")
    second.create_keystore()  # the volume of the first signer with the keystore of the second
    with pytest.raises(StartError) as info:
        start(second)
    assert "the journal is of the seed" in str(info.value)


async def test_older_journal_without_audit_log_is_refused_with_the_highwater_file(env, aiohttp_client):
    """Proof b13 test 2. Without the high-water file this start is accepted (SPEC 7.10: missing file = no check): the
    journal has one line and audit.log none, which is the case of a crash before the first line was written."""
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    api.close()
    shutil.copy(env.paths.journal, env.paths.journal + ".old")
    api = await make_api(env, aiohttp_client)
    for nonce in range(3):
        assert (await api.fund(f"rb-fund-{nonce}", index=5, nonce=nonce, value_wei=VALUE))[0] == 200
    api.close()
    write_mark(env, *last_line(env))
    remove_journal(env)
    shutil.copy(env.paths.journal + ".old", env.paths.journal)
    os.unlink(env.paths.audit)
    with pytest.raises(StartError):
        start(env)


# ---------------------------------------------------------------- L1: the time of a request


async def _held_request(api, env, body: dict, path: str = "/v1/sign/fund", length: int | None = None):
    """Proof b11: the headers now, the body later. Gives (reader, writer, body bytes)."""
    server = api.client.server
    reader, writer = await asyncio.open_connection(server.host, server.port)
    data = json.dumps(body).encode()
    head = (
        f"POST {path} HTTP/1.1\r\nHost: signer\r\n"
        f"Authorization: Bearer {env.token}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data) if length is None else length}\r\n\r\n"
    )
    writer.write(head.encode())
    await writer.drain()
    await asyncio.sleep(0.3)  # the server has the headers and waits for the body
    return reader, writer, data


async def _answer(reader) -> tuple[int, dict]:
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = await reader.read(65536)
        assert chunk, "the connection was closed without an answer"
        raw += chunk
    head, _, rest = raw.partition(b"\r\n\r\n")
    length = int([line.split(b":")[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")][0])
    while len(rest) < length:
        rest += await reader.read(65536)
    return int(head.split()[1]), json.loads(rest[:length])


async def _finish(reader, writer, data: bytes) -> tuple[int, dict]:
    writer.write(data)
    await writer.drain()
    result = await _answer(reader)
    writer.close()
    return result


async def test_a_held_body_is_recorded_on_the_day_of_the_signature(bare, env):
    """Proof b11, the same test: 6 requests with the headers on one day and the body 30 hours later."""
    api = bare
    clock = env.clock
    old_day = clock.now.strftime("%Y-%m-%d")
    held = [await _held_request(api, env, fund_request(f"rb-held-{n}", index=n, nonce=n, value_wei=VALUE)) for n in range(6)]
    clock.advance(30 * 3600)
    today = clock.now.strftime("%Y-%m-%d")
    assert today != old_day
    results = [await _finish(*h) for h in held]
    assert [status for status, _ in results] == [200] * 6
    days = [api.state.journal.get_signature(f"rb-held-{n}")["utc_day"] for n in range(6)]
    assert days == [today] * 6
    # the cap of today counts them: 6 x 0.3 of 2.0, so one more of 0.3 is over the cap
    status, refused = await api.fund("rb-today-6", index=6, nonce=6, value_wei=VALUE)
    assert (status, refused["error"]) == (403, "daily_cap_exceeded")


async def test_a_body_held_over_midnight_counts_on_the_new_day(bare, env):
    """Headers at 23:59:59 UTC, body 2 seconds later: the checks, the journal row and the audit line have 00:00:01."""
    api = bare
    env.clock.now = datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC)
    held = await _held_request(api, env, fund_request("rb-midnight-1", index=2, nonce=0, value_wei=VALUE))
    env.clock.advance(2)
    status, _ = await _finish(*held)
    assert status == 200
    assert api.state.journal.get_signature("rb-midnight-1")["utc_day"] == "2026-09-30"
    line = [entry for entry in api.audit_lines() if entry.get("idempotency_key") == "rb-midnight-1"][0]
    assert line["ts"] == "2026-09-30T00:00:01.000000Z"
    spend = int(VALUE) + 21000 * 60 * GWEI
    assert api.state.journal.daily_spend("anvil", "2026-09-30") == spend
    assert api.state.journal.daily_spend("anvil", "2026-09-29") == 0


async def test_two_held_requests_have_each_their_own_time(bare, env):
    api = bare
    first = await _held_request(api, env, fund_request("rb-own-1", index=2, nonce=0, value_wei=VALUE))
    second = await _held_request(api, env, fund_request("rb-own-2", index=3, nonce=1, value_wei=VALUE))
    env.clock.advance(3600)
    assert (await _finish(*first))[0] == 200
    env.clock.advance(3600)
    assert (await _finish(*second))[0] == 200
    times = {entry.get("idempotency_key"): entry["ts"] for entry in api.audit_lines()}
    assert [times["rb-own-1"], times["rb-own-2"]] == ["2026-09-29T13:00:00.000000Z", "2026-09-29T14:00:00.000000Z"]


async def test_a_body_that_does_not_come_is_refused_after_the_body_timeout(bare, env, monkeypatch):
    api = bare
    monkeypatch.setattr(api_mod, "BODY_SECONDS", 0.5)
    reader, writer, _ = await _held_request(api, env, fund_request("rb-late-1"), length=300)
    status, result = await asyncio.wait_for(_answer(reader), 10)
    assert (status, result["error"]) == (400, "invalid_request")
    assert "the body did not come in 0.5 seconds" in result["detail"]
    # The connection is closed: the rest of the body cannot start a second request. aiohttp reads and drops what
    # comes for its lingering time (10 seconds) before it closes a connection with a body that is not complete.
    assert await asyncio.wait_for(reader.read(), 20) == b""
    writer.close()
    assert api.journal_rows() == 0
    assert api.audit_lines()[-1]["result"] == "invalid_request"


# ---------------------------------------------------------------- L4: the fee budget and the order of the calls


async def test_evm_native_sweep_signed_before_the_funding_the_value_bound_holds(bare, env):
    """Proof b02 test 1. The proof asserts that the budget is 0 after "native sweep, then funding"; that claim was
    wrong (SPEC 7.10, L4). What holds in this order too: the token-sweep fees are not more than the fundings."""
    api = bare
    value = CAPS["max_fund_value_wei"]  # 0.3
    status, native = await api.sweep_native("rb-order-n0", index=3, nonce=0, value_wei=str(value - 21000 * 60 * GWEI))
    assert status == 200
    status, fund = await api.fund("rb-order-f0", index=3, nonce=0, value_wei=str(value))
    assert status == 200 and fund["to"] == native["from"]
    # a native sweep signed before the funding takes nothing from a budget of 0: the budget is the funding
    assert api.state.journal.sweep_budget("anvil", env.address("shop", 3)) == value
    fees = 0
    for nonce in range(1, 4):
        fee = 150000 * 1000 * GWEI  # 0.15
        status, _ = await api.sweep(
            f"rb-order-s{nonce}",
            index=3,
            nonce=nonce,
            gas_limit=150000,
            max_fee_per_gas_wei=str(1000 * GWEI),
            max_priority_fee_per_gas_wei="0",
        )
        if status != 200:
            break
        fees += fee
    assert status == 403
    assert fees == value  # two sweeps of 0.15: all of the funding, and not more


async def test_value_bound_holds_in_any_order(bare, env):
    """Proof b02 test 3, the same test."""
    api = bare
    value = CAPS["max_fund_value_wei"]
    nonce = 0
    fees = 0
    assert (await api.sweep_native("rb-value-n0", index=4, nonce=nonce, value_wei=str(value)))[0] == 200
    nonce += 1
    funded = 0
    for number in range(3):
        status, _ = await api.fund(f"rb-value-f{number}", index=4, nonce=number, value_wei=str(value))
        assert status == 200
        funded += value
        while True:
            status, _ = await api.sweep(
                f"rb-value-s{nonce}",
                index=4,
                nonce=nonce,
                gas_limit=150000,
                max_fee_per_gas_wei=str(1000 * GWEI),
                max_priority_fee_per_gas_wei="0",
            )
            if status != 200:
                break
            fees += 150000 * 1000 * GWEI
            nonce += 1
    assert fees <= funded
    assert fees == funded  # the budget is used, and not more


# ---------------------------------------------------------------- review B, Job 3: the limits after a real kill


def test_sign_and_derive_limits_hold_over_a_kill(env):
    """Proof b10, the same test: a real process, SIGKILL, and the real clock."""
    config = CONFIG.replace(NO_LIMITS, "max_signatures_per_minute = 5\nmax_derive_per_minute = 3\nmax_other_per_minute = 4")
    derive = {"store": "shop", "family": "evm", "first_index": 0, "count": 2}
    with procs.running(env, config=config) as (port, process):
        assert procs.http(port, env.token, "POST", "/v1/derive", derive)[0] == 200
        for nonce in range(5):
            status = procs.http(
                port, env.token, "POST", "/v1/sign/fund", fund_request(f"rb-kill-{nonce}", nonce=nonce, index=1)
            )[0]
            assert status == 200
        assert procs.http(port, env.token, "POST", "/v1/sign/fund", fund_request("rb-kill-5", nonce=5, index=1))[0] == 429
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=10)
    with procs.running(env, config=config) as (port, _):
        assert procs.http(port, env.token, "POST", "/v1/sign/fund", fund_request("rb-kill-6", nonce=6, index=1))[0] == 429
        assert [procs.http(port, env.token, "POST", "/v1/derive", derive)[0] for _ in range(3)] == [200, 200, 429]
