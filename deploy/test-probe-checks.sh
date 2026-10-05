#!/bin/bash
# Test of probe-checks.sh.
#
# Part 1, stand-ins: a real Postgres with the tables and columns that the probe reads (names and types
# of the backend migration), a false signer (an audit.log that the test writes, and a container log with the
# request lines of the v4 signer) and a false backend route. It tests the queries, the limits, the output
# form, and that no secret is in the output.
# Part 2, the real signer image (no keystore): the formats that the probe reads from the signer
# (audit.log, the request lines on stderr, exit code of audit-verify, status) are those of the real signer.
# Part 2 is skipped, and the script says so, when the image $ACCTPOOL_SIGNER_IMAGE does not exist.
# Part 2 also tests the high-water file (SPEC 7.11) with the real audit-verify, and part 4 is the end to end
# test: seed (exec -it, the words stay in the memory of the test), mark of the probe, restore of an older copy
# of the volume, the start is refused and names the high-water file (v4 has no journal rebuild).
#
# Where: on the test host, from the plugin folder:   deploy/test-probe-checks.sh
#        VERBOSE=1 also prints the FAIL lines of each test.
# Needs: rootless podman and the images postgres:17-alpine, bitcart:0.10.3.0, bitcart-eth:0.10.3.0.
#        Part 2: the signer image, built from a copy of the committed source (see test-config-examples.sh):
#            cd ~/acctpool-build/v4-backend/signer && ./fetch-wheels.sh \
#              && podman build --network none -t localhost/v4d-signer:test .
# Containers: v4d-db, v4d-worker, v4d-signer or v4d-signer-real (3 at the same time), network v4d-net,
#        volume v4d-signer-data. They are removed at the end. All use the log driver k8s-file (a file of the
#        container, not the journal of the host).
# The plugin itself is not tested here.
set -uo pipefail

PLUGIN=$(cd "$(dirname "$0")/.." && pwd)
PROBE=$PLUGIN/deploy/probe-checks.sh
SIGNER_IMAGE=${ACCTPOOL_SIGNER_IMAGE:-localhost/v4d-signer:test}
WORK=$(mktemp -d "$HOME/acctpool-build/v4d-probe.XXXXXX")
# made in the test, used only by the stand-ins and the test signer
SIGNER_TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
failures=0
SIGNER_NAME=v4d-signer
STATUS_URL=http://127.0.0.1:8000/plugins/acctpool/status

cleanup() {
    podman rm -f v4d-db v4d-worker v4d-signer v4d-signer-real v4d-fill >/dev/null 2>&1
    podman volume rm -f v4d-signer-data >/dev/null 2>&1
    podman network rm -f v4d-net >/dev/null 2>&1
    podman unshare rm -rf "$WORK" 2>/dev/null || rm -rf "$WORK"
}
trap cleanup EXIT

# the containers run as users that are not the test user: they must be able to enter the folder
chmod 755 "$WORK"
mkdir -p "$WORK/fake/acctpool_signer" "$WORK/data" "$WORK/run" "$WORK/probe-state" "$WORK/real" "$WORK/hw"
chmod 755 "$WORK/hw"
# the high-water file of the host (SPEC 7.11); the real signer of part 2 and 4 has it as a read-only mount
HW=$WORK/hw/audit.highwater
ZERO_MARK="0 $(printf '0%.0s' $(seq 1 64))"
# hw_set <content>: a new file in the place of the old one (rename, as the probe does). A running container
# keeps the file of its start: a bind mount of a file holds the old file after a rename.
hw_set() {
    printf '%s\n' "$1" >"$HW.new" && chmod 644 "$HW.new" && mv -f "$HW.new" "$HW"
}
hw_set "$ZERO_MARK"
VOLUME_DIR=$WORK/data
AS_ROOT=
printf '%s' "$SIGNER_TOKEN" >"$WORK/run/signer.token"
chmod 644 "$WORK/run/signer.token"

# a plugins tree with the owner of the user who runs the test, and its checksum list
TREE=$WORK/plugins-docker
OWNER=$(id -u)
mkdir -p "$TREE/acctpool/rules/__pycache__" "$TREE/acctpool/components" "$TREE/other/rules"
cp "$PLUGIN/rules/91_acctpool.py" "$TREE/acctpool/rules/"
cp "$PLUGIN/components/acctpool.yml" "$TREE/acctpool/components/"
echo "def rule(services, settings): pass" >"$TREE/other/rules/50_other.py"
echo "bytecode" >"$TREE/acctpool/rules/__pycache__/91_acctpool.cpython-312.pyc"
# the test user is in the place of root: owner = test user, no write bit for group and others
chmod -R go-w,a+rX "$TREE"
make_list() {
    (cd "$TREE" && find . -type f ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum) >"$WORK/plugins-docker.sha256"
}
make_list

cat >"$WORK/fake/acctpool_signer/__init__.py" <<'PY'
PY
cat >"$WORK/fake/acctpool_signer/__main__.py" <<'PY'
import sys

if sys.argv[1:] == ["audit-verify"]:
    with open("/data/verify_rc") as f:
        sys.exit(int(f.read()))
sys.exit(64)
PY
cat >"$WORK/fake/server.py" <<'PY'
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def state():
    with open("/fake/state.json") as f:
        return json.load(f)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, code, body=None):
        data = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        s = state()
        auth = self.headers.get("Authorization", "")
        if self.server.server_port == 7070:
            if self.path != "/v1/status":
                return self.answer(404)
            if auth != "Bearer " + s["signer_token"]:
                return self.answer(401, {"error": "unauthorized"})
            return self.answer(200, {"keystore": s["keystore"], "seed_id": "0123456789abcdef"})
        if self.path != "/plugins/acctpool/status" or not s["plugin"]:
            return self.answer(404)
        # the probe sends no token, so the real route answers 401
        return self.answer(401, {"detail": "Not authenticated"})


for port in (7070, 8000):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever).start()
PY

