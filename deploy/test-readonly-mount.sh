#!/bin/bash
# Test of the read-only mount of compose/plugins/docker against the REAL stock entrypoint of the
# backend image (set -e, find ... ! -user electrum -exec chown), with the BITCART_VOLUMES value that
# the generator rule sets (SPEC 7.7). The value is read from a compose file that the real generator
# makes in this test, so the test covers rule -> compose file -> entrypoint.
#
# It proves:
#   - files with the owner root in the read-only tree do not stop the start
#   - __pycache__ files that the generator writes as root into the rules folders do no harm
#   - the backend user can read the module files (mode 644 / 755) and cannot write to the tree
#   - /datadir, /backups, /plugins/backend, /plugins/admin and /plugins/store still get the owner fix
#   - control: with the stock BITCART_VOLUMES value the same tree stops the start
#
# Where: on the test host, from the plugin folder:   deploy/test-readonly-mount.sh [image|ref]
#        image (default) = the entrypoint that is in the pinned image, which is what runs in production.
#        ref = the newer entrypoint of the bitcart-docker reference copy, mounted over the one of the image.
#        The two files are different (the image has an SSH key block, the reference copy does not).
#        The owner loop is the same in the two files.
# Needs: rootless podman, the image bitcart:0.10.3.0, network access to PyPI (oyaml for the generator),
#        and the reference input in $ACCTPOOL_REF (see scripts/test-generator.sh).
# Containers: v4d-ro-* (one at a time). They are removed at the end.
#
# USER IDS. Rootless podman maps user ids, and the test uses no --userns option (default mapping):
# root (0) in the container is the user who runs podman, and uid 1000 in the container (electrum) is a
# sub-uid of that user. So a file that the test user makes on the host IS a root-owned file for the
# container, which is the case of the payment host (owner root). "podman unshare" runs a command with
# the same mapping; the test uses it to read owners as the container sees them and to remove files.
# With docker on the payment host there is no mapping: uid 0 and uid 1000 are the same in and out.
set -uo pipefail

PLUGIN=$(cd "$(dirname "$0")/.." && pwd)
REF=${ACCTPOOL_REF:-$HOME/acctpool-build/ref}
IMAGE=${ACCTPOOL_BACKEND_IMAGE:-docker.io/bitcart/bitcart:0.10.3.0}
WORK=$(mktemp -d "$HOME/acctpool-build/v4d-ro.XXXXXX")
PLUGINS=$WORK/compose/plugins
TREE=$PLUGINS/docker
STOCK_VOLUMES="/datadir /backups /plugins"
failures=0
ENTRYPOINT_SOURCE=${1:-image}
REF_ENTRYPOINT=$REF/bitcart-docker/compose/scripts/docker-entrypoint.sh
ENTRYPOINT_MOUNT=()
case "$ENTRYPOINT_SOURCE" in
image) ;;
ref) ENTRYPOINT_MOUNT=(-v "$REF_ENTRYPOINT:/usr/local/bin/docker-entrypoint.sh:ro") ;;
*) echo "usage: $0 [image|ref]" >&2; exit 2 ;;
esac

cleanup() {
    podman rm -f v4d-ro-gen v4d-ro-backend >/dev/null 2>&1
    podman unshare rm -rf "$WORK"
}
trap cleanup EXIT

say() { printf '\n=== %s\n' "$*"; }
result() { # name expected-word actual-word
    if [ "$2" = "$3" ]; then
        echo "pass  $1 ($3)"
    else
        echo "FAIL  $1: expected $2, got $3"
        failures=$((failures + 1))
    fi
}
# owners as the container sees them: "<uid> <count>" for each uid, in one line
owner_summary() { podman unshare find "$@" -printf '%U\n' | sort | uniq -c | awk '{printf "uid %s: %s  ", $2, $1}'; }
count_not_owner() { local uid=$1; shift; podman unshare find "$@" ! -user "$uid" | wc -l; }
tree_sums() { (cd "$TREE" && find . -type f ! -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum); }

