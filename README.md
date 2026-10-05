# acctpool: one deposit address per invoice for Bitcart (USDT and native coins on EVM chains)

A [Bitcart](https://github.com/bitcart/bitcart) **docker plugin**. Each pool invoice gets its own deposit address, so the customer never enters a sending address and payments from exchanges and custodial wallets are credited. The plugin funds gas and sweeps every paid address to a pinned merchant destination. A separate signer container holds the seed.

Built and tested against **Bitcart 0.10.3.0** (docker deployment, `bitcart-docker`). It runs in production on the author's installation (Polygon and Ethereum) since October 2026.

This is an independent plugin. It is not part of the Bitcart project and the Bitcart maintainers do not support it.

## What it does

- Unique deposit address per invoice, derived from the signer's seed. No sender-address field; a payment from any wallet or exchange is credited.
- Assets: USDT and the native coin of the chain.
- Chains: Polygon and Ethereum. BNB Smart Chain is in the chain table but needs the stock Bitcart `bnb` daemon. Tron is not supported (a later phase).
- Automatic gas funding from a fee wallet, automatic sweep to the pinned destination of the store, retries and idempotency.
- Address pool lifecycle (`ready` → `in_invoice` → `retired`; an address is never given out twice).
- Admin REST API under `/api/plugins/acctpool/*` (scope `server_management`) and one admin page: addresses, deposits, payouts, manual and batch withdraw.

## How it works

- **Chain layer = the stock Bitcart daemon.** Deposit addresses are loaded into the stock `eth`/`matic` daemon as diskless watch-only wallets; its `new_transaction` events are the detection. A reconcile loop reads balances again after a daemon restart (diskless wallets are lost on restart and missed payments give no event).
- **Backend plugin** (`backend/forkedpool/acctpool`, backend and worker): Bitcart hooks (`create_payment_method`, `post_create_payment_method`, `get_request`, invoice status hooks, `check_pending`), crediting through Bitcart's `PaymentProcessor`, and worker loops with one leader (Postgres advisory lock): derive-ahead, watch-load + reconcile, payout.
- **Second opinion** (`secondcheck.py`): before every funding and sweep signature, the chain id and the balance are checked against an RPC provider of a different company than the daemon's.
- **Signer** (`signer/`, separate container on an internal network; only the worker reaches it): holds the seed and signs three EIP-1559 transaction shapes: gas funding from the fee wallet, USDT sweep, native sweep. Requests cannot name a destination, contract, chain id or data: those come only from `pools.toml`. Caps on gas, fee, funding value, per-address totals and fee-wallet daily spend. Hash-chained audit log, a journal, and a high-water file that refuses a restored, older volume. It has no network access and never broadcasts. See `signer/README.md` and `signer/THREATS.md`.

The full design is in `SPEC-v4.md` (current) and `SPEC.md` (history of the rules; code comments cite both).

## Layout

```
backend/forkedpool/acctpool/   backend plugin (mounted read-only at /app/modules/forkedpool)
components/acctpool.yml        compose component (signer service, mounts)
rules/91_acctpool.py           bitcart-docker generator rule (read-only plugin mount)
signer/                        signer service, its tests, Dockerfile, threat analysis
deploy/                        deploy guide, example config, probe checks and their tests
backend_tests/plugin/          backend plugin tests (inside the pinned Bitcart image)
integration/evm/               end-to-end test: stock Bitcart + stock eth daemon + anvil + signer
scripts/                       test runners
```

## Install

Follow `deploy/DEPLOY.md` step by step. Each numbered step is one approval: do one step, do its check, then stop. Read "Read before step 1" first: the first deploy recreates containers, and the plugin mounts `compose/plugins/docker` read-only (the admin page can then no longer install docker-type plugins).

Do a full rehearsal on a test installation before you use it on a host that takes payments.

## Tests

The test runners run on your workstation and execute the suites on a test host over ssh, with rootless podman:

```
export TEST_HOST=<ssh name of the test host>
scripts/test-signer.sh          # signer unit tests, image test, lint
scripts/test-backend.sh --lint  # backend plugin tests
scripts/test-integration.sh     # end-to-end flows on anvil
```

The signer image needs wheels that `signer/fetch-wheels.sh` downloads and checks by SHA-256; they are not in the repository.

## Known limitations

- EVM only. No Tron.
- Each invoice address is swept on its own: the gas cost is per invoice, not per amount. Set store minimums so that the fee stays a small part of a payment.
- A payment that arrives after its invoice closed is not credited: it is recorded as a `late` deposit with a `late_payment` event. A retired address stays watched for 30 days.
- Read `deploy/DEPLOY.md` "Rollback" before you enable pool mode for a wallet.

## License

MIT, see `LICENSE`. Bitcart itself is MIT-licensed by its authors.
