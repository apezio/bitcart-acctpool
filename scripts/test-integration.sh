#!/bin/bash
# v4 integration test (SPEC-v4 5). Run on your workstation with TEST_HOST set:
#
#   scripts/test-integration.sh [--keep] [--clean] [--commit <rev>] [flow ...]
#
#   flows (default: all, in this order): usdt native expiry two-at-once daemon-restart worker-kill second-opinion
#                                         replace reuse
#   --keep    leave containers, networks, the volume and files for a look (remove them with --clean)
#   --clean   remove everything of this test, run nothing
#   --commit  the commit whose signer/ is built into the signer image (default HEAD; never the working tree)
#
# On the test host ($TEST_HOST), everything with the prefix v4i-:
#   v4i-anvil    anvil, chain id 31337, a block every second
#   v4i-proxy    test-only JSON-RPC proxy in front of anvil (integration/evm/proxy.py): the provider of the daemon
#                (can block broadcasts) and the second-opinion provider of the plugin (can lie or be down)
#   v4i-eth      the STOCK daemon docker.io/bitcart/bitcart-eth:0.10.3.0 (provider: the proxy)
#   v4i-pgredis  postgres + redis (image localhost/v4i-pgredis:1, built here)
#   v4i-signer   the signer image built from `git archive <commit> signer`, only on v4i-signer-net (--internal)
#   v4i-bitcart  stock Bitcart 0.10.3.0 with the plugin: real backend, real worker, the driver
# Cleanup: every container, network, volume and file of the test is removed at the end (trap), and checked.
set -euo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
TEST_HOST=${TEST_HOST:?set TEST_HOST to the ssh name of the test host}
REV=HEAD
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --commit) REV="$2"; shift ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done
COMMIT=$(git -C "$SRC" rev-parse --short "$REV^{commit}")
BUILD_DIR="$SRC/integration/evm/build"
case "$BUILD_DIR" in /*/integration/evm/build) rm -rf "$BUILD_DIR" ;; *) echo "bad build dir" >&2; exit 2 ;; esac
mkdir -p "$BUILD_DIR"
git -C "$SRC" archive "$COMMIT" signer | tar -x -C "$BUILD_DIR"
for sub in backend/forkedpool integration/evm; do
    "$SRC/scripts/dev-sync.sh" "$sub" >/dev/null
done
rm -rf "$BUILD_DIR"

QUOTED=$(printf '%q ' "$COMMIT" "${ARGS[@]+"${ARGS[@]}"}")
# shellcheck disable=SC2029
ssh -o BatchMode=yes "$TEST_HOST" "nice bash -s -- $QUOTED" <<'REMOTE'
set -euo pipefail
COMMIT=$1; shift
BUILD=$HOME/acctpool-build
DST=$BUILD/v4-backend
WORK=$BUILD/v4i-work
BITCART=docker.io/bitcart/bitcart:0.10.3.0
DAEMON=docker.io/bitcart/bitcart-eth:0.10.3.0
FOUNDRY=ghcr.io/foundry-rs/foundry:stable
SIGNER=localhost/acctpool-signer:v4i-$COMMIT
PGREDIS=localhost/v4i-pgredis:1
KEEP=0; CLEAN=0; FLOWS=()
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        --clean) CLEAN=1 ;;
        *) FLOWS+=("$arg") ;;
    esac
done
[ ${#FLOWS[@]} -gt 0 ] || FLOWS=(usdt native expiry two-at-once daemon-restart worker-kill second-opinion replace reuse)
CONTAINERS="v4i-bitcart v4i-signer v4i-eth v4i-proxy v4i-pgredis v4i-anvil v4i-forge"

cleanup() {
    podman rm -f -t 2 $CONTAINERS >/dev/null 2>&1 || true
    podman volume rm -f v4i-signer-data >/dev/null 2>&1 || true
    podman network rm -f v4i-net v4i-signer-net >/dev/null 2>&1 || true
    podman image rm -f "$SIGNER" >/dev/null 2>&1 || true
    # files of the signer user belong to a sub-uid
    if [ -d "$WORK" ]; then podman unshare rm -rf "$WORK"; fi
    return 0
}
verify_clean() {
    local left
    left=$( { podman ps -a --format '{{.Names}}'; podman network ls --format '{{.Name}}'; podman volume ls --format '{{.Name}}'; } | grep '^v4i-' || true)
    [ -z "$left" ] && [ ! -e "$WORK" ] && echo "cleanup: no v4i- container, network, volume or file left" && return 0
    echo "cleanup FAILED, left: $left" >&2
    return 1
}
if [ "$CLEAN" = 1 ]; then cleanup; verify_clean; exit $?; fi
cleanup
if [ "$KEEP" != 1 ]; then trap 'cleanup; verify_clean' EXIT; fi
mkdir -p "$WORK/shared" "$WORK/signer"
chmod 700 "$WORK"

echo "=== images (one build at a time)"
SIGNER_SRC="$DST/integration/evm/build/signer"
mkdir -p "$BUILD/v4-cache/wheels" "$SIGNER_SRC/wheels"
cp -n "$BUILD/v4-cache/wheels/"*.whl "$BUILD/bkd-cache/wheels/"*.whl "$SIGNER_SRC/wheels/" 2>/dev/null || true
bash "$SIGNER_SRC/fetch-wheels.sh" | sed 's/^/    /'
cp -n "$SIGNER_SRC/wheels/"*.whl "$BUILD/v4-cache/wheels/"
podman build -q --network none -t "$SIGNER" "$SIGNER_SRC" >/dev/null
echo "    signer image $SIGNER from commit $COMMIT"
podman image exists "$PGREDIS" || podman build -q -t "$PGREDIS" -f "$DST/integration/evm/Containerfile.pgredis" "$DST/integration/evm" >/dev/null
podman run --rm --name v4i-forge --security-opt label=disable -v "$DST/integration/evm:/c:ro" --entrypoint sh "$FOUNDRY" -c '
    set -e; mkdir -p /tmp/t/src; cp /c/TestUSDT.sol /tmp/t/src/
    forge build --root /tmp/t --use 0.8.28 >/tmp/b.log 2>&1 || { cat /tmp/b.log >&2; exit 1; }
    forge inspect --root /tmp/t TestUSDT bytecode' | tail -1 > "$WORK/shared/token.bytecode"
grep -q '^0x60' "$WORK/shared/token.bytecode" || { echo "no bytecode from forge" >&2; exit 1; }

echo "=== networks and services"
podman network create v4i-net >/dev/null
podman network create --internal v4i-signer-net >/dev/null
podman run -d --name v4i-anvil --network v4i-net "$FOUNDRY" "anvil --host 0.0.0.0 --chain-id 31337 --block-time 1 --silent" >/dev/null
podman run -d --name v4i-pgredis --network v4i-net --tmpfs /var/lib/postgresql/data \
    -e POSTGRES_PASSWORD=v4i -e POSTGRES_DB=bitcart "$PGREDIS" >/dev/null
podman run -d --name v4i-proxy --network v4i-net --security-opt label=disable -v "$DST/integration/evm:/c:ro" \
    --entrypoint python "$BITCART" /c/proxy.py >/dev/null
podman run -d --name v4i-eth --network v4i-net --memory 2g -e ETH_SERVER=http://v4i-proxy:8546 -e ETH_DEBUG=false "$DAEMON" >/dev/null
printf '[anvil]\nurl = "http://v4i-proxy:8547"\n' > "$WORK/second.toml"
head -c 32 /dev/urandom > "$WORK/signer/master.key"
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$WORK/signer/signer.token"
cp "$WORK/signer/signer.token" "$WORK/worker.token"
chmod 600 "$WORK/worker.token"
# "worker" is the log server of the worker in a production Bitcart; here worker and backend share this container
podman run -d --name v4i-bitcart --network v4i-net,v4i-signer-net --security-opt label=disable --add-host worker:127.0.0.1 --init \
    -v "$DST:/plug:ro" -v "$DST/backend/forkedpool:/app/modules/forkedpool:ro" -v "$WORK/shared:/shared" \
    -v "$WORK/second.toml:/run/acctpool/second.toml:ro" -v "$WORK/worker.token:/run/acctpool/signer.token:ro" -w /app \
    -e BITCART_ENV=production -e DB_HOST=v4i-pgredis -e DB_PASSWORD=v4i -e REDIS_HOST=v4i-pgredis \
    -e BITCART_CRYPTOS=eth -e ETH_HOST=v4i-eth -e ETH_PORT=5002 -e ACCTPOOL_SIGNER_URL=http://v4i-signer:7070 \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 \
    --entrypoint python "$BITCART" -c 'import time; time.sleep(10**7)' >/dev/null
driver() { podman exec v4i-bitcart python /plug/integration/evm/driver.py "$@"; }
for _ in $(seq 1 60); do podman exec v4i-pgredis pg_isready -q -h 127.0.0.1 -U postgres && break; sleep 1; done
for _ in $(seq 1 60); do podman exec v4i-bitcart python -c 'import urllib.request as u; u.urlopen("http://v4i-eth:5002/spec", timeout=2)' 2>/dev/null && break; sleep 2; done
driver chain-setup

echo "=== signer: keystore made with exec in the running container (SPEC 7.9); network v4i-signer-net only"
cp "$WORK/shared/pools.toml" "$WORK/signer/pools.toml"
chmod 0400 "$WORK/signer/master.key" "$WORK/signer/signer.token" "$WORK/signer/pools.toml"
podman unshare chown -R 10001:10001 "$WORK/signer"
podman volume create v4i-signer-data >/dev/null
podman run -d --name v4i-signer --network v4i-signer-net --read-only --cap-drop ALL --security-opt no-new-privileges \
    -v "$WORK/signer/pools.toml:/etc/acctpool/pools.toml:ro,Z" -v "$WORK/signer/master.key:/run/acctpool/master.key:ro,Z" \
    -v "$WORK/signer/signer.token:/run/acctpool/signer.token:ro,Z" -v v4i-signer-data:/data "$SIGNER" >/dev/null
sleep 3
ISOLATED=$(podman image inspect "$SIGNER" --format '{{json .Config.Cmd}}' | grep -c '"-I"' || true)
ISOLATED=$ISOLATED python3 - <<'PY'
# This program is the terminal of `init`: the words stay in its memory, are typed back, and are never shown.
import os, pty, re, sys
python = ["python", "-I"] if os.environ["ISOLATED"] != "0" else ["python"]
pid, master = pty.fork()
if pid == 0:
    os.execvp("podman", ["podman", "exec", "-it", "v4i-signer", *python, "-m", "acctpool_signer", "init"])
shown, answered = b"", 0
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
        words = [w for _, w in re.findall(r"\b(\d{1,2})\. ([a-z]+)", text)]
        os.write(master, (words[int(prompts[answered]) - 1] + "\n").encode())
        answered += 1
_, status = os.waitpid(pid, 0)
words = re.findall(r"\b\d{1,2}\. ([a-z]+)", shown.decode(errors="replace"))
if os.waitstatus_to_exitcode(status) != 0 or len(words) != 24:
    print(f"FAIL signer init: {len(words)} words"); sys.exit(1)
print(f"    ok   signer init: 24 words kept in memory, {answered} typed back")
PY
podman restart -t 2 v4i-signer >/dev/null
sleep 3
driver fee-wallet

echo "=== Bitcart: stock migration, worker and backend with the plugin"
podman exec v4i-bitcart alembic upgrade head >"$WORK/shared/alembic.log" 2>&1 || { tail -5 "$WORK/shared/alembic.log"; exit 1; }
TEST_CHAIN=$(cat "$WORK/shared/test-chain")
TIMES='{"payout": 2, "reconcile": 3, "watch": 30, "ready": 3, "state": 3, "retry": 2}'
ENV=(-e ACCTPOOL_TEST_CHAIN="$TEST_CHAIN" -e ACCTPOOL_TEST_TIMES="$TIMES")
# $1: other times for this worker (the replace flow needs a short replace_after)
start_worker() {
    podman exec -d -e ACCTPOOL_TEST_CHAIN="$TEST_CHAIN" -e ACCTPOOL_TEST_TIMES="${1:-$TIMES}" v4i-bitcart \
        sh -c 'python worker.py >>/shared/worker.log 2>&1' >/dev/null
}
start_worker
podman exec -d "${ENV[@]}" v4i-bitcart sh -c 'python -m uvicorn main:app --host 127.0.0.1 --port 8000 >/shared/backend.log 2>&1'

passed=0; failed=0; results=()
run() {
    if driver "$1"; then passed=$((passed + 1)); results+=("PASS $1"); else failed=$((failed + 1)); results+=("FAIL $1"); return 1; fi
}
if ! run setup; then
    echo "--- worker log"; tail -40 "$WORK/shared/worker.log" || true
    echo "--- backend log"; tail -40 "$WORK/shared/backend.log" || true
    exit 1
fi
for flow in "${FLOWS[@]}"; do
    case "$flow" in
        daemon-restart)
            if run restart-a; then
                echo "    podman stop v4i-eth (the daemon is down; its diskless wallets are gone)"
                podman stop -t 5 v4i-eth >/dev/null
                run restart-pay || true
                podman start v4i-eth >/dev/null
                run restart-b || true
            fi ;;
        worker-kill)
            if run kill-a; then
                driver stop-worker
                sleep 2
                podman exec v4i-bitcart sh -c 'for p in /proc/[0-9]*; do tr "\0" " " < $p/cmdline; echo; done' | grep -c 'worker.py' \
                    | sed 's/^/    worker processes left after SIGKILL: /' || true
                start_worker
                run kill-b || true
            fi ;;
        replace)
            driver stop-worker; sleep 2; start_worker "${TIMES%\}}, \"replace_after\": 8}"
            run replace || true
            driver stop-worker; sleep 2; start_worker ;;
        *) run "$flow" || true ;;
    esac
done
run summary || true

echo "--- signer audit: calls by result, and the hash chain"
podman exec v4i-signer python -c '
import collections, json
counts = collections.Counter()
for line in open("/data/audit.log"):
    entry = json.loads(line)
    counts[(entry["call"], entry["result"])] += 1
for key, n in sorted(counts.items()):
    print("   ", n, *key)
'
podman exec v4i-signer python -m acctpool_signer audit-verify | sed 's/^/    /' || { failed=$((failed + 1)); results+=("FAIL audit-verify"); }
if grep -q -F -f "$WORK/worker.token" "$WORK/shared/worker.log" "$WORK/shared/backend.log"; then
    failed=$((failed + 1)); results+=("FAIL the signer token is in a Bitcart log")
fi
# Tracebacks of plugin code. Expected and not counted: the loops while the daemon was stopped (connection errors).
podman exec -i v4i-bitcart python - <<'PY' || { failed=$((failed + 1)); results+=("FAIL plugin errors in the worker log"); }
import re, sys
text = re.sub(r"\x1b\[[0-9;]*m", "", open("/shared/worker.log", errors="replace").read())
records = re.split(r"(?m)^(?=\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", text)
ours = [r for r in records if ("acctpool" in r or "forkedpool" in r) and re.search(r"\[(error|critical)", r)]
expected = ("ConnectionFailedError", "ClientConnectorError", "Cannot connect", "Connection refused", "broadcast blocked",
            "Server disconnected", "ServerDisconnectedError")
bad = [r for r in ours if not any(e in r for e in expected)]
print(f"    worker log: {len(records)} records, {len(ours)} plugin errors, {len(ours) - len(bad)} of them while the daemon was down or blocked")
for record in bad[:5]:
    print("    FAIL " + record[:1500])
sys.exit(1 if bad or len(records) < 5 else 0)
PY
echo "=== RESULTS (signer commit $COMMIT)"
printf '    %s\n' "${results[@]}"
echo "=== $passed steps passed, $failed failed"
[ "$failed" = 0 ]
REMOTE