set_state() { # keystore plugin
    printf '{"signer_token": "%s", "keystore": %s, "plugin": %s}\n' "$SIGNER_TOKEN" "$1" "$2" >"$WORK/fake/state.json"
}

# journal : the audit.log of the stand-in signer. Lines come from stdin, newest last:
#   <age in seconds> <call> <result> [replay] [notjson]
# The audit lines have the form of the real signer (ts "%Y-%m-%dT%H:%M:%S.%fZ", call, result, replay, seq).
cat >"$WORK/journal.py" <<'PY'
import datetime, json, sys, time
now = time.time()
with open(sys.argv[1], "w") as log:
    for seq, text in enumerate([line for line in sys.stdin.read().splitlines() if line.strip()], 1):
        age, call, result, *rest = text.split()
        moment = datetime.datetime.fromtimestamp(now - float(age), datetime.timezone.utc)
        entry = {"ts": moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "call": call, "result": result, "seq": seq}
        if "replay" in rest:
            entry["replay"] = True
        log.write(("this is not json" if "notjson" in rest else json.dumps(entry)) + "\n")
PY
journal() {
    python3 "$WORK/journal.py" "$WORK/data/audit.log" || { echo "FAIL cannot write the test audit.log"; exit 2; }
    chmod 644 "$WORK/data/audit.log"
    # each stand-in audit.log is a new volume: its high-water file starts at 0 (the tests of the mark are below)
    hw_set "$ZERO_MARK"
}
repeat() { # count, line
    local i
    for i in $(seq 1 "$1"); do echo "$((30 + i)) $2"; done
}
# the normal audit.log: a start line, old signatures, a refusal by a cap, and <count> signatures of the last hour
normal_journal() { # count of new signatures, extra lines
    {
        echo "90000 start ok"
        repeat 100 "sign/sweep ok" | sed 's/^/72/'
        echo "60 sign/fund cap_exceeded"
        echo "60 derive ok"
        repeat "$1" "sign/fund ok"
        printf '%s' "${2:-}"
    } | sort -rn | journal
}

db() {
    podman exec -i v4d-db psql -U postgres -d bitcart -X -q -v ON_ERROR_STOP=1 >/dev/null
}

