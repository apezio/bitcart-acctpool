#!/bin/bash
# Proof that deploy/pools.toml.example and deploy/second.toml.example are accepted by the config
# loaders of the signer and of the plugin, with each chain block enabled.
#
# Where: on the test host, from the plugin folder:   deploy/test-config-examples.sh
# Needs: rootless podman, the images bitcart:0.10.3.0 and bitcart-eth:0.10.3.0, and a copy of the
#        signer and backend source in $ACCTPOOL_SRC (default ~/acctpool-build/dpl-src).
#        Make the copy on your workstation, in the plugin folder: the signer as committed, the backend of the tree
#        that is tested (HEAD, or the working tree before a commit):
#            ssh "$TEST_HOST" 'rm -rf ~/acctpool-build/dpl-src && mkdir -p ~/acctpool-build/dpl-src'
#            git archive HEAD signer | ssh "$TEST_HOST" 'tar xf - -C ~/acctpool-build/dpl-src'
#            tar -c --exclude __pycache__ backend | ssh "$TEST_HOST" 'tar xf - -C ~/acctpool-build/dpl-src'
# Containers: v4d-config (one at a time). Removed at the end. No network.
#
# The placeholders of the examples are not valid addresses on purpose. The test proves that the
# signer refuses the file as it is, then it puts test addresses (made in the test, random, no key
# exists for them) in the place of the placeholders and loads the file again.
set -uo pipefail

PLUGIN=$(cd "$(dirname "$0")/.." && pwd)
SRC=${ACCTPOOL_SRC:-$HOME/acctpool-build/dpl-src}
WORK=$(mktemp -d "$HOME/acctpool-build/v4d-config.XXXXXX")
failures=0

cleanup() {
    podman rm -f v4d-config >/dev/null 2>&1
    rm -rf "$WORK"
}
trap cleanup EXIT

for need in "$SRC/signer/acctpool_signer/config.py" "$SRC/backend/forkedpool/acctpool/secondcheck.py" \
    "$PLUGIN/deploy/pools.toml.example" "$PLUGIN/deploy/second.toml.example"; do
    [ -e "$need" ] || { echo "FAIL missing input: $need" >&2; exit 2; }
done
cp "$PLUGIN/deploy/pools.toml.example" "$PLUGIN/deploy/second.toml.example" "$WORK/"

cat >"$WORK/common.py" <<'PY'
import re

