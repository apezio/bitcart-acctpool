# acctpool: deploy steps for the payment host

v4 (SPEC-v4): the stock Bitcart daemon of each chain is the chain layer. The plugin has no RPC config of its own (`rpc.toml` is gone); the worker reads ONE second-opinion RPC URL per chain from `/root/acctpool/second.toml` (step 2.5) and checks every payout against it before it asks for a signature. Tron is not part of v4.

Status of this file: the commands were NOT run on a docker host. The test host had podman only. These parts were tested there: the generator rule (`scripts/test-generator.sh`), the probe fragment with stand-ins and with the real signer image, including the high-water file with the real `audit-verify` and an end to end test (seed with `exec -it`, mark of the probe, restore of an older copy of the volume, start refused) (`deploy/test-probe-checks.sh`), the read-only plugins tree with the entrypoint of the backend image and a compose file of the real generator (`deploy/test-readonly-mount.sh`), the two example config files with the loaders of the signer and of the plugin (`deploy/test-config-examples.sh`), `fetch-wheels.sh` and the image build with `--network none` (podman), and the check commands of steps 2.2, 2.3 and 2.5 (podman in the place of docker). Do a full rehearsal of this file on a test installation before you use it on a host that takes payments.

Each numbered step is one approval. Do one step, do its check, then stop and report.

Names in the commands: `/root/bitcart-docker` is the bitcart-docker folder and `compose` is the project name (the default). Change them if your installation is different.

The commands use these shell variables. Set them again in each new root shell. Replace each `CHANGE_ME` (step 1 gives `B`, step 2.2 gives the three user ids).

```
HOST=CHANGE_ME          # public name of the Bitcart API
STORE=CHANGE_ME         # store name from pools.toml
B=CHANGE_ME             # backup folder of step 1
SIGNER_UID=CHANGE_ME; SIGNER_GID=CHANGE_ME; WORKER_UID=CHANGE_ME
```

## Read before step 1

- `./start.sh` RECREATES CONTAINERS. The first deploy, and each later change of a file in `compose/plugins/docker/`, makes a new compose file. Expect 1 to 2 minutes without the payment API for ALL coins. Choose a quiet time.
- With this plugin, backend and worker mount `compose/plugins/docker` READ-ONLY. Reason: the start script runs the generator rules of that tree as root, so a backend that can write there can run code as root at the next start. No secret and no config goes into that tree.
- Effect of the read-only mount: the Bitcart admin page can no longer install or remove docker-type plugins. A manual copy by root is the only path. Plugins of the types backend, admin and store are not changed.
- The plugin also changes `BITCART_VOLUMES` of backend and worker, so that their entrypoint does not try to change the owner of files in the read-only tree. All files in `compose/plugins/docker` have the owner root and no write bit for group and others (`chown -R root:root`, `chmod -R go-w,a+rX`). The files must be readable for all (mode 644, folders 755): the backend reads its module from that tree as the user 1000.
- The generator writes `__pycache__` folders into the `rules` folders. They do no harm (tested). They are not in the checksum list.
- Use the start sequence of step 3 for EACH `./start.sh`, `./restart.sh` and `./update.sh`: it examines the plugins tree first.
- The signer image is built by hand from `/root/acctpool/signer-src/` (step 2.2). Compose does not build it and does not pull it. If the image is missing, the start fails.
- Every bind-mounted file must exist before `./start.sh`. If a file is missing, docker makes a DIRECTORY with that name and the container does not start correctly.
- The containers do not run as root. A file with owner root and mode 600 cannot be read in the container. The owner of each secret file is the user id of the process that reads it. The folders (`/root/acctpool`, `/var/lib/acctpool-key`) have owner root and mode 700, so no other user of the host can get to the files.
- NEVER delete the volume `compose_acctpool_signer_data` and never use `docker compose down -v`. The volume has the keystore, the signer journal and the audit log. The one exception is a burned seed before money arrived (step 4.4).
- The seed (24 words) is shown one time. It is the only recovery path. Without the written copy, money on deposit addresses is lost if the host is lost.
- The seed words must never be in a log. The signer refuses `init` and `restore` as the main process of a container, because the log driver copies all output of the main process. The two commands run only with `docker exec -it` in the running signer container. Step 4.2 proves, before the seed is made, that docker does not copy `exec` output into a log.
- The signer container writes its audit lines to its log. The component sets the log driver `json-file` with 5 files of 10 MB at most. With this driver the audit lines are NOT sent to the host journal or to syslog. The master copy is in the volume (journal and `audit.log`), so the backup of the volume (step 5) is the only other copy.
- The high-water file `/root/acctpool/audit.highwater` (step 2.4) is the one record outside the volume that a restore cannot take back. The probe writes it as root; the signer reads it at its start and refuses a volume that is older than it. Never delete it and never write a lower value into it by hand (the one exception is step 4.4). A restore of the signer volume follows "Restore of the signer volume" below.
- The signer container must run without an init program: no `init: true` in compose (the component has none) and no `"init": true` in the Docker daemon config. With an init program, `init` and `restore` refuse to run, and no seed can be made. Step 2.2 checks the daemon, step 3 the container.
- The master key is not in a backup on purpose. If it is lost, make a new key and restore the keystore from the written seed (step 4.6 shows the method).
- Polygon is the first chain. Ethereum and BNB Smart Chain are later phases ("Later chains"). Tron is not in v4.
- The plugin loads one diskless watch-only wallet pair (native + USDT) per watched deposit address into the stock daemon. The daemon forgets them at its restart; the plugin loads them again when Bitcart reconnects (check_pending) and every 10 minutes, and a balance reconcile (every minute) finds payments that came while the daemon was down. About 3,000 addresses load in 21 s.

You must have before step 1: ONE second-opinion RPC URL for each chain, from a company that is not the provider of the stock daemon (`MATIC_SERVER` etc. of bitcart-docker stay as they are), the payout addresses of the first store (USDT and native coin), the native coin for the fee wallet (do not send it before step 4 is complete), the test results of the signer and the plugin, and the two review reports. For a store with its own checkout front end: the change of the section "Checkout front ends" must be live first.

