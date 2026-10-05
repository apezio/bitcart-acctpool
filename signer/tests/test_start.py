"""Start of the real process: refusals (SPEC 3.1), start without keystore, process hardening (SPEC 3.6)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import fund_request, sweep_native_request, sweep_request
from procs import SIGNER_DIR, environ, http, run_cli, running


def refused_start(env, **variables: str) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "acctpool_signer", "serve"],
        env={**environ(env), **variables},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 1, (done.returncode, done.stderr)
    assert "refused" in done.stderr
    assert "Traceback" not in done.stderr
    assert done.stdout == ""  # no audit line, no listener
    return done.stderr


def test_start_with_keystore(env):
    with running(env) as (port, process):
        status, text, _ = http(port, env.token, "GET", "/v1/status")
        assert status == 200
        body = json.loads(text)
        assert body["keystore"] is True
        assert body["fee_wallets"] == {"evm": env.fee_address}
    assert process.returncode == 0
    first = json.loads((env.root / "stdout.txt").read_text().splitlines()[0])
    assert (first["call"], first["result"], first["seed_id"], first["seq"]) == ("start", "ok", body["seed_id"], 1)
    assert first["config_sha256"] == body["config_sha256"]
    assert first["backend"] in ("coincurve", "native")
    assert f"signing backend: {first['backend']}" in (env.root / "stderr.txt").read_text()
    # the status calls of this test had the token: the start and the stop are the only audit lines
    lines = [json.loads(line) for line in (env.root / "stdout.txt").read_text().splitlines()]
    assert [(line["call"], line["result"], line["seq"]) for line in lines] == [("start", "ok", 1), ("stop", "ok", 2)]


def test_start_without_keystore_serves_status_only(env_no_keystore):
    env = env_no_keystore
    with running(env) as (port, _):
        status, text, _ = http(port, env.token, "GET", "/v1/status")
        assert status == 200
        assert json.loads(text)["keystore"] is False
        calls = [
            ("/v1/derive", {"store": "shop", "family": "evm", "first_index": 0, "count": 1}),
            ("/v1/sign/fund", fund_request("start-fund-1")),
            ("/v1/sign/sweep", sweep_request("start-sweep-1")),
            ("/v1/sign/sweep_native", sweep_native_request("start-native-1")),
        ]
        for path, body in calls:
            status, text, _ = http(port, env.token, "POST", path, body)
            assert status == 503
            assert json.loads(text)["error"] == "keystore_missing"
    assert not os.path.exists(env.paths.keystore)
    assert "no keystore" in (env.root / "stderr.txt").read_text()


def test_config_missing_or_invalid_refuses_the_start(env):
    good = Path(env.paths.config).read_text()
    os.unlink(env.paths.config)
    assert "config" in refused_start(env)
    for text in (
        "",
        "[signer",
        good.replace("account = 2", "account = 9000"),
        good.replace("chain_id = 137", "chain_id = 31337"),
    ):
        Path(env.paths.config).write_text(text)
        refused_start(env)


@pytest.mark.parametrize("size", [None, 0, 16, 31, 33, 64])
def test_master_key_missing_or_wrong_length_refuses_the_start(env, size):
    os.unlink(env.paths.master_key)
    if size is not None:
        Path(env.paths.master_key).write_bytes(os.urandom(size))
    assert "master key" in refused_start(env)


def test_master_key_is_necessary_without_keystore_too(env_no_keystore):
    os.unlink(env_no_keystore.paths.master_key)
    assert "master key" in refused_start(env_no_keystore)


@pytest.mark.parametrize(
    "token", [None, "", "\n", "t" * 31, "t" * 31 + "\n", "t" * 20 + " " + "t" * 20, "t" * 600, "tökén" * 8]
)
def test_token_missing_or_short_refuses_the_start(env, token):
    os.unlink(env.paths.token)
    if token is not None:
        Path(env.paths.token).write_text(token)
    message = refused_start(env)
    assert "token" in message
    if token and token.strip():
        assert token.strip() not in message


def test_keystore_that_fails_authentication_refuses_the_start(env):
    original = Path(env.paths.keystore).read_text()
    doc = json.loads(original)
    changes = {
        "ciphertext": lambda d: d["seeds"][0].update(ciphertext=flip(d["seeds"][0]["ciphertext"])),
        "tag": lambda d: d["seeds"][0].update(tag=flip(d["seeds"][0]["tag"])),
        "nonce": lambda d: d["seeds"][0].update(nonce=flip(d["seeds"][0]["nonce"])),
        "created": lambda d: d["seeds"][0].update(created="2001" + d["seeds"][0]["created"][4:]),
        "version": lambda d: d.update(version=2),
    }
    for name, change in changes.items():
        changed = json.loads(json.dumps(doc))
        change(changed)
        Path(env.paths.keystore).write_text(json.dumps(changed))
        message = refused_start(env)
        assert "keystore" in message, name
        assert env.master_key.hex() not in message
    Path(env.paths.keystore).write_text("")
    refused_start(env)
    # wrong master key
    Path(env.paths.keystore).write_text(original)
    Path(env.paths.master_key).write_bytes(os.urandom(32))
    assert "authentication failed" in refused_start(env)


@pytest.mark.parametrize("active", [0, 1])
def test_keystore_with_two_seeds_refuses_the_start(env, active):
    env.add_second_seed(active)
    assert "more than one seed" in refused_start(env)
    done = run_cli(env, "verify")
    assert (done.returncode, done.stdout) == (1, "")
    assert "more than one seed" in done.stderr


def test_wrong_signing_backend_refuses_the_start(env):
    """The image sets ACCTPOOL_SIGNING_BACKEND=coincurve: a fall back to the pure-Python backend stops the start."""
    from acctpool_signer.__main__ import signing_backend

    active = signing_backend()
    assert active in ("coincurve", "native")
    other = "native" if active == "coincurve" else "coincurve"
    message = refused_start(env, ACCTPOOL_SIGNING_BACKEND=other)
    assert "signing backend" in message
    with running(env, ACCTPOOL_SIGNING_BACKEND=active) as (port, _):
        status, _, _ = http(port, env.token, "GET", "/v1/status")
        assert status == 200
    # eth_keys takes the pure-Python backend when this variable of eth_keys names it; the guard sees that too
    if active == "coincurve":
        forced = {"ECC_BACKEND_CLASS": "eth_keys.backends.NativeECCBackend", "ACCTPOOL_SIGNING_BACKEND": "coincurve"}
        assert "signing backend" in refused_start(env, **forced)


def flip(hex_text: str) -> str:
    return f"{int(hex_text[0], 16) ^ 1:x}" + hex_text[1:]


def test_data_folder_that_is_not_writable_refuses_the_start(env):
    if os.getuid() == 0:
        pytest.skip("root can write everywhere")
    os.chmod(env.paths.data, 0o500)
    try:
        assert "data folder" in refused_start(env)
    finally:
        os.chmod(env.paths.data, 0o700)


def test_process_has_no_core_dump_and_is_not_dumpable(env):
    with running(env) as (_, process):
        limits = Path(f"/proc/{process.pid}/limits").read_text()
        core = next(line for line in limits.splitlines() if line.startswith("Max core file size"))
        assert core.split()[4:6] == ["0", "0"]
        # The files in /proc of a process that is not dumpable are owned by root: the same uid cannot read its memory.
        if os.getuid() != 0:
            assert os.stat(f"/proc/{process.pid}/mem").st_uid == 0
            with pytest.raises(PermissionError):
                open(f"/proc/{process.pid}/environ").read()
            with pytest.raises(PermissionError):
                open(f"/proc/{process.pid}/mem", "rb")


def test_harden_function():
    code = (
        "import ctypes, resource, os\n"
        "from acctpool_signer.hardening import harden\n"
        "libc = ctypes.CDLL(None)\n"
        "assert libc.prctl(3, 0, 0, 0, 0) == 1\n"
        "harden()\n"
        "assert libc.prctl(3, 0, 0, 0, 0) == 0\n"
        "assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)\n"
        "assert os.umask(0) == 0o077\n"
        "print('hardened')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": SIGNER_DIR}, capture_output=True, text=True
    )
    assert done.stdout == "hardened\n", done.stderr


def test_files_of_the_signer_are_for_the_owner_only(env):
    with running(env) as (port, _):
        http(port, env.token, "POST", "/v1/derive", {"store": "shop", "family": "evm", "first_index": 0, "count": 1})
    names = sorted(os.listdir(env.paths.data))
    assert names == ["audit.log", "journal.lock", "journal.sqlite3", "keystore.json"]
    for name in names:
        mode = os.stat(os.path.join(env.paths.data, name)).st_mode & 0o777
        assert mode == 0o600, (name, oct(mode))
