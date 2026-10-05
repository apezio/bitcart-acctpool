"""Audit log (SPEC 3.5, SPEC-v4 3): one hash-chained file, its verification, and what a changed file looks like.

The journal has the newest line (meta audit_line); audit.log has all lines. The tests of the start with a journal
that is lost, older or of another signer are in test_recovery.py.
"""

import hashlib
import json
import os
import subprocess
import sys

import pytest
from conftest import check_audit, make_api
from procs import SIGNER_DIR, run_cli

from acctpool_signer import audit
from acctpool_signer.errors import AuditError

SPEC_FIELDS = [
    "ts", "call", "result", "chain", "store", "index", "nonce", "value_wei", "amount", "max_fee_per_gas_wei", "tx_hash",
    "idempotency_key", "prev", "seq",
]  # fmt: skip


async def traffic(api):
    """Eight lines of different kinds (the fixture wrote line 1). A status call with the token, a call without the token
    and an unknown path write no line."""
    await api.get("/v1/status")
    await api.sweep_native("audit-native-1", index=2, nonce=0)
    await api.fund("audit-fund-1", index=1, nonce=0)
    await api.fund("audit-fund-1", index=1, nonce=0)
    await api.sweep("audit-sweep-1", index=1, nonce=0)
    await api.fund("audit-fund-2", index=1, nonce=1, value_wei=str(10**30))
    await api.get("/v1/status", token="wrong-token-wrong-token-wrong-token-xx")
    await api.get("/v1/export")
    await api.sweep("audit-sweep-2", index=1, nonce=1, to="0x00000000000000000000000000000000000000ee")
    await api.derive("shop", 20, 5)


def lines_of(env) -> list[bytes]:
    return open(env.paths.audit, "rb").read().splitlines()


def write_lines(env, lines: list[bytes]) -> None:
    with open(env.paths.audit, "wb") as f:
        f.write(b"".join(line + b"\n" for line in lines))


async def test_every_request_is_one_line_with_the_spec_fields(capsys, api, env):
    await traffic(api)
    lines = api.audit_lines()
    assert len(lines) == 8
    for number, line in enumerate(lines, start=1):
        assert [name for name in SPEC_FIELDS if name not in line] == []
        assert line["seq"] == number
    assert [line["call"] for line in lines] == [
        "derive", "sign/sweep_native", "sign/fund", "sign/fund", "sign/sweep", "sign/fund", "sign/sweep", "derive",
    ]  # fmt: skip
    assert [line["result"] for line in lines] == ["ok", "ok", "ok", "ok", "ok", "cap_exceeded", "invalid_request", "ok"]
    assert lines[3]["replay"] is True
    assert (lines[1]["value_wei"], lines[1]["amount"], lines[1]["gas_limit"]) == (str(5 * 10**16), None, 21000)
    assert lines[2]["tx_hash"] == lines[3]["tx_hash"]
    assert lines[5]["tx_hash"] is None
    assert (lines[0]["family"], lines[0]["count"]) == ("evm", 20)
    # stdout has the same lines as the file, and the journal has the newest one
    printed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert [line.encode() for line in printed] == lines_of(env)
    assert api.state.journal.get_meta("audit_line").encode() == lines_of(env)[-1]


async def test_chain_of_hashes(api, env):
    await traffic(api)
    raw = lines_of(env)
    assert json.loads(raw[0])["prev"] == "0" * 64
    for before, line in zip(raw, raw[1:], strict=False):
        assert json.loads(line)["prev"] == hashlib.sha256(before).hexdigest()
    assert check_audit(env) == 8


async def test_removed_line_is_detected(api, env):
    await traffic(api)
    original = lines_of(env)
    assert check_audit(env) == 8
    for position in range(len(original) - 1):
        write_lines(env, original[:position] + original[position + 1 :])
        with pytest.raises(AuditError):
            check_audit(env)
    # the last line removed: one line behind the journal, the case of a failed write (the signer writes it again)
    write_lines(env, original[:-1])
    assert check_audit(env) == 7
    write_lines(env, original)
    assert check_audit(env) == 8