## Step 1: backup

```
cd /root/bitcart-docker
B=/root/acctpool-backup-$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$B"
docker exec compose-database-1 pg_dumpall -c -U postgres \
  > "$B/database.sql"
cp -a compose/generated.yml .env .deploy "$B/"
docker ps --format '{{.Names}}' | sort > "$B/containers.txt"
docker inspect -f '{{.Config.Hostname}}' compose-worker-1 \
  > "$B/worker-hostname.txt"
curl -s "https://$HOST/api/cryptos" > "$B/cryptos.json"
echo "$B"
```

Check:

```
tail -n 3 "$B/database.sql" | grep -c 'database cluster dump complete'
cmp compose/generated.yml "$B/generated.yml" && echo same
wc -l "$B/containers.txt"; cat "$B/cryptos.json"
```

The first command prints `1`. The second prints `same`. The container list and the coin list are those that you expect.

## Step 2: files

### 2.1 Copy the files to the host

On the machine that has the reviewed source, in the plugin folder, make two checksum lists. The `wheels` folder is not in the list: step 2.2 makes it on the host.

```
find components rules backend -type f \
  ! -path '*/__pycache__/*' -print0 | sort -z \
  | xargs -0 sha256sum > acctpool.sha256
( cd signer && find . -type f ! -path './tests/*' \
  ! -path './wheels/*' ! -path '*/__pycache__/*' -print0 \
  | sort -z | xargs -0 sha256sum ) > signer-src.sha256
```

On the host, make the folders:

```
install -d -m 700 -o root -g root /root/acctpool /var/lib/acctpool-key \
  /root/acctpool-stage /root/acctpool/signer-src
```

Copy to the host:

- the three folders `components`, `rules`, `backend` to `/root/acctpool-stage/acctpool/`. The `signer` folder does NOT go there. Do not copy the folders into `compose/plugins/` at this time: a restart of the installation would then start a half-installed plugin.
- the content of the `signer` folder, without `tests` and without `wheels`, to `/root/acctpool/signer-src/`.
- the files `acctpool.sha256`, `signer-src.sha256`, `deploy/pools.toml.example`, `deploy/second.toml.example`, `deploy/probe-checks.sh` to `/root/acctpool-stage/`.

```
chown -R root:root /root/acctpool/signer-src /root/acctpool-stage
chmod -R go-rwx /root/acctpool/signer-src /root/acctpool-stage
```

Check:

```
cd /root/acctpool-stage/acctpool
sha256sum -c --quiet --strict ../acctpool.sha256 && echo plugin-ok
find . -type f ! -path '*/__pycache__/*' | wc -l
wc -l < ../acctpool.sha256
ls
cd /root/acctpool/signer-src
sha256sum -c --quiet --strict /root/acctpool-stage/signer-src.sha256 \
  && echo signer-ok
find . -type f ! -path '*/__pycache__/*' | wc -l
wc -l < /root/acctpool-stage/signer-src.sha256
```

`plugin-ok` and `signer-ok` show. In each pair the two numbers are equal (no file that is not in the list). `ls` shows `backend components rules` and no `signer`.

### 2.2 Build the signer image and read its user id

The image adds ONE package to the Bitcart Ethereum daemon image: `coincurve` (libsecp256k1). The build itself has no network. The wheel file comes first, from `fetch-wheels.sh`: it downloads one file with an exact name from PyPI and compares its SHA-256 with the value in the script and in `requirements.txt`. A file with another hash is refused and not kept.

The host needs HTTPS to `files.pythonhosted.org` for this one download. Without it, copy the file `coincurve-21.0.0-cp312-cp312-musllinux_1_2_x86_64.whl` into `/root/acctpool/signer-src/wheels/` by hand; the script then checks the hash of that file.

The base image must be on the host. Check it; if it is missing, pull it by its digest (the digest is in the `FROM` line of the Dockerfile):

```
docker image inspect -f '{{.Id}}' docker.io/bitcart/bitcart-eth:0.10.3.0
grep '^FROM' /root/acctpool/signer-src/Dockerfile
```

Fetch the wheel and check the hash:

```
cd /root/acctpool/signer-src
bash fetch-wheels.sh
sha256sum wheels/*.whl
grep -o 'sha256:[0-9a-f]*' requirements.txt
```

Expected output of `fetch-wheels.sh` (these lines are from the test host):

```
fetch-wheels: download coincurve-21.0.0-cp312-cp312-musllinux_1_2_x86_64.whl
fetch-wheels: ok, SHA-256 is correct: coincurve-21.0.0-cp312-cp312-musllinux_1_2_x86_64.whl
```

When the file is already there, the one line is `fetch-wheels: ok, in wheels/ with the correct SHA-256: coincurve-...whl`. The last two commands print the same value: `bcc0831f07cb75b91c35c13b1362e7b9dc76c376b27d01ff577bec52005e22a8`. A line that starts with `fetch-wheels: REFUSED` (exit code 1) means that the file is not the reviewed file: stop. Do not change the hash in the script.

Build and examine the image:

```
chown -R root:root wheels; chmod -R go-rwx wheels
docker build --network none -t forked/acctpool-signer:0.1.0 \
  /root/acctpool/signer-src
docker image inspect -f '{{.Id}} {{.Config.User}}' \
  forked/acctpool-signer:0.1.0
docker run --rm --network none --entrypoint python \
  forked/acctpool-signer:0.1.0 -I -c \
  'from eth_keys import keys; print(type(keys.backend).__name__)'
docker run --rm --network none --entrypoint python \
  forked/acctpool-signer:0.1.0 \
  -c 'import os; print(os.getuid(), os.getgid())'
docker exec compose-worker-1 id -u electrum
```

