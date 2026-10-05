"""SQLite journal, the memory of the rules (public data; one writer). meta `audit_line`: the newest audit line."""

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import JournalError

SCHEMA_VERSION = "v4"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS derived (store TEXT PRIMARY KEY, highest_index INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS signatures (
    idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, kind TEXT NOT NULL, chain TEXT NOT NULL,
    nonce INTEGER NOT NULL, from_address TEXT NOT NULL, to_address TEXT NOT NULL, value_wei TEXT NOT NULL,
    max_fee_per_gas_wei TEXT NOT NULL, fee_wei TEXT NOT NULL, spend_delta_wei TEXT NOT NULL, utc_day TEXT NOT NULL,
    request TEXT NOT NULL, response TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS signatures_nonce ON signatures (chain, from_address, nonce);
CREATE INDEX IF NOT EXISTS signatures_to ON signatures (chain, to_address);
CREATE INDEX IF NOT EXISTS signatures_day ON signatures (chain, utc_day);
"""
# value_wei: native coin that moves (0 for a token sweep). fee_wei: gas_limit x max_fee_per_gas_wei.
COLUMNS = ("idempotency_key", "request_hash", "kind", "chain", "nonce", "from_address", "to_address", "value_wei",
           "max_fee_per_gas_wei", "fee_wei", "spend_delta_wei", "utc_day", "request", "response")  # fmt: skip


class Journal:
    def __init__(self, path: str, readonly: bool = False) -> None:
        """readonly (audit-verify): no lock, no schema change."""
        self._lock: int | None = None
        self._depth = 0
        if not readonly:
            lock = os.path.join(os.path.dirname(os.path.abspath(path)), "journal.lock")
            self._lock = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the close of the fd releases it
            except OSError:
                self.close()
                raise JournalError(f"another signer process has the journal open (lock file {lock})") from None
        try:
            self._db = sqlite3.connect(path, isolation_level=None, timeout=5)  # transaction(): BEGIN IMMEDIATE
            self._db.row_factory = sqlite3.Row
            if not readonly:
                self._db.execute("PRAGMA journal_mode = WAL")  # a reader (backup) does not stop a write
                self._db.execute("PRAGMA synchronous = FULL")
                self._db.executescript(SCHEMA)
                if self.get_meta("schema") not in (None, SCHEMA_VERSION):
                    raise JournalError(f"the journal has schema {self.get_meta('schema')}; this signer knows {SCHEMA_VERSION}")
                self.set_meta("schema", SCHEMA_VERSION)
        except sqlite3.DatabaseError as e:
            self.close()
            raise JournalError(f"the journal {path} cannot be opened: {type(e).__name__}") from None
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if hasattr(self, "_db"):
            self._db.close()
        if self._lock is not None:
            os.close(self._lock)
        self._lock = None

    def check_seed(self, seed_id: str) -> None:
        """The journal belongs to one seed (SPEC 7.10): the rules of another seed are not the rules of this one."""
        recorded = self.get_meta("seed_id")
        if recorded is None:
            self.set_meta("seed_id", seed_id)
        elif recorded != seed_id:
            raise JournalError(
                f"the journal is of the seed {recorded}, the keystore has the seed {seed_id}: they are of two signers. "
                "Put the volume of this seed in place; see README 'Recovery'"
            )

    in_transaction = property(lambda self: self._depth > 0)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All checks and the insert of one request are in one transaction. Nested use joins the outer one."""
        if self._depth:
            yield
            return
        self._db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:  # also after a failed COMMIT
                self._db.execute("ROLLBACK")
            raise
        finally:
            self._depth = 0

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction():
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def highest_index(self, store: str) -> int | None:
        row = self._db.execute("SELECT highest_index FROM derived WHERE store = ?", (store,)).fetchone()
        return None if row is None else row["highest_index"]

    def record_derived(self, store: str, highest: int) -> None:
        with self.transaction():
            self._db.execute(
                "INSERT INTO derived VALUES (?, ?) ON CONFLICT (store) DO UPDATE SET highest_index = "
                "max(highest_index, excluded.highest_index)",
                (store, highest),
            )

    def get_signature(self, idempotency_key: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM signatures WHERE idempotency_key = ?", (idempotency_key,)).fetchone()

    def nonce_group(self, chain: str, from_address: str, nonce: int) -> list[sqlite3.Row]:
        """All signatures for one (chain, from, nonce). At most one of them can ever be in a block."""
        query = "SELECT * FROM signatures WHERE chain = ? AND from_address = ? AND nonce = ?"
        return self._db.execute(query, (chain, from_address, nonce)).fetchall()

    def fund_total(self, chain: str, to_address: str) -> int:
        """Funding value signed for one address. A replacement has the nonce of its original and counts once."""
        query = "SELECT nonce, value_wei FROM signatures WHERE kind = 'fund' AND chain = ? AND to_address = ?"
        per_nonce: dict[int, int] = {}
        for row in self._db.execute(query, (chain, to_address)):
            per_nonce[row["nonce"]] = max(per_nonce.get(row["nonce"], 0), int(row["value_wei"]))
        return sum(per_nonce.values())

    def sweep_budget(self, chain: str, address: str) -> int:
        """Fee that the token sweeps of an address may use (SPEC 7.8, 7.9). One kind + nonce counts only its highest."""
        rows = self._db.execute(
            "SELECT kind, nonce, value_wei, fee_wei FROM signatures WHERE chain = ? AND ((kind = 'fund' AND to_address = ?) "
            "OR (kind != 'fund' AND from_address = ?)) ORDER BY rowid",
            (chain, address, address),
        ).fetchall()
        budget, highest = 0, {}
        for row in rows:
            size = int(row["value_wei"]) + (0 if row["kind"] == "fund" else int(row["fee_wei"]))
            group = (row["kind"], row["nonce"])
            size, highest[group] = max(0, size - highest.get(group, 0)), max(size, highest.get(group, 0))
            if row["kind"] == "fund":
                budget += size
            elif row["kind"] == "sweep":
                budget -= size
            else:
                budget = min(budget, max(0, budget - size))
        return budget

    def daily_spend(self, chain: str, utc_day: str) -> int:
        query = "SELECT spend_delta_wei FROM signatures WHERE kind = 'fund' AND chain = ? AND utc_day = ?"
        return sum(int(row["spend_delta_wei"]) for row in self._db.execute(query, (chain, utc_day)))

    def insert_signature(self, *values: object) -> None:
        with self.transaction():
            query = f"INSERT INTO signatures ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})"  # noqa: S608
            self._db.execute(query, values)
