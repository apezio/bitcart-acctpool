#!/bin/bash
# acctpool checks for the health probe of the payment host (SPEC-v4 4.3 events; signer README).
#
# Output: one line per check, "OK acctpool-<name>" or "FAIL acctpool-<name> <reason>", nothing else (15 lines).
# A check that cannot get its data is a FAIL. No line has a token, a key, a URL or an address.
# Run as root, as a child of the probe; the reader on the monitoring host alerts on every FAIL line:
#     timeout 150 bash /usr/local/sbin/acctpool-probe-checks >>"$STATUS_TMP" 2>/dev/null
# Argument "files": only plugin-files (start sequence of the deploy guide). Argument "highwater": only audit-chain
# and audit-highwater (the backup job, deploy guide step 5). In these two modes the exit code is 1 on a FAIL.
# Settings: /root/acctpool/probe.conf (root, mode 600, shell syntax, no secret) for the ACCTPOOL_* values below.
#
# Checks (no API token is used; plugin data comes from psql in the database container):
#   signer-container  docker inspect: the signer container runs
#   signer-status     GET /v1/status of the signer from inside the worker, with the worker's user and token file:
#                     network, token and keystore
#   plugin-loaded     GET <backend>/plugins/acctpool/status from inside the worker WITHOUT a token: 200, 401, 403
#                     mean "the route exists"
#   engine-alive      plugin_acctpool_state row "leader" written in the last 5 minutes (database clock)
#   open-invoices     no address in_invoice while the plugin does not run in the backend or the worker (their
#                     payments would not be credited)
#   ready-addresses   at least READY_MIN ready addresses for each store of an enabled pool
#   payout-age        no address pending_payout/in_payout and no payout planned/signed/broadcast older than
#                     PAYOUT_MAX_HOURS
#   alert-events      no event of the alert kinds of SPEC-v4 4.3 (and address_mismatch, address_in_use) in the
#                     last hour, above ALERT_EVENTS_MAX. The worker (it has the daemon) writes fee_wallet_low
#                     and unswept_high.
#   audit-chain       "audit-verify" in the signer container: the chain of audit.log, the journal agrees, the log
#                     has the line of the high-water file that the container saw at its start
#   audit-highwater   the high-water file on the host (SPEC 7.11): "<seq> <sha256 of that audit line>". The probe
#                     reads the last complete line of audit.log from the host folder of the volume BEFORE
#                     audit-verify, and writes it only when audit-verify gave 0 and the volume continues the mark;
#                     never lower; temp file, fsync, rename
#   signer-journal    the volume continues the chain of the mark: it has line <seq> of the high-water file, with
#                     that hash. Else the volume was replaced or put back (the signer caps start again). The mark
#                     then does not move, so the alarm stays until a person acts (docs/VOLUME-RESTORE.md).
#   signer-rate       audit.log: signatures (call sign/..., result ok, not a replay) in the last hour
#   signer-refusals   the container log: the v4 signer writes one JSON line per request to stderr, and a call with
#                     a wrong token gets NO audit line. Answers 401, 429 and 5xx of the last hour, each status.
#   signer-volume     the size of the files of the signer volume
#   plugin-files      compose/plugins/docker: owner root, no write bit for group and others (also __pycache__),
#                     and each file and link is the one of the checksum list that root made after the install
#                     (__pycache__ is not in the list)

[ -r /root/acctpool/probe.conf ] && . /root/acctpool/probe.conf