Check: the build has no error. Its install step shows `Installed 1 package` and `+ coincurve==21.0.0` (the lines of the test host). Without the wheel the build stops with `ERROR: no wheel file in signer/wheels/`. `inspect` prints the image id and the user `10001:10001`. Write the image id down. The next command prints `CoinCurveECCBackend`. The next prints `10001 10001`: these are SIGNER_UID and SIGNER_GID. The last command prints the user id of the worker process (stock image: `1000`): this is WORKER_UID. Set the three variables now.

The tag in `components/acctpool.yml` (`image:`) and the tag of the build must be the same. After each change of the signer source: copy, checksum check, `fetch-wheels.sh`, build, then the start sequence of step 3 (compose makes a new signer container when the image id is different).

```
case "$SIGNER_UID$SIGNER_GID$WORKER_UID" in
  *[!0-9]*|'') echo "STOP: set the three user ids" ;;
  *) echo ids-ok ;;
esac
```

Check that the Docker daemon adds no init program to containers:

```
grep -n '"init"' /etc/docker/daemon.json
ps -o args= -C dockerd | grep -c -- '--init'
```

Expected: the `grep` prints nothing (or says that the file does not exist), and the count is `0`. A line `"init": true` in `daemon.json`, or `--init` on the command line of `dockerd`: stop. Changing the daemon config is its own approval (it restarts all containers). `docker info` shows only the name of the init program, not whether it is on for all containers; step 3 (check 3) proves it on the signer container itself.

### 2.3 `pools.toml`

```
install -m 644 -o root -g root \
  /root/acctpool-stage/pools.toml.example /root/acctpool/pools.toml
```

Edit `/root/acctpool/pools.toml`: the store name, the account number, the USDT payout address (`destinations`) and the native coin payout address (`native_destinations`). Get each payout address from its source two times and compare all characters. Leave the blocks of the other chains as comments.

Check with the config loader of the signer itself (it prints the chains and the destinations, which are public):

```
docker run --rm --network none --entrypoint python \
  -v /root/acctpool/pools.toml:/p.toml:ro \
  forked/acctpool-signer:0.1.0 -I -c '
from acctpool_signer.config import load_config
c = load_config("/p.toml")
for ch in c.chains.values():
    print("chain", ch.name, ch.family, ch.chain_id, ch.usdt)
for s in c.stores.values():
    print("store", s.name, s.account)
    print("  usdt to", s.destinations)
    print("  native to", s.native_destinations)'
stat -c '%a %U:%G %n' /root/acctpool /root/acctpool/pools.toml
```

The output shows only the chains of this phase (`chain polygon evm 137 0xc2132D05D31c914a87C6611C10748AEb04B58e8F`), and your two payout addresses. A last line `ConfigError: ...` means that the file is refused; the text names the key. With a placeholder left in the file the text is `stores.examplestore.destinations.polygon: must be an EIP-55 checksummed address`. The signer checks one more rule at its start: a destination must not be its own fee wallet.

### 2.4 Master key, token files and the high-water file

`set -C` makes the command fail if the file exists. Do not replace a master key that a keystore uses.

```
( umask 077; set -C
  head -c 32 /dev/urandom > /var/lib/acctpool-key/master.key )
chown "$SIGNER_UID:$SIGNER_GID" /var/lib/acctpool-key/master.key
chmod 600 /var/lib/acctpool-key/master.key

( umask 077; set -C
  head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' \
    > /root/acctpool/signer.token )
( umask 077; set -C
  cat /root/acctpool/signer.token > /root/acctpool/worker.token )
chown "$SIGNER_UID:$SIGNER_GID" /root/acctpool/signer.token
chown "$WORKER_UID:$WORKER_UID" /root/acctpool/worker.token
chmod 600 /root/acctpool/signer.token /root/acctpool/worker.token
```

There are two token files with the same content because the signer and the worker can have different user ids. If you change the token, change the two files and restart the signer and the worker.

Check (the commands print sizes and owners, not the content):

```
stat -c '%s %a %u:%g %n' /var/lib/acctpool-key/master.key \
  /root/acctpool/signer.token /root/acctpool/worker.token
cmp /root/acctpool/signer.token /root/acctpool/worker.token && echo same
stat -c '%a %U %n' /var/lib/acctpool-key
```

Expected: `32 600 <SIGNER_UID>:<SIGNER_GID>` for the key, `64 600` with the correct owner for each token, `same`, and `700 root` for the folder. Make sure that `/var/lib/acctpool-key` is not in a backup (step 5).

The high-water file (SPEC 7.11, signer README "High-water file"). Write it before the first start of the signer: for a bind mount whose file is missing, docker makes a FOLDER, and the signer refuses a folder. `0` and 64 zeros is the value before the first audit line; the probe writes the real values later. `set -C` keeps a file that exists: never replace a mark that the probe wrote.

```
( umask 022; set -C
  printf '0 %064d\n' 0 > /root/acctpool/audit.highwater )
chown root:root /root/acctpool/audit.highwater
chmod 644 /root/acctpool/audit.highwater
```

Check:

```
stat -c '%s %a %U %F %n' /root/acctpool/audit.highwater
cut -c1-12 /root/acctpool/audit.highwater
```

Expected: `67 644 root regular file /root/acctpool/audit.highwater` and `0 0000000000`. The file has no secret: mode 644, so that the signer user can read it through the read-only mount.

### 2.5 `second.toml`

The operator who has the provider key writes `/root/acctpool/second.toml`, one table per chain (see `second.toml.example`). Do not paste the URL into a chat, a ticket or a shell history (use an editor). The URL must not be the provider of the stock daemon: the check is worth something only when it asks another company.

```
[polygon]
url = "https://..."
```

```
chown "$WORKER_UID:$WORKER_UID" /root/acctpool/second.toml
chmod 600 /root/acctpool/second.toml
```

Check (prints the chain id that the URL answers, never the URL):