async def test_removed_end_is_detected_with_the_journal(api, env):
    await traffic(api)
    original = lines_of(env)
    for keep in (6, 5, 1, 0):
        write_lines(env, original[:keep])
        # the chain of the lines that stay is complete; only the journal shows that the end is gone
        assert audit.verify(env.paths.audit, None).seq == keep
        with pytest.raises(AuditError) as info:
            check_audit(env)
        assert f"the journal ends with audit line 8 and audit.log with line {keep}" in str(info.value)
    # one line behind is the case of a failed write after the commit: accepted, the signer writes the line next
    write_lines(env, original[:7])
    assert check_audit(env) == 7


async def test_changed_added_and_moved_lines_are_detected(api, env):
    await traffic(api)
    original = lines_of(env)

    def changed(position: int, **values) -> list[bytes]:
        entry = json.loads(original[position])
        entry.update(values)
        return [*original[:position], json.dumps(entry, separators=(",", ":")).encode(), *original[position + 1 :]]

    def chain_made_again(lines: list[bytes]) -> list[bytes]:
        """What a person does who knows the format: seq and prev of all lines are made new."""
        out: list[bytes] = []
        for number, line in enumerate(lines, start=1):
            entry = json.loads(line)
            entry["seq"] = number
            entry["prev"] = hashlib.sha256(out[-1]).hexdigest() if out else "0" * 64
            out.append(json.dumps(entry, separators=(",", ":")).encode())
        return out

    cases = {
        "value changed": changed(2, value_wei="1"),
        "result changed": changed(5, result="ok"),
        "tx hash changed": changed(4, tx_hash="0x" + "00" * 32),
        "last line changed": changed(7, store="other"),
        "one character changed": [*original[:3], original[3].replace(b"audit-fund-1", b"audit-fund-9"), *original[4:]],
        "line added in the middle": [*original[:4], original[3], *original[4:]],
        "line added at the end without chain": [*original, original[-1]],
        "two lines exchanged": [*original[:2], original[3], original[2], *original[4:]],
        "first line removed and seq written again": [
            json.dumps({**json.loads(line), "seq": n}, separators=(",", ":")).encode()
            for n, line in enumerate(original[1:], start=1)
        ],
        "not JSON": [*original[:6], b"not json", *original[7:]],
        "empty line": [*original[:6], b"", *original[6:]],
        "line removed and the chain made again": chain_made_again(original[:4] + original[5:]),
        "value changed and the chain made again": chain_made_again(changed(2, value_wei="1")),
    }
    for name, lines in cases.items():
        write_lines(env, lines)
        with pytest.raises(AuditError):
            check_audit(env)
        if "chain made again" in name:
            # the file alone has no fault; the journal shows the change
            assert audit.verify(env.paths.audit, None).seq == len(lines), name
    with open(env.paths.audit, "wb") as f:
        f.write(b"\n".join(original))  # the newline of the last line is missing: a write in progress (S2)
    assert check_audit(env) == 7
    with open(env.paths.audit, "wb") as f:
        f.write(b"".join(line + b"\n" for line in original[:-1]) + b'{"junk":')
    with pytest.raises(AuditError):
        check_audit(env)  # a part of a line that is not the journal's next line


async def test_audit_verify_while_the_signer_writes(api, env, monkeypatch):
    """S2: the probe runs audit-verify while the signer writes. The journal is read first; audit.log may then have a
    newer line, or a part of the journal's next line at the end (a write in progress): no alarm."""
    from acctpool_signer.journal import Journal

    await traffic(api)
    api.close()
    original = lines_of(env)
    with open(env.paths.audit, "wb") as f:
        f.write(b"".join(line + b"\n" for line in original[:-1]) + original[-1][:40])
    assert check_audit(env) == 7
    write_lines(env, original)
    journal = Journal(env.paths.journal, readonly=True)
    reads = iter([original[-2].decode(), original[-1].decode()])  # line 8 was committed after the first read
    monkeypatch.setattr(journal, "get_meta", lambda key: next(reads))
    try:
        assert audit.verify(env.paths.audit, journal).seq == 8
    finally:
        journal.close()
    # the start is strict: a journal that is older than audit.log is refused
    with pytest.raises(AuditError):
        audit.compare(original[-2].decode(), audit.scan(env.paths.audit, 7))