DOCKER=${ACCTPOOL_DOCKER:-docker}
PROJECT=${ACCTPOOL_PROJECT:-compose}
SIGNER=${ACCTPOOL_SIGNER_CONTAINER:-$PROJECT-acctpool-signer-1}
WORKER=${ACCTPOOL_WORKER_CONTAINER:-$PROJECT-worker-1}
DATABASE=${ACCTPOOL_DATABASE_CONTAINER:-$PROJECT-database-1}
# the stock entrypoint starts as root and changes to this user; the checks must use the same user
WORKER_USER=${ACCTPOOL_WORKER_USER:-electrum}
STATUS_URL=${ACCTPOOL_PROBE_STATUS_URL:-http://backend:8000/plugins/acctpool/status}
PLUGINS_DIR=${ACCTPOOL_PLUGINS_DIR:-/root/bitcart-docker/compose/plugins/docker}
PLUGINS_OWNER=${ACCTPOOL_PLUGINS_OWNER:-0}
CHECKSUM_FILE=${ACCTPOOL_CHECKSUM_FILE:-/root/acctpool/plugins-docker.sha256}
# the lock of the high-water write (owner root, mode 700)
STATE_DIR=${ACCTPOOL_PROBE_STATE_DIR:-/var/lib/acctpool-probe}
READY_MIN=${ACCTPOOL_READY_MIN:-20}
PAYOUT_MAX_HOURS=${ACCTPOOL_PAYOUT_MAX_HOURS:-24}
SIGN_MAX_PER_HOUR=${ACCTPOOL_SIGN_MAX_PER_HOUR:-40}
SIGNER_REFUSALS_MAX=${ACCTPOOL_SIGNER_REFUSALS_MAX:-0}
ALERT_EVENTS_MAX=${ACCTPOOL_ALERT_EVENTS_MAX:-0}
SIGNER_VOLUME_MAX_MB=${ACCTPOOL_SIGNER_VOLUME_MAX_MB:-500}
# with the defaults the whole run stays inside the "timeout 150" of the probe line
AUDIT_VERIFY_TIMEOUT=${ACCTPOOL_AUDIT_VERIFY_TIMEOUT:-60}
STEP_TIMEOUT=${ACCTPOOL_STEP_TIMEOUT:-25}
HIGHWATER_HOST_FILE=${ACCTPOOL_HIGHWATER_HOST_FILE:-/root/acctpool/audit.highwater}
# the host folder of the signer volume; empty = the mount point that "docker volume inspect" gives
SIGNER_VOLUME=${ACCTPOOL_SIGNER_VOLUME:-${PROJECT}_acctpool_signer_data}
SIGNER_VOLUME_DIR=${ACCTPOOL_SIGNER_VOLUME_DIR:-}
# a command in front of the host steps that read the volume and write the high-water file: empty on the payment
# host (the probe runs as root); the test with rootless podman sets "podman unshare"
AS_ROOT=${ACCTPOOL_AS_ROOT:-}

# One result line. The reason is cut and cleaned, so a strange answer cannot break the status file.
emit() {
    local reason
    reason=$(printf '%s' "${3:-}" | tr -c 'A-Za-z0-9 _.,:%=/()+-' ' ' | tr -s ' ' | cut -c1-160)
    if [ "$1" = OK ]; then echo "OK acctpool-$2"; else echo "FAIL acctpool-$2 ${reason:-no reason given}"; fi
}

# Prints the line of each name in $1 from the output in $2 (cleaned), or a FAIL line when it has none.
collect() {
    local name line
    for name in $1; do
        line=$(printf '%s\n' "$2" | grep -E "^(OK|FAIL) acctpool-$name( |$)" | head -1)
        case "$line" in
        '') emit FAIL "$name" "no result" ;;
        OK*) emit OK "$name" ;;
        *) line=${line#"FAIL acctpool-$name"} && emit FAIL "$name" "${line# }" ;;
        esac
    done
}

# One SQL value from the Bitcart database (local connections need no password). Only numbers go into SQL.
sql() {
    timeout "$STEP_TIMEOUT" "$DOCKER" exec "$DATABASE" psql -U postgres -d bitcart -X -At -v ON_ERROR_STOP=1 -c "$1" 2>/dev/null
}

for value in "$READY_MIN" "$PAYOUT_MAX_HOURS" "$SIGN_MAX_PER_HOUR" "$STEP_TIMEOUT" "$PLUGINS_OWNER" \
    "$SIGNER_REFUSALS_MAX" "$ALERT_EVENTS_MAX" "$SIGNER_VOLUME_MAX_MB" "$AUDIT_VERIFY_TIMEOUT"; do
    case "$value" in '' | *[!0-9]*) emit FAIL probe-config "a threshold is not a whole number" && exit 0 ;; esac