set_state true true
echo 0 >"$WORK/data/verify_rc"
chmod 755 "$WORK/data" "$WORK/fake" "$WORK/fake/acctpool_signer" "$WORK/run"
chmod 644 "$WORK/fake"/*.py "$WORK/fake/acctpool_signer"/*.py "$WORK/data/verify_rc"
normal_journal 5

start_worker() { # network options
    podman rm -f v4d-worker >/dev/null 2>&1
    podman run -d --name v4d-worker --log-driver k8s-file --security-opt label=disable "$@" \
        -v "$WORK/fake:/fake:ro" -v "$WORK/run:/run/acctpool:ro" \
        --entrypoint python docker.io/bitcart/bitcart:0.10.3.0 /fake/server.py >/dev/null
}

podman rm -f v4d-db v4d-worker v4d-signer v4d-signer-real >/dev/null 2>&1
podman run -d --name v4d-db --log-driver k8s-file -e POSTGRES_USER=postgres -e POSTGRES_DB=bitcart -e POSTGRES_HOST_AUTH_METHOD=trust \
    docker.io/library/postgres:17-alpine >/dev/null || exit 2
start_worker --network none -e ACCTPOOL_SIGNER_URL=http://127.0.0.1:7070 || exit 2
# stand_in_signer <lines>: a new stand-in signer; its main process prints the lines to stderr (the container log),
# as the v4 signer prints one JSON line per request there. python -I of the probe ignores PYTHONPATH: the false
# package is in the place where the real image has it.
stand_in_signer() {
    printf '%s\n' "${1:-}" >"$WORK/signer-log.txt" && chmod 644 "$WORK/signer-log.txt"
    podman rm -f v4d-signer >/dev/null 2>&1
    podman run -d --name v4d-signer --log-driver k8s-file --network none --security-opt label=disable \
        -v "$WORK/fake/acctpool_signer:/home/electrum/.venv/lib/python3.12/site-packages/acctpool_signer:ro" \
        -v "$WORK/data:/data:ro" -v "$WORK/signer-log.txt:/signer-log.txt:ro" --entrypoint python \
        docker.io/bitcart/bitcart-eth:0.10.3.0 -c 'import sys, time
sys.stderr.write(open("/signer-log.txt").read())
sys.stderr.flush()
time.sleep(3600)' >/dev/null || exit 2
    for _ in $(seq 1 20); do
        [ "$(podman logs v4d-signer 2>&1 | wc -l)" -ge "$(grep -c '' "$WORK/signer-log.txt")" ] && return 0
        sleep 0.5
    done
}
# request <status>...: request lines of the v4 signer (stderr), one per status
request() {
    local status
    for status in "$@"; do
        printf '2026-09-29 12:00:00,000 INFO acctpool_signer: {"method": "POST", "path": "/v1/sign/fund", "status": %s, "remote": "10.89.0.3", "ms": 2}\n' "$status"
    done
}
stand_in_signer "$(request 200 200 403 409)"

for _ in $(seq 1 60); do
    # the image starts a temporary server first: wait for the real one with the bitcart database
    podman exec v4d-db psql -U postgres -d bitcart -X -Atc 'select 1' >/dev/null 2>&1 && sleep 2 &&
        podman exec v4d-db psql -U postgres -d bitcart -X -Atc 'select 1' >/dev/null 2>&1 && break
    sleep 1
done

# only the columns that the probe reads, with the names and types of the backend migration (SPEC-v4 4.3)
db <<SQL || { echo "FAIL cannot create the test tables"; exit 2; }
create table plugin_acctpool_pools (id bigserial primary key, wallet_id text, chain text, asset text, store text,
    enabled boolean);
create table plugin_acctpool_addresses (id bigserial primary key, store text, index int, address text, status text,
    invoice_id text, assigned_at timestamptz);
create table plugin_acctpool_payouts (id bigserial primary key, address_id bigint, chain text, kind text, state text,
    created timestamptz not null default now());
create table plugin_acctpool_events (id bigserial primary key, kind text, chain text, address text,
    detail jsonb not null default '{}', created timestamptz not null default now());
create table plugin_acctpool_state (key text primary key, value jsonb not null, updated timestamptz not null default now());
insert into plugin_acctpool_pools (wallet_id, chain, asset, store, enabled) values
    ('w1', 'polygon', 'usdt', 'teststore', true), ('w2', 'ethereum', 'usdt', 'offstore', false);
insert into plugin_acctpool_addresses (store, index, address, status, assigned_at)
    select 'teststore', i, 'a' || i, case when i < 25 then 'ready' else 'retired' end,
           case when i < 25 then null else now() - interval '3 days' end from generate_series(0, 39) i;
update plugin_acctpool_addresses set status = 'pending_payout', assigned_at = now() - interval '23 hours' where index = 25;
insert into plugin_acctpool_payouts (address_id, chain, kind, state, created) values
    (1, 'polygon', 'fund', 'confirmed', now() - interval '3 days'),
    (2, 'polygon', 'sweep', 'failed', now() - interval '3 days'),
    (3, 'polygon', 'sweep', 'broadcast', now() - interval '1 hour');
insert into plugin_acctpool_events (kind, chain, created) values
    ('payout_failed', 'polygon', now() - interval '2 hours'), ('payout', 'polygon', now() - interval '5 minutes');
insert into plugin_acctpool_state (key, value) values ('leader', '{"worker": "w"}'), ('signer', '{"keystore": true}');
SQL

OUT=
PROBE_RC=0
PROBE_MODE=
probe() { # extra environment as NAME=value arguments; PROBE_MODE: the argument of the probe (files, highwater)
    OUT=$(env ACCTPOOL_DOCKER=podman ACCTPOOL_SIGNER_CONTAINER="$SIGNER_NAME" ACCTPOOL_WORKER_CONTAINER=v4d-worker \
        ACCTPOOL_DATABASE_CONTAINER=v4d-db ACCTPOOL_PROBE_STATUS_URL="$STATUS_URL" \
        ACCTPOOL_PLUGINS_DIR="$TREE" ACCTPOOL_PLUGINS_OWNER="$OWNER" ACCTPOOL_CHECKSUM_FILE="$WORK/plugins-docker.sha256" \
        ACCTPOOL_PROBE_STATE_DIR="$WORK/probe-state" ACCTPOOL_HIGHWATER_HOST_FILE="$HW" \
        ACCTPOOL_SIGNER_VOLUME=v4d-signer-data ACCTPOOL_SIGNER_VOLUME_DIR="$VOLUME_DIR" ACCTPOOL_AS_ROOT="$AS_ROOT" \
        "$@" bash "$PROBE" ${PROBE_MODE:+"$PROBE_MODE"} 2>&1)
    PROBE_RC=$?
}

ALL="signer-container signer-status plugin-loaded engine-alive open-invoices ready-addresses payout-age alert-events \
audit-chain audit-highwater signer-rate signer-refusals signer-journal signer-volume plugin-files"
LINES=15

# expect <test name> <names that must FAIL, in one argument>; every other name must be OK
expect() {
    local title=$1 must_fail=" $2 " name want got problems=""
    for name in $ALL; do
        want=OK
        case "$must_fail" in *" $name "*) want=FAIL ;; esac
        got=$(printf '%s\n' "$OUT" | grep -E "^(OK|FAIL) acctpool-$name( |$)" | cut -d' ' -f1 | tr '\n' ' ')
        [ "$got" = "$want " ] || problems="$problems $name(want $want, got ${got:-nothing})"
    done
    # form: only result lines, one line for each check, no token
    printf '%s\n' "$OUT" | grep -Evq '^(OK acctpool-[a-z-]+|FAIL acctpool-[a-z-]+ [^ ].*)$' && problems="$problems bad-line-form"
    [ "$(printf '%s\n' "$OUT" | wc -l)" = "$LINES" ] || problems="$problems line-count"
    printf '%s\n' "$OUT" | grep -Fq -e "$SIGNER_TOKEN" && problems="$problems TOKEN-IN-OUTPUT"
    if [ -z "$problems" ]; then
        echo "pass  $title"
        [ -z "${VERBOSE:-}" ] || printf '%s\n' "$OUT" | grep '^FAIL' | sed 's/^/        /'
    else
        echo "FAIL  $title:$problems"
        printf '%s\n' "$OUT" | sed 's/^/        /'
        failures=$((failures + 1))
    fi
}

echo "=== part 1: stand-ins"
probe
expect "all good" ""
echo "--- output of the good run"
printf '%s\n' "$OUT"
echo "---"

# ----------------------------------------------------------------------------- ready addresses
db <<<"update plugin_acctpool_addresses set status = 'retired' where index < 6"
probe
expect "19 ready addresses, minimum 20" "ready-addresses"
probe ACCTPOOL_READY_MIN=19
expect "19 ready addresses, minimum 19" ""
db <<<"update plugin_acctpool_pools set enabled = true where wallet_id = 'w2'"
probe ACCTPOOL_READY_MIN=19
expect "second enabled signer store with no address" "ready-addresses"
db <<<"update plugin_acctpool_pools set enabled = false where wallet_id = 'w2';
       update plugin_acctpool_addresses set status = 'ready' where index < 6"

# ----------------------------------------------------------------------------- payouts
db <<<"insert into plugin_acctpool_payouts (address_id, chain, kind, state, created)
       values (3, 'polygon', 'sweep', 'signed', now() - interval '25 hours')"
probe
expect "payout 25 hours not final" "payout-age"
db <<<"update plugin_acctpool_payouts set state = 'confirmed' where state = 'signed'"
db <<<"update plugin_acctpool_addresses set assigned_at = now() - interval '25 hours' where index = 25"
probe
expect "address 25 hours in pending_payout" "payout-age"
db <<<"update plugin_acctpool_addresses set status = 'in_payout' where index = 25"
probe
expect "address 25 hours in in_payout" "payout-age"
db <<<"update plugin_acctpool_addresses set status = 'pending_payout', assigned_at = now() - interval '23 hours' where index = 25"
probe
expect "address 23 hours in pending_payout; retired for 3 days" ""

# ----------------------------------------------------------------------------- leader and open invoices
leader() { db <<<"update plugin_acctpool_state set updated = now() - interval '$1 minutes' where key = 'leader'"; }
leader 6
probe
expect "leader row is 6 minutes old, no open pool invoice" "engine-alive"
db <<<"update plugin_acctpool_addresses set status = 'in_invoice', invoice_id = 'inv' || index where index in (30, 31)"
probe
expect "leader row is 6 minutes old, 2 pool invoices are open" "engine-alive open-invoices"
[ -z "${VERBOSE:-}" ] || printf '%s\n' "$OUT" | grep open-invoices | sed 's/^/        /'
db <<<"delete from plugin_acctpool_state where key = 'leader'"
probe
expect "no leader row, pool invoices are open" "engine-alive open-invoices"
db <<<"insert into plugin_acctpool_state (key, value) values ('leader', '{}')"
probe
expect "leader alive and route there, pool invoices are open" ""
set_state true false
probe
expect "plugin route missing, pool invoices are open" "plugin-loaded open-invoices"
db <<<"update plugin_acctpool_addresses set status = 'pending_payout', assigned_at = now() where index in (30, 31)"
probe
expect "plugin route missing, the pool invoices are paid and wait for payouts" "plugin-loaded"
db <<<"update plugin_acctpool_addresses set status = 'retired' where index in (30, 31)"
set_state true true

# ----------------------------------------------------------------------------- alert events of the plugin
db <<<"insert into plugin_acctpool_events (kind, chain, address, detail, created) values
       ('second_opinion_mismatch', 'polygon', '0xabc', '{\"detail\": \"<script>\"}', now() - interval '10 minutes')"
probe
expect "a second-opinion mismatch 10 minutes ago" "alert-events"
printf '%s\n' "$OUT" | grep -q "^FAIL acctpool-alert-events in the last hour: 1 second_opinion_mismatch$" \
    && echo "pass  the line names the kind and the count only" \
    || { echo "FAIL  the line of the alert: $(printf '%s\n' "$OUT" | grep alert-events)"; failures=$((failures + 1)); }
probe ACCTPOOL_ALERT_EVENTS_MAX=1
expect "1 event with the limit 1" ""
db <<<"insert into plugin_acctpool_events (kind, chain, created) values ('fee_wallet_low', 'polygon', now() - interval '1 minute')"
probe ACCTPOOL_ALERT_EVENTS_MAX=1
expect "a second alert: fee_wallet_low" "alert-events"
db <<<"insert into plugin_acctpool_events (kind, chain, created) values ('credit', 'polygon', now())"
db <<<"update plugin_acctpool_events set created = now() - interval '61 minutes' where kind <> 'credit'"
probe
expect "the alerts are older than 1 hour; a credit event is no alert" ""

# ----------------------------------------------------------------------------- stand-in signer and its journal
set_state false true
probe
expect "signer has no keystore" "signer-status"
set_state true true
chmod 600 "$WORK/run/signer.token"
probe
expect "worker user cannot read the token file" "signer-status"
chmod 644 "$WORK/run/signer.token"
printf 'wrong-token-wrong-token-wrong-token' >"$WORK/run/signer.token"
probe
expect "worker has a wrong token" "signer-status"
printf '%s' "$SIGNER_TOKEN" >"$WORK/run/signer.token"

echo 1 >"$WORK/data/verify_rc"
probe
expect "audit chain broken (audit-verify runs in each run): the mark is not moved" "audit-chain audit-highwater"
echo 0 >"$WORK/data/verify_rc"
probe
expect "audit chain good in the next run" ""
probe ACCTPOOL_PROBE_STATE_DIR="$WORK/plugins-docker.sha256/folder"
expect "the state folder cannot be made (no lock for the write of the mark)" "audit-highwater"
probe
expect "audit chain good again" ""

normal_journal 41
probe
expect "41 signatures in one hour, maximum 40" "signer-rate"
normal_journal 40 "$(repeat 30 'sign/sweep ok replay')
"
probe
expect "40 signatures and 30 replays in one hour, maximum 40" ""
normal_journal 5 "40 sign/fund ok notjson
"
probe
expect "audit line that is not JSON" "signer-rate"
normal_journal 5

# refusals: the request lines on stderr (a wrong token gets 401 and no audit line); audit lines are no refusals
stand_in_signer "$(request 200 429)
{\"ts\": \"2026-09-29T12:00:00.000000Z\", \"call\": \"sign/fund\", \"result\": \"rate_limited\", \"seq\": 9}
"
probe
expect "one call was refused by the rate limit" "signer-refusals"
probe ACCTPOOL_SIGNER_REFUSALS_MAX=1
expect "one refusal with the limit 1" ""
stand_in_signer "$(request 401 401 401 500 200)"
probe ACCTPOOL_SIGNER_REFUSALS_MAX=2
expect "3 wrong tokens and 1 internal error, limit 2" "signer-refusals"
printf '%s\n' "$OUT" | grep -q "^FAIL acctpool-signer-refusals in the last hour: 3 http 401, maximum 2 each$" \
    && echo "pass  the line names the status and the count only" \
    || { echo "FAIL  the refusal line: $(printf '%s\n' "$OUT" | grep signer-refusals)"; failures=$((failures + 1)); }
probe
expect "3 wrong tokens and 1 internal error, limit 0" "signer-refusals"
[ -z "${VERBOSE:-}" ] || printf '%s\n' "$OUT" | grep signer-refusals | sed 's/^/        /'
stand_in_signer "$(request 200 403 409 404)"
probe
expect "200, 403, 409 and 404 are no refusals" ""

# the volume continues the chain of the high-water mark (the real signer: part 2)
probe
LINES_NOW=$(grep -c '' "$WORK/data/audit.log")
[ "$(cut -d' ' -f1 "$HW")" = "$LINES_NOW" ]
check_mark=$?
[ "$check_mark" = 0 ] && echo "pass  the probe wrote the mark: line $LINES_NOW" || { echo "FAIL  the mark: $(cat "$HW")"; failures=$((failures + 1)); }
MARK_NOW=$(cat "$HW")
hw_set "$((LINES_NOW + 3)) ${MARK_NOW#* }"
probe
expect "the volume ends below the line of the mark (older or new volume)" "signer-journal audit-highwater"
probe
expect "the alarm stays: the mark does not move" "signer-journal audit-highwater"
hw_set "$LINES_NOW $(printf '%s' other | sha256sum | cut -d' ' -f1)"
probe
expect "line of the mark with another hash (another volume)" "signer-journal audit-highwater"
hw_set "$MARK_NOW"
probe
expect "the mark of this volume again" ""
mv "$WORK/data/audit.log" "$WORK/data/audit.log.away"
probe
expect "the signer has no audit.log" "signer-rate signer-journal audit-highwater"
mv "$WORK/data/audit.log.away" "$WORK/data/audit.log"
probe
expect "audit.log good again" ""

truncate -s 501M "$WORK/data/big-file"
probe
expect "the signer volume has 501 MB, maximum 500" "signer-volume"
rm -f "$WORK/data/big-file"

# ----------------------------------------------------------------------------- plugins tree
echo "# changed" >>"$TREE/acctpool/rules/91_acctpool.py"
probe
expect "a rule file is changed" "plugin-files"
cp "$PLUGIN/rules/91_acctpool.py" "$TREE/acctpool/rules/"
echo "def rule(services, settings): pass" >"$TREE/other/rules/99_new.py"
probe
expect "a rule file that is not in the list" "plugin-files"
rm "$TREE/other/rules/99_new.py"
mv "$TREE/acctpool/components/acctpool.yml" "$WORK/"
probe
expect "a file of the list is missing" "plugin-files"
mv "$WORK/acctpool.yml" "$TREE/acctpool/components/"
mkdir -p "$TREE/other/rules/__pycache__" && echo "more bytecode" >"$TREE/other/rules/__pycache__/50_other.cpython-312.pyc"
chmod go-w "$TREE/other/rules/__pycache__" "$TREE/other/rules/__pycache__/50_other.cpython-312.pyc"
probe
expect "a new __pycache__ file is not an alarm" ""
podman unshare chown 1:1 "$TREE/other/rules/50_other.py"
probe
expect "one file has an owner that is not the owner of the tree" "plugin-files"
podman unshare chown 0:0 "$TREE/other/rules/50_other.py"
chmod g+w "$TREE/acctpool/rules/91_acctpool.py"
probe
expect "one rule file has the write bit for the group" "plugin-files"
chmod g-w "$TREE/acctpool/rules/91_acctpool.py"
chmod o+w "$TREE/other/rules"
probe
expect "one rules folder has the write bit for others" "plugin-files"
chmod o-w "$TREE/other/rules"
chmod 666 "$TREE/acctpool/rules/__pycache__/91_acctpool.cpython-312.pyc"
probe
expect "one bytecode file has mode 666" "plugin-files"
chmod 644 "$TREE/acctpool/rules/__pycache__/91_acctpool.cpython-312.pyc"
ln -s /etc/hostname "$TREE/other/link"
probe
expect "a symbolic link in the tree" "plugin-files"
rm "$TREE/other/link"
probe ACCTPOOL_CHECKSUM_FILE="$WORK/none"
expect "no checksum list" "plugin-files"
probe ACCTPOOL_PLUGINS_DIR="$WORK/none"
expect "no plugins folder" "plugin-files"
probe
expect "plugins tree is good again" ""

files_mode() { # expected exit code, expected first word, title, extra environment
    local want_rc=$1 want_word=$2 title=$3 out rc
    shift 3
    out=$(env ACCTPOOL_PLUGINS_DIR="$TREE" ACCTPOOL_PLUGINS_OWNER="$OWNER" \
        ACCTPOOL_CHECKSUM_FILE="$WORK/plugins-docker.sha256" ACCTPOOL_DOCKER=/bin/false "$@" bash "$PROBE" files 2>&1)
    rc=$?
    if [ "$rc" = "$want_rc" ] && [ "$(printf '%s\n' "$out" | wc -l)" = 1 ] && [ "${out%% *}" = "$want_word" ] &&
        [ "$(printf '%s' "$out" | cut -d' ' -f2)" = acctpool-plugin-files ]; then
        echo "pass  $title"
    else
        echo "FAIL  $title: exit code $rc, output: $out"
        failures=$((failures + 1))
    fi
}
files_mode 0 OK "argument files: good tree, one line, exit code 0"
echo "# changed" >>"$TREE/other/rules/50_other.py"
files_mode 1 FAIL "argument files: changed rule file, one line, exit code 1"
make_list
files_mode 0 OK "argument files: new checksum list"
files_mode 1 FAIL "argument files: no checksum list" ACCTPOOL_CHECKSUM_FILE="$WORK/none"

probe ACCTPOOL_READY_MIN="1; drop table plugin_acctpool_pools"
[ "$OUT" = "FAIL acctpool-probe-config a threshold is not a whole number" ] && echo "pass  threshold that is not a number" ||
    { echo "FAIL  threshold that is not a number: $OUT"; failures=$((failures + 1)); }
probe
expect "tables are still there after the bad threshold" ""

# ----------------------------------------------------------------------------- part 2: the real signer
echo "=== part 2: the real signer image $SIGNER_IMAGE"
if ! podman image exists "$SIGNER_IMAGE"; then
    echo "SKIP  part 2: the image does not exist. The formats of the real signer are NOT tested."
    PART2=skipped
else
    PART2=done
    podman rm -f v4d-signer >/dev/null 2>&1
    # pools.toml: the example of the deploy folder, with test addresses in the place of the placeholders
    cp "$PLUGIN/deploy/pools.toml.example" "$WORK/real/pools.toml.example"
    chmod 777 "$WORK/real"
    podman run --rm --name v4d-fill --log-driver k8s-file --network none --security-opt label=disable -v "$WORK/real:/real" \
        --entrypoint python docker.io/bitcart/bitcart-eth:0.10.3.0 -c '
import os, re
from eth_utils import to_checksum_address
text = open("/real/pools.toml.example").read()
text = re.sub(r"\"0xREPLACE_[A-Z_]+\"", lambda m: "\"" + to_checksum_address(os.urandom(20)) + "\"", text)
open("/real/pools.toml", "w").write(text)' || { echo "FAIL cannot make pools.toml"; exit 2; }
    head -c 32 /dev/urandom >"$WORK/real/master.key"
    printf '%s' "$SIGNER_TOKEN" >"$WORK/real/signer.token"
    podman unshare chown 10001:10001 "$WORK/real/master.key" "$WORK/real/signer.token"
    podman unshare chmod 600 "$WORK/real/master.key" "$WORK/real/signer.token"
    podman unshare chmod 644 "$WORK/real/pools.toml"
    podman network create --internal v4d-net >/dev/null || exit 2
    # the component: the high-water file of the host is a read-only mount; no init program
    start_signer() {
        podman run -d --name v4d-signer-real --log-driver k8s-file --network v4d-net --security-opt label=disable \
            --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --memory 300m \
            -v v4d-signer-data:/data -v "$WORK/real/pools.toml:/etc/acctpool/pools.toml:ro" \
            -v "$WORK/real/master.key:/run/acctpool/master.key:ro" \
            -v "$WORK/real/signer.token:/run/acctpool/signer.token:ro" \
            -v "$HW:/run/acctpool/audit.highwater:ro" "$SIGNER_IMAGE" >/dev/null
    }
    # until the signer answers /v1/status with the token (a status call with the token writes no audit line)
    wait_signer() {
        for _ in $(seq 1 30); do
            [ "$(podman inspect -f '{{.State.Status}}' v4d-signer-real 2>/dev/null)" = running ] &&
                podman exec v4d-signer-real python -I -c 'import urllib.request
token = open("/run/acctpool/signer.token").read().strip()
request = urllib.request.Request("http://127.0.0.1:7070/v1/status", headers={"Authorization": "Bearer " + token})
urllib.request.urlopen(request, timeout=2)' 2>/dev/null && return 0
            sleep 1
        done
        return 1
    }
    new_volume() {
        podman volume rm -f v4d-signer-data >/dev/null 2>&1
        podman volume create v4d-signer-data >/dev/null || exit 2
        VOLDIR=$(podman volume inspect -f '{{.Mountpoint}}' v4d-signer-data)
        case "$VOLDIR" in */volumes/v4d-signer-data/_data) ;; *) echo "FAIL unexpected volume folder: $VOLDIR"; exit 2 ;; esac
    }
    # "<seq> <sha256>" of the last line of audit.log in a folder, made without the code of the probe
    last_mark() {
        podman unshare sh -c 'tail -n 1 "$1"' _ "$1/audit.log" | python3 -c 'import hashlib, json, sys
line = sys.stdin.buffer.read().rstrip(b"\n")
print(json.loads(line)["seq"], hashlib.sha256(line).hexdigest())'
    }
    check() { # status, title
        if [ "$1" = 0 ]; then
            echo "pass  $2"
        else
            echo "FAIL  $2"
            failures=$((failures + 1))
        fi
    }
    hw_is() { [ -f "$HW" ] && [ "$(cat "$HW")" = "$1" ]; }
    show() { printf '%s\n' "$OUT" | grep -E "$1" | cut -c1-230 | sed 's/^/        /'; }

    new_volume
    VOLUME_DIR=
    AS_ROOT="podman unshare"
    # the deploy step: "0" and 64 zeros before the first start
    hw_set "$ZERO_MARK"
    start_signer || { echo "FAIL the signer container did not start"; exit 2; }
    start_worker --network v4d-net -e ACCTPOOL_SIGNER_URL=http://v4d-signer-real:7070 || exit 2
    SIGNER_NAME=v4d-signer-real
    wait_signer || { echo "FAIL the signer does not answer"; podman logs v4d-signer-real 2>&1 | tail -3 | cut -c1-200; exit 2; }
    podman logs v4d-signer-real 2>&1 | tail -3 | cut -c1-200 | sed 's/^/        signer log: /'

    probe
    expect "real signer without a keystore, high-water file 0: only the keystore check fails" "signer-status"
    show 'signer|audit'

    # ------------------------------------------------------------------------- high-water file (SPEC 7.11)
    MARK1=$(last_mark "$VOLDIR")
    hw_is "$MARK1" && [ "$(stat -c %a "$HW")" = 644 ] && [ "${MARK1%% *}" -ge 1 ]
    check $? "high-water: the first run from the zero file writes the last line of the volume (line ${MARK1%% *}, mode 644)"
    ! ls -A "$WORK/hw" | grep -q tmp
    check $? "high-water: no temp file is left in the folder"
    podman restart -t 5 v4d-signer-real >/dev/null && wait_signer
    SAYS=$(podman exec v4d-signer-real python -I -m acctpool_signer audit-verify 2>&1)
    grep -q "the log has line ${MARK1%% *} of the high-water file" <<<"$SAYS"
    check $? "high-water: after a restart the real signer reads the mark of the probe"
    grep -v WARNING <<<"$SAYS" | cut -c1-200 | sed 's/^/        audit-verify: /'
    probe
    expect "real signer after a restart" "signer-status"
    MARK2=$(last_mark "$VOLDIR")
    hw_is "$MARK2" && [ "${MARK2%% *}" -gt "${MARK1%% *}" ]
    check $? "high-water: normal advance from line ${MARK1%% *} to line ${MARK2%% *}"

    # The next cases put a file on the host by rename. The running signer keeps the file of its start (line
    # MARK1), so its audit-verify still gives 0, and only the comparison of the probe can see the case.
    SEQ2=${MARK2%% *}
    HASH2=${MARK2#* }
    OTHER=$(printf '%s' other | sha256sum | cut -d' ' -f1)
    hw_set "$((SEQ2 + 5)) $HASH2"
    probe
    expect "high-water: the file has line $((SEQ2 + 5)), the volume ends with line $SEQ2 (a restore)" "signer-status signer-journal audit-highwater"
    show audit-highwater
    hw_is "$((SEQ2 + 5)) $HASH2"
    check $? "high-water: a lower seq in the volume: nothing written"
    hw_set "$SEQ2 $OTHER"
    probe
    expect "high-water: the same seq with another hash" "signer-status signer-journal audit-highwater"
    show audit-highwater
    hw_is "$SEQ2 $OTHER"
    check $? "high-water: the same seq with another hash: nothing written"
    hw_set "$MARK2"
    probe
    expect "high-water: the same seq and the same hash: nothing to do" "signer-status"

    # audit-verify fails: a complete line with the next seq and a wrong prev at the end of audit.log. The probe
    # would take this line as the new mark if it did not need exit code 0 of audit-verify.
    SIZE=$(podman unshare stat -c %s "$VOLDIR/audit.log")
    podman unshare sh -c 'printf "%s\n" "$2" >>"$1"' _ "$VOLDIR/audit.log" \
        "{\"ts\": \"2026-01-01T00:00:00.000000Z\", \"call\": \"test\", \"prev\": \"$OTHER\", \"seq\": $((SEQ2 + 1))}"
    probe
    expect "high-water: audit-verify fails (a changed line at the end of audit.log)" "signer-status audit-chain audit-highwater"
    show audit-highwater
    hw_is "$MARK2"
    check $? "high-water: audit-verify failed: nothing written"
    podman unshare truncate -s "$SIZE" "$VOLDIR/audit.log"

    mv "$HW" "$HW.saved" && mkdir "$HW"
    probe
    expect "high-water: the file is a folder" "signer-status audit-highwater"
    show audit-highwater
    [ -d "$HW" ] && [ -z "$(ls -A "$HW")" ]
    check $? "high-water: a folder: nothing written"
    rmdir "$HW"
    probe
    expect "high-water: the file is missing" "signer-status audit-highwater"
    show audit-highwater
    [ ! -e "$HW" ]
    check $? "high-water: a missing file: nothing written"
    mv "$HW.saved" "$HW"

    # the mode for the backup job: two lines, exit code 1 on a FAIL
    PROBE_MODE=highwater probe
    [ "$PROBE_RC" = 0 ] && [ "$OUT" = "OK acctpool-audit-chain
OK acctpool-audit-highwater" ]
    check $? "argument highwater: two OK lines, exit code 0"
    hw_set "$((SEQ2 + 5)) $HASH2"
    PROBE_MODE=highwater probe
    [ "$PROBE_RC" = 1 ] && [ "$(printf '%s\n' "$OUT" | wc -l)" = 2 ] && printf '%s\n' "$OUT" | grep -q '^FAIL acctpool-audit-highwater ' &&
        hw_is "$((SEQ2 + 5)) $HASH2"
    check $? "argument highwater: a lower seq in the volume: FAIL line, exit code 1, nothing written"
    hw_set "$MARK2"

    # ------------------------------------------------------------------------- refusals in the container log
    podman exec -u electrum v4d-worker python -c '
import urllib.request, urllib.error
for path in ("/v1/status", "/v1/derive", "/v1/sign/fund"):
    request = urllib.request.Request("http://v4d-signer-real:7070" + path, headers={"Authorization": "Bearer wrong-token-" + "x" * 32})
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as e:
        print("        call with a wrong token:", path, "answer", e.code)'
    sleep 1
    probe
    expect "real signer: calls with a wrong token are seen" "signer-status signer-refusals"
    show refusals

    podman restart -t 5 v4d-signer-real >/dev/null && wait_signer
    probe ACCTPOOL_SIGNER_REFUSALS_MAX=100
    expect "real signer after a restart: the volume continues the mark" "signer-status"

    # a new empty volume while the high-water file has the mark of the old one
    MARK3=$(cat "$HW")
    podman rm -f v4d-signer-real >/dev/null
    new_volume
    start_signer
    CODE=$(timeout 30 podman wait v4d-signer-real 2>/dev/null)
    LOG=$(podman logs v4d-signer-real 2>&1)
    [ "$CODE" = 1 ] &&
        grep -q "audit.log ends with line 0, and the high-water file /run/acctpool/audit.highwater has line ${MARK3%% *}" <<<"$LOG" &&
        grep -q "high-water file" <<<"$LOG"
    check $? "a new empty volume and the mark of the old volume: the start is refused (exit code ${CODE:-none}) and names the high-water file"
    grep 'refused' <<<"$LOG" | cut -c1-230 | sed 's/^/        /'
    # deploy guide 4.4: a new volume before money arrived gets the zero mark again
    podman rm -f v4d-signer-real >/dev/null
    hw_set "$ZERO_MARK"
    start_signer && wait_signer
    probe ACCTPOOL_SIGNER_REFUSALS_MAX=100
    expect "real signer with a new volume and the zero mark (set by a person): no alarm" "signer-status"
fi

# ----------------------------------------------------------------------------- containers that are not there
echo "=== part 3: containers that are stopped"
# the stand-in for the signer status is in the worker container; the real signer is its own container
STATUS=""
[ "$PART2" = done ] && STATUS="signer-status"
# the volume and the container log of a stopped container can still be read
GONE="signer-container $STATUS audit-chain audit-highwater"
podman stop -t 1 "$SIGNER_NAME" >/dev/null 2>&1
probe
expect "signer container stopped" "$GONE"
podman rm -f "$SIGNER_NAME" >/dev/null 2>&1
GONE="$GONE signer-refusals"
probe
expect "signer container missing" "$GONE"

podman stop -t 1 v4d-db >/dev/null 2>&1
probe
expect "database stopped" "$GONE \
engine-alive open-invoices ready-addresses payout-age alert-events"
podman stop -t 1 v4d-worker >/dev/null 2>&1
probe
expect "every container stopped (the volume and the file checks need no container)" "$GONE \
engine-alive open-invoices ready-addresses payout-age alert-events signer-status plugin-loaded"

# ----------------------------------------------------------------------------- part 4: end to end
# Seed, mark of the probe, restore of an older copy of the volume, refused start. The seed is made as the signer
# README says (exec -it into the running signer); a program types the 3 words back. The words stay in the memory
# of that program: it prints numbers only. The seed is a throwaway test seed.
if [ "$PART2" = done ]; then
    echo "=== part 4: end to end: seed, mark of the probe, older copy of the volume, refused start"
    podman rm -f v4d-signer-real >/dev/null 2>&1
    new_volume
    hw_set "$ZERO_MARK"
    start_signer && wait_signer
    check $? "e2e: the signer runs with the zero mark and no keystore"
    cat >"$WORK/driver.py" <<'PY'
import os, pty, re, subprocess, sys, time

command = ["podman", "exec", "-it", "v4d-signer-real", "python", "-I", "-m", "acctpool_signer", "init"]
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
ok = os.waitstatus_to_exitcode(status) == 0 and len(words) == 24 and answered == 3 and "seed id: " in text
time.sleep(2)
log = subprocess.run(["podman", "logs", "v4d-signer-real"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode(errors="replace")
found = sum(1 for i in range(23) if " ".join(words[i:i + 2]) in log)
print("init: exit code %d, %d words shown, %d typed back; pairs of words in the container log: %d"
      % (os.waitstatus_to_exitcode(status), len(words), answered, found))
sys.exit(0 if ok and found == 0 else 1)
PY
    python3 "$WORK/driver.py" | sed 's/^/        /'
    check "${PIPESTATUS[0]}" "e2e: seed made with exec -it in the running signer, no word in the container log"
    rm -f "$WORK/driver.py"
    podman restart -t 5 v4d-signer-real >/dev/null && wait_signer &&
        podman exec v4d-signer-real python -I -c 'import json, urllib.request
token = open("/run/acctpool/signer.token").read().strip()
request = urllib.request.Request("http://127.0.0.1:7070/v1/status", headers={"Authorization": "Bearer " + token})
assert json.load(urllib.request.urlopen(request, timeout=5))["keystore"] is True'
    check $? "e2e: after a restart the signer has the keystore"

    # the backup: a copy of the volume while the signer is stopped
    podman stop -t 5 v4d-signer-real >/dev/null
    mkdir -p "$WORK/volume-copy"
    podman unshare cp -a "$VOLDIR/." "$WORK/volume-copy/"
    K=$(last_mark "$WORK/volume-copy")
    K=${K%% *}
    podman start v4d-signer-real >/dev/null && wait_signer
    # signatures after the backup: three derive calls give three audit lines
    podman exec -i v4d-signer-real python -I - <<'PY'
import json, urllib.request
token = open("/run/acctpool/signer.token").read().strip()
for first in (0, 5, 10):
    body = json.dumps({"store": "examplestore", "family": "evm", "first_index": first, "count": 5}).encode()
    head = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:7070/v1/derive", body, head), timeout=5).read()
PY
    check $? "e2e: three derive calls after the backup"
    PROBE_MODE=highwater probe
    M=$(cut -d' ' -f1 "$HW")
    [ "$PROBE_RC" = 0 ] && hw_is "$(last_mark "$VOLDIR")" && [ "$M" -gt "$K" ]
    check $? "e2e: the probe wrote the mark: line $M; the copy of the volume ends with line $K"

    # restore of the older copy: stop, the volume gets the files of the copy, start
    podman stop -t 5 v4d-signer-real >/dev/null
    podman unshare find "$VOLDIR" -mindepth 1 -delete
    podman unshare cp -a "$WORK/volume-copy/." "$VOLDIR/"
    [ "$(last_mark "$VOLDIR")" = "$(last_mark "$WORK/volume-copy")" ]
    check $? "e2e: the volume is the older copy again (line $K)"
    podman start v4d-signer-real >/dev/null
    CODE=$(timeout 30 podman wait v4d-signer-real 2>/dev/null)
    LOG=$(podman logs v4d-signer-real 2>&1)
    REFUSAL=$(grep 'refused' <<<"$LOG" | tail -1)
    [ "$CODE" = 1 ] &&
        grep -q "audit.log ends with line $K, and the high-water file /run/acctpool/audit.highwater has line $M" <<<"$REFUSAL" &&
        grep -q "high-water file" <<<"$REFUSAL"
    check $? "e2e: the start with the older volume is refused (exit code ${CODE:-none}) and the message names the high-water file"
    cut -c1-400 <<<"$REFUSAL" | fold -w 200 | sed 's/^/        /'
    [ "$(cut -d' ' -f1 "$HW")" = "$M" ]
    check $? "e2e: the high-water file still has line $M"
    podman rm -f v4d-signer-real >/dev/null
    podman volume rm -f v4d-signer-data >/dev/null
fi

echo
[ "$PART2" = done ] || echo "NOTE  part 2 and part 4 were skipped"
if [ "$failures" = 0 ]; then
    echo "PASS test-probe-checks (part 2: $PART2)"
else
    echo "FAILED test-probe-checks: $failures"
    exit 1
fi
