"""Command line (serve, init, restore, verify, audit-verify; SPEC 3.3, 3.5) and the start of the service (SPEC 3.1)."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import secrets
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiohttp import web

from . import audit, hd
from . import keystore as keystore_mod
from .api import LIMITED, State, create_app
from .audit import AuditLog, check_highwater
from .config import load_config
from .errors import StartError
from .hardening import harden, lock_memory
from .journal import Journal

log = logging.getLogger("acctpool_signer")


@dataclass(frozen=True)
class Paths:
    config: str
    master_key: str
    token: str
    data: str
    highwater: str  # the health probe writes it; read-only mount (SPEC 7.11)
    keystore = property(lambda self: os.path.join(self.data, "keystore.json"))
    journal = property(lambda self: os.path.join(self.data, "journal.sqlite3"))
    audit = property(lambda self: os.path.join(self.data, "audit.log"))


def paths_from_env() -> Paths:
    names = {"CONFIG": "/etc/acctpool/pools.toml", "MASTER_KEY_FILE": "/run/acctpool/master.key", "DATA": "/data",
             "TOKEN_FILE": "/run/acctpool/signer.token", "HIGHWATER_FILE": "/run/acctpool/audit.highwater"}  # fmt: skip
    config, master_key, data, token, highwater = (os.environ.get(f"ACCTPOOL_{n}", d) for n, d in names.items())
    return Paths(config, master_key, token, data, highwater)


def read_token(path: str) -> str:
    try:
        with open(path, "rb") as f:
            token = f.read(514).decode("ascii").strip()
    except OSError as e:
        raise StartError(f"cannot read the token file {path}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise StartError(f"the token file {path} must be ASCII text") from None
    if not 32 <= len(token) <= 512 or not token.isprintable() or " " in token:
        raise StartError(f"the token in {path} must have 32 to 512 characters, without spaces")
    return token


def signing_backend() -> str:
    """The ECDSA backend of eth_keys: "coincurve" (libsecp256k1) or "native" (pure Python)."""
    from eth_keys import keys

    return type(keys.backend).__name__.removesuffix("ECCBackend").lower()


def build_state(paths: Paths, now: Any = None, monotonic: Any = None) -> State:
    """Everything that can refuse the start; no socket is open before it returns. now, monotonic: test clocks."""
    config = load_config(paths.config)
    # eth_keys falls back to pure Python without a message; the image makes that a refusal
    backend, wanted = signing_backend(), os.environ.get("ACCTPOOL_SIGNING_BACKEND", "")
    if wanted and wanted != backend:
        raise StartError(f"the signing backend is {backend!r}, ACCTPOOL_SIGNING_BACKEND needs {wanted!r}")
    master_key, token = keystore_mod.read_master_key(paths.master_key), read_token(paths.token)
    keystore = keystore_mod.load(paths.keystore, master_key) if os.path.lexists(paths.keystore) else None
    fee = keystore.wallet.fee_address if keystore else None
    for store in config.stores.values():  # a destination that is the fee wallet: a sweep would pay the payer
        for kind in ("destinations", "native_destinations"):
            for chain in (chain for chain, address in getattr(store, kind).items() if address == fee):
                raise StartError(f"stores.{store.name}.{kind}.{chain}: is the fee wallet of this signer")
    if not os.path.isdir(paths.data) or not os.access(paths.data, os.W_OK):
        raise StartError(f"the data folder {paths.data} is not a writable folder")
    journal = Journal(paths.journal)
    try:
        if keystore is not None:
            journal.check_seed(keystore.active)
        found = check_highwater(paths.highwater, paths.audit)
        audit = AuditLog(paths.audit, journal, found, now=now)
    except BaseException:
        journal.close()
        raise
    clocks = {name: value for name, value in (("now", now), ("monotonic", monotonic)) if value is not None}
    state = State(config=config, token=token, keystore=keystore, journal=journal, audit=audit, **clocks)
    # the rate limits after a restart, from the accepted calls of the last 60 seconds
    for line in found.tail:
        entry = json.loads(line)
        age = (state.now() - datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%S.%f%z")).total_seconds()
        if entry["call"] in LIMITED and entry["result"] != "rate_limited" and 0 <= age < 60:
            state.limits[LIMITED[entry["call"]]].times.append(state.monotonic() - age)
    return state


async def run(state: State) -> None:
    quiet = logging.getLogger("acctpool_signer.http")
    quiet.disabled = True  # aiohttp would log the bytes of a bad request; guard() logs one line per request
    runner = web.AppRunner(create_app(state), access_log=None, logger=quiet, auto_decompress=False, keepalive_timeout=30)
    await runner.setup()
    await web.TCPSite(runner, state.config.listen_host, state.config.listen_port, backlog=64, reuse_address=True).start()
    log.info("listening on %s:%d", state.config.listen_host, state.config.listen_port)
    stop = asyncio.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(number, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        for limit in state.limits.values():
            limit.report(state.monotonic(), stop=True)
        try:
            state.audit.write("stop", "ok")
        except Exception as e:
            log.error("audit line 'stop' not written: %s", type(e).__name__)
        state.close()


def serve(paths: Paths) -> None:
    locked = lock_memory()
    state = build_state(paths)
    if state.keystore is None:
        log.warning("no keystore at %s: only /v1/status is served", paths.keystore)
    if not locked:
        log.warning("the memory is not locked (RLIMIT_MEMLOCK has a limit): the seed can go to the swap space")
    log.info("signing backend: %s", signing_backend())
    seed_id = state.keystore.active if state.keystore else None
    state.audit.write("start", "ok", seed_id=seed_id, config_sha256=state.config.sha256, backend=signing_backend())
    asyncio.run(run(state))


SERVE_COMMAND = ("python", "-I", "-u", "-m", "acctpool_signer", "serve")
PROC = "/proc"
CLEAR_SCREEN = "\x1b[3J\x1b[H\x1b[2J"  # with the lines that the terminal keeps above the screen


def _summary(store: keystore_mod.Keystore) -> str:
    return f"seed id: {store.active}\nfee wallet (evm): {store.wallet.fee_address}"


def refuse_main_process(command: str) -> None:
    """init and restore show or read the seed words; the log keeps what a container's main process shows (not always
    process 1). Accepted only: not process 1, and process 1 is the image's "serve" command in this process's cgroup."""
    try:
        read = {name: open(os.path.join(PROC, name), "rb").read(4096) for name in ("1/cmdline", "1/cgroup", "self/cgroup")}  # noqa: SIM115
        first = tuple(part.decode("utf-8", "replace") for part in read["1/cmdline"].split(b"\0") if part)
        reason = "process 1 is in another container" if read["1/cgroup"] != read["self/cgroup"] else None
        reason = "process 1 is not the signer service of this image" if first != SERVE_COMMAND else reason
    except OSError as e:
        reason = f"process 1 cannot be read ({type(e).__name__})"
    reason = "this process is process 1" if os.getpid() == 1 else reason
    if reason is not None:
        raise StartError(
            f"{command} must not be the main process of a container ({reason}): the container log would keep the seed "
            f"words. Start the service, then: docker exec -it <container> python -I -m acctpool_signer {command}"
        )


def _new_keystore(paths: Paths, what: str) -> bytes:
    master_key = keystore_mod.read_master_key(paths.master_key)
    if os.path.lexists(paths.keystore):
        raise StartError(f"a keystore exists at {paths.keystore}; {what} does not replace it")
    return master_key


def cmd_init(paths: Paths) -> int:
    master_key = _new_keystore(paths, "init")
    if not sys.stdout.isatty():
        raise StartError("init shows the seed words one time; start it on a terminal (docker exec -it)")
    entropy = os.urandom(hd.ENTROPY_BYTES)
    words = hd.entropy_to_words(entropy).split()
    if hd.words_to_entropy(" ".join(words)) != entropy:
        raise StartError("self-check of the word encoding failed; nothing is written")
    # words first: if the terminal goes away, no keystore exists for a seed that nobody has
    print("Write these 24 words down now. They are shown this one time and are the only recovery path.")
    print("Do this only on a terminal that is not recorded.\n")
    for row in range(0, hd.WORD_COUNT, 4):
        print("   ".join(f"{i + 1:2d}. {words[i]:<10}" for i in range(row, row + 4)))
    if not sys.stdin.isatty():
        raise StartError("no terminal to type the words back; nothing is written")
    print("\nType 3 of the words, to show that you have them. The keystore is written after that.")
    for position in sorted(secrets.SystemRandom().sample(range(len(words)), 3)):
        if getpass.getpass(f"word {position + 1} (no echo): ").strip().lower() != words[position]:
            raise StartError("that is not the word; nothing is written. Start init again: it makes a new seed")
    store = keystore_mod.create(paths.keystore, master_key, entropy)
    print(CLEAR_SCREEN + "The keystore is written. Start the signer again; it reads the keystore at the start.\n")
    print(_summary(store))
    return 0


def cmd_restore(paths: Paths) -> int:
    master_key = _new_keystore(paths, "restore")
    if not sys.stdin.isatty():  # a pipe would mean that the words are in a file or in a shell history
        raise StartError("restore reads the seed words from a terminal only (docker exec -it)")
    try:
        entropy = hd.words_to_entropy(getpass.getpass("24 seed words (no echo): "))
    except hd.SeedError as e:
        raise StartError(f"the words are not accepted: {e}") from None
    print(_summary(keystore_mod.create(paths.keystore, master_key, entropy)))
    return 0


def cmd_verify(paths: Paths) -> int:
    print(_summary(keystore_mod.load(paths.keystore, keystore_mod.read_master_key(paths.master_key))))
    return 0


def cmd_audit_verify(paths: Paths) -> int:
    journal = Journal(paths.journal, readonly=True) if os.path.exists(paths.journal) else None
    try:
        found = audit.verify(paths.audit, journal, paths.highwater)  # the probe runs this command, also while serving
    finally:
        if journal is not None:
            journal.close()
    anchored = "the journal agrees" if journal else "no journal, the end of the log is not checked"
    compared = "no high-water file" if found.mark is None else f"the log has line {found.mark[0]} of the high-water file"
    print(f"audit log ok: {found.seq} lines, chain complete, {anchored}; {compared}")
    return 0


COMMANDS = {"serve": lambda paths: serve(paths) or 0, "init": cmd_init, "restore": cmd_restore, "verify": cmd_verify,
            "audit-verify": cmd_audit_verify}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m acctpool_signer", description="acctpool signer")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        harden()
        if args.command in ("init", "restore"):
            refuse_main_process(args.command)
        return COMMANDS[args.command](paths_from_env())
    except StartError as e:
        print(f"acctpool-signer: refused: {e}", file=sys.stderr)
    except OSError as e:
        print(f"acctpool-signer: refused: {type(e).__name__}: {e.strerror}", file=sys.stderr)
    except Exception as e:  # no traceback and no exception text: the text of a library error can have key material
        print(f"acctpool-signer: stopped: {type(e).__name__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