say "0. the entrypoint under test: $ENTRYPOINT_SOURCE"
podman run --rm --name v4d-ro-backend --network none --security-opt label=disable "${ENTRYPOINT_MOUNT[@]}" \
    --entrypoint cat "$IMAGE" /usr/local/bin/docker-entrypoint.sh >"$WORK/entrypoint.sh"
cat "$WORK/entrypoint.sh"
echo "--- diff: reference copy (<) and the file of the image (>); information, not a test"
podman run --rm --name v4d-ro-backend --network none --entrypoint cat "$IMAGE" /usr/local/bin/docker-entrypoint.sh |
    diff "$REF_ENTRYPOINT" - && echo "(no difference)"
echo "---"
# the three properties that the design depends on
grep -Eq '^set -e' "$WORK/entrypoint.sh" && r=yes || r=no
result "entrypoint stops on the first error (set -e)" yes "$r"
grep -Eq 'for volume in \$BITCART_VOLUMES' "$WORK/entrypoint.sh" &&
    grep -Eq 'find "\$volume" \\! -user electrum .*-exec chown electrum .\{\}. \+' "$WORK/entrypoint.sh" && r=yes || r=no
result "entrypoint runs chown on each file below the folders of BITCART_VOLUMES" yes "$r"
grep -Eq '^exec gosu electrum' "$WORK/entrypoint.sh" && r=yes || r=no
result "entrypoint changes to the user electrum" yes "$r"

say "1. make the tree as on the payment host: owner root, mode 644 / 755"
mkdir -p "$WORK/compose" "$WORK/datadir" "$WORK/backups"
cp -r "$REF/bitcart-docker/generator" "$WORK/generator"
(cd "$REF/bitcart-docker/compose" && tar cf - --exclude=./plugins --exclude=./generated.yml --exclude=./metadata.json .) |
    tar xf - -C "$WORK/compose"
mkdir -p "$PLUGINS/backend" "$PLUGINS/admin" "$PLUGINS/store" "$TREE/solana" "$TREE/forked_host" "$TREE/acctpool"
cp -r "$REF/solana/components" "$REF/solana/rules" "$TREE/solana/"
cp -r "$REF/forked_host/rules" "$TREE/forked_host/"
cp -r "$PLUGIN/components" "$PLUGIN/rules" "$PLUGIN/backend" "$TREE/acctpool/"
find "$WORK" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "+ chmod -R go-w,a+rX $TREE        (the owner is the test user = root for the container)"
chmod -R go-w,a+rX "$TREE"
# one root-owned file in each folder that must still get the owner fix
for folder in "$WORK/datadir" "$WORK/backups" "$PLUGINS/backend" "$PLUGINS/admin" "$PLUGINS/store"; do
    mkdir -p "$folder/sub" && echo data >"$folder/sub/file"
done
echo "owners in the read-only tree, as the container sees them: $(owner_summary "$TREE")"
result "files in the tree with an owner that is not root" 0 "$(count_not_owner 0 "$TREE")"
result "files in the tree that group or others can write" 0 "$(find "$TREE" ! -type l -perm /022 | wc -l)"
tree_sums >"$WORK/sums-before.txt"

