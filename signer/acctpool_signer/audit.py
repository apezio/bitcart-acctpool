"""Hash-chained audit log (SPEC 3.5, 7.11): a line goes into the journal, then into audit.log (fsync) and stdout."""

import hashlib
import json
import logging
import os
import re
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import IO, Any

from .errors import AuditError
from .journal import Journal

log = logging.getLogger("acctpool_signer")

GENESIS = "0" * 64
# Always present (null when not applicable), then the optional fields that have a value, then prev and seq.
FIELDS = ("ts", "call", "result", "chain", "store", "index", "nonce", "value_wei", "amount", "max_fee_per_gas_wei",
          "tx_hash", "idempotency_key")  # fmt: skip
EXTRA_FIELDS = ("replaces", "replay", "count", "family", "token", "gas_limit", "seed_id", "config_sha256", "backend", "detail")
HIGHWATER_RE = re.compile(rb"(0|[1-9][0-9]{0,18}) ([0-9a-f]{64})\n?")  # "<seq> <sha256 of that line>"; "0 <zeros>": none


def line_hash(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


@dataclass
class Scan:
    seq: int = 0
    hash: str = GENESIS
    size: int = 0  # bytes of the complete lines
    part: bytes = b""  # a part of a line at the end (a crash, or the write of the running signer)
    marked: dict = field(default_factory=dict)  # seq -> hash, for the seqs that scan() was asked for
    mark: tuple[int, str] | None = None  # the high-water mark (check_highwater)
    tail: deque = field(default_factory=lambda: deque(maxlen=1000))


def scan(path: str, *want: int) -> Scan:
    """Check the chain of audit.log, one line at a time."""
    found = Scan()
    if not os.path.exists(path):
        return found
    with open(path, "rb") as f:
        for raw in f:
            if not raw.endswith(b"\n"):
                found.part = raw
                break
            try:
                entry = json.loads(raw)
                good = type(entry["seq"]) is int and entry["seq"] == found.seq + 1 and entry["prev"] == found.hash
            except (ValueError, KeyError, TypeError):
                good = False
            if not good:
                raise AuditError(
                    f"audit log: line {found.seq + 1} does not continue the chain (a line is changed, removed or added)"
                )
            found.seq, found.hash, found.size = found.seq + 1, line_hash(raw[:-1]), found.size + len(raw)
            if found.seq in want:
                found.marked[found.seq] = found.hash
            found.tail.append(raw[:-1])
    return found


def compare(line: str | None, found: Scan, ahead: bool = False) -> str | None:
    """None when audit.log has the journal's newest line (ahead: or has it and newer ones), the line when one behind."""
    entry = json.loads(line) if line is not None else {"seq": 0, "prev": None}
    digest = line_hash(line.encode()) if line is not None else GENESIS
    if (entry["seq"], digest) == (found.seq, found.hash) or (ahead and found.marked.get(entry["seq"]) == digest):
        return None
    if entry["seq"] == found.seq + 1 and entry["prev"] == found.hash:
        return line
    raise AuditError(
        f"the journal ends with audit line {entry['seq']} and audit.log with line {found.seq}, and they do not agree: "
        "the journal or the log is missing, older, changed, or of another signer. See README 'Recovery'"
    )


def verify(path: str, journal: Journal | None = None, highwater: str | None = None) -> Scan:
    """audit-verify, also while the signer writes: the journal line first, then one scan (chain and high-water mark),
    then the journal line again. A part of a line at the end is accepted only as the start of the journal's next line."""
    first = journal.get_meta("audit_line") if journal else None
    want = json.loads(first)["seq"] if first else 0
    found = check_highwater(highwater, path, want) if highwater else scan(path, want)
    last = journal.get_meta("audit_line") if journal else None
    seq = json.loads(last)["seq"] if last else 0
    writing = last is not None and seq == found.seq + 1 and (last + "\n").encode().startswith(found.part)
    if found.part and not writing:
        raise AuditError("audit log: the last line is not complete")
    if journal is not None:
        try:
            compare(first, found, ahead=True)
        except AuditError:
            compare(last, found)
        if seq < found.seq:  # the running signer commits the journal before it writes the file
            raise AuditError(f"the journal ends with audit line {seq} and audit.log with line {found.seq}: the journal "
                             "is older than audit.log (a restored or copied journal). See README 'Recovery'")  # fmt: skip
    return found


def check_highwater(path: str, audit_path: str, *want: int) -> Scan:
    """Scan audit.log; refuse it when it does not have the high-water line. No high-water file: no check."""
    try:
        with open(path, "rb") as f:
            match = HIGHWATER_RE.fullmatch(f.read(101))
    except FileNotFoundError:
        return scan(audit_path, *want)
    except OSError as e:
        raise AuditError(f"the high-water file {path} cannot be read ({type(e).__name__}: {e.strerror})") from None
    if match is None or (match.group(1) == b"0" and match.group(2) != GENESIS.encode()):
        raise AuditError(f"the high-water file {path} does not have the form '<seq> <sha256 of that audit line>'")
    mark = (int(match.group(1)), match.group(2).decode())
    found = scan(audit_path, mark[0], *want)
    found.mark = mark
    if mark[0] > found.seq:
        raise AuditError(
            f"audit.log ends with line {found.seq}, and the high-water file {path} has line {mark[0]}: the volume is older "
            "than the audit lines that the probe saw (a restore of a backup, or a new volume). See README 'Recovery'"
        )
    if mark[0] and found.marked.get(mark[0]) != mark[1]:
        raise AuditError(
            f"audit line {mark[0]} does not agree with the high-water file {path}: the log is of another signer, or it "
            "was changed. See README 'Recovery'"
        )
    return found


class AuditLog:
    def __init__(self, path: str, journal: Journal, found: Scan, stream: IO[str] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:  # fmt: skip
        self.now = now or (lambda: datetime.now(UTC))
        self._journal, self._stream = journal, stream
        compare(journal.get_meta("audit_line"), found)
        self._fd: int | None = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._file_seq, self._size = found.seq, found.size
        if os.fstat(self._fd).st_size != self._size:
            log.warning("audit log: a part of a line at the end is removed (a crash during a write)")
            os.ftruncate(self._fd, self._size)
        self.flush()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._fd = None

    def write(self, call: str, result: str, at: datetime | None = None, **fields: Any) -> None:
        """One line; `at` is the time of the request. In a journal transaction flush() comes after the commit."""
        if set(fields) - set(FIELDS) - set(EXTRA_FIELDS):
            raise ValueError("unknown audit field")
        self.flush()
        last = self._journal.get_meta("audit_line")
        entry = {name: fields.get(name) for name in FIELDS}
        entry.update(ts=(at or self.now()).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), call=call, result=result)
        entry.update({name: fields[name] for name in EXTRA_FIELDS if fields.get(name) is not None})
        entry["prev"] = line_hash(last.encode()) if last is not None else GENESIS
        entry["seq"] = json.loads(last)["seq"] + 1 if last is not None else 1
        self._journal.set_meta("audit_line", json.dumps(entry, separators=(",", ":")))
        if not self._journal.in_transaction:
            self.flush()

    def flush(self) -> None:
        """Append the newest line of the journal when audit.log does not have it. Raises AuditError when it cannot."""
        if self._fd is None:
            raise AuditError("audit log: the file is closed")
        line = self._journal.get_meta("audit_line")
        if line is None or json.loads(line)["seq"] <= self._file_seq:
            return
        data = line.encode("ascii") + b"\n"
        try:
            if os.write(self._fd, data) != len(data):
                raise OSError(0, "short write")
            os.fsync(self._fd)
        except OSError as e:
            try:  # the file must end with a complete line
                os.ftruncate(self._fd, self._size)
            except OSError:
                # the next line would go behind the part: a line that is not JSON in the middle of the file
                log.error("audit log: the file could not be cut back; the signer stops (the next start cuts it)")
                os._exit(1)
            raise AuditError(f"audit log: the write failed ({e.strerror}); the line stays in the journal") from None
        self._file_seq, self._size = self._file_seq + 1, self._size + len(data)
        try:  # stdout is the copy in the container log; a fault there does not stop the signer
            (self._stream or sys.stdout).write(line + "\n")
            (self._stream or sys.stdout).flush()
        except (OSError, ValueError) as e:
            log.error("audit log: the copy to stdout failed: %s", type(e).__name__)