done

# The generator runs the rules of this tree as root at each start; backend and worker mount it read-only.
check_plugin_files() {
    local problem="" writable changed listed extra
    if [ ! -d "$PLUGINS_DIR" ]; then
        problem="the plugins folder does not exist"
    elif [ ! -r "$CHECKSUM_FILE" ]; then
        problem="the checksum list does not exist"
    else
        # a symbolic link has no mode of its own: only its owner is examined
        writable=$(find "$PLUGINS_DIR" \( ! -user "$PLUGINS_OWNER" -o \( ! -type l -perm /022 \) \) 2>/dev/null | wc -l)
        changed=$(cd "$PLUGINS_DIR" && timeout "$STEP_TIMEOUT" sha256sum -c --strict "$CHECKSUM_FILE" 2>/dev/null | grep -vc ': OK$')
        listed=$(sed -E 's/^[0-9a-f]{64} [ *]//' "$CHECKSUM_FILE" | sort)
        # a link is never in the list: the generator follows a link to a folder with rules
        extra=$(cd "$PLUGINS_DIR" && find . \( -type f -o -type l \) ! -path '*/__pycache__/*' | sort |
            comm -23 - <(printf '%s\n' "$listed") | wc -l)
        [ -n "$listed" ] || problem="the checksum list is empty"
        [ "$writable" = 0 ] || problem="$writable files or folders can be written by a user who is not root"
        [ "$extra" = 0 ] || problem="${problem:+$problem, }$extra files or links are not in the checksum list"
        [ "$changed" = 0 ] || problem="${problem:+$problem, }$changed files are changed or missing"
    fi
    [ -z "$problem" ] && emit OK plugin-files && return 0
    emit FAIL plugin-files "$problem"
    return 1
}

# The host part of the signer checks (python3 of the host, as root). No audit line is printed.
#   scan <volume folder> <high-water file> <signatures max> <MB max>: the lines signer-volume, signer-rate,
#       signer-journal, and "OK acctpool-audit-highwater <seq> <hash>" (the new mark) or its FAIL line
#   write <high-water file> <seq> <hash>: the line audit-highwater
read -r -d '' VOLUME_PY <<'PY'
import collections, datetime, hashlib, json, os, re, stat, sys, time

MARK_RE = re.compile(rb"(0|[1-9][0-9]{0,18}) ([0-9a-f]{64})\n?")


def say(state, name, reason=""):
    print(("%s acctpool-%s %s" % (state, name, reason)).strip())