say "2. run the real generator (as root in its container) on this tree"
echo "+ podman run --rm --name v4d-ro-gen --security-opt label=disable -v WORK:/work -w /work \\"
echo "    -e BITCART_CRYPTOS=btc,ltc,bch,xmr,trx,eth,matic -e BITCART_HOST=pay.example.com ... \\"
echo "    --entrypoint sh IMAGE -c 'uv pip install oyaml; python -m generator'"
podman run --rm --name v4d-ro-gen --security-opt label=disable -v "$WORK:/work" -w /work \
    --env BITCART_CRYPTOS=btc,ltc,bch,xmr,trx,eth,matic --env BITCART_HOST=pay.example.com \
    --env BITCART_VERSION=0.10.3.0 --env NAME=compose --entrypoint sh "$IMAGE" \
    -c 'uv pip install -q --python /app/.venv/bin/python oyaml && python -m generator' || {
    echo "FAIL the generator did not run"
    exit 2
}
echo "bytecode that the generator wrote (owner as the container sees it):"
podman unshare find "$TREE" -path '*/__pycache__*' -printf '    %U:%G %m %y %P\n' | sort -k4
result "__pycache__ folders in the rules folders" 3 "$(find "$TREE" -type d -name __pycache__ -path '*/rules/*' | wc -l)"
result "files in the tree with an owner that is not root, after the generator" 0 "$(count_not_owner 0 "$TREE")"
grep 'BITCART_VOLUMES:\|plugins/docker:/plugins/docker' "$WORK/compose/generated.yml" | sort | uniq -c
NEW_VOLUMES=$(grep -m1 'BITCART_VOLUMES:' "$WORK/compose/generated.yml" | sed 's/.*BITCART_VOLUMES: //')
result "BITCART_VOLUMES in the generated file" "/datadir /backups /plugins/backend /plugins/admin /plugins/store" "$NEW_VOLUMES"
result "services with the new BITCART_VOLUMES value" 2 "$(grep -c "BITCART_VOLUMES: $NEW_VOLUMES\$" "$WORK/compose/generated.yml")"
result "services with the read-only plugins mount" 2 "$(grep -c -- '- ./plugins/docker:/plugins/docker:ro$' "$WORK/compose/generated.yml")"

