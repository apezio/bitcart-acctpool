#!/bin/bash
# Signer tests on the test host (SPEC-v4 section 3). Run on your workstation with TEST_HOST set:
#   scripts/test-signer.sh                 sync, unit tests, image test, lint
#   scripts/test-signer.sh unit [args]     sync, unit tests only; args go to pytest (default: all of signer/tests)
#   scripts/test-signer.sh image           sync, image build + start + calls + backend proof, then remove
#   scripts/test-signer.sh lint            sync, ruff check and ruff format --check with signer/ruff.toml
# The unit tests run in the stock base image. coincurve is installed there from signer/requirements.txt
# (hash-pinned), so that the tests use the backend of the signer image. SGN_BACKEND=native skips that
# install: the same tests then run on the pure-Python backend of the stock image.
# The proof tests of review A and B were adapted into signer/tests (test_review_a.py, test_review_b.py) where
# their subject still exists in v4; the others were about deleted code (Tron, rebuild, rotation, flood layer).
# Containers, volumes and the image have the prefix v4s- and are removed at the end.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
HOST=${TEST_HOST:?set TEST_HOST to the ssh name of the test host}
MODE="${1:-all}"
[ $# -gt 0 ] && shift
case "$MODE" in unit|image|lint|all) ;; *) echo "usage: $0 [unit|image|lint|all] [pytest args]" >&2; exit 2;; esac

"$HERE/dev-sync.sh" signer

# The remote part is one script on stdin; its arguments are quoted for the remote shell.
ARGS=$(printf '%q ' "${SGN_BACKEND:-coincurve}" "$MODE" "$@")
# shellcheck disable=SC2029
ssh -o BatchMode=yes "$HOST" "bash -s -- $ARGS" <<'REMOTE'
set -euo pipefail
BACKEND="$1"; MODE="$2"; shift 2
case "$BACKEND" in coincurve|native) ;; *) echo "SGN_BACKEND must be coincurve or native" >&2; exit 2;; esac
BUILD=$HOME/acctpool-build
PLUGIN=$BUILD/v4-backend
BASE=docker.io/bitcart/bitcart-eth:0.10.3.0
IMAGE=localhost/v4s-signer:test
SMOKE=""

cleanup() {
    podman rm -f v4s-unit v4s-lint v4s-smoke v4s-smoke-init >/dev/null 2>&1 || true
    podman pod rm -f v4s-pod >/dev/null 2>&1 || true
    podman volume rm -f v4s-smoke-data >/dev/null 2>&1 || true
    podman rmi -f "$IMAGE" >/dev/null 2>&1 || true
    rm -f "$BUILD"/v4s-wrong.whl "$BUILD"/v4s-good.whl "$BUILD"/v4s-fetch.err "$BUILD"/v4s-smoke.*.init "$BUILD"/v4s-smoke.*.py
    rm -rf "$BUILD"/v4s-hw.*
    # the files are owned by a sub-uid (the uid of the signer in the container)
    [ -n "$SMOKE" ] && podman unshare rm -rf "$SMOKE"
    return 0
}
final() {
    cleanup
    # the check that nothing of this test stays on the shared host
    LEFT=$( { podman ps -a --format '{{.Names}}'; podman pod ls --format '{{.Name}}'; podman volume ls --format '{{.Name}}';
              podman images --format '{{.Repository}}'; } | grep 'v4s-' || true)
    if [ -n "$LEFT" ]; then echo "FAIL left on the test host: $LEFT"; exit 1; fi
    echo "cleanup: no v4s- container, pod, volume or image is left"
}
trap final EXIT
cleanup

# The wheel of coincurve for the image build and for the unit tests (SPEC 7.8). The sync removed the folder.
"$PLUGIN/signer/fetch-wheels.sh"