```
IMG=$(docker inspect -f '{{.Config.Image}}' compose-worker-1)
docker run --rm -u 0 --entrypoint python -v /root/acctpool/second.toml:/s.toml:ro "$IMG" -c '
import json, tomllib, urllib.request
for chain, row in tomllib.load(open("/s.toml", "rb")).items():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
    req = urllib.request.Request(row["url"], body, {"Content-Type": "application/json"})
    print(chain, int(json.load(urllib.request.urlopen(req, timeout=10))["result"], 16))'
```

Expected: `polygon 137`. Without an entry for a chain, payouts on that chain wait and the event `second_opinion_down` is written (the check is never skipped).

### 2.6 Dry run of the generator on a copy

This step does not change the installation. Make the script as a file: a command given with `bash -c` puts its own text into the environment that the generator gets.

```
rm -rf /root/acctpool-dry; mkdir -m 700 /root/acctpool-dry
cp -a /root/bitcart-docker/compose /root/acctpool-dry/compose
cp -a /root/acctpool-stage/acctpool \
  /root/acctpool-dry/compose/plugins/docker/acctpool
cat > /root/acctpool-dry/run.sh <<'EOF'
#!/bin/bash
cd /root/bitcart-docker || exit 1
. helpers.sh
load_env
docker run --rm -v /root/acctpool-dry/compose:/app/compose \
  --env-file <(env | grep '^BITCART_') \
  --env-file <(env | grep '^REVERSEPROXY_') \
  --env NAME="$NAME" \
  "${BITCARTGEN_DOCKER_IMAGE:-bitcart/docker-compose-generator}"
docker compose -f /root/acctpool-dry/compose/generated.yml config -q \
  && echo compose-file-valid
EOF
bash /root/acctpool-dry/run.sh
diff -u /root/bitcart-docker/compose/generated.yml \
  /root/acctpool-dry/compose/generated.yml
```

Check: `compose-file-valid` shows. The diff is the same as `deploy/generated-diff.txt`: the service `acctpool-signer` (no `build` key, `pull_policy: never`, the memory and log limits), the network `acctpool_internal`, the volume `acctpool_signer_data`, and on `backend` and `worker` the line `./plugins/docker:/plugins/docker` with `:ro` added and the new value of `BITCART_VOLUMES` (`/datadir /backups /plugins/backend /plugins/admin /plugins/store`), two new mounts on `backend`, three new mounts, one environment line and the `networks` list on `worker`. All other lines are equal. If the diff shows a different line, stop. If the diff has no `:ro` line, no `BITCART_VOLUMES` line and no new mount, the rule did not find the stock mount or the stock `BITCART_VOLUMES` value: stop, the plugin does not fit this bitcart-docker version.

Then remove the copy (it has a copy of your plugin tree): `rm -rf /root/acctpool-dry`.

### 2.7 Put the plugin folder in its place

Do this immediately before step 3. The folder name must be `acctpool`.

```
cd /root/bitcart-docker
P=compose/plugins/docker
find $P -type f -path '*/rules/*' ! -path '*/__pycache__/*' | sort
find $P -type l
cp -a /root/acctpool-stage/acctpool $P/acctpool
( cd $P/acctpool \
  && sha256sum -c --quiet --strict /root/acctpool-stage/acctpool.sha256 \
  && echo checksums-ok )
ls -l /root/acctpool/pools.toml /root/acctpool/second.toml \
  /root/acctpool/signer.token /root/acctpool/worker.token \
  /root/acctpool/audit.highwater /var/lib/acctpool-key/master.key
```

Check: the rule list shows only rule files that you know (read each file that you do not know before you continue). The second `find` shows no symbolic link. `checksums-ok` shows. The six files are files (the line starts with `-`), not folders.

Then set the owner and the modes, remove the bytecode of the time when the containers could write to the tree, make the checksum list of the full tree, and install the check script. Make the list again after each planned change of the tree, and only after you examined the change.

```
find $P -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R root:root $P
chmod -R go-w,a+rX $P
( cd $P && find . -type f -print0 | sort -z | xargs -0 sha256sum ) \
  > /root/acctpool/plugins-docker.sha256
chmod 600 /root/acctpool/plugins-docker.sha256
install -m 700 -o root -g root /root/acctpool-stage/probe-checks.sh \
  /usr/local/sbin/acctpool-probe-checks
/usr/local/sbin/acctpool-probe-checks files
```

Check: the last command prints `OK acctpool-plugin-files`.

## Step 3: start

This is the step that recreates containers. Expect 1 to 2 minutes without the payment API.

### The start sequence (use it for EACH start, restart and update)

```
cd /root/bitcart-docker
/usr/local/sbin/acctpool-probe-checks files \
  && docker image inspect -f '{{.Id}}' forked/acctpool-signer:0.1.0 \
  && ./start.sh
```

The first command examines the plugins tree: each file is the file of the checksum list, no other file and no symbolic link is there, and no user but root can write. It prints `OK acctpool-plugin-files`, or a `FAIL` line, and then the start does not run. The second command prints the id of the signer image (the id of step 2.2), or an error if the image is missing.

For an update, use `./update.sh` in the place of `./start.sh`.

### Checks after the start

Check 1, stock services:

```
docker ps --format '{{.Names}}' | sort | diff "$B/containers.txt" -
docker ps -a --format '{{.Names}} {{.Status}}' | grep -v ' Up '
docker inspect -f '{{.Config.Hostname}}' compose-worker-1 \
  | diff "$B/worker-hostname.txt" -
curl -s "https://$HOST/api/cryptos" | diff "$B/cryptos.json" -
ls /var/lib/docker/volumes/compose_bitcart_datadir/_data/.plugins-failed
cmp compose/generated.yml "$B/generated.yml" || echo changed-as-planned
```

Expected: the first diff shows one new line, `compose-acctpool-signer-1`. The second command shows nothing. The hostname diff and the coin diff show nothing. `ls` says that the file does not exist.

Check 2, the worker has its network and its files:

