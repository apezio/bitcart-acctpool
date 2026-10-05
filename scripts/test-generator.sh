#!/bin/bash
# Generator check for the acctpool docker plugin.
#
# Runs the real bitcart-docker generator (without and with this plugin) and fails when the
# generated compose file differs in anything but: the signer service, its network, its volume,
# the mounts / environment / network that the rule adds to backend and worker, and two stock
# values of these two services: the mount ./plugins/docker:/plugins/docker becomes read-only,
# and BITCART_VOLUMES no longer has a folder that includes /plugins/docker.
# Two more runs use stock components where the mount, or the BITCART_VOLUMES value, has a
# different form: the rule must then change nothing and add nothing, and the checker must fail.
#
# Where: on the test host, from the plugin folder:   scripts/test-generator.sh [diff-output-file]
# Needs: rootless podman, the pinned backend image (Python 3.12 + PyYAML), network access to PyPI
#        (the generator imports oyaml, which the backend image does not have; it is installed
#        inside the test container only).
# Reference input, read-only, in $ACCTPOOL_REF (default ~/acctpool-build/ref):
#   bitcart-docker/generator  bitcart-docker/compose   the stock generator and its compose folder
#   solana/                                            the Solana docker plugin folder
#   forked_host/                                       the folder with the worker hostname rule
set -euo pipefail

PLUGIN=$(cd "$(dirname "$0")/.." && pwd)
REF=${ACCTPOOL_REF:-$HOME/acctpool-build/ref}
IMAGE=${ACCTPOOL_GENERATOR_IMAGE:-docker.io/bitcart/bitcart:0.10.3.0}
NAME=v4d-generator
DIFF_OUT=${1:-}
TEST_HOST_NAME=pay.example.com
TEST_CRYPTOS=btc,ltc,bch,xmr,trx,eth,matic

for need in "$REF/bitcart-docker/generator/generator.py" "$REF/bitcart-docker/compose" \
    "$REF/solana/components/solana.yml" "$REF/solana/rules" "$REF/forked_host/rules" \
    "$PLUGIN/components/acctpool.yml" "$PLUGIN/rules/91_acctpool.py"; do
    [ -e "$need" ] || { echo "FAIL missing input: $need" >&2; exit 2; }
done

