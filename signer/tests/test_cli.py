"""Commands init, restore, verify (SPEC 3.3), on a pseudo terminal as the operator uses them with "exec -it".

What these tests cannot see: the log of a container. The image test of scripts/test-signer.sh does that part
(the words are not in the container log and not in the journal of the host; "run ... init" is refused).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import helpers
import pytest
from conftest import make_env
from mnemonic import Mnemonic as ReferenceMnemonic
from procs import environ, fake_proc, run_cli, run_in_terminal

from acctpool_signer import __main__ as cli
from acctpool_signer import keystore

WORD_LINE = re.compile(r"\b(\d{1,2})\. ([a-z]+)")
PROMPT = re.compile(r"word (\d+) \(no echo\): ")


def words_in(shown: str) -> list[str]:
    return [word for _, word in WORD_LINE.findall(shown)]


def type_back(shown: str, prompts: int) -> str:
    """The answer of a careful operator: the word with the number of the newest prompt."""
    words = words_in(shown)
    number = int(PROMPT.findall(shown)[prompts - 1])
    return words[number - 1]


def public_values(words: str) -> tuple[str, str]:
    """(seed id, fee wallet address) of a seed, by the independent calculation."""
    fee_address = helpers.address_from_key(helpers.bip32_derive(helpers.bip39_seed(words), helpers.FEE_PATH))
    return hashlib.sha256(fee_address.encode()).hexdigest()[:16], fee_address


def parts_of(words: str) -> list[str]:
    """Runs of three words: one word can be in a message by chance, three in the right order cannot."""
    w = words.split()
    return [" ".join(w[i : i + 3]) for i in range(len(w) - 2)]


def test_init_shows_the_words_on_the_terminal_and_writes_the_keystore_after_the_confirmation(env_no_keystore):
    env = env_no_keystore
    code, shown = run_in_terminal(env, "init", answer=type_back)
    assert code == 0, shown
    words = words_in(shown)
    assert len(words) == 24
    assert [int(number) for number, _ in WORD_LINE.findall(shown)] == list(range(1, 25))
    phrase = " ".join(words)
    assert ReferenceMnemonic("english").check(phrase)
    # 3 words were asked for, each with its number, and what was typed is not shown
    asked = PROMPT.findall(shown)
    assert len(asked) == 3
    assert len(set(asked)) == 3
    # after each question the terminal shows nothing of the answer: white space only, then the next question
    pieces = re.split(r"word \d+ \(no echo\): ", shown[shown.index("Type 3 of the words") :])
    assert len(pieces) == 4
    assert [piece.strip() for piece in pieces[1:3]] == ["", ""]
    assert pieces[3].lstrip().startswith("\x1b[3J")

    seed_id, fee_address = public_values(phrase)
    assert f"seed id: {seed_id}" in shown
    assert f"fee wallet (evm): {fee_address}" in shown
    loaded = keystore.load(env.paths.keystore, env.master_key)
    assert loaded.active == seed_id
    assert loaded.wallet.fee_address == fee_address
    assert loaded.wallet.deposit_key(1, 0) == helpers.bip32_derive(helpers.bip39_seed(phrase), helpers.deposit_path(1, 0))

    entropy = ReferenceMnemonic("english").to_entropy(phrase)
    assert bytes(entropy).hex() not in shown.lower()
    # the files of the data folder have no word and no entropy
    for name in os.listdir(env.paths.data):
        raw = (Path(env.paths.data) / name).read_bytes()
        assert bytes(entropy) not in raw
        assert bytes(entropy).hex().encode() not in raw.lower()
        assert not any(part.encode() in raw for part in parts_of(phrase))
    assert os.stat(env.paths.keystore).st_mode & 0o777 == 0o600
    assert os.listdir(env.paths.data) == ["keystore.json"]


def test_two_inits_give_different_seeds(tmp_path):
    seeds = set()
    for name in ("a", "b", "c"):
        env = make_env(tmp_path / name, keystore=False)
        code, shown = run_in_terminal(env, "init", answer=type_back)
        assert code == 0
        seeds.add(" ".join(words_in(shown)))
    assert len(seeds) == 3


def test_init_writes_nothing_when_a_word_is_wrong(env_no_keystore):
    env = env_no_keystore

    def second_is_wrong(shown: str, prompts: int) -> str:
        return type_back(shown, prompts) if prompts != 2 else "notaword"

    code, shown = run_in_terminal(env, "init", answer=second_is_wrong)
    assert code == 1
    assert "nothing is written" in shown
    assert len(PROMPT.findall(shown)) == 2  # no third question
    assert os.listdir(env.paths.data) == []


def test_init_writes_nothing_when_the_terminal_goes_away(env_no_keystore):
    """F09 of review A: the words are shown, and the connection is lost before the operator typed a word."""
    env = env_no_keystore
    code, shown = run_in_terminal(env, "init", close_at=b"word ")
    assert len(words_in(shown)) == 24
    assert code != 0
    assert os.listdir(env.paths.data) == []


def test_init_writes_nothing_when_the_words_cannot_be_shown(env_no_keystore, monkeypatch):
    """The proof test of F09: the terminal is gone at the time of the first line."""
    import builtins

    env = env_no_keystore

    def terminal_is_gone(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "print", terminal_is_gone)
    with pytest.raises(OSError):
        cli.cmd_init(env.paths)
    monkeypatch.undo()
    assert not os.path.exists(env.paths.keystore), "a keystore exists for a seed that nobody has seen"
    assert os.listdir(env.paths.data) == []


def test_init_refuses_when_a_keystore_exists(env):
    before = Path(env.paths.keystore).read_bytes()
    code, shown = run_in_terminal(env, "init", answer=type_back)
    assert code == 1
    assert "refused" in shown
    assert words_in(shown) == []
    assert Path(env.paths.keystore).read_bytes() == before


def test_init_refuses_without_a_terminal(env_no_keystore):
    env = env_no_keystore
    done = run_cli(env, "init")
    assert done.returncode == 1
    assert "terminal" in done.stderr
    assert done.stdout == ""
    assert not os.path.exists(env.paths.keystore)
    assert os.listdir(env.paths.data) == []


# /proc/1/cmdline and the cgroup files as podman 5.8 showed them on the test host (review B, M2). The image
# test of scripts/test-signer.sh does the same cases with real containers.
SERVE = b"python\0-I\0-u\0-m\0acctpool_signer\0serve\0"
SAME = (b"0::/\n", b"0::/\n")
OTHER_CONTAINER = (b"0::/../../libpod-618b1a9d3708.scope/container\n", b"0::/\n")
NOT_EXEC = {
    "run --init: process 1 is the init program": (b"/run/podman-init\0--\0python\0-I\0-m\0acctpool_signer\0init\0", SAME),
    "pod with a shared PID namespace: process 1 is the pause program": (b"/catatonit\0-P\0", OTHER_CONTAINER),
    "--pid=host: process 1 is the init of the host": (
        b"/usr/lib/systemd/systemd\0--switched-root\0",
        (b"0::/../init.scope\n", b"0::/\n"),
    ),
    "--pid=container:<signer>: process 1 is the signer of another container": (SERVE, OTHER_CONTAINER),
    "process 1 is the signer without isolated mode": (b"python\0-u\0-m\0acctpool_signer\0serve\0", SAME),
    "process 1 is another command of the signer": (b"python\0-I\0-u\0-m\0acctpool_signer\0verify\0", SAME),
}


def not_process_1(monkeypatch) -> None:
    # pytest is process 1 of the test container (the shell of the test run starts it with exec)
    monkeypatch.setattr(cli.os, "getpid", lambda: 25)


def refused_in_process(env, monkeypatch, command: str) -> str:
    """cli.main() of the command: it must refuse before it shows or reads a word. Gives all that it printed."""
    shown = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: shown.append(" ".join(str(a) for a in args)))
    for name, value in {"ACCTPOOL_MASTER_KEY_FILE": env.paths.master_key, "ACCTPOOL_DATA": env.paths.data}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    assert cli.main([command]) == 1
    text = "\n".join(shown)
    assert "must not be the main process of a container" in text
    assert f"exec -it <container> python -I -m acctpool_signer {command}" in text
    assert words_in(text) == []
    assert os.listdir(env.paths.data) == []
    return text


@pytest.mark.parametrize("command", ["init", "restore"])
@pytest.mark.parametrize("case", list(NOT_EXEC))
def test_init_and_restore_are_refused_when_process_1_is_not_the_signer_of_this_container(
    env_no_keystore, monkeypatch, tmp_path, command, case
):
    """F01 of review A and M2 of review B: the container log keeps all that the main process shows, also with a
    terminal (run -it), and the main process is not always process 1 (an init program, a shared PID namespace)."""
    first, cgroups = NOT_EXEC[case]
    not_process_1(monkeypatch)
    monkeypatch.setattr(cli, "PROC", fake_proc(tmp_path / "proc", first, cgroups))
    text = refused_in_process(env_no_keystore, monkeypatch, command)
    assert "process 1 is" in text


@pytest.mark.parametrize("command", ["init", "restore"])
def test_init_and_restore_are_refused_as_process_1(env_no_keystore, monkeypatch, tmp_path, command):
    # "run -t ... init": the command is process 1. The only case that needs a changed pid (the image test runs it).
    monkeypatch.setattr(cli, "PROC", fake_proc(tmp_path / "proc"))
    monkeypatch.setattr(cli.os, "getpid", lambda: 1)
    assert "this process is process 1" in refused_in_process(env_no_keystore, monkeypatch, command)


@pytest.mark.parametrize("command", ["init", "restore"])
def test_init_and_restore_are_refused_when_proc_cannot_be_read(env_no_keystore, monkeypatch, tmp_path, command):
    not_process_1(monkeypatch)
    monkeypatch.setattr(cli, "PROC", str(tmp_path / "no-proc"))
    assert "process 1 cannot be read" in refused_in_process(env_no_keystore, monkeypatch, command)


@pytest.mark.parametrize("command", ["init", "restore"])
def test_init_and_restore_are_refused_in_the_test_container_with_the_real_proc(env_no_keystore, command):
    """No change of the code: the real /proc of the test container, where process 1 is pytest. The command is a child."""
    env = env_no_keystore
    assert open("/proc/1/cmdline", "rb").read().split(b"\0")[:6] != SERVE.split(b"\0")[:6]
    done = subprocess.run(
        [sys.executable, "-m", "acctpool_signer", command],
        env=environ(env),
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert done.returncode == 1
    assert "must not be the main process of a container (process 1 is not the signer service of this image)" in done.stderr
    assert words_in(done.stdout + done.stderr) == []
    assert os.listdir(env.paths.data) == []


def test_a_process_from_exec_in_the_signer_container_is_accepted(monkeypatch, tmp_path):
    # the files that "exec" into a running signer gives: process 1 is "serve", the same cgroup
    not_process_1(monkeypatch)
    monkeypatch.setattr(cli, "PROC", fake_proc(tmp_path / "proc", SERVE, SAME))
    cli.refuse_main_process("init")
    # and the same with the cgroup paths of cgroup v1 or of a host cgroup namespace: equal paths
    monkeypatch.setattr(cli, "PROC", fake_proc(tmp_path / "proc2", SERVE, (b"0::/system.slice/docker-1.scope\n",) * 2))
    cli.refuse_main_process("restore")


def test_serve_command_is_the_command_of_the_image():
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    assert f"CMD {json.dumps(list(cli.SERVE_COMMAND))}" in dockerfile


@pytest.mark.parametrize("command", ["init", "restore", "verify"])
def test_commands_refuse_without_a_master_key(env_no_keystore, command):
    env = env_no_keystore
    Path(env.paths.master_key).write_bytes(os.urandom(31))
    code, shown = run_in_terminal(env, command, answer=type_back)
    assert code == 1
    assert "master key" in shown
    assert words_in(shown) == []
    assert not os.path.exists(env.paths.keystore)


def test_restore_gives_the_same_seed_id_and_addresses(env, tmp_path):
    """PLAN 12.7: a new signer from the offline words gives the same addresses."""
    new = make_env(tmp_path / "new-host", keystore=False)
    assert new.master_key != env.master_key
    code, shown = run_in_terminal(new, "restore", typed=env.words)
    assert code == 0, shown
    seed_id, fee_address = public_values(env.words)
    assert f"seed id: {seed_id}" in shown
    assert f"fee wallet (evm): {fee_address}" in shown
    # no echo: the terminal did not show what was typed
    assert not any(part in shown for part in parts_of(env.words))
    assert [word for word in env.words.split() if len(word) > 5 and word in shown] == []

    old = keystore.load(env.paths.keystore, env.master_key)
    restored = keystore.load(new.paths.keystore, new.master_key)
    assert restored.active == old.active == seed_id
    for account, index in [(1, 0), (1, 1), (1, 199), (2, 0), (2, 5)]:
        assert restored.wallet.deposit_address(account, index) == old.wallet.deposit_address(account, index)
    assert restored.wallet.deposit_address(1, 7) == env.address("shop", 7)
    assert restored.wallet.fee_address == env.fee_address
    raw = Path(new.paths.keystore).read_bytes()
    assert env.entropy.hex().encode() not in raw
    assert not any(part.encode() in raw for part in parts_of(env.words))


def test_restore_accepts_upper_case_and_more_spaces(env, tmp_path):
    new = make_env(tmp_path / "new-host", keystore=False)
    code, shown = run_in_terminal(new, "restore", typed="  " + env.words.upper().replace(" ", "   ") + "  ")
    assert code == 0, shown
    assert keystore.load(new.paths.keystore, new.master_key).wallet.fee_address == env.fee_address


def test_restore_refuses_wrong_words(env, tmp_path):
    words = env.words.split()
    other = "zoo" if words[-1] != "zoo" else "abandon"
    cases = {
        "23 words": words[:23],
        "25 words": [*words, "abandon"],
        "12 words": words[:12],
        "no words": [],
        "unknown word": [*words[:7], "bitcart", *words[8:]],
        "two words exchanged": [words[1], words[0], *words[2:]] if words[0] != words[1] else [*words[:-1], other],
    }
    for name, typed in cases.items():
        new = make_env(tmp_path / name.replace(" ", "-"), keystore=False)
        code, shown = run_in_terminal(new, "restore", typed=" ".join(typed))
        if code == 0:
            # the checksum has 8 bits: 1 of 256 wrong inputs is a valid seed, a different one
            assert name == "two words exchanged"
            assert keystore.load(new.paths.keystore, new.master_key).wallet.fee_address != env.fee_address
            continue
        assert code == 1, name
        assert "not accepted" in shown
        assert not os.path.exists(new.paths.keystore)
        assert not any(part in shown for part in parts_of(env.words))


def test_restore_refuses_when_a_keystore_exists(env):
    before = Path(env.paths.keystore).read_bytes()
    code, shown = run_in_terminal(env, "restore", typed=env.words)
    assert code == 1
    assert "refused" in shown
    assert "no echo" not in shown  # it did not ask for the words
    assert Path(env.paths.keystore).read_bytes() == before


def test_restore_refuses_without_a_terminal(env_no_keystore):
    env = env_no_keystore
    done = run_cli(env, "restore")
    assert done.returncode == 1
    assert "terminal" in done.stderr
    assert not os.path.exists(env.paths.keystore)


def test_verify_shows_public_values_only(env):
    done = run_cli(env, "verify")
    assert done.returncode == 0, done.stderr
    seed_id, fee_address = public_values(env.words)
    assert done.stdout == f"seed id: {seed_id}\nfee wallet (evm): {fee_address}\n"
    assert done.stderr == ""


def test_verify_refuses_a_wrong_master_key_and_a_changed_keystore(env, env_no_keystore):
    done = run_cli(env_no_keystore, "verify")
    assert done.returncode == 1
    assert "keystore" in done.stderr

    doc = json.loads(Path(env.paths.keystore).read_text())
    doc["seeds"][0]["created"] = "2001" + doc["seeds"][0]["created"][4:]
    good = Path(env.paths.keystore).read_text()
    Path(env.paths.keystore).write_text(json.dumps(doc))
    done = run_cli(env, "verify")
    assert (done.returncode, done.stdout) == (1, "")
    assert "authentication failed" in done.stderr

    Path(env.paths.keystore).write_text(good)
    Path(env.paths.master_key).write_bytes(os.urandom(32))
    done = run_cli(env, "verify")
    assert (done.returncode, done.stdout) == (1, "")
    assert "authentication failed" in done.stderr


@pytest.mark.parametrize("command", ["export", "show", "dump", "sign", "words", ""])
def test_there_is_no_command_that_shows_a_stored_seed(env, command):
    done = run_cli(env, command)
    assert done.returncode == 2
    assert done.stdout == ""
    assert not any(part in done.stderr for part in parts_of(env.words))
