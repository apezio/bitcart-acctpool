"""The signer as a real process, for the tests that need one."""

import json
import os
import pty
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from conftest import CONFIG

SIGNER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def environ(env) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": SIGNER_DIR,
        "ACCTPOOL_CONFIG": env.paths.config,
        "ACCTPOOL_MASTER_KEY_FILE": env.paths.master_key,
        "ACCTPOOL_TOKEN_FILE": env.paths.token,
        "ACCTPOOL_DATA": env.paths.data,
        "ACCTPOOL_HIGHWATER_FILE": env.paths.highwater,
    }


def http(port: int, token: str | None, method: str, path: str, body=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        headers=headers,
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode(), dict(response.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


@contextmanager
def running(env, config: str = CONFIG, preexec_fn=None, **variables: str):
    """The signer as a real process. Gives (port, process); stdout and stderr go to files in env.root."""
    port = free_port()
    env.write_config(config, port)
    out, err = open(env.root / "stdout.txt", "ab"), open(env.root / "stderr.txt", "ab")
    process = subprocess.Popen(
        [sys.executable, "-m", "acctpool_signer", "serve"],
        env={**environ(env), **variables},
        stdout=out,
        stderr=err,
        preexec_fn=preexec_fn,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError(f"the signer stopped: {(env.root / 'stderr.txt').read_text()}")
            try:
                http(port, env.token, "GET", "/v1/status")
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        else:
            raise AssertionError("the signer did not start")
        yield port, process
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=20)
        out.close()
        err.close()


# The commands init and restore run only through "exec" in the container of a running signer: process 1 must be
# the "serve" command of the image, in the same cgroup (__main__.refuse_main_process). In the test container
# process 1 is the shell of the test run. The commands of these tests read a small /proc tree of the test instead,
# where process 1 is the signer: this program sets it and runs the command. The check itself is tested with the
# real /proc and with other trees in test_cli.py, and with real containers in the image test.
LAUNCHER = "import sys; from acctpool_signer import __main__ as cli; cli.PROC = sys.argv[1]; sys.exit(cli.main(sys.argv[2:]))"


def fake_proc(folder, first: bytes = b"python\0-I\0-u\0-m\0acctpool_signer\0serve\0", cgroups=(b"0::/\n", b"0::/\n")) -> str:
    """A /proc tree with the files that the check reads: 1/cmdline, 1/cgroup, self/cgroup."""
    for name, data in (("1/cmdline", first), ("1/cgroup", cgroups[0]), ("self/cgroup", cgroups[1])):
        path = os.path.join(folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return str(folder)


def command_line(env, *args: str) -> list[str]:
    return [sys.executable, "-c", LAUNCHER, fake_proc(env.root / "proc"), *args]


def run_cli(env, *args: str, **variables: str) -> subprocess.CompletedProcess:
    """A command without a terminal: stdin, stdout and stderr are pipes."""
    return subprocess.run(
        command_line(env, *args),
        env={**environ(env), **variables},
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def run_in_terminal(env, command: str, typed: str | None = None, prompt: bytes = b"no echo", answer=None, close_at=None):
    """A command on a pseudo terminal, as with 'docker exec -it'. Gives (exit code, all that the terminal showed).

    typed: one line that is sent after the first prompt.
    answer: a function (all text until now, number of prompts until now) -> the line for the newest prompt.
    close_at: bytes; the terminal is closed (as a lost ssh connection) when they were shown.
    """
    master, slave = pty.openpty()
    process = subprocess.Popen(
        command_line(env, command),
        env=environ(env),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    shown = b""
    prompts = 0
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: the command closed the terminal
                break
            if not chunk:
                break
            shown += chunk
            if close_at is not None and close_at in shown:
                break
            # the words are typed after the prompt: the echo is off at that time
            while shown.count(prompt) > prompts:
                prompts += 1
                if answer is not None:
                    os.write(master, answer(shown.decode(), prompts).encode() + b"\n")
                elif typed is not None and prompts == 1:
                    os.write(master, typed.encode() + b"\n")
    finally:
        os.close(master)
    return process.wait(timeout=60), shown.decode().replace("\r\n", "\n")