WORK=$(mktemp -d "$HOME/acctpool-build/v4d-gen.XXXXXX")
cleanup() {
    podman rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

for tree in base with nomount novolumes; do
    mkdir -p "$WORK/$tree/compose/plugins/docker"
    cp -r "$REF/bitcart-docker/generator" "$WORK/$tree/generator"
    # the stock compose folder without any plugin and without an old result
    (cd "$REF/bitcart-docker/compose" && tar cf - --exclude=./plugins --exclude=./generated.yml --exclude=./metadata.json .) |
        tar xf - -C "$WORK/$tree/compose"
    mkdir -p "$WORK/$tree/compose/plugins/docker/solana" "$WORK/$tree/compose/plugins/docker/forked_host"
    cp -r "$REF/solana/components" "$REF/solana/rules" "$WORK/$tree/compose/plugins/docker/solana/"
    cp -r "$REF/forked_host/rules" "$WORK/$tree/compose/plugins/docker/forked_host/"
done
for tree in with nomount novolumes; do
    mkdir -p "$WORK/$tree/compose/plugins/docker/acctpool"
    cp -r "$PLUGIN/components" "$PLUGIN/rules" "$WORK/$tree/compose/plugins/docker/acctpool/"
done
# "upstream changed the mount": the stock entry gets a form that the rule does not know
for component in backend worker; do
    file=$WORK/nomount/generator/docker-components/$component.yml
    grep -q '"./plugins/docker:/plugins/docker"' "$file" || { echo "FAIL stock mount not in $component.yml" >&2; exit 2; }
    sed -i 's|"./plugins/docker:/plugins/docker"|"./plugins/docker:/plugins/docker:rw"|' "$file"
done
# "upstream changed BITCART_VOLUMES": one service is sufficient, the rule is all or nothing
file=$WORK/novolumes/generator/docker-components/worker.yml
grep -q 'BITCART_VOLUMES: /datadir /backups /plugins$' "$file" || { echo "FAIL stock BITCART_VOLUMES not in worker.yml" >&2; exit 2; }
sed -i 's|BITCART_VOLUMES: /datadir /backups /plugins$|BITCART_VOLUMES: /datadir /backups /plugins /more|' "$file"
find "$WORK" -name __pycache__ -type d -prune -exec rm -rf {} +

cat >"$WORK/check.py" <<'PY'
"""Compares two generated compose files. Exit 0 only when the difference is exactly the expected one."""

import sys

import yaml

SIGNER = "acctpool-signer"
NET = "acctpool_internal"
VOL = "acctpool_signer_data"
MODULES = "./plugins/docker/acctpool/backend/forkedpool:/app/modules/forkedpool:ro"
SECOND = "/root/acctpool/second.toml:/run/acctpool/second.toml:ro"
TOKEN = "/root/acctpool/worker.token:/run/acctpool/signer.token:ro"
SOLANA = "./plugins/docker/solana/backend/forked:/app/modules/forked:ro"
HIGHWATER = "/root/acctpool/audit.highwater:/run/acctpool/audit.highwater:ro"
STOCK = "./plugins/docker:/plugins/docker"
STOCK_RO = STOCK + ":ro"
VOLUMES_KEY = "BITCART_VOLUMES"
STOCK_VOLUMES = "/datadir /backups /plugins"
NEW_VOLUMES = "/datadir /backups /plugins/backend /plugins/admin /plugins/store"
ENV_KEY = "ACCTPOOL_SIGNER_URL"
ENV_VALUE = "http://acctpool-signer:7070"

errors = []


def check(condition, message):
    if not condition:
        errors.append(message)


def changed_keys(old, new):
    return {key for key in set(old) | set(new) if old.get(key) != new.get(key)}


def only_added(old, new, added):
    """True when new is old plus every item of added exactly one time; the order of the old items is kept."""
    return [item for item in new if item not in added] == old and all(new.count(item) == 1 for item in added)


def read_only_plugins(old):
    """The old list with the stock plugins mount made read-only, at the same position. None = entry not there."""
    if old.count(STOCK) != 1:
        return None
    return [STOCK_RO if item == STOCK else item for item in old]


def check_volumes(name, old, new, added):
    expected = read_only_plugins(old)
    if expected is None:
        return check(False, f"{name}: the stock mount {STOCK} is not in the file without the plugin")
    check(STOCK not in new, f"{name}: the plugins tree is still mounted read-write")
    check(
        only_added(expected, new, added),
        f"{name} volumes: not the old list with the plugins mount read-only, plus {len(added)} new mounts",
    )


def check_environment(name, old, new, added):
    """Only BITCART_VOLUMES changes, from exactly the stock value to exactly the new value, plus the added keys."""
    check(old.get(VOLUMES_KEY) == STOCK_VOLUMES, f"{name}: {VOLUMES_KEY} without the plugin is not the stock value")
    check(new.get(VOLUMES_KEY) == NEW_VOLUMES, f"{name}: {VOLUMES_KEY} is {new.get(VOLUMES_KEY)!r}")
    expected = {**old, VOLUMES_KEY: NEW_VOLUMES, **added}
    check(new == expected, f"{name} environment: a different value changed")
    for path in str(new.get(VOLUMES_KEY, "")).split():
        check(not "/plugins/docker".startswith(path), f"{name}: the entrypoint runs chown in /plugins/docker ({path})")


def without(mapping, key):
    return {k: v for k, v in (mapping or {}).items() if k != key}


def networks_of(service):
    networks = service.get("networks") or []
    return list(networks)


def main(base_path, new_path, host):
    with open(base_path) as f:
        base = yaml.safe_load(f)
    with open(new_path) as f:
        new = yaml.safe_load(f)
    check(set(base) == set(new), f"top-level keys differ: {sorted(set(base) ^ set(new))}")
    old_services, new_services = base["services"], new["services"]

    check(set(new_services) - set(old_services) == {SIGNER}, f"new services: {sorted(set(new_services) - set(old_services))}")
    check(not set(old_services) - set(new_services), f"removed services: {sorted(set(old_services) - set(new_services))}")
    for name, service in old_services.items():
        if name not in ("backend", "worker"):
            check(service == new_services.get(name), f"service {name} changed")
        if name != "worker":
            check(NET not in networks_of(new_services.get(name, {})), f"service {name} joined {NET}")

    check(without(new.get("networks"), NET) == (base.get("networks") or {}), "another network changed")
    check((new.get("networks") or {}).get(NET) == {"internal": True}, f"network {NET} is not exactly internal: true")
    check(without(new.get("volumes"), VOL) == (base.get("volumes") or {}), "another volume changed")
    check(VOL in (new.get("volumes") or {}) and new["volumes"][VOL] is None, f"volume {VOL} missing or has options")

    old, cur = old_services["backend"], new_services["backend"]
    check(changed_keys(old, cur) == {"volumes", "environment"}, f"backend changed keys: {sorted(changed_keys(old, cur))}")
    check_environment("backend", old["environment"], cur["environment"], {})
    check_volumes("backend", old["volumes"], cur["volumes"], [MODULES])

    old, cur = old_services["worker"], new_services["worker"]
    expected = {"volumes", "environment", "networks"}
    check(changed_keys(old, cur) == expected, f"worker changed keys: {sorted(changed_keys(old, cur))}")
    check_volumes("worker", old["volumes"], cur["volumes"], [MODULES, TOKEN, SECOND])
    check_environment("worker", old["environment"], cur["environment"], {ENV_KEY: ENV_VALUE})
    check("networks" not in old, "base worker already has networks: the expected value below is wrong")
    check(cur.get("networks") == ["default", NET], f"worker networks: {cur.get('networks')}")

    # what the other plugins set must be there, before and after
    for label, services in (("base", old_services), ("with", new_services)):
        check("solana" in services, f"{label}: solana service missing")
        for name in ("backend", "worker"):
            check(SOLANA in services[name]["volumes"], f"{label}: Solana module mount missing on {name}")
            check(services[name]["environment"].get("SOL_HOST") == "solana", f"{label}: SOL_HOST missing on {name}")
        check(services["worker"].get("hostname") == host, f"{label}: worker hostname is {services['worker'].get('hostname')}")

    signer = new_services.get(SIGNER, {})
    # init: with an init program process 1 is not the signer, and the seed commands refuse to run (SPEC 7.11)
    for key in ("build", "ports", "expose", "links", "depends_on", "network_mode", "privileged", "cap_add", "devices", "init"):
        check(key not in signer, f"signer has {key}")
    check(signer.get("networks") == [NET], f"signer networks: {signer.get('networks')}")
    check(signer.get("read_only") is True, "signer root filesystem is not read-only")
    check(signer.get("cap_drop") == ["ALL"], "signer cap_drop is not [ALL]")
    check("no-new-privileges:true" in (signer.get("security_opt") or []), "signer no-new-privileges missing")
    check(bool(signer.get("mem_limit")), "signer memory limit missing")
    check(signer.get("memswap_limit") == signer.get("mem_limit"), "signer can use swap (memswap_limit is not mem_limit)")
    check((signer.get("ulimits") or {}).get("memlock") == {"soft": -1, "hard": -1}, "signer memlock limit")
    logging = signer.get("logging") or {}
    options = logging.get("options") or {}
    check(logging.get("driver") == "json-file", "signer log driver is not named")
    check(bool(options.get("max-size")) and bool(options.get("max-file")), "signer log has no size limit")
    check(all(isinstance(value, str) for value in options.values()), "signer log options must be strings")
    check(signer.get("restart") == "unless-stopped", "signer restart policy")
    check(signer.get("pull_policy") == "never", "signer pull_policy is not never")
    check(str(signer.get("image", "")).startswith("forked/acctpool-signer:"), "signer image name")
    volumes = signer.get("volumes") or []
    check(f"{VOL}:/data" in volumes, "signer data volume missing")
    check("/root/acctpool/pools.toml:/etc/acctpool/pools.toml:ro" in volumes, "signer pools.toml mount missing")
    check(HIGHWATER in volumes, "signer high-water file mount missing")
    check((signer.get("environment") or {}).get("ACCTPOOL_HIGHWATER_FILE") == "/run/acctpool/audit.highwater",
          "signer ACCTPOOL_HIGHWATER_FILE is not the mounted file")
    for volume in volumes:
        if volume != f"{VOL}:/data":
            check(volume.startswith("/") and volume.endswith(":ro"), f"signer mount not absolute and read-only: {volume}")
            check("/plugins/" not in volume.split(":")[0], f"signer mount source is inside the plugins tree: {volume}")
    for value in (signer.get("environment") or {}).values():
        check("$" not in str(value), "signer environment takes a value from the host environment")

    for message in errors:
        print(f"FAIL {message}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
PY

cat >"$WORK/run.sh" <<'SH'
set -eu
uv pip install -q --python /app/.venv/bin/python oyaml
for tree in base with nomount novolumes; do
    cd "/work/$tree"
    python -m generator
done
cd /work
cp with/compose/generated.yml with.yml
if ! python check.py base/compose/generated.yml with.yml "$BITCART_HOST"; then
    echo "RESULT generator-diff FAIL"
    exit 1
fi
echo "RESULT generator-diff OK"

# Stock mount or stock BITCART_VOLUMES in a form that the rule does not know:
# the rule changes nothing and adds nothing, and the checker fails.
python - <<'UNKNOWN'
import sys
import yaml

problems = []
for tree, marker_service, marker in (
    ("nomount", "backend", "./plugins/docker:/plugins/docker:rw"),
    ("novolumes", "worker", "/datadir /backups /plugins /more"),
):
    services = yaml.safe_load(open(f"/work/{tree}/compose/generated.yml"))["services"]
    if marker not in yaml.safe_dump(services[marker_service]):
        problems.append(f"{tree}: the changed stock value is not there, the test setup is wrong")
    for name in ("backend", "worker"):
        text = yaml.safe_dump(services[name])
        for word in ("acctpool", "forkedpool", "ACCTPOOL", "/plugins/docker:ro", "/plugins/backend /plugins/admin"):
            if word in text:
                problems.append(f"{tree}: {name} has {word}")
for problem in problems:
    print("FAIL rule-with-unknown-stock-form:", problem)
sys.exit(1 if problems else 0)
UNKNOWN
for tree in nomount novolumes; do
    if python check.py base/compose/generated.yml "$tree/compose/generated.yml" "$BITCART_HOST" >/dev/null; then
        echo "FAIL the checker accepted the file of the run $tree"
        exit 1
    fi
done
echo "RESULT rule-with-unknown-stock-form OK (2 runs: rule changed nothing, checker refused the file)"

# The checker also refuses a base file where BITCART_VOLUMES is not the stock value.
python - <<'BASE'
import yaml
data = yaml.safe_load(open("/work/base/compose/generated.yml"))
data["services"]["backend"]["environment"]["BITCART_VOLUMES"] = "/datadir /backups /plugins /more"
yaml.safe_dump(data, open("/work/base-changed.yml", "w"))
BASE
if python check.py base-changed.yml with.yml "$BITCART_HOST" >/dev/null; then
    echo "FAIL the checker accepted a base file with a changed stock value"
    exit 1
fi
echo "RESULT changed-stock-value OK (checker refused)"

# The checker must see a difference that is not allowed. Each line is one change to the good file.
n=0
mutate() {
    n=$((n + 1))
    python - "$1" <<'MUT'
import sys
import yaml
data = yaml.safe_load(open("/work/with.yml"))
s = data["services"]
change = sys.argv[1]
if change == "signer-port":
    s["acctpool-signer"]["ports"] = ["7070:7070"]
elif change == "signer-default-network":
    s["acctpool-signer"]["networks"].append("default")
elif change == "signer-writable-root":
    s["acctpool-signer"]["read_only"] = False
elif change == "backend-on-signer-network":
    s["backend"]["networks"] = ["default", "acctpool_internal"]
elif change == "backend-token":
    s["backend"]["volumes"].append("/root/acctpool/worker.token:/run/acctpool/signer.token:ro")
elif change == "backend-plugins-writable":
    v = s["backend"]["volumes"]
    v[v.index("./plugins/docker:/plugins/docker:ro")] = "./plugins/docker:/plugins/docker"
elif change == "worker-plugins-writable":
    v = s["worker"]["volumes"]
    v[v.index("./plugins/docker:/plugins/docker:ro")] = "./plugins/docker:/plugins/docker"
elif change == "backend-read-only-but-stock-volumes":
    s["backend"]["environment"]["BITCART_VOLUMES"] = "/datadir /backups /plugins"
elif change == "worker-read-only-but-stock-volumes":
    s["worker"]["environment"]["BITCART_VOLUMES"] = "/datadir /backups /plugins"
elif change == "volumes-changed-but-mount-writable":
    for name in ("backend", "worker"):
        v = s[name]["volumes"]
        v[v.index("./plugins/docker:/plugins/docker:ro")] = "./plugins/docker:/plugins/docker"
elif change == "volumes-list-has-docker-plugins":
    s["worker"]["environment"]["BITCART_VOLUMES"] += " /plugins/docker"
elif change == "volumes-list-lost-datadir":
    s["backend"]["environment"]["BITCART_VOLUMES"] = "/backups /plugins/backend /plugins/admin /plugins/store"
elif change == "plugins-mount-moved":
    v = s["worker"]["volumes"]
    v.append(v.pop(v.index("./plugins/docker:/plugins/docker:ro")))
elif change == "other-stock-mount-read-only":
    v = s["backend"]["volumes"]
    v[v.index("./plugins/backend:/plugins/backend")] = "./plugins/backend:/plugins/backend:ro"
elif change == "other-stock-mount-removed":
    s["worker"]["volumes"].remove("bitcart_datadir:/datadir")
elif change == "signer-log-without-limit":
    del s["acctpool-signer"]["logging"]["options"]["max-size"]
elif change == "signer-log-removed":
    del s["acctpool-signer"]["logging"]
elif change == "signer-can-swap":
    s["acctpool-signer"]["memswap_limit"] = "2g"
elif change == "signer-has-build":
    s["acctpool-signer"]["build"] = {"context": "./plugins/docker/acctpool/signer"}
elif change == "signer-init":
    s["acctpool-signer"]["init"] = True
elif change == "signer-highwater-lost":
    s["acctpool-signer"]["volumes"].remove("/root/acctpool/audit.highwater:/run/acctpool/audit.highwater:ro")
elif change == "signer-highwater-writable":
    v = s["acctpool-signer"]["volumes"]
    v[v.index("/root/acctpool/audit.highwater:/run/acctpool/audit.highwater:ro")] = "/root/acctpool/audit.highwater:/run/acctpool/audit.highwater"
elif change == "signer-pull-policy-build":
    s["acctpool-signer"]["pull_policy"] = "build"
elif change == "worker-env-changed":
    s["worker"]["environment"]["BITCART_CRYPTOS"] = "btc"
elif change == "worker-lost-default-network":
    s["worker"]["networks"] = ["acctpool_internal"]
elif change == "worker-hostname-lost":
    del s["worker"]["hostname"]
elif change == "solana-mount-lost":
    s["worker"]["volumes"] = [v for v in s["worker"]["volumes"] if "solana" not in v]
elif change == "other-service-changed":
    s["database"]["restart"] = "no"
elif change == "network-not-internal":
    data["networks"]["acctpool_internal"] = {}
elif change == "extra-volume":
    data["volumes"]["surprise"] = None
else:
    sys.exit(f"unknown change {change}")
yaml.safe_dump(data, open("/work/mutant.yml", "w"))
MUT
    if python check.py base/compose/generated.yml mutant.yml "$BITCART_HOST" >/dev/null; then
        echo "FAIL self-test: the checker accepted $1"
        exit 1
    fi
}
for change in signer-port signer-default-network signer-writable-root backend-on-signer-network backend-token \
    worker-env-changed worker-lost-default-network worker-hostname-lost solana-mount-lost other-service-changed \
    network-not-internal extra-volume backend-plugins-writable worker-plugins-writable plugins-mount-moved \
    other-stock-mount-read-only other-stock-mount-removed signer-has-build signer-pull-policy-build \
    backend-read-only-but-stock-volumes worker-read-only-but-stock-volumes volumes-changed-but-mount-writable \
    volumes-list-has-docker-plugins volumes-list-lost-datadir signer-log-without-limit signer-log-removed \
    signer-can-swap signer-init signer-highwater-lost signer-highwater-writable; do
    mutate "$change"
done
echo "RESULT checker-self-test OK ($n forbidden changes refused)"
SH

podman rm -f "$NAME" >/dev/null 2>&1 || true
# Only BITCART_* values that the test sets: nothing from the host environment reaches the generator.
podman run --rm --name "$NAME" --security-opt label=disable \
    -v "$WORK:/work" -w /work \
    --env BITCART_CRYPTOS="$TEST_CRYPTOS" \
    --env BITCART_HOST="$TEST_HOST_NAME" \
    --env BITCART_VERSION=0.10.3.0 \
    --env NAME=compose \
    --entrypoint sh "$IMAGE" /work/run.sh

DIFF=$(diff -u --label without-acctpool --label with-acctpool \
    "$WORK/base/compose/generated.yml" "$WORK/with/compose/generated.yml" || true)
[ -n "$DIFF" ] || { echo "FAIL the two generated files are equal: the plugin was not loaded" >&2; exit 1; }
if [ -n "$DIFF_OUT" ]; then
    printf '%s\n' "$DIFF" >"$DIFF_OUT"
    echo "diff written to $DIFF_OUT"
else
    printf '%s\n' "$DIFF"
fi
echo "PASS test-generator"