async def test_a_failed_cut_back_stops_the_process(api, env, monkeypatch):
    """S3: when the part of a line cannot be cut back, the next line would go behind it (a line that is not JSON in
    the middle of the file, every start refused). The process stops at once; the next start cuts the part."""

    class Exited(Exception):
        pass

    def cannot_cut(fd: int, size: int) -> None:
        raise OSError(28, "No space left on device")

    def exit_(code: int) -> None:
        raise Exited(code)

    monkeypatch.setattr(audit.os, "write", real_os_write_of_a_part(10))
    monkeypatch.setattr(audit.os, "ftruncate", cannot_cut)
    monkeypatch.setattr(audit.os, "_exit", exit_)
    with pytest.raises(Exited) as info:
        api.state.audit.write("derive", "ok", chain="anvil")
    assert info.value.args == (1,)


async def test_chain_goes_on_after_a_restart(env, aiohttp_client):
    first = await make_api(env, aiohttp_client)
    await first.derive()
    await first.fund("audit-restart-1")
    first.close()
    second = await make_api(env, aiohttp_client)
    await second.fund("audit-restart-2", nonce=1)
    lines = second.audit_lines()
    assert [line["seq"] for line in lines] == [1, 2, 3]
    assert lines[2]["prev"] == hashlib.sha256(lines_of(env)[1]).hexdigest()
    assert check_audit(env) == 3


async def test_values_that_fail_the_field_check_are_not_written(api, env):
    bad = "x" * 40 + '\n{"seq":1}'
    await api.fund("audit-unsafe-1", chain=bad, store=["shop"], index="3", nonce=-5, value_wei="1e99")
    line = api.audit_lines()[-1]
    assert line["result"] == "invalid_request"
    assert (line["chain"], line["store"], line["index"], line["nonce"], line["value_wei"]) == (None, None, None, None, None)
    assert line["idempotency_key"] == "audit-unsafe-1"
    # a name that is a string of the right length is written, as a JSON string in one line
    await api.fund("audit-unsafe-2", chain='no\nsuch"chain')
    line = api.audit_lines()[-1]
    assert (line["result"], line["chain"]) == ("unknown_chain", 'no\nsuch"chain')
    assert len(lines_of(env)) == 3
    assert check_audit(env) == 3


def real_os_write_of_a_part(limit: int):
    """os.write as the kernel does it at a size limit: a part of the bytes goes into the file, no error."""
    real = os.write

    def write(fd: int, data: bytes) -> int:
        return real(fd, data[:limit])

    return write


