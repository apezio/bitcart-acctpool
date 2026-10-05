"""The start with files that do not agree (review A: F04, F05, F06, F08; SPEC-v4 3).

The journal is the master record: the rules (one signature for each nonce, the totals, the daily cap, the budgets,
the highest index) have their memory there. audit.log may be one line behind the journal (a crash or a failed write
after the commit) or end with a part of a line: the start completes it. Every other difference refuses the start.
"""

import json
import os
import shutil

import pytest
from conftest import CAPS, check_audit, make_api, make_env
from procs import run_cli

from acctpool_signer import audit as audit_mod
from acctpool_signer.__main__ import build_state
from acctpool_signer.errors import AuditError, StartError

VALUE = CAPS["max_fund_value_wei"]


async def traffic(env, aiohttp_client):
    """derive, 3 fundings of address 5 (its total is at the cap), one refused funding: 5 lines."""
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    for nonce in range(3):
        assert (await api.fund(f"rec-fund-{nonce}", index=5, nonce=nonce, value_wei=str(VALUE)))[0] == 200
    status, refused = await api.fund("rec-fund-3", index=5, nonce=3, value_wei=str(VALUE))
    assert (status, refused["error"]) == (403, "cap_exceeded")
    return api


def lines_of(env) -> list[dict]:
    return [json.loads(line) for line in open(env.paths.audit, "rb").read().splitlines()]


def start(env):
    """The start of the service, without the network part. The caller closes it."""
    return build_state(env.paths)


# ---------------------------------------------------------------- the journal is not the journal of the log


async def test_start_is_refused_when_the_journal_file_is_gone(env, aiohttp_client):
    api = await traffic(env, aiohttp_client)
    api.close()
    os.unlink(env.paths.journal)
    assert len(lines_of(env)) == 5
    with pytest.raises(StartError) as info:
        start(env)
    assert "README" in str(info.value)
    # the refused start made no journal that the next start would accept
    with pytest.raises(StartError):
        start(env)
    done = run_cli(env, "serve")
    assert done.returncode == 1
    assert "refused" in done.stderr


async def test_start_is_refused_when_the_journal_is_older_than_the_audit_log(env, aiohttp_client):
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    api.close()
    backup = env.paths.journal + ".backup"
    shutil.copy(env.paths.journal, backup)  # the nightly backup

    for nonce in range(3):
        api = await make_api(env, aiohttp_client)
        assert (await api.fund(f"rec-old-{nonce}", index=5, nonce=nonce, value_wei=str(VALUE)))[0] == 200
        api.close()
        saved = env.paths.journal + ".now"
        shutil.copy(env.paths.journal, saved)
        shutil.copy(backup, env.paths.journal)  # the journal of the backup; audit.log is 1, 2, 3 lines ahead
        with pytest.raises(StartError):
            start(env)
        done = run_cli(env, "audit-verify")  # A1: the probe's check says it too, not only the next start
        assert done.returncode == 1 and "older than audit.log" in done.stderr, done.stderr
        shutil.copy(saved, env.paths.journal)


async def test_no_second_signature_for_a_nonce_after_a_journal_rollback(env, aiohttp_client):
    """The proof test of F04: with the old journal, the signer must not sign nonce 0 again for another address."""
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    api.close()
    backup = env.paths.journal + ".backup"
    shutil.copy(env.paths.journal, backup)
    api = await traffic(env, aiohttp_client)
    api.close()
    shutil.copy(backup, env.paths.journal)
    with pytest.raises(StartError):
        await make_api(env, aiohttp_client)


async def test_start_is_refused_with_the_journal_of_another_signer(env, aiohttp_client, tmp_path):
    api = await traffic(env, aiohttp_client)
    api.close()
    other = make_env(tmp_path / "other-signer")
    api = await traffic(other, aiohttp_client)  # same calls, same number of lines, other seed
    assert (await api.fund("rec-more-1", index=1, nonce=9))[0] == 200
    api.close()
    shutil.copy(other.paths.journal, env.paths.journal)
    with pytest.raises(StartError) as info:
        start(env)
    # the seed record of the journal is compared first (review B, L2); the audit log would refuse it too
    assert "the journal is of the seed" in str(info.value)