def read_mark(path):
    """(seq, hash) of the high-water file, or the reason why it has none."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return "the high-water file is not a regular file (a folder or a link); nothing written"
        with open(path, "rb") as f:
            match = MARK_RE.fullmatch(f.read(101))
    except FileNotFoundError:
        return "the high-water file does not exist; the deploy step writes 0 and 64 zeros before the first start"
    except OSError as e:
        return "the high-water file cannot be read (%s)" % type(e).__name__
    if match is None:
        return "the high-water file does not have the form <seq> <hash>; nothing written"
    return int(match.group(1)), match.group(2).decode()


def scan(folder, mark_path, sign_limit, mb_limit):
    mark = read_mark(mark_path)  # before the volume: so a mark of a run at the same time is never above it
    try:
        megabytes = sum(os.lstat(os.path.join(top, n)).st_size for top, _, names in os.walk(folder) for n in names) // 2**20
        if not os.path.isdir(folder):
            say("FAIL", "signer-volume", "the folder of the signer volume was not found")
        elif megabytes > mb_limit:
            say("FAIL", "signer-volume", "%d MB, maximum %d MB" % (megabytes, mb_limit))
        else:
            say("OK", "signer-volume")
    except OSError as e:
        say("FAIL", "signer-volume", "cannot read the signer volume (%s)" % type(e).__name__)
    try:
        seq, last, at_mark, tail = 0, b"", None, collections.deque(maxlen=50000)
        with open(os.path.join(folder, "audit.log"), "rb") as f:
            for raw in f:  # the chain gives line n the seq n; audit-verify checks it
                if not raw.endswith(b"\n"):
                    break  # a part of a line at the end (a crash) is not a line
                seq, last = seq + 1, raw[:-1]
                tail.append(last)
                if isinstance(mark, tuple) and seq == mark[0]:
                    at_mark = hashlib.sha256(last).hexdigest()
    except OSError as e:
        for name in ("signer-rate", "signer-journal", "audit-highwater"):
            say("FAIL", name, "cannot read audit.log of the signer volume (%s)" % type(e).__name__)
        return
    signatures = bad = 0
    for line in reversed(tail):
        try:
            entry = json.loads(line)
            moment = datetime.datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%S.%fZ")
            if time.time() - moment.replace(tzinfo=datetime.timezone.utc).timestamp() > 3600:
                break
            signatures += str(entry["call"]).startswith("sign/") and entry["result"] == "ok" and not entry.get("replay")
        except (ValueError, KeyError, TypeError, AttributeError):
            bad += 1
    if bad or signatures > sign_limit:
        reason = "%d audit lines cannot be read" % bad if bad else "%d signatures in the last hour, maximum %d" % (signatures, sign_limit)
        say("FAIL", "signer-rate", reason)
    else:
        say("OK", "signer-rate")
    if seq == 0:
        problem = "audit.log of the signer volume has no complete line"
    elif isinstance(mark, str):
        return say("OK", "signer-journal") or say("FAIL", "audit-highwater", mark)
    elif mark[0] > seq:
        problem = ("the volume ends with audit line %d and the high-water file has line %d: an older or a new volume "
                   "(a restore), see docs/VOLUME-RESTORE.md; nothing written" % (seq, mark[0]))
    elif mark[0] and at_mark != mark[1]:
        problem = "audit line %d of the volume has another hash than the high-water file: another or a changed audit log; nothing written" % mark[0]
    else:
        return say("OK", "signer-journal") or say("OK", "audit-highwater", "%d %s" % (seq, hashlib.sha256(last).hexdigest()))
    say("FAIL", "signer-journal", problem)
    say("FAIL", "audit-highwater", problem)


def write(path, seq, digest):
    mark = read_mark(path)
    if isinstance(mark, str):
        return say("FAIL", "audit-highwater", mark)
    if mark[0] == seq and mark[1] != digest:
        return say("FAIL", "audit-highwater", "audit line %d: another hash than the high-water file; nothing written" % seq)
    if mark[0] >= seq:
        return say("OK", "audit-highwater")  # never lower: a run at the same time wrote a newer line
    folder = os.path.dirname(os.path.abspath(path))
    temp = os.path.join(folder, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, b"%d %s\n" % (seq, digest.encode()))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(temp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        os.unlink(temp)
        raise
    folder_fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(folder_fd)
    finally:
        os.close(folder_fd)
    say("OK", "audit-highwater")


try:
    if sys.argv[1] == "scan":
        scan(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    elif sys.argv[1] == "write" and re.fullmatch(r"[1-9][0-9]{0,18} [0-9a-f]{64}", sys.argv[3] + " " + sys.argv[4]):
        write(sys.argv[2], int(sys.argv[3]), sys.argv[4])
except OSError as e:
    say("FAIL", "audit-highwater", "%s: %s" % (type(e).__name__, e.strerror))
PY

# Prints audit-chain and audit-highwater; keeps the scan output in SCAN for the other signer lines. Status 1 on a
# FAIL. The volume is read BEFORE audit-verify: the line that is written was in the file that audit-verify checked.
audit_and_highwater() {
    local folder=$SIGNER_VOLUME_DIR code mark result
    [ -n "$folder" ] || folder=$(timeout "$STEP_TIMEOUT" "$DOCKER" volume inspect -f '{{.Mountpoint}}' "$SIGNER_VOLUME" 2>/dev/null)
    SCAN=$(timeout "$STEP_TIMEOUT" $AS_ROOT python3 -I -c "$VOLUME_PY" scan "${folder:-/nonexistent}" \
        "$HIGHWATER_HOST_FILE" "$SIGN_MAX_PER_HOUR" "$SIGNER_VOLUME_MAX_MB" 2>/dev/null)
    # docker exec uses the user of the image (the signer's own user). Its output is not printed.
    timeout "$AUDIT_VERIFY_TIMEOUT" "$DOCKER" exec "$SIGNER" python -I -m acctpool_signer audit-verify >/dev/null 2>&1
    code=$?
    [ "$code" = 0 ] && emit OK audit-chain ||
        emit FAIL audit-chain "audit-verify failed with code $code (124 = not complete in $AUDIT_VERIFY_TIMEOUT seconds)"
    mark=$(printf '%s\n' "$SCAN" | sed -n 's/^OK acctpool-audit-highwater \([1-9][0-9]\{0,18\} [0-9a-f]\{64\}\)$/\1/p')
    if [ "$code" != 0 ]; then
        result="FAIL acctpool-audit-highwater audit-verify did not give 0: the mark is not moved"
    elif [ -z "$mark" ]; then
        result=$SCAN
    elif ! mkdir -p -m 700 "$STATE_DIR" 2>/dev/null || ! { exec 9>>"$STATE_DIR/highwater.lock"; } 2>/dev/null ||
        ! flock -w "$STEP_TIMEOUT" 9; then
        result="FAIL acctpool-audit-highwater the lock file in the state folder of the probe cannot be used"
    else  # one write at a time: two runs could otherwise write a lower line last
        result=$(timeout "$STEP_TIMEOUT" $AS_ROOT python3 -I -c "$VOLUME_PY" write "$HIGHWATER_HOST_FILE" ${mark} 2>/dev/null)
        exec 9>&-
    fi
    collect audit-highwater "$result"
    [ "$code" = 0 ] && printf '%s\n' "$result" | grep -q '^OK acctpool-audit-highwater'
}

case "${1:-}" in
files) check_plugin_files && exit 0 || exit 1 ;;
highwater) audit_and_highwater && exit 0 || exit 1 ;;
esac

state=$(timeout "$STEP_TIMEOUT" "$DOCKER" inspect -f '{{.State.Status}}' "$SIGNER" 2>/dev/null)
[ "$state" = running ] && emit OK signer-container || emit FAIL signer-container "state ${state:-unknown}"

read -r -d '' WORKER_PY <<'PY'
import json, os, sys, urllib.error, urllib.request


def get(url, token=None):
    """(status, JSON body), or (0, the type of the exception): its text can have the URL."""
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token} if token else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a proxy must not get the token
    try:
        with opener.open(request, timeout=8) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, type(e).__name__


def signer_status():
    url = os.environ.get("ACCTPOOL_SIGNER_URL", "")
    if not url:
        return "ACCTPOOL_SIGNER_URL is not set in the worker"
    try:
        with open("/run/acctpool/signer.token") as f:
            token = f.read().strip()
    except OSError:
        return "the worker user cannot read the token file"
    code, data = get(url.rstrip("/") + "/v1/status", token)
    if code != 200:
        return "http %d" % code if code else "no answer (%s)" % data
    return None if isinstance(data, dict) and data.get("keystore") is True else "keystore is not loaded"


def plugin_loaded():
    code, data = get(sys.argv[1])
    if code in (200, 401, 403):
        return None
    return "the plugin route does not exist" if code == 404 else "http %d" % code if code else "no answer (%s)" % data


for name, check in (("signer-status", signer_status), ("plugin-loaded", plugin_loaded)):
    reason = check()
    print("OK acctpool-" + name if reason is None else "FAIL acctpool-%s %s" % (name, reason))
PY
worker=$(timeout "$STEP_TIMEOUT" "$DOCKER" exec -u "$WORKER_USER" "$WORKER" python -c "$WORKER_PY" "$STATUS_URL" </dev/null 2>/dev/null)
collect "signer-status plugin-loaded" "$worker"

alive=$(sql "select coalesce((select case when updated > now() - interval '5 minutes' then 'alive'
    else 'the worker leader wrote no state for more than 5 minutes' end
    from plugin_acctpool_state where key = 'leader'), 'no leader row')")
[ "$alive" = alive ] && emit OK engine-alive || emit FAIL engine-alive "${alive:-query failed}"

# Without the plugin a payment to a pool address is not credited: never remove it while pool invoices are open.
open_invoices=$(sql "select count(*) from plugin_acctpool_addresses where status = 'in_invoice'")
where=""
printf '%s\n' "$worker" | grep -q '^OK acctpool-plugin-loaded$' || where="the backend"
[ "$alive" = alive ] || where="${where:+$where and }the worker"
case "$open_invoices" in
'' | *[!0-9]*) emit FAIL open-invoices "query failed" ;;
0) emit OK open-invoices ;;
*) [ -z "$where" ] && emit OK open-invoices ||
    emit FAIL open-invoices "$open_invoices pool invoices are open and the plugin does not run in $where" ;;
esac

low=$(sql "select coalesce(string_agg(store || ' has ' || ready, ', ' order by store), 'none') from (
    select s.store, (select count(*) from plugin_acctpool_addresses a where a.store = s.store and a.status = 'ready') as ready
    from (select distinct store from plugin_acctpool_pools where enabled) s) t where ready < $READY_MIN")
case "$low" in
none) emit OK ready-addresses ;;
'') emit FAIL ready-addresses "query failed" ;;
*) emit FAIL ready-addresses "$low, minimum $READY_MIN" ;;
esac

old=$(sql "select (select count(*) from plugin_acctpool_addresses where status in ('pending_payout', 'in_payout')
    and assigned_at < now() - interval '$PAYOUT_MAX_HOURS hours') || ' ' || (select count(*) from plugin_acctpool_payouts
    where state in ('planned', 'signed', 'broadcast') and created < now() - interval '$PAYOUT_MAX_HOURS hours')")
case "$old" in
"0 0") emit OK payout-age ;;
[0-9]*" "[0-9]*) emit FAIL payout-age "${old% *} addresses wait and ${old#* } payouts are not final after $PAYOUT_MAX_HOURS hours" ;;
*) emit FAIL payout-age "query failed" ;;
esac

ALERT_KINDS="'fee_wallet_low', 'payout_waiting_24h', 'late_payment', 'second_opinion_mismatch', 'second_opinion_down',
    'ready_low', 'unswept_high', 'signer_down', 'payout_failed', 'stock_fallback', 'address_mismatch',
    'address_in_use'"
events=$(sql "select case when sum(n) > $ALERT_EVENTS_MAX then string_agg(n || ' ' || kind, ', ' order by kind)
    else 'none' end from (select kind, count(*) as n from plugin_acctpool_events
    where kind in ($ALERT_KINDS) and created > now() - interval '1 hour' group by kind) t")
case "$events" in
none) emit OK alert-events ;;
'') emit FAIL alert-events "query failed" ;;
*) emit FAIL alert-events "in the last hour: $events" ;;
esac

audit_and_highwater
collect "signer-rate" "$SCAN"

# One JSON line per request on stderr, e.g. {"method": "GET", "path": "/v1/status", "status": 401, ...}
if logs=$(timeout "$STEP_TIMEOUT" "$DOCKER" logs --since 1h "$SIGNER" 2>&1); then
    over=$(printf '%s\n' "$logs" | grep -oE '"status": (401|429|5[0-9][0-9])[,}]' | tr -dc '0-9\n' | sort | uniq -c |
        awk -v max="$SIGNER_REFUSALS_MAX" '$1 > max { printf "%s%d http %s", sep, $1, $2; sep = ", " }')
    [ -z "$over" ] && emit OK signer-refusals ||
        emit FAIL signer-refusals "in the last hour: $over, maximum $SIGNER_REFUSALS_MAX each"
else
    emit FAIL signer-refusals "cannot read the container log of the signer"
fi

collect "signer-journal signer-volume" "$SCAN"
check_plugin_files
exit 0