unit() {
    echo "== signer unit tests (container v4s-unit, $BASE, signing backend $BACKEND)"
    # signer/ is mounted read-only: the tests write to /tmp only. Test tools are installed in the container.
    # /small: a file system of 600 kB for the test of a full volume
    podman run --rm --name v4s-unit --tmpfs /small:rw,size=600k,mode=1777 \
        --security-opt label=disable -v "$PLUGIN/signer:/work/signer:ro" -w /work \
        -e PYTHONDONTWRITEBYTECODE=1 -e SGN_BACKEND="$BACKEND" \
        "$BASE" sh -c '
            VENV=/home/electrum/.venv/bin/python
            uv pip install -q --python $VENV pytest pytest-asyncio pytest-aiohttp >&2 || exit 3
            if [ "$SGN_BACKEND" = coincurve ]; then
                uv pip install -q --no-config --offline --no-index --find-links signer/wheels --python $VENV \
                    --require-hashes --no-deps --only-binary :all: -r signer/requirements.txt >&2 || exit 3
            fi
            python -m pytest -c signer/pytest.ini --rootdir signer "$@"' sh "$@"
}

lint() {
    echo "== ruff check and ruff format --check, config signer/ruff.toml (container v4s-lint)"
    podman run --rm --name v4s-lint --security-opt label=disable -v "$PLUGIN/signer:/work/signer:ro" -w /work/signer -e RUFF_NO_CACHE=true \
        "$BASE" sh -c '
            uv pip install -q --python /home/electrum/.venv/bin/python ruff >&2 || exit 3
            ruff --version
            ruff check --config ruff.toml . && ruff format --config ruff.toml --check .'
}

in_signer() {
    # a call from inside the container: it has no network and no published port
    podman exec -i v4s-smoke python - "$@"
}

