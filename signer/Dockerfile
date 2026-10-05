# acctpool signer. The base image has the runtime dependencies (eth-account, aiohttp, pycryptodome).
# One package is added: coincurve, so that eth_keys signs with libsecp256k1 (constant time) and not with
# its pure-Python backend. The build has NO network (podman build --network none): the wheel file is in
# wheels/, put there by fetch-wheels.sh. Version and SHA-256 are in requirements.txt.
# The tag and the digest: the image of this tag that the tests used. Another image under the same tag is refused.
FROM docker.io/bitcart/bitcart-eth:0.10.3.0@sha256:0c039c8edd8d1b0ef14c166d8806ec1b8a0bf400d890521e82ff514d1f0f4f47

USER root

# "wheels*" and not "wheels/": a folder that is not there must give the message below, not a copy error.
COPY requirements.txt wheels* /tmp/build/

# Before the tools are removed: uv installs the wheel, from the folder only (--offline --no-index).
# --no-config: the uv settings of the Bitcart source tree are not for this package.
# The last line stops the build when eth_keys does not select the new backend.
RUN cd / \
    && if ! ls /tmp/build/*.whl >/dev/null 2>&1; then \
        echo "ERROR: no wheel file in signer/wheels/. Run signer/fetch-wheels.sh before the image build." >&2; exit 1; fi \
    && uv pip install --no-config --offline --no-index --find-links /tmp/build \
        --python /home/electrum/.venv/bin/python \
        --require-hashes --no-deps --only-binary :all: -r /tmp/build/requirements.txt \
    && rm -rf /tmp/build \
    && python -c "from eth_keys import keys; assert type(keys.backend).__name__ == 'CoinCurveECCBackend'"

# Fixed uid/gid: the host files that are mounted read-only (master key, token) must be readable by this uid.
RUN addgroup -S -g 10001 acctpool \
    && adduser -S -D -H -u 10001 -G acctpool -h /nonexistent -s /sbin/nologin acctpool \
    && mkdir -p /opt/acctpool /data \
    && chown 10001:10001 /data \
    && chmod 700 /data

# The package goes into the site-packages of the interpreter, so that Python can run in isolated mode (-I):
# no PYTHONPATH, no current folder and no user folder in the module path.
COPY acctpool_signer /home/electrum/.venv/lib/python3.12/site-packages/acctpool_signer

# Compile now: the root filesystem is read-only at run time.
# Then remove what the signer does not use: git, apk, uv, pip, just, ssl_client, the Bitcart source, the
# packages web3, requests, websockets, grpc and opentelemetry, the Python modules ensurepip and venv,
# and at the end busybox with all its links (the shell too). Python does the last step, because the
# shell that runs this line is one of the files that go away.
RUN python -m compileall -q /home/electrum/.venv/lib/python3.12/site-packages/acctpool_signer \
    && chmod -R a+rX,go-w /home/electrum/.venv/lib/python3.12/site-packages/acctpool_signer \
    && cd /home/electrum/.venv/lib/python3.12/site-packages \
    && rm -rf web3 web3-* ens requests requests-* websockets websockets-* grpc grpcio-* opentelemetry opentelemetry_* \
    && rm -rf /usr/local/lib/python3.12/ensurepip /usr/local/lib/python3.12/venv \
    && rm -rf /sbin/apk /usr/bin/git /usr/bin/git-* /usr/libexec/git-core /usr/bin/ssl_client /bin/uv /bin/uvx \
        /usr/local/bin/pip* /home/electrum/.venv/bin/just /home/electrum/.venv/bin/opentelemetry-* \
        /home/electrum/.venv/bin/websockets /home/electrum/site \
    && python -c "import os; dirs = ('/bin', '/sbin', '/usr/bin', '/usr/sbin'); links = [os.path.join(d, n) for d in dirs for n in os.listdir(d)]; [os.unlink(p) for p in links if os.path.islink(p) and os.readlink(p).endswith('busybox')]; os.unlink('/bin/busybox')"

# LD_PRELOAD: the base image loads jemalloc into each process; the signer uses the allocator of the C library.
ENV LD_PRELOAD= \
    ACCTPOOL_SIGNING_BACKEND=coincurve \
    ACCTPOOL_CONFIG=/etc/acctpool/pools.toml \
    ACCTPOOL_MASTER_KEY_FILE=/run/acctpool/master.key \
    ACCTPOOL_TOKEN_FILE=/run/acctpool/signer.token \
    ACCTPOOL_DATA=/data

USER 10001:10001
# a folder that the signer user cannot write: without -I the current folder is in the module path
WORKDIR /opt/acctpool
VOLUME /data

# -I: isolated mode. -u: stdout without a buffer (the audit lines).
CMD ["python", "-I", "-u", "-m", "acctpool_signer", "serve"]