```
docker exec -u electrum compose-worker-1 python -c '
import os, socket
for f in ("second.toml", "signer.token"):
    p = "/run/acctpool/" + f
    print(f, os.path.isfile(p), os.access(p, os.R_OK))
socket.create_connection(("example.com", 443), 5)
print("outbound ok")'
docker exec -u electrum compose-backend-1 python -c '
import os
print(os.path.exists("/run/acctpool/second.toml"),
      os.path.exists("/run/acctpool/signer.token"))'
```

Expected: `True True` two times and `outbound ok` for the worker. `False False` for the backend (only the worker asks for signatures).

Check 2b, backend and worker cannot write to the plugins tree:

```
for c in compose-backend-1 compose-worker-1; do
  docker inspect $c -f \
    '{{range .Mounts}}{{.Destination}} rw={{.RW}}{{"\n"}}{{end}}' \
    | grep '^/plugins/docker '
  docker exec -u electrum $c touch /plugins/docker/write-test
done
ls compose/plugins/docker/write-test
```

Expected: `/plugins/docker rw=false` two times, `Read-only file system` two times, and `ls` says that the file does not exist.

Check 2c, the plugins tree after the start:

```
/usr/local/sbin/acctpool-probe-checks files
docker exec compose-backend-1 printenv BITCART_VOLUMES
```

Expected: `OK acctpool-plugin-files` and `/datadir /backups /plugins/backend /plugins/admin /plugins/store`. If the first line is a `FAIL` for the write test: an old container started again between step 2.7 and this step, and its entrypoint gave the tree to the user 1000. Do `chown -R root:root` and `chmod -R go-w,a+rX` on `compose/plugins/docker` again, then do the check again. The new containers cannot change the owner.

Check 3, the signer is isolated and hardened:

```
S=compose-acctpool-signer-1
docker inspect $S \
  -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker network inspect compose_acctpool_internal \
  -f '{{.Internal}} {{range .Containers}}{{.Name}} {{end}}'
docker port $S
docker inspect $S \
  -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}'
docker inspect $S \
  -f '{{.HostConfig.SecurityOpt}} {{.HostConfig.Memory}} {{.Config.User}}'
docker inspect $S -f '{{.HostConfig.MemorySwap}} {{.HostConfig.Ulimits}}'
docker inspect $S \
  -f '{{.HostConfig.LogConfig.Type}} {{.HostConfig.LogConfig.Config}}'
docker inspect $S -f \
  '{{range .Mounts}}{{.Destination}} {{.Source}} rw={{.RW}}{{"\n"}}{{end}}' \
  | grep '^/run/acctpool/audit.highwater '
docker inspect $S -f 'init={{.HostConfig.Init}}'
docker exec $S python -I -c \
  'print(open("/proc/1/cmdline").read().replace("\0", " ").strip())'
docker exec $S python -I -c '
import socket
try:
    socket.create_connection(("example.com", 443), 5)
    print("PROBLEM: the signer has internet access")
except OSError:
    print("no route, correct")'
```

Expected: only `compose_acctpool_internal`. Then `true` with exactly the signer and the worker. `docker port` prints nothing. Then `true [ALL]`. Then `[no-new-privileges:true] 314572800 10001:10001` (300 MB). Then `314572800` (no swap) and a `memlock` limit of `-1`. Then `json-file map[max-file:5 max-size:10m]`. Then `/run/acctpool/audit.highwater /root/acctpool/audit.highwater rw=false`. Then `init=<nil>` or `init=false`. Then `python -I -u -m acctpool_signer serve`: process 1 of the container is the signer, not an init program (a different line: stop, `init` would refuse to run). Then `no route, correct`.

Check 4, the plugin and the signer answer:

```
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://$HOST/api/plugins/acctpool/status"
docker logs compose-backend-1 2>&1 | grep -c 'Failed to load plugin'
/usr/local/sbin/acctpool-probe-checks
```

Expected: `401` (the route exists and wants a token; `404` = the plugin is not loaded). `0` failed plugins. The probe prints 15 lines. At this time one line is expected to fail: `FAIL acctpool-signer-status keystore is not loaded` (no seed yet); `acctpool-alert-events` can show `signer_down` for the same reason. `OK acctpool-signer-container`, `OK acctpool-plugin-loaded`, `OK acctpool-audit-chain`, `OK acctpool-audit-highwater` and `OK acctpool-plugin-files` must show. After this run `cut -d' ' -f1 /root/acctpool/audit.highwater` prints a number above 0: the probe wrote its first mark. This list comes from the probe test with the real signer image, not from a test with the real worker engine: write down every other `FAIL` line and examine it before step 4.

If a check fails: see "Rollback of step 3".

## Step 4: seed, written copy, restore test

No money goes to a fee wallet or a deposit address before 4.6 is complete.

### 4.1 The terminal

The words are shown on the screen one time. Nothing may record that screen or the keys that you type.

- Use a plain SSH session from your own computer to the host. Do NOT use a web console that records the terminal (for example ttyd with `script`), `script`, a tmux or screen session with a log, a session recorder (for example asciinema), a screen share, or an AI agent session.
- Only the person who keeps the seed looks at the screen.
- sudo can record a session (`log_output`, `log_input`), and `pam_tty_audit` can record typed keys. Examine the host before you start:

```
echo "tmux: ${TMUX:-none}  screen: ${STY:-none}"
grep -rsn '^[^#]*\(log_output\|log_input\)' /etc/sudoers /etc/sudoers.d
grep -rsn '^[^#]*pam_tty_audit' /etc/pam.d
```

Expected: `tmux: none  screen: none`, and the two `grep` commands print nothing (they skip comment lines). If one prints a line, stop and use a session that is not recorded.

### 4.2 Log check before the seed (mandatory)

This check proves that docker does not put the output of `docker exec -it` into a log. The seed step (4.3) is permitted only when the marker word is in NONE of the places below. The command lines do not contain the marker word itself (the text is joined inside the container, and the search pattern has `[M]`), so the audit log of the command lines cannot give a false result.