image() {
    echo "== signer image test (containers v4s-smoke, v4s-smoke-init)"
    # the script refuses a file with a wrong SHA-256, in the folder and from a download
    WHEEL=$(ls "$PLUGIN/signer/wheels/"*.whl)
    mv "$WHEEL" "$BUILD/v4s-good.whl"
    head -c 1000 "$BUILD/v4s-good.whl" > "$WHEEL"
    if "$PLUGIN/signer/fetch-wheels.sh" >/dev/null 2>"$BUILD/v4s-fetch.err"; then echo "FAIL a changed wheel is accepted"; return 1; fi
    grep -q "REFUSED: wheels/.* has a wrong SHA-256" "$BUILD/v4s-fetch.err"
    echo "ok   fetch-wheels.sh refuses a changed file in wheels/"
    mv "$WHEEL" "$BUILD/v4s-wrong.whl"
    if WHEEL_URL_1="file://$BUILD/v4s-wrong.whl" "$PLUGIN/signer/fetch-wheels.sh" >/dev/null 2>"$BUILD/v4s-fetch.err"; then
        echo "FAIL a download with a wrong hash is accepted"; return 1
    fi
    grep -q "REFUSED: the download .* Nothing is kept" "$BUILD/v4s-fetch.err"
    [ -z "$(ls -A "$PLUGIN/signer/wheels/")" ]
    echo "ok   fetch-wheels.sh refuses a download with a wrong SHA-256 and keeps nothing"

    # without the wheel the build stops with a message
    if podman build --network none -q -t "$IMAGE" "$PLUGIN/signer" >"$BUILD/v4s-fetch.err" 2>&1; then
        echo "FAIL the image build works without the wheel"; return 1
    fi
    grep -q "ERROR: no wheel file in signer/wheels/. Run signer/fetch-wheels.sh" "$BUILD/v4s-fetch.err"
    echo "ok   image build without the wheel stops: $(grep -m1 "^ERROR: no wheel" "$BUILD/v4s-fetch.err")"
    rm -f "$BUILD/v4s-wrong.whl" "$BUILD/v4s-fetch.err"
    mv "$BUILD/v4s-good.whl" "$WHEEL"
    "$PLUGIN/signer/fetch-wheels.sh" | sed 's/^/     /'

    podman build --network none -q -t "$IMAGE" "$PLUGIN/signer"
    echo "ok   image build with --network none"

    SMOKE=$(mktemp -d "$BUILD/v4s-smoke.XXXXXX")
    head -c 32 /dev/urandom > "$SMOKE/master.key"
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$SMOKE/signer.token"
    cat > "$SMOKE/pools.toml" <<'TOML'
[signer]
listen = "127.0.0.1:7070"

[chains.anvil]
family = "evm"
chain_id = 31337
usdt = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
gas_limit_cap = 150000
max_fee_per_gas_cap_wei = "2000000000000"
max_fund_value_wei = "300000000000000000"
max_fund_total_per_address_wei = "900000000000000000"
fee_wallet_daily_cap_wei = "20000000000000000000"

[stores.teststore]
account = 1
[stores.teststore.destinations]
anvil = "0x1111111111111111111111111111111111111111"
[stores.teststore.native_destinations]
anvil = "0x2222222222222222222222222222222222222222"
TOML
    chmod 0400 "$SMOKE/master.key" "$SMOKE/signer.token" "$SMOKE/pools.toml"
    # uid 10001 is the signer user of the image; without this it cannot read files with mode 0400
    podman unshare chown -R 10001:10001 "$SMOKE"
    podman volume create v4s-smoke-data >/dev/null

    # as the compose component: no network, read-only root, no capabilities, a memory limit.
    # The log driver writes a file of the container (k8s-file), not the journal of the host: a fault of the test
    # that shows seed words would put them into a file that is removed with the container, not into the journal
    # of the host and its syslog copy. "podman logs" reads that file.
    RUN=(--log-driver k8s-file --security-opt label=disable --network none --read-only --cap-drop ALL --security-opt no-new-privileges --memory 300m --memory-swap 300m
        -v "$SMOKE/pools.toml:/etc/acctpool/pools.toml:ro"
        -v "$SMOKE/master.key:/run/acctpool/master.key:ro"
        -v "$SMOKE/signer.token:/run/acctpool/signer.token:ro"
        -v v4s-smoke-data:/data)

    start() {
        podman run -d --name v4s-smoke "${RUN[@]}" "$@" "$IMAGE" >/dev/null
        for _ in $(seq 1 50); do
            if in_signer status >/dev/null 2>&1 <<<"$CLIENT"; then return 0; fi
            sleep 0.2
        done
        echo "the signer did not start" >&2
        podman logs v4s-smoke >&2 || true
        return 1
    }

    CLIENT=$(cat <<'PY'
import json, os, sys, urllib.error, urllib.request

def call(method, path, body=None):
    token = open("/run/acctpool/signer.token").read().strip()
    request = urllib.request.Request("http://127.0.0.1:7070" + path, method=method,
        data=None if body is None else json.dumps(body).encode(), headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

def check(condition, text):
    print(("ok   " if condition else "FAIL ") + text)
    if not condition:
        sys.exit(1)

mode = sys.argv[1]
status, body = call("GET", "/v1/status")
if mode == "status":
    sys.exit(0 if status == 200 else 1)
check(status == 200, "GET /v1/status -> 200: " + json.dumps(body))
check(os.getuid() == 10001, "the process is uid 10001, not root")
check(not os.path.exists("/bin/sh") and not os.path.exists("/bin/busybox"), "no shell in the image")
try:
    open("/opt/acctpool/write-test", "w")
    check(False, "the root filesystem is read-only")
except OSError:
    check(True, "the root filesystem is read-only")
check(open("/proc/1/status").read().count("CapEff:\t0000000000000000") == 1, "no capabilities")
if mode == "nokeystore":
    check(body["keystore"] is False and not os.path.exists("/data/keystore.json"), "no keystore was written")
    sys.exit(0)
if mode == "empty":
    check(body["keystore"] is False and body["seed_id"] is None, "keystore: false before init")
    status, body = call("POST", "/v1/derive", {"store": "teststore", "family": "evm", "first_index": 0, "count": 1})
    check(status == 503 and body["error"] == "keystore_missing", "derive without keystore -> 503 keystore_missing")
if mode == "full":
    check(body["keystore"] is True and len(body["seed_id"]) == 16, "keystore: true after init")
    check(body["fee_wallets"]["evm"].startswith("0x"), "fee wallet address in the status")
    status, derived = call("POST", "/v1/derive", {"store": "teststore", "family": "evm", "first_index": 0, "count": 3})
    check(status == 200 and len(derived["addresses"]) == 3, "derive 3 addresses")
    sign = {"idempotency_key": "smoke:fund:1", "chain": "anvil", "store": "teststore", "index": 2, "nonce": 0,
            "max_fee_per_gas_wei": "60000000000", "max_priority_fee_per_gas_wei": "30000000000", "replaces": None}
    status, early = call("POST", "/v1/sign/sweep", {**sign, "idempotency_key": "smoke:sweep:0", "gas_limit": 70000,
                                                   "amount": "2000000", "token": None})
    check(status == 403 and early["error"] == "cap_exceeded", "sign/sweep before a funding -> 403 cap_exceeded (fee budget)")
    status, fund = call("POST", "/v1/sign/fund", {**sign, "value_wei": "5000000000000000"})
    check(status == 200 and fund["to"] == derived["addresses"][2]["address"] and fund["from"] == body["fee_wallets"]["evm"],
          "sign/fund: fee wallet -> derived address")
    status, sweep = call("POST", "/v1/sign/sweep", {**sign, "idempotency_key": "smoke:sweep:1", "gas_limit": 70000,
                                                   "amount": "2000000", "token": None})
    check(status == 200 and sweep["to"] == body["chains"]["anvil"]["usdt"] and sweep["from"] == fund["to"],
          "sign/sweep: derived address -> pinned USDT contract")
    status, refused = call("POST", "/v1/sign/sweep", {**sign, "idempotency_key": "smoke:sweep:2", "gas_limit": 70000,
                           "amount": "2000000", "token": None, "to": "0x00000000000000000000000000000000000000ee"})
    check(status == 400 and refused["error"] == "invalid_request", "sign/sweep with a 'to' field is refused")
    native = {**sign, "idempotency_key": "smoke:native:1", "nonce": 1, "gas_limit": 21000,
              "max_priority_fee_per_gas_wei": sign["max_fee_per_gas_wei"], "value_wei": "50000000000000000"}
    status, swept = call("POST", "/v1/sign/sweep_native", native)
    check(status == 200 and swept["from"] == fund["to"] and swept["to"] == body["stores"]["teststore"]["native_destinations"]["anvil"],
          "sign/sweep_native: derived address -> pinned native destination")
    status, refused = call("POST", "/v1/sign/sweep_native", {**native, "idempotency_key": "smoke:native:2", "nonce": 0})
    check(status == 409 and refused["error"] == "idempotency_conflict", "native sweep with the nonce of the token sweep -> 409")
    status, refused = call("POST", "/v1/sign/sweep_native", {**native, "idempotency_key": "smoke:native:3", "nonce": 2,
                                                            "value_wei": "1260000000000000"})
    check(status == 403 and refused["error"] == "cap_exceeded", "native sweep with a fee of 50% of the coin -> 403 cap_exceeded")
    status, refused = call("POST", "/v1/derive", {"store": "teststore", "family": "evm", "first_index": 50, "count": 1})
    check(status == 400 and refused["error"] == "invalid_request", "derive with a gap is refused")
    results = [call("POST", "/v1/derive", {"store": "teststore", "family": "evm", "first_index": 0, "count": 1})[0]
               for _ in range(12)]
    accepted = results.count(200)
    # the derive calls of this test before this line count too, also the one before the restart of the container
    check(results == [200] * accepted + [429] * (12 - accepted) and 5 <= accepted <= 7,
          "derive: limit of 10 calls in a minute (default); %d more were accepted, then 429" % accepted)
PY
)

    start
    in_signer empty <<<"$CLIENT"

    # F01 of review A and M2 of review B. init and restore must be refused when they are the main process of a
    # container, also when the main process is not process 1: with an init program (run --init), with the PID
    # namespace of the host, in a pod with a shared PID namespace, and with the PID namespace of the running signer.
    # Their output goes to a file here, not to the terminal. The container log is read before the container goes.
    podman pod create --name v4s-pod --infra-name v4s-pod-infra --share pid,ipc,uts >/dev/null
    for extra in "" "--init" "--pid=host" "--pod=v4s-pod" "--pid=container:v4s-smoke"; do
        for command in init restore; do
            code=0
            # shellcheck disable=SC2086
            podman run -t $extra --name v4s-smoke-init "${RUN[@]}" "$IMAGE" python -I -m acctpool_signer "$command" \
                > "$SMOKE.init" 2>&1 || code=$?
            LOG=$(podman logs v4s-smoke-init 2>&1)
            podman rm -f v4s-smoke-init >/dev/null
            if ! grep -q "acctpool-signer: refused: $command must not be the main process of a container" "$SMOKE.init"; then
                echo "FAIL 'podman run -t $extra ... $command' is not refused (exit code $code):"
                grep -v -E '[0-9]{1,2}\. [a-z]+' "$SMOKE.init" | head -5
                return 1
            fi
            [ "$code" = 1 ]
            [ "$(grep -c -E '[0-9]{1,2}\. [a-z]+' "$SMOKE.init")" = 0 ]
            [ "$(grep -c -E '[0-9]{1,2}\. [a-z]+' <<<"$LOG")" = 0 ]
            grep -q "must not be the main process" <<<"$LOG"
            echo "ok   'podman run -t $extra ... $command' is refused ($(sed -n 's/.* of a container (\([^)]*\)).*/\1/p' "$SMOKE.init" | head -1))," \
                "no word on the terminal and in the container log"
        done
    done
    podman pod rm -f v4s-pod >/dev/null
    rm -f "$SMOKE.init"
    in_signer nokeystore <<<"$CLIENT"

    # init as README says: "exec -it" in the container that runs. A program types the 3 words back.
    # It keeps the words in its memory, looks for them in the logs, and shows numbers only.
    cat > "$SMOKE.driver.py" <<'PY'
import os, pty, re, subprocess, sys, time

command = ["podman", "exec", "-it", "v4s-smoke", "python", "-I", "-m", "acctpool_signer", "init"]
pid, master = pty.fork()
if pid == 0:
    os.execvp(command[0], command)
shown = b""
answered = 0
while True:
    try:
        chunk = os.read(master, 4096)
    except OSError:
        break
    if not chunk:
        break
    shown += chunk
    text = shown.decode(errors="replace")
    prompts = re.findall(r"word (\d+) \(no echo\): ", text)
    while len(prompts) > answered:
        words = [word for _, word in re.findall(r"\b(\d{1,2})\. ([a-z]+)", text)]
        os.write(master, (words[int(prompts[answered]) - 1] + "\n").encode())
        answered += 1
_, status = os.waitpid(pid, 0)
text = shown.decode(errors="replace")
words = [word for _, word in re.findall(r"\b(\d{1,2})\. ([a-z]+)", text)]

def check(condition, line):
    print(("ok   " if condition else "FAIL ") + line)
    if not condition:
        sys.exit(1)

check(os.waitstatus_to_exitcode(status) == 0 and len(words) == 24 and answered == 3,
      "init with 'exec -it': 24 words on the terminal, 3 words typed back, exit code 0")
check("seed id: " in text and "fee wallet (evm): 0x" in text, "init shows the seed id and the fee wallet")
time.sleep(3)  # the log driver writes with a delay
parts = [" ".join(words[i:i + 2]) for i in range(23)] + ["%d. %s" % (i + 1, words[i]) for i in range(24)]
places = {
    "podman logs": ["podman", "logs", "v4s-smoke"],
    "journal of the host, this container": ["journalctl", "CONTAINER_NAME=v4s-smoke", "--since", "-15min", "--no-pager", "-o", "cat"],
    "journal of the host, all entries of the last 15 minutes": ["journalctl", "--since", "-15min", "--no-pager", "-o", "cat"],
}
for name, command in places.items():
    done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = done.stdout.decode(errors="replace")
    found = sum(1 for part in parts if part in log)
    lines = len(re.findall(r"\b\d{1,2}\. [a-z]+ +\d{1,2}\. [a-z]+", log))
    if name.endswith("minutes"):
        # other containers of this host write to the same journal: a word table of another seed is not of this run
        check(found == 0, "%s: %d of the words of this run found (%d bytes read)" % (name, found, len(log)))
        if lines:
            print("     NOTE: %d lines of a word table of OTHER seeds are in the journal of this host (not from this test)" % lines)
        continue
    check(found == 0 and lines == 0, "%s: %d of the words found, %d lines of a word table (%d bytes read)" % (name, found, lines, len(log)))
    if name == "podman logs":
        check("signing backend" in log, "podman logs: the log of the signer is there (the search was not in an empty log)")
PY
    python3 "$SMOKE.driver.py"
    rm -f "$SMOKE.driver.py"
    podman exec v4s-smoke python -I -m acctpool_signer verify | sed 's/^/     verify: /'
    podman rm -f v4s-smoke >/dev/null

    start
    in_signer full <<<"$CLIENT"

    # F13 of review A: 40 connections without the token, each with 30 kB that are 30 MB after gzip
    podman exec -i v4s-smoke python - <<'PY' || true
import gzip, socket, threading, time
packed = gzip.compress(b" " * (30 * 1024 * 1024), 9)
head = f"POST /v1/derive HTTP/1.1\r\nHost: x\r\nContent-Encoding: gzip\r\nContent-Length: {len(packed)}\r\n\r\n".encode()
def attack():
    try:
        with socket.create_connection(("127.0.0.1", 7070), timeout=20) as s:
            s.sendall(head + packed)
            s.recv(100)
            time.sleep(3)
    except OSError:
        pass
threads = [threading.Thread(target=attack) for _ in range(40)]
[t.start() for t in threads]
[t.join() for t in threads]
PY
    sleep 2
    STATE=$(podman inspect v4s-smoke --format 'status={{.State.Status}} oom_killed={{.State.OOMKilled}}')
    [ "$STATE" = "status=running oom_killed=false" ]
    in_signer status <<<"$CLIENT"
    echo "ok   40 connections with packed bodies, no token, memory limit 300 MB: $STATE, and the signer answers"
    podman exec v4s-smoke python -I -m acctpool_signer audit-verify | sed 's/^/     /'

    # SPEC 7.6: which backend signs in the image, and that its signatures are the ones of the pure-Python backend.
    # The proof is a file of the unit tests; it goes into the container on stdin.
    podman exec -i v4s-smoke python - < "$PLUGIN/signer/tests/backend_proof.py"
    # (the log goes into a variable first: grep -q in a pipe stops the writer, and pipefail makes that an error)
    LOGS=$(podman logs v4s-smoke 2>&1)
    grep -q "signing backend: coincurve" <<<"$LOGS"
    echo "ok   the log of the running signer has 'signing backend: coincurve'"
    echo "     signer log (stderr) and audit lines (stdout):"
    cut -c1-200 <<<"$LOGS" | sed 's/^/       /'
    podman rm -f v4s-smoke >/dev/null

    # M1 of review B: the high-water file of the probe. It is owned by root (the user of this test is root in the
    # container) and mounted read-only, as in production. A mark above the journal refuses the start; the mark of
    # the last line lets it start.
    HW=$(mktemp -d "$BUILD/v4s-hw.XXXXXX")
    chmod 755 "$HW"
    MARK=$(podman run --rm --name v4s-smoke-init --log-driver k8s-file --network none --security-opt label=disable \
        -v v4s-smoke-data:/data "$IMAGE" python -I -c "
import hashlib, json
line = open('/data/audit.log', 'rb').read().splitlines()[-1]
print(json.loads(line)['seq'], hashlib.sha256(line).hexdigest())")
    SEQ=${MARK%% *}
    HASH=${MARK#* }
    printf '%s %s\n' "$((SEQ + 1))" "$HASH" > "$HW/audit.highwater"
    chmod 644 "$HW/audit.highwater"
    HWMOUNT=(-v "$HW/audit.highwater:/run/acctpool/audit.highwater:ro")
    if podman run --rm --name v4s-smoke-init "${RUN[@]}" "${HWMOUNT[@]}" "$IMAGE" > "$SMOKE.refusal" 2>&1; then
        echo "FAIL the signer started with a journal below the high-water file"; rm -f "$SMOKE.refusal"; return 1
    fi
    grep -q "audit.log ends with line $SEQ, and the high-water file /run/acctpool/audit.highwater has line $((SEQ + 1))" "$SMOKE.refusal"
    echo "ok   high-water file with line $((SEQ + 1)), the log ends with line $SEQ: the start is refused"
    printf '%s %s\n' "$SEQ" "$HASH" > "$HW/audit.highwater"
    start "${HWMOUNT[@]}"
    podman exec v4s-smoke python -I -m acctpool_signer audit-verify | sed 's/^/     /'
    echo "ok   high-water file with the last line ($SEQ): the signer starts"
    podman rm -f v4s-smoke >/dev/null
    rm -f "$SMOKE.refusal"

    # eth_keys can be told to use the pure-Python backend. The signer of the image must refuse to start then.
    if podman run --rm --name v4s-smoke-init "${RUN[@]}" -e ECC_BACKEND_CLASS=eth_keys.backends.NativeECCBackend \
        "$IMAGE" > "$SMOKE.refusal" 2>&1; then
        echo "FAIL the signer started with the pure-Python backend"; rm -f "$SMOKE.refusal"; return 1
    fi
    grep -q "refused: the signing backend is 'native'" "$SMOKE.refusal"
    echo "ok   start with the pure-Python backend is refused: $(cat "$SMOKE.refusal")"
    rm -f "$SMOKE.refusal"
    # the tools that installed the package are not in the image
    podman run --rm --name v4s-smoke-init "${RUN[@]}" "$IMAGE" python -I -c "
import importlib.util, os, shutil, sys
tools = ('uv', 'uvx', 'pip', 'pip3', 'sh', 'apk', 'git', 'git-shell', 'git-upload-pack', 'ssl_client', 'busybox', 'just')
assert not any(shutil.which(tool) for tool in tools)
assert not os.path.exists('/tmp/build')
print('ok   no uv, pip, shell, apk, git or ssl_client in the image')
modules = ('web3', 'requests', 'websockets', 'grpc', 'opentelemetry', 'ensurepip', 'venv', 'pip')
assert not any(importlib.util.find_spec(name) for name in modules), [n for n in modules if importlib.util.find_spec(n)]
print('ok   no web3, requests, websockets, grpc, opentelemetry, ensurepip or venv in the image')
assert not os.environ.get('LD_PRELOAD')
assert 'jemalloc' not in open('/proc/self/maps').read()
print('ok   no library is loaded with LD_PRELOAD')
assert sys.flags.isolated == 1 and '' not in sys.path and os.getcwd() not in sys.path
assert importlib.util.find_spec('acctpool_signer').origin.startswith('/home/electrum/.venv/lib/python3.12/site-packages/')
print('ok   isolated mode: the module path has no current folder, the package is in site-packages')
assert not os.access('/opt/acctpool', os.W_OK) and not os.access('/home/electrum/.venv/lib/python3.12/site-packages/acctpool_signer', os.W_OK)
print('ok   the signer user cannot write the code and the work folder')"
    CMDLINE=$(podman inspect "$IMAGE" --format '{{.Config.Cmd}}')
    [ "$CMDLINE" = "[python -I -u -m acctpool_signer serve]" ]
    echo "ok   command of the image: $CMDLINE"
    echo "== image test passed; container, volume, image and test files are removed"
}

case "$MODE" in
    unit) unit "$@" ;;
    image) image ;;
    lint) lint ;;
    all) unit "$@"; image; lint ;;
esac
REMOTE