# The mounts are those of the generated compose file for the backend service.
backend() { # BITCART_VOLUMES value, mount option of the plugins tree (ro or rw)
    local volumes=$1 mode=$2
    echo "+ podman run --name v4d-ro-backend --network none --security-opt label=disable \\"
    echo "    -e 'BITCART_VOLUMES=$volumes' \\"
    echo "    -v datadir:/datadir -v backups:/backups -v plugins/backend:/plugins/backend \\"
    echo "    -v plugins/admin:/plugins/admin -v plugins/store:/plugins/store \\"
    echo "    -v plugins/docker:/plugins/docker:$mode \\"
    echo "    -v plugins/docker/acctpool/backend/forkedpool:/app/modules/forkedpool:ro \\"
    echo "    ${ENTRYPOINT_MOUNT[*]:+-v ref/docker-entrypoint.sh:/usr/local/bin/docker-entrypoint.sh:ro }IMAGE sh -c '<tests>'"
    podman rm -f v4d-ro-backend >/dev/null 2>&1
    podman run --name v4d-ro-backend --network none --security-opt label=disable \
        --env "BITCART_VOLUMES=$volumes" \
        -v "$WORK/datadir:/datadir" -v "$WORK/backups:/backups" \
        -v "$PLUGINS/backend:/plugins/backend" \
        -v "$PLUGINS/admin:/plugins/admin" \
        -v "$PLUGINS/store:/plugins/store" \
        -v "$TREE:/plugins/docker:$mode" \
        -v "$TREE/acctpool/backend/forkedpool:/app/modules/forkedpool:ro" \
        "${ENTRYPOINT_MOUNT[@]}" \
        "$IMAGE" sh -c '
            echo "STARTED as $(id -un) uid $(id -u)"
            try() { if "$@" >/dev/null 2>/tmp/err; then echo "  DONE      $*"; else echo "  REFUSED   $* ($(sed "s/.*: //" /tmp/err | head -1))"; fi; }
            echo " write tests on the read-only tree and the module mount"
            try touch /plugins/docker/new-file
            try mkdir /plugins/docker/new-plugin
            try sh -c "echo x >> /plugins/docker/acctpool/rules/91_acctpool.py"
            try rm /plugins/docker/acctpool/rules/91_acctpool.py
            try rm -rf /plugins/docker/acctpool/rules/__pycache__
            try mv /plugins/docker/acctpool /plugins/docker/moved
            try chmod 777 /plugins/docker/acctpool/rules
            try chown electrum /plugins/docker/acctpool/rules/91_acctpool.py
            try touch /app/modules/forkedpool/new-file
            echo " read tests as the backend user"
            echo "  module files: $(find /app/modules/forkedpool -type f | wc -l), not readable: $(find /app/modules/forkedpool -type f ! -readable | wc -l), folders that cannot be entered: $(find /app/modules/forkedpool -type d ! -executable | wc -l)"
            try find /app/modules/forkedpool -type f -exec cat {} +
            try cat /plugins/docker/acctpool/rules/91_acctpool.py
            try python -c "import modules.forkedpool"
            try python -c "import modules.forkedpool.acctpool.chain.keccak as k; assert k"
            echo " write tests on the other mounts"
            try touch /plugins/backend/new-file /plugins/admin/new-file /plugins/store/new-file
            try touch /plugins/backend/sub/file /plugins/admin/sub/file /plugins/store/sub/file
            try touch /datadir/sub/file /backups/sub/file
        ' >"$WORK/out.txt" 2>&1
    RC=$?
    sed 's/^/    | /' "$WORK/out.txt" | grep -v '^    |  *try\|^    |  *echo\|^    | *$' | cut -c1-220 | head -70
    echo "    exit code of the container: $RC"
    podman rm -f v4d-ro-backend >/dev/null 2>&1
}
started() { grep -q '^STARTED as electrum' "$WORK/out.txt" && echo started || echo not-started; }
refused() { grep -c '^  REFUSED ' "$WORK/out.txt"; }
done_count() { grep -c '^  DONE ' "$WORK/out.txt"; }

say "3. MAIN CASE: root-owned tree with root-written __pycache__, read-only, new BITCART_VOLUMES"
backend "$NEW_VOLUMES" ro
result "container start" started "$(started)"
result "write operations that were refused (8 on the tree, 1 on the module mount)" 9 "$(refused)"
result "read and write operations that were done (4 reads, 3 writes to other mounts)" 7 "$(done_count)"
grep -q 'module files: [1-9][0-9]*, not readable: 0, folders that cannot be entered: 0' "$WORK/out.txt" && r=yes || r=no
result "the backend user can read every module file" yes "$r"
result "files in the tree with an owner that is not root, after the container run" 0 "$(count_not_owner 0 "$TREE")"
tree_sums >"$WORK/sums-after.txt"
cmp -s "$WORK/sums-before.txt" "$WORK/sums-after.txt" && r=same || r=different
result "files of the tree before and after (sha256)" same "$r"
for folder in datadir backups compose/plugins/backend compose/plugins/admin compose/plugins/store; do
    echo "owners in $folder: $(owner_summary "$WORK/$folder")"
    result "owner fix in /${folder#compose/}: files with an owner that is not electrum (1000)" 0 "$(count_not_owner 1000 "$WORK/$folder")"
done

say "4. a module file with mode 600 (wrong mode): what occurs"
chmod 600 "$TREE/acctpool/backend/forkedpool/__init__.py"
backend "$NEW_VOLUMES" ro
result "container start" started "$(started)"
grep -q 'not readable: 1,' "$WORK/out.txt" && r=yes || r=no
result "the backend user cannot read the file with mode 600" yes "$r"
chmod 644 "$TREE/acctpool/backend/forkedpool/__init__.py"

say "5. CONTROL: the same tree, read-only, with the STOCK value of BITCART_VOLUMES"
# new root-owned files, so that the owner fix of this run has something to do
backend "$STOCK_VOLUMES" ro
result "container with the stock BITCART_VOLUMES and a read-only root-owned tree" not-started "$(started)"
grep -c 'Read-only file system' "$WORK/out.txt" | sed 's/^/    chown errors: /'

say "6. CONTROL: stock installation (read-write tree, stock BITCART_VOLUMES)"
backend "$STOCK_VOLUMES" rw
result "container start" started "$(started)"
echo "owners in the tree after the stock run: $(owner_summary "$TREE")"
result "stock behaviour: the entrypoint gave the full tree to electrum (files not owned by 1000)" 0 "$(count_not_owner 1000 "$TREE")"

echo
if [ "$failures" = 0 ]; then
    echo "PASS test-readonly-mount ($ENTRYPOINT_SOURCE entrypoint)"
else
    echo "FAILED test-readonly-mount ($ENTRYPOINT_SOURCE entrypoint): $failures"
    exit 1
fi