```
S=compose-acctpool-signer-1
T0=$(date '+%Y-%m-%d %H:%M:%S')
docker exec -it $S python -I -c 'print("ACCTPOOL" + "-EXEC-" + "MARKER")'
sleep 3
docker logs $S 2>&1 | grep -c 'ACCTPOOL-EXEC-[M]ARKER'
grep -c 'ACCTPOOL-EXEC-[M]ARKER' "$(docker inspect -f '{{.LogPath}}' $S)"
journalctl --since "$T0" --no-pager | grep -c 'ACCTPOOL-EXEC-[M]ARKER'
grep -c 'ACCTPOOL-EXEC-[M]ARKER' /var/log/messages
```

Expected: the `exec` command shows `ACCTPOOL-EXEC-MARKER` on the terminal. Each of the four counts is `0` (the last command can also say that `/var/log/messages` does not exist). If a count is not `0`, stop: do not make the seed. Find the cause first (daemon log settings in `/etc/docker/daemon.json`, a log shipper, a terminal recorder).

Control, to prove that the search finds the word when it is in a log (the output of a main process always goes to the log):

```
docker run --name acctpool-marker-control --network none \
  --log-driver json-file --entrypoint python forked/acctpool-signer:0.1.0 -I \
  -c 'print("ACCTPOOL" + "-EXEC-" + "MARKER")'
docker logs acctpool-marker-control 2>&1 | grep -c 'ACCTPOOL-EXEC-[M]ARKER'
docker rm acctpool-marker-control
```

Expected: `1`. If it is `0`, the search does not work: stop.

On the test host (rootless podman) the same method gave: `exec -it` output 0 times in `podman logs`, and the output of a main process 1 time. It was not tested with docker.

### 4.3 Make the seed

Only after 4.1 and 4.2. `init` runs in the signer container that runs now (it serves `/v1/status` only while it has no keystore). Never use `docker run` for `init`: the signer refuses it as the main process of a container.

```
docker exec -it compose-acctpool-signer-1 python -I -m acctpool_signer init
```

`init` shows the 24 words, then asks for 3 of them (typed, no echo). Only then it writes the keystore. Write the 24 words on paper and into the password manager. Do not put them in a file, a photo or a chat. `init` clears the screen and its scroll buffer at the end; close the terminal window after this step too. Then restart the signer, because it reads the keystore only at the start:

```
docker restart compose-acctpool-signer-1
docker exec compose-acctpool-signer-1 python -I -m acctpool_signer verify
/usr/local/sbin/acctpool-probe-checks | grep signer
```

Check: `verify` prints the seed id and the fee wallet address (not secret). Write them down. The probe shows `OK acctpool-signer-status`.

### 4.4 A burned seed

A seed is burned when its words were in a recorded terminal or a screen share, in a log (a check of 4.2 that was not done or not clean), in a file, a photo or a chat, or on a screen that another person saw. A burned seed is never used, also when nothing seems lost.

Before any money arrived (no native coin sent to the fee wallet, no pool enabled): make a new seed. The volume has nothing of value at this time.

```
cd /root/bitcart-docker
docker stop compose-acctpool-signer-1
docker rm compose-acctpool-signer-1
docker volume rm compose_acctpool_signer_data
printf '0 %064d\n' 0 > /root/acctpool/audit.highwater
```

The last command sets the high-water file to 0 again: the mark belongs to the removed volume, and the signer refuses to start a new volume below it. This is the one case where the mark is set back by hand, and only before money arrived.

Then use the start sequence of step 3 (it makes a new signer container and a new empty volume; the other containers can be recreated too). Then do 4.1, 4.2 and 4.3 again. Destroy the paper and the password manager entry of the burned seed, and write down its seed id as burned. When the worker sees the new seed id, it retires the ready addresses of the burned seed (event `ready_mismatch`, reason `seed_changed`); they are never given to an invoice.

After money arrived at an address of that seed: this is an incident, and this version has no seed rotation. Set pool mode off for each wallet, do a batch withdraw of all balances at once, and stop. Ask the developer for a plan before the next step.

### 4.5 Addresses of the live signer

Write this helper file. It asks a signer for the first 5 addresses of a store and prints them. It reads the token from the mounted file and does not print it.

```
cat > /root/acctpool/derive-check.py <<'EOF'
import json, sys, urllib.request
url, store = sys.argv[1], sys.argv[2]
token = open("/run/acctpool/signer.token").read().strip()
body = json.dumps({"store": store, "family": "evm",
                   "first_index": 0, "count": 5}).encode()
head = {"Authorization": "Bearer " + token,
        "Content-Type": "application/json"}
request = urllib.request.Request(url + "/v1/derive", body, head)
answer = json.load(urllib.request.urlopen(request, timeout=10))
print("seed", answer["seed_id"])
for item in answer["addresses"]:
    print(item["index"], item["address"])
EOF
docker exec -i -u electrum compose-worker-1 python - \
  http://acctpool-signer:7070 "$STORE" \
  < /root/acctpool/derive-check.py | tee /root/acctpool/live-addresses.txt
```

Check: 1 seed line and 5 address lines.

### 4.6 Restore test (mandatory)

The test makes a second signer from the WRITTEN words: a second signer container, with a different master key, a test volume and an internal network with no route out. `restore` runs through `exec -it` in that running container, the same path as `init`. The test must give the same seed id, the same fee wallet and the same addresses.

```
R=acctpool-restore-test
( umask 077; set -C
  head -c 32 /dev/urandom > /var/lib/acctpool-key/restore-test.key )
chown "$SIGNER_UID:$SIGNER_GID" /var/lib/acctpool-key/restore-test.key
chmod 600 /var/lib/acctpool-key/restore-test.key
docker volume create acctpool_restore_test
docker network create --internal acctpool_restore_net
docker run -d --name $R --network acctpool_restore_net \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp --ulimit memlock=-1:-1 \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=5 \
  -v acctpool_restore_test:/data \
  -v /root/acctpool/pools.toml:/etc/acctpool/pools.toml:ro \
  -v /var/lib/acctpool-key/restore-test.key:/run/acctpool/master.key:ro \
  -v /root/acctpool/signer.token:/run/acctpool/signer.token:ro \
  forked/acctpool-signer:0.1.0
docker exec -it $R python -I -m acctpool_signer restore
```