async def test_start_refuses_a_log_with_a_changed_last_line(env, aiohttp_client):
    """The proof tests of F08: the start checks the chain and each line, not only the number of the last line."""
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    assert (await api.fund("rec-f08-0", nonce=0))[0] == 200
    assert (await api.sweep("rec-f08-1", nonce=0, amount="500000000"))[0] == 200
    api.close()
    lines = open(env.paths.audit, "rb").read().splitlines()
    entry = json.loads(lines[-1])
    assert entry["amount"] == "500000000"
    entry["amount"] = "5"
    with open(env.paths.audit, "wb") as f:
        f.write(b"".join(line + b"\n" for line in [*lines[:-1], json.dumps(entry, separators=(",", ":")).encode()]))
    with pytest.raises(AuditError):
        start(env)
    # line 2 (the funding) removed, a copy of the last line added: the number of the last line is the same
    with open(env.paths.audit, "wb") as f:
        f.write(b"".join(line + b"\n" for line in [lines[0], lines[2], lines[2]]))
    with pytest.raises(AuditError):
        start(env)
    with open(env.paths.audit, "wb") as f:
        f.write(b"".join(line + b"\n" for line in lines))
    start(env).close()


# ---------------------------------------------------------------- one process


def test_second_process_on_the_same_volume_is_refused(env):
    """The proof test of F06."""
    first = start(env)
    first.audit.write("start", "ok")
    with pytest.raises(StartError) as info:
        start(env)
    assert "another signer process" in str(info.value)
    done = run_cli(env, "serve")
    assert done.returncode == 1
    assert "another signer process" in done.stderr
    # the first process was not disturbed, and the lock is free when it stops
    first.audit.write("derive", "ok", store="shop", index=0, count=1)
    assert check_audit(env) == 2
    first.close()
    second = start(env)
    second.audit.write("start", "ok")
    second.close()
    assert check_audit(env) == 3


def test_commands_that_read_only_do_not_need_the_lock(env):
    first = start(env)
    first.audit.write("start", "ok")
    assert run_cli(env, "audit-verify").returncode == 0
    assert run_cli(env, "verify").returncode == 0
    first.close()


# ---------------------------------------------------------------- audit.log is one line behind or cut: the start completes it


async def test_cut_line_at_the_end_is_removed_at_the_start(env, aiohttp_client):
    api = await traffic(env, aiohttp_client)
    api.close()
    good = open(env.paths.audit, "rb").read()
    with open(env.paths.audit, "ab") as f:
        f.write(b'{"ts":"2026-09-29T12:00:01.000000Z","call":"sign/fund","result":"ok","chain":"an')
    assert run_cli(env, "audit-verify").returncode == 1  # the probe sees it
    start(env).close()
    assert open(env.paths.audit, "rb").read() == good
    assert check_audit(env) == 5
    # the rules have their memory
    api = await make_api(env, aiohttp_client)
    status, result = await api.fund("rec-cut-1", index=5, nonce=3, value_wei="1")
    assert (status, result["error"]) == (403, "cap_exceeded")
    status, result = await api.fund("rec-cut-2", index=6, nonce=0, value_wei=str(VALUE))
    assert (status, result["error"]) == (409, "idempotency_conflict")


async def test_failed_write_after_the_commit_is_completed_by_the_next_write_and_the_start(env, aiohttp_client, monkeypatch):
    """A full volume after the commit: 500, the signature is in the journal, audit.log gets the line later."""
    api = await traffic(env, aiohttp_client)
    fd, real = api.state.audit._fd, os.write
    monkeypatch.setattr(audit_mod.os, "write", lambda f, data: real(f, data[:10]) if f == fd else real(f, data))
    status, result = await api.fund("rec-crash-1", index=6, nonce=3)
    assert (status, result["error"]) == (500, "internal")
    monkeypatch.undo()
    assert len(lines_of(env)) == 5  # the part of the line was cut back
    assert run_cli(env, "audit-verify").returncode == 0  # one line behind the journal is accepted
    api.close()
    start(env).close()
    assert [line["idempotency_key"] for line in lines_of(env)[5:]] == ["rec-crash-1"]
    # the worker sends the same request again: the stored answer
    api = await make_api(env, aiohttp_client)
    status, result = await api.fund("rec-crash-1", index=6, nonce=3)
    assert status == 200
    assert lines_of(env)[-1]["replay"] is True
    assert check_audit(env) == 7


async def test_log_more_than_one_line_behind_or_not_there_is_refused(env, aiohttp_client):
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    assert (await api.fund("rec-f05-0", nonce=0))[0] == 200
    shutil.copy(env.paths.audit, env.paths.audit + ".backup")  # the nightly backup: 2 lines
    assert (await api.fund("rec-f05-1", nonce=1))[0] == 200
    assert (await api.sweep("rec-f05-2", nonce=0))[0] == 200
    api.close()
    good = open(env.paths.audit, "rb").read()
    shutil.copy(env.paths.audit + ".backup", env.paths.audit)
    with pytest.raises(AuditError) as info:
        start(env)
    assert "the journal ends with audit line 4 and audit.log with line 2" in str(info.value)
    os.unlink(env.paths.audit)
    with pytest.raises(AuditError):
        start(env)
    with open(env.paths.audit, "wb") as f:
        f.write(good)
    start(env).close()
    assert check_audit(env) == 4
