#!/bin/bash
# Unit tests of the backend plugin (SPEC-v4 5), inside the pinned Bitcart image with postgres and redis, on the
# test host. Run on your workstation with TEST_HOST set:
#
#   scripts/test-backend.sh [--keep] [--lint] [--clean] [pytest arguments]
#
#   --keep   leave postgres and redis running for the next run (remove them with --clean)
#   --lint   also run ruff (check + format check) with Bitcart's own ruff settings
#   --clean  remove the containers and the pod, run nothing
#
# The daemon and the signer are fakes inside the test process (backend_tests/plugin/fakes.py).
# Containers: v4u-pg, v4u-redis, v4u-test in the pod v4u-pod. They are removed at the end, also after a failure.
set -euo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
TEST_HOST=${TEST_HOST:?set TEST_HOST to the ssh name of the test host}
for sub in backend/forkedpool backend_tests/plugin integration/evm rules; do
    "$SRC/scripts/dev-sync.sh" "$sub" >/dev/null
done

QUOTED=""
[ $# -eq 0 ] || QUOTED=$(printf '%q ' "$@")
# shellcheck disable=SC2029
ssh -o BatchMode=yes "$TEST_HOST" "nice bash -s -- $QUOTED" <<'REMOTE'
set -euo pipefail
DST=$HOME/acctpool-build/v4-backend
IMAGE=docker.io/bitcart/bitcart:0.10.3.0
KEEP=0; LINT=0; CLEAN=0; ARGS=()
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        --lint) LINT=1 ;;
        --clean) CLEAN=1 ;;
        *) ARGS+=("$arg") ;;
    esac
done
cleanup() {
    podman rm -f v4u-test v4u-redis v4u-pg >/dev/null 2>&1 || true
    podman pod rm -f v4u-pod >/dev/null 2>&1 || true
}
if [ "$CLEAN" = 1 ]; then
    cleanup
    podman ps -a --format '{{.Names}}' | grep '^v4u-' && { echo "containers left" >&2; exit 1; }
    echo "no v4u- container left"
    exit 0
fi
[ "$KEEP" = 1 ] || trap cleanup EXIT
podman rm -f v4u-test >/dev/null 2>&1 || true
if [ "$(podman inspect -f '{{.State.Running}}' v4u-pg 2>/dev/null)" != "true" ]; then
    cleanup
    podman pod create --name v4u-pod --infra=false >/dev/null
    podman run -d --name v4u-pg --pod v4u-pod --tmpfs /var/lib/postgresql/data -e POSTGRES_PASSWORD=v4u \
        docker.io/library/postgres:17-alpine -c max_connections=200 -c fsync=off >/dev/null
    podman run -d --name v4u-redis --pod v4u-pod --network container:v4u-pg docker.io/library/redis:alpine >/dev/null
fi
for _ in $(seq 1 60); do podman exec v4u-pg pg_isready -q -h 127.0.0.1 -U postgres && break; sleep 1; done

STEPS="uv pip install -q --python /app/.venv/bin/python 'pytest>=8.3,<9' 'httpx>=0.28,<1'"
if [ "$LINT" = 1 ]; then
    # tests and the integration driver: without the security rules (S) and the private-member rule (SLF001)
    TESTS="/plug/backend_tests/plugin /plug/integration/evm"
    STEPS="$STEPS && uvx -q ruff check --config /app/pyproject.toml /plug/backend/forkedpool /plug/rules \
 && uvx -q ruff check --config /app/pyproject.toml --ignore S,SLF001 $TESTS \
 && uvx -q ruff format --check --config /app/pyproject.toml /plug/backend/forkedpool /plug/rules $TESTS"
fi
STEPS="$STEPS && python -m pytest -p no:cacheprovider -c /plug/backend_tests/plugin/pytest.ini --rootdir /plug/backend_tests/plugin /plug/backend_tests/plugin \"\$@\""
# label=disable: the host has SELinux enforcing, and the code folder must keep its labels
podman run --rm --name v4u-test --pod v4u-pod --network container:v4u-pg --security-opt label=disable \
    -v "$DST:/plug:ro" -v "$DST/backend/forkedpool:/app/modules/forkedpool:ro" -w /app \
    -e DB_HOST=127.0.0.1 -e DB_PASSWORD=v4u -e REDIS_HOST=127.0.0.1 -e PYTHONDONTWRITEBYTECODE=1 \
    --entrypoint sh "$IMAGE" -c "$STEPS" sh "${ARGS[@]+"${ARGS[@]}"}"
REMOTE