Type the words from the PAPER copy at the `restore` prompt, not from the password manager (no echo). Do the test a second time with the password manager copy if you want to prove the two copies. Then:

```
docker restart $R
docker exec $R python -I -m acctpool_signer verify
docker run --rm -i --network acctpool_restore_net \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  -v /root/acctpool/signer.token:/run/acctpool/signer.token:ro \
  --entrypoint python forked/acctpool-signer:0.1.0 - \
  http://$R:7070 "$STORE" < /root/acctpool/derive-check.py \
  | diff /root/acctpool/live-addresses.txt - && echo restore-test-pass
```

Check: `verify` prints the same seed id and the same fee wallet address as in 4.3. `restore-test-pass` shows. If one value is different: stop, do not fund, do not enable a pool. The written copy is wrong or the code is wrong.

Remove the test parts:

```
docker rm -f acctpool-restore-test
docker network rm acctpool_restore_net
docker volume rm acctpool_restore_test
shred -u /var/lib/acctpool-key/restore-test.key
rm -f /root/acctpool/live-addresses.txt
docker ps -a --format '{{.Names}}' | grep -c 'restore\|marker'
```

Check: the last command prints `0`.

Only now: send the native coin for the float to the fee wallet address from 4.3.

## Step 5: backup rules

In your backup tool:

- Add an exclude for `/var/lib/acctpool-key/`.
- Include the volume `compose_acctpool_signer_data` (encrypted keystore, journal, audit log). The stock `backup.sh` of bitcart-docker does not include it. It is the only copy of the audit lines apart from the container log files, which are limited to 50 MB and are not sent to syslog. Use the method of `signer/README.md` ("Backup") for a journal that is in use.
- Know that `/root/acctpool/` has the token and the provider keys. Include it only if the backup is encrypted or the store is trusted.

- After each copy of the volume, the high-water file must move to the newest audit line: run `/usr/local/sbin/acctpool-probe-checks highwater` as root on the host. It runs `audit-verify` and writes the mark (the same step as in each probe run), prints two lines (`acctpool-audit-chain`, `acctpool-audit-highwater`) and exits with 1 when one is `FAIL`. When the backup is a script on the host, make it the last command of that script and send its output to the backup report. When the backup tool pulls from another host, add a root cron entry on the payment host a few minutes after the pull, for example `25 3 * * * root /usr/local/sbin/acctpool-probe-checks highwater >> /var/log/acctpool-highwater.log 2>&1`, and check that log in the backup review. The probe run every 10 minutes also writes the mark; this call closes the gap right after the backup.

Check: make a dry run (a file list) of the backup. `master.key` is not in the list. `keystore.json` is in the list. After the next real backup, search the backup store for `master.key`: no result. Then run `/usr/local/sbin/acctpool-probe-checks highwater; echo "exit $?"`: two `OK` lines and `exit 0`.

## Restore of the signer volume

Follow `docs/VOLUME-RESTORE.md`. Never put back the journal or `audit.log` alone: the journal is the memory of the signer rules (nonces, per-address totals, fee budgets, daily spend, highest index), and they are restored together from ONE backup. Only the keystore can be put back alone (case 1 of that file). The v4 signer has no journal rebuild: a signer that starts on a volume older than the high-water file is refused with a message that names the high-water file (case 2). Do not delete the high-water file and do not write a lower value into it to get past the refusal: that is a decision for the operator (the section "When lines K+1 to M are lost" of that file).

## Step 6: pool mode for ONE wallet, probe checks

### 6.1 Probe

The script is installed (step 2.7). Optional settings go into `/root/acctpool/probe.conf` (owner root, mode 600); the defaults are at the top of the script. The probe needs no API token: it reads the plugin tables with `psql` in the database container, `audit.log` in the host folder of the signer volume, the container log of the signer (a call with a wrong token gets 401 and a request line there, but no audit line), and it runs `audit-verify` inside the signer container. Add one line to the health probe that runs the script and appends its output to the status file (the script header shows the line). The fee wallet balance and the money on deposit addresses are watched by the worker (it has the daemon): it writes the events `fee_wallet_low` (less than about 10 USDT payouts of gas) and `unswept_high`, and the probe line `acctpool-alert-events` fails on them.

Check:

```
/usr/local/sbin/acctpool-probe-checks
ACCTPOOL_READY_MIN=1000000 /usr/local/sbin/acctpool-probe-checks \
  | grep ready
```

Expected: 15 lines, all `OK`. The second command shows a `FAIL` line after the first pool is enabled: this proves that the alert path sees a failure.

What the lines mean, in short: the signer container runs and its keystore is loaded; the plugin route exists; a worker leads the plugin loops (last minutes); no pool invoice is open while the plugin does not run (`acctpool-open-invoices`, see "Rollback"); enough ready addresses for each signer store; no payout waits more than 24 hours; no alert event in the last hour (fee wallet low, payout failed or waiting, second opinion mismatch or down, signer down, ready addresses low, late payment, stock fallback, unswept high, address mismatch); the audit chain of the signer is correct (`audit-verify` in each run); the high-water file moved to the newest audit line of the volume; the signature rate and the refused calls (401, 429, 5xx) at the signer; the signer volume continues the audit chain of the high-water file (else it was replaced or put back; the mark does not move, so the alarm stays until a person acts); the size of the signer volume; the files of the plugins tree.

The line `acctpool-plugin-files` fails when a file or folder in `compose/plugins/docker` can be written by a user who is not root, when a file is not the file of the checksum list, and when a file or a symbolic link is there that is not in the list. Find the cause before the next start: the generator runs the rules of that tree as root.