CHAINS = ("ethereum", "bnb")
# SPEC-v4 4.1
TABLE = {
    "polygon": (137, "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"),
    "ethereum": (1, "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
    "bnb": (56, "0x55d398326f99059fF775485246999027B3197955"),
}
results = []


def enable(text, names):
    """Remove the "# " of the lines between BEGIN <name> and END <name>, for the names that are given."""
    out, inside = [], None
    for line in text.splitlines():
        begin = re.fullmatch(r"# --- BEGIN ([a-z0-9]+) ---", line)
        end = re.fullmatch(r"# --- END ([a-z0-9]+) ---", line)
        if begin:
            assert inside is None, f"BEGIN {begin.group(1)} inside {inside}"
            inside = begin.group(1)
            assert inside in CHAINS, f"unknown block {inside}"
        elif end:
            assert inside == end.group(1), f"END {end.group(1)} does not close {inside}"
            inside = None
        elif inside is not None:
            assert line.startswith("#"), f"line in block {inside} is not a comment line: {line!r}"
            out.append(re.sub(r"^# ?", "", line) if inside in names else line)
        else:
            out.append(line)
    assert inside is None, f"block {inside} has no END"
    return "\n".join(out) + "\n"


def report(title, good, detail=""):
    results.append(good)
    print(f"{'pass' if good else 'FAIL'}  {title}{(': ' + detail) if detail and not good else ''}")


def finish():
    raise SystemExit(0 if all(results) else 1)
PY

cat >"$WORK/signer_test.py" <<'PY'
import os
import re
import sys

sys.path.insert(0, "/work")
from common import CHAINS, TABLE, enable, finish, report  # noqa: E402

from acctpool_signer.config import parse_config  # noqa: E402
from acctpool_signer.errors import ConfigError  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

example = open("/work/pools.toml.example").read()


def fill(text):
    """Test addresses in the place of the placeholders. They are random: no key exists for them."""
    return re.sub(r'"0xREPLACE_[A-Z_]+"', lambda m: '"' + to_checksum_address(os.urandom(20)) + '"', text)


def load(text):
    try:
        return parse_config(text.encode()), ""
    except ConfigError as e:
        return None, str(e)


config, error = load(example)
report(
    "the example as it is: refused for the placeholder of the first destination",
    config is None and error == "stores.examplestore.destinations.polygon: must be an EIP-55 checksummed address",
    error,
)
report("the example as it is has placeholders", "REPLACE" in example)

sets = [()] + [(name,) for name in CHAINS] + [CHAINS]
for names in sets:
    text = fill(enable(example, names))
    title = "polygon" + "".join(" + " + name for name in names)
    config, error = load(text)
    if config is None:
        report(f"{title}: loaded by the signer", False, error)
        continue
    wanted = ("polygon", *names)
    problems = []
    if "REPLACE" in re.sub(r"(?m)^\s*#.*$", "", text):
        problems.append("a placeholder is left in an active line")
    if tuple(config.chains) != wanted:
        problems.append(f"chains are {list(config.chains)}")
    for name in wanted:
        chain = config.chains.get(name)
        if chain is not None and (chain.chain_id, chain.usdt) != TABLE[name]:
            problems.append(f"{name} is not the chain of the chain table")
    store = config.stores["examplestore"]
    if set(store.destinations) != set(wanted) or set(store.native_destinations) != set(wanted):
        problems.append("a destination or a native destination is missing")
    if (config.max_signatures_per_minute, config.max_derive_per_minute, config.max_other_per_minute) != (30, 10, 120):
        problems.append("rate limits are not 30, 10, 120")
    report(f"{title}: loaded by the signer, chains, contracts and destinations as wanted", not problems, "; ".join(problems))

# a wrong key and a destination that is the token contract must be refused: the loader does examine the file
text = fill(enable(example, ())).replace("gas_limit_cap = 150000", "gas_limit_kap = 150000", 1)
config, error = load(text)
report("a wrong spelling of a cap is refused", config is None and "unknown key" in error, error)
text = re.sub(r'(?m)^polygon = "0x[0-9a-fA-F]{40}"', 'polygon = "' + TABLE["polygon"][1] + '"', fill(enable(example, ())), count=1)
config, error = load(text)
report("a destination that is the USDT contract is refused", config is None and "token contract" in error, error)
finish()
PY

cat >"$WORK/plugin_test.py" <<'PY'
import os
import sys

sys.path.insert(0, "/work")
from common import finish, report  # noqa: E402

os.environ["ACCTPOOL_SECOND_OPINION"] = "/work/second.toml.example"
from modules.forkedpool.acctpool import secondcheck  # noqa: E402
from modules.forkedpool.acctpool.constants import CHAINS  # noqa: E402

report("second.toml.example: the polygon URL is read", secondcheck.url_of(CHAINS["polygon"]) == "https://CHANGE_ME")
for name in ("ethereum", "bnb"):
    try:
        secondcheck.url_of(CHAINS[name])
        report(f"second.toml.example: {name} is commented out, payouts there wait", False)
    except secondcheck.Down as e:
        report(f"second.toml.example: {name} is commented out, payouts there wait", "https" not in str(e))
finish()
PY

# the daemon image runs as a user that is not root: it must be able to read the folder
chmod 755 "$WORK" && chmod 644 "$WORK"/*

run() { # title, image, -v, mount of the source, folder of the packages, script
    echo "=== $1"
    podman rm -f v4d-config >/dev/null 2>&1
    podman run --rm --name v4d-config --network none --security-opt label=disable \
        -v "$WORK:/work:ro" "${@:3:2}" -w "$5" --env PYTHONPATH="$5" --env PYTHONDONTWRITEBYTECODE=1 \
        --entrypoint python "$2" "/work/$6" || failures=$((failures + 1))
}

run "signer loader (acctpool_signer.config.parse_config) and pools.toml.example" \
    docker.io/bitcart/bitcart-eth:0.10.3.0 -v "$SRC/signer:/src:ro" /src signer_test.py
run "plugin loader (secondcheck.url_of) and second.toml.example" \
    docker.io/bitcart/bitcart:0.10.3.0 -v "$SRC/backend/forkedpool:/app/modules/forkedpool:ro" /app plugin_test.py

echo
if [ "$failures" = 0 ]; then
    echo "PASS test-config-examples"
else
    echo "FAILED test-config-examples: $failures"
    exit 1
fi
