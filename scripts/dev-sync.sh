#!/bin/bash
# Copy the plugin tree (or one subfolder) from your workstation to the test host ($TEST_HOST).
# Absolute paths only: a relative rsync once copied a whole home directory. SRC is the root of the repository
# (or worktree) that holds this script; DST is the folder of this branch on the test host.
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
TEST_HOST=${TEST_HOST:?set TEST_HOST to the ssh name of the test host}
DST=$(ssh "$TEST_HOST" 'echo "$HOME"')/acctpool-build/v4-backend
SUB="${1:-}"
case "$SRC" in /*) ;; *) echo "SRC is not absolute" >&2; exit 2;; esac
case "$SUB" in *..*|/*) echo "bad subfolder" >&2; exit 2;; esac
if [ -n "$SUB" ]; then
    [ -d "$SRC/$SUB" ] || { echo "no such folder: $SRC/$SUB" >&2; exit 2; }
    ssh "$TEST_HOST" "mkdir -p '$DST/$SUB'"
    rsync -a --delete --exclude __pycache__ --exclude .pytest_cache "$SRC/$SUB/" "$TEST_HOST:$DST/$SUB/"
else
    ssh "$TEST_HOST" "mkdir -p '$DST'"
    rsync -a --delete --exclude .git --exclude __pycache__ --exclude .pytest_cache "$SRC/" "$TEST_HOST:$DST/"
fi
echo "synced ${SUB:-all} -> $TEST_HOST:$DST/${SUB}"