### 6.2 One wallet

Open the admin page of the plugin (path `/api/plugins/acctpool/ui` on the API host). Make a pool for ONE wallet of the test store: chain, signer store key, min. withdraw. The invoice limit (default 500 USD), the ready target and the gas price factor are the settings of the page. Enable the pool.

Check:

```
docker exec compose-database-1 psql -U postgres -d bitcart -At -c \
  "select wallet_id, chain, asset, store, enabled from plugin_acctpool_pools"
docker exec compose-database-1 psql -U postgres -d bitcart -At -c \
  "select status, count(*) from plugin_acctpool_addresses group by 1"
/usr/local/sbin/acctpool-probe-checks
```

Expected: one row with `t`. The ready count goes to the ready target in some minutes. All probe lines are `OK`. Then make one test invoice for that wallet: it shows a deposit address from the table and no sender-address prompt. An invoice of a different wallet is the same as before.

## Checkout front ends

A checkout of your own (not the stock Bitcart checkout) must not ask for a sending address on a pool invoice: that step does not work for a pool invoice. A pool payment method has `metadata.acctpool`, and its `user_address` equals its `payment_address`; skip the sender step for such a method and show the deposit address with a line such as "Send only USDT on the <network> network. Other coins or networks are not credited." That change goes live BEFORE pool mode is enabled for a wallet of that store. With the plugin off, it does nothing. For the rollback the order is the opposite: pool mode off first, then the checkout change can be reverted.

## Later chains

Each chain is its own phase with its own approval. Polygon is the first.

- Ethereum: in `pools.toml` remove the comment marks of all `ethereum` blocks (chain, destinations, native destinations, recovery tokens); add an `[ethereum]` table to `second.toml`. Put measured cap values in the place of the example values. Do the checks of 2.3 and 2.5. Restart the signer, the backend and the worker (`docker restart compose-acctpool-signer-1 compose-backend-1 compose-worker-1`; no new compose file, the mounts are the same). The EVM fee wallet has the same address on all EVM chains; send it native coin of the new chain. On Ethereum the fee rule waits while the fee is more than 5% of the amount (and more than $3 for $50 or more).
- BNB Smart Chain: the same steps. First, production needs the Bitcart BNB daemon (the `bnb` component of bitcart-docker, `bnb` in `BITCART_CRYPTOS`). It is NOT enabled on the payment host today. Without it there is no Bitcart wallet of the currency `bnb`, and no pool can use the chain. Enabling it is a change of the stock installation (a new container, `./start.sh` recreates containers) with its own approval and its own backup (step 1). USDT on BNB Smart Chain has 18 decimals, not 6.
- A pool for the native coin (a Bitcart wallet without a contract) needs a `native_destinations` entry for its chain. Without it, the signer refuses every native payout of that store on that chain; this includes the native coin that a USDT payout did not use.

## Rollback

### Rollback of step 3 (the start went wrong)

At this time no pool is enabled, so no pool invoice is open. If a pool was enabled, use "Rollback of the system".

```
cd /root/bitcart-docker
mv compose/plugins/docker/acctpool /root/acctpool-stage/acctpool.removed
./start.sh
diff "$B/generated.yml" compose/generated.yml && echo same-as-before
docker ps --format '{{.Names}}' | sort | diff "$B/containers.txt" -
curl -s "https://$HOST/api/cryptos" | diff "$B/cryptos.json" -
```

Check: `same-as-before` shows and the two diffs show nothing. `./start.sh` recreates the backend and the worker again (1 to 2 minutes). Without the plugin folder the installation is the stock installation again: the plugins tree is mounted read-write, the entrypoint gives its files to the user 1000, and the admin page can install docker-type plugins. If docker made a folder in the place of a missing file, remove the empty folder with `rmdir` before the next try. The files in `/root/acctpool`, the key and the volume stay.

### Rollback of the system, in this order

Known limit: when the plugin is not loaded while pool invoices are open, the stock task that examines pending invoices asks the stock daemon about them, gets no answer, and sets them to invalid. A customer who pays such an invoice is then not credited automatically. Thus the plugin is removed only when no pool invoice is open. The probe line `acctpool-open-invoices` fails when pool invoices are open and the plugin does not run in the backend or in the worker.

1. Set pool mode off for each wallet (admin page, or `PATCH /api/plugins/acctpool/pools/<id>` with `enabled` false). New invoices use the sender-address flow immediately. Open pool invoices complete normally. Check: `select wallet_id, enabled from plugin_acctpool_pools` shows `f` for each row, and a new test invoice shows the sender-address prompt.
2. Wait until no pool invoice is open. This takes the invoice expiry time of the stores at most (paid, expired or invalid invoices are not open). Check, repeat until it prints `0`:

```
docker exec compose-database-1 psql -U postgres -d bitcart -At -c \
  "select count(*) from plugin_acctpool_addresses where status = 'in_invoice'"
```

3. Do a batch withdraw of all balances (admin page). Check: `select count(*) from plugin_acctpool_addresses where status in ('pending_payout', 'in_payout')` gives `0`, and `select count(*) from plugin_acctpool_payouts where state not in ('confirmed', 'failed')` gives `0`.
4. Let the signer and the check of retired addresses run while late payments are possible. Check: the probe lines stay `OK`. To stop them for all time, keep the written seed: a standard wallet can import it with the paths `m/44'/60'/<account>'/0'/<index>'` (deposit addresses) and `m/44'/60'/9000'/0'/0'` (fee wallet).
5. Only then: do the check of step 2 again (it must print `0`), remove the folder `compose/plugins/docker/acctpool` and run `./start.sh` (containers are recreated again, and the plugins tree is read-write again). Remove the probe line. The image `forked/acctpool-signer` and the folder `/root/acctpool/signer-src` can stay. Check: the commands of "Rollback of step 3". The plugin tables stay in the database. Do not delete the volume `compose_acctpool_signer_data`, the master key or the written seed while an address that was given to a customer can get money.