async def test_no_answer_goes_out_when_the_log_cannot_be_written(api, env, monkeypatch):
    """The journal takes the signature, audit.log takes only a part of the line (a short write).

    During the fault: 500, the signed bytes do not leave the signer, and no other signature is made: every later request
    first writes the line that audit.log does not have, and fails. After the fault: the signer works, without a
    restart; the same key gives the answer that was stored, and the log has all lines. The test with a real full volume
    is in test_server.py, and the test with a real file size limit is test_short_write_of_the_kernel.
    """
    assert (await api.fund("audit-limit-0", nonce=0))[0] == 200
    size = os.path.getsize(env.paths.audit)
    monkeypatch.setattr(audit.os, "write", real_os_write_of_a_part(100))
    for key, nonce in (("audit-limit-1", 1), ("audit-limit-2", 2)):
        status, result = await api.fund(key, nonce=nonce)
        assert (status, result) == (500, {"error": "internal", "detail": "internal error"})
        # the file has complete lines only: the part of the line was removed
        raw = open(env.paths.audit, "rb").read()
        assert len(raw) == size
        assert raw.endswith(b"\n")
    status, result = await api.sweep("audit-limit-3")
    assert (status, result["error"]) == (500, "internal")
    assert len(api.transactions) == 1
    # the journal has the first signature of the fault only (its nonce is used)
    assert api.journal_rows() == 2
    monkeypatch.undo()

    # the cause is gone: the next call works, and the key of the time of the fault gives its stored answer
    status, result = await api.fund("audit-limit-4", nonce=3)
    assert status == 200
    assert (result["decoded"]["nonce"], result["decoded"]["signer"]) == (3, env.fee_address)
    status, again = await api.fund("audit-limit-1", nonce=1)
    assert status == 200
    tx = again["decoded"]
    assert (tx["nonce"], tx["to"], tx["value"], tx["signer"]) == (1, env.address("shop", 3), 5 * 10**15, env.fee_address)
    status, other = await api.fund("audit-limit-9", nonce=1)
    assert (status, other["error"]) == (409, "idempotency_conflict")
    assert (await api.fund("audit-limit-2", nonce=2))[0] == 200
    lines = api.audit_lines()
    assert [line["seq"] for line in lines] == list(range(1, len(lines) + 1))
    keys = [line["idempotency_key"] for line in lines if line["result"] == "ok" and not line.get("replay")]
    assert keys[-4:] == ["audit-limit-0", "audit-limit-1", "audit-limit-4", "audit-limit-2"]
    assert check_audit(env) == len(lines)


CHILD = r"""
import json, os, resource, sys
from acctpool_signer.audit import AuditLog, scan
from acctpool_signer.journal import Journal

folder = sys.argv[1]
path = os.path.join(folder, "audit.log")
journal = Journal(os.path.join(folder, "journal.sqlite3"))
log = AuditLog(path, journal, scan(path), stream=open(os.devnull, "w"))
while os.path.getsize(path) < 300_000:
    log.write("derive", "ok", chain="anvil", store="shop", index=0, count=1)
size = os.path.getsize(path)
resource.setrlimit(resource.RLIMIT_FSIZE, (size + 100, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
result = {"size_before": size}
try:
    log.write("sign/fund", "ok", chain="anvil", store="shop", index=3, nonce=0, value_wei="4000000000000000",
              max_fee_per_gas_wei="60000000000", tx_hash="0x" + "ab" * 32, idempotency_key="f03-short-write")
    result["write"] = "returned without error"
except Exception as e:
    result["write"] = "raised " + type(e).__name__
result["size_after"] = os.path.getsize(path)
print(json.dumps(result))
"""


def test_short_write_of_the_kernel(env):
    """The proof test of F03 of review A, with the write of the AuditLog class in its own process."""
    from acctpool_signer.__main__ import build_state

    done = subprocess.run(
        [sys.executable, "-c", CHILD, env.paths.data],
        env={**os.environ, "PYTHONPATH": SIGNER_DIR},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    # The size limit is for all files of the process: the write of audit.log (a short write) or of the journal WAL
    # fails. Then audit.log has no part of the line; a line that is in the journal is written at the next start.
    assert result["write"] in ("raised AuditError", "raised OperationalError")
    assert result["size_after"] == result["size_before"]
    raw = open(env.paths.audit, "rb").read()
    assert raw.endswith(b"\n")
    assert b"f03-short-write" not in raw
    # the start after it is not refused
    state = build_state(env.paths)
    state.close()
    assert check_audit(env) == len(lines_of(env))


async def test_audit_verify_command(api, env):
    await traffic(api)
    api.close()
    done = run_cli(env, "audit-verify")
    assert done.returncode == 0, done.stderr
    assert "8 lines" in done.stdout
    original = lines_of(env)

    write_lines(env, original[:4] + original[5:])
    done = run_cli(env, "audit-verify")
    assert done.returncode == 1
    assert "line 5" in done.stderr
    assert done.stdout == ""

    write_lines(env, original[:-2])
    done = run_cli(env, "audit-verify")
    assert done.returncode == 1
    assert "do not agree" in done.stderr

    write_lines(env, original)
    assert run_cli(env, "audit-verify").returncode == 0
