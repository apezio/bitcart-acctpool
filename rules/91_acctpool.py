# Adds the acctpool backend module and its files to the stock backend and worker services, and makes
# the stock mount of compose/plugins/docker read-only in these two services.
#
# Why read-only: the start script runs the generator rules from that tree. With the stock read-write
# mount, a compromised backend can change a rule, and the next ./start.sh runs it as root.
#
# Why BITCART_VOLUMES changes too: the stock entrypoint runs chown on every file below the folders of
# that list, and it stops when chown fails. On a read-only mount chown always fails, so /plugins/docker
# must not be below a folder of the list. The other three plugin folders stay in the list by name.
#
# The rule edits two values in place and adds entries. It never replaces a list or a dict, because
# other plugin rules (the Solana module mount, the worker hostname) write to the same services, and the
# generator loads plugin folders in os.listdir() order, which is not defined.
#
# Host paths are outside compose/plugins on purpose. The files must exist before ./start.sh, or docker
# creates a directory in place of each missing file.

SIGNER_SERVICE = "acctpool-signer"
NETWORK = "acctpool_internal"
SIGNER_URL = "http://acctpool-signer:7070"

STOCK_PLUGINS_MOUNT = "./plugins/docker:/plugins/docker"
READ_ONLY_PLUGINS_MOUNT = STOCK_PLUGINS_MOUNT + ":ro"
VOLUMES_KEY = "BITCART_VOLUMES"
STOCK_VOLUMES = "/datadir /backups /plugins"
VOLUMES_WITHOUT_DOCKER_PLUGINS = "/datadir /backups /plugins/backend /plugins/admin /plugins/store"
# Only our package is mounted, read-only, next to the image's own /app/modules/__init__.py.
MODULES_MOUNT = "./plugins/docker/acctpool/backend/forkedpool:/app/modules/forkedpool:ro"
# The second-opinion RPC URLs (SPEC-v4 4.7): the worker only, it is the one that asks for signatures.
SECOND_MOUNT = "/root/acctpool/second.toml:/run/acctpool/second.toml:ro"
# The worker gets its own copy of the token: worker and signer run with different user ids,
# and one file with mode 600 has one owner.
TOKEN_MOUNT = "/root/acctpool/worker.token:/run/acctpool/signer.token:ro"  # noqa: S105


def plugins_mount_index(service):
    """Position of the stock mount, or None when the service is not in the known stock form."""
    volumes, environment = service.get("volumes"), service.get("environment")
    if not isinstance(volumes, list) or not isinstance(environment, dict):
        return None
    if environment.get(VOLUMES_KEY) != STOCK_VOLUMES or volumes.count(STOCK_PLUGINS_MOUNT) != 1:
        return None
    return volumes.index(STOCK_PLUGINS_MOUNT)


def add_network(service, network):
    # A service without "networks" is on "default". With an explicit list it must name "default" itself.
    networks = service.setdefault("networks", ["default"])
    if isinstance(networks, dict):
        networks.setdefault(network, {})
    elif network not in networks:
        networks.append(network)


def add_environment(service, key, value):
    environment = service.setdefault("environment", {})
    if isinstance(environment, dict):
        environment[key] = value
    else:
        environment.append(f"{key}={value}")


def rule(services, settings):
    # Without the signer component the network does not exist, and a reference to it breaks the whole compose file.
    if SIGNER_SERVICE not in services:
        return
    backend, worker = services.get("backend"), services.get("worker")
    if backend is None or worker is None:
        return
    # All or nothing: when the stock mount or the stock BITCART_VOLUMES value is not there in its known
    # form on both services, upstream has changed. Then the rule changes nothing and adds nothing: the
    # plugin must not run with a plugins tree that the containers can write, and a read-only mount with
    # the stock BITCART_VOLUMES stops the containers at start. The generator test then fails.
    found = {name: plugins_mount_index(service) for name, service in (("backend", backend), ("worker", worker))}
    if None in found.values():
        return
    for name, service in (("backend", backend), ("worker", worker)):
        service["volumes"][found[name]] = READ_ONLY_PLUGINS_MOUNT
        service["environment"][VOLUMES_KEY] = VOLUMES_WITHOUT_DOCKER_PLUGINS
        service["volumes"].append(MODULES_MOUNT)
    worker["volumes"].extend([TOKEN_MOUNT, SECOND_MOUNT])
    add_environment(worker, "ACCTPOOL_SIGNER_URL", SIGNER_URL)
    add_network(worker, NETWORK)
