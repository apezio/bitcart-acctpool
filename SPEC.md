# acctpool — build specification (interfaces)

This file is the contract between the parts. It is the build history of the first engine; `SPEC-v4.md` replaces it where they differ.

Scope of this build: EVM only (Polygon and Ethereum share all code). Tron is a later phase. Do not write Tron code. Do not add abstractions for chains that are not in scope.

## 0. Rules for every builder

- Work only in the repository and in `~/acctpool-build/` on the test host. Touch only the folders that your task names.
- NEVER connect to a production host. The test host is the only remote host you may use. No `sudo` there.
- No mainnet, no testnet, no real keys, no real RPC provider. Tests use a fake RPC or a local `anvil` chain.
- No `git commit`, no `git push`. The orchestrator commits.
- Containers on the test host: rootless `podman`. Give every container a name with your task prefix and remove it at the end (`podman rm -f`). Run at most 3 containers at the same time. Images already pulled: `docker.io/bitcart/bitcart:0.10.3.0` (backend + worker, Python 3.12), `docker.io/bitcart/bitcart-eth:0.10.3.0` (has eth-account 0.13.7, web3 7.14.1, aiohttp 3.13.3, pycryptodome 3.23.0, mnemonic; does NOT have `cryptography`, `pytest`), `docker.io/library/postgres:17-alpine`, `docker.io/library/redis:alpine`, `ghcr.io/foundry-rs/foundry:stable` (anvil 1.5.1).
- Copy code to the test host with `scripts/dev-sync.sh <subfolder>` only. Never write your own rsync line.
- No new runtime dependency without a reason written in your report. Test-only dependencies (pytest, pytest-asyncio) are installed inside the test container at test time.
- Reference source, read-only: `../ref/bitcart` (Bitcart 0.10.3.0), `../ref/bitcart-docker`, `bitcart-solana-usdt` (a working docker plugin + backend module for this same installation).
- Code style: match Bitcart (ruff, line length 127, type hints). Comments say why, not what.
- Secrets: a private key, seed or master key must never reach a log line, an exception text, an API response, a test fixture file, or the database. Test seeds are generated inside the test.
- Report at the end: what you built, the exact test commands and their real output summary (pass/fail counts), what is not done, and every assumption you made. Do not report a test as passed if you did not run it.

## 1. Repository layout

```
signer/acctpool_signer/     signer service (Python package)
signer/tests/               signer unit tests
signer/Dockerfile           FROM docker.io/bitcart/bitcart-eth:0.10.3.0
backend/forkedpool/__init__.py
backend/forkedpool/acctpool/   Bitcart backend plugin (mounted at /app/modules/forkedpool)
backend/forkedpool/acctpool/chain/   pure chain library: rpc.py, evm.py, quorum.py (no Bitcart imports)
backend/forkedpool/acctpool/versions/  alembic migrations
backend_tests/              tests that run inside the pinned backend image
integration/                end-to-end tests with anvil + signer
components/acctpool.yml     docker compose component
rules/91_acctpool.py        bitcart-docker generator rule
deploy/                     example config, probe checks (files only, nothing is applied)
scripts/                    helper scripts
```

## 2. Names and identifiers

- `chain`: `polygon` (chain id 137, Bitcart currency `matic`), `ethereum` (chain id 1, Bitcart currency `eth`). Test chain: `anvil` (chain id 31337).
- `family`: `evm`.
- `store`: a short key from the signer config, for example `forkednet`. Each store has a hardened account number.
- `index`: address counter per (store, family), starts at 0.
- Derivation path, all levels hardened: `m/44'/60'/<account>'/0'/<index>'`. Fee wallet per family: `m/44'/60'/9000'/0'/0'`. One fee wallet address serves all EVM chains.
- Amounts are strings of base units in JSON (wei, or token units with 6 decimals for USDT). Never floats.
- Addresses are EIP-55 checksummed in every API and table.

## 3. Signer

### 3.1 Files and environment

- `ACCTPOOL_CONFIG` (default `/etc/acctpool/pools.toml`): root-owned config, read-only mount.
- `ACCTPOOL_MASTER_KEY_FILE` (default `/run/acctpool/master.key`): 32 raw bytes, read-only mount.
- `ACCTPOOL_TOKEN_FILE` (default `/run/acctpool/signer.token`): bearer token, at least 32 characters.
- `ACCTPOOL_DATA` (default `/data`): the signer's volume: `keystore.json`, `journal.sqlite3`, `audit.log`.
- The signer refuses to start when: config invalid, master key missing or wrong length, token missing or short, keystore authentication fails. It starts WITHOUT a keystore only to serve `/v1/status` with `keystore: false`; every other call then returns 503.

### 3.2 Config (`pools.toml`)

```toml
[signer]
listen = "0.0.0.0:7070"

[chains.polygon]
family = "evm"
chain_id = 137
usdt = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
gas_limit_cap = 150000
max_fee_per_gas_cap_wei = "2000000000000"
max_fund_value_wei = "300000000000000000"
max_fund_total_per_address_wei = "900000000000000000"
fee_wallet_daily_cap_wei = "20000000000000000000"

[stores.forkednet]
account = 1
[stores.forkednet.destinations]
polygon = "0x0000000000000000000000000000000000000001"

# optional, empty in normal operation. A token listed here may be swept with /v1/sign/sweep and "token" set.
[recovery_tokens]
polygon = []
```

Validation at start: checksummed addresses, chain ids unique, account numbers unique and not 9000, every destination chain exists in `[chains]`, destination is not the zero address and not the USDT contract.

### 3.3 Keystore

- CLI inside the container: `python -m acctpool_signer init` creates the seed (24 words, from `os.urandom`), writes `keystore.json`, prints the words ONCE to the terminal, and refuses to run when a keystore exists. `python -m acctpool_signer restore` reads 24 words from the terminal (no echo) and writes the keystore. `python -m acctpool_signer verify` prints the seed id and the fee wallet address, nothing secret.
- Format: JSON `{version, seeds: [{seed_id, created, nonce, ciphertext, tag}], active}`. AES-256-GCM (pycryptodome). The authenticated header is `version|seed_id|created`. `seed_id` = first 16 hex characters of SHA-256 of the fee wallet address of that seed (public, not secret-derived beyond that).
- The plaintext is the BIP39 entropy, not the words.
- The file is written with mode 600, atomically (write temp, fsync, rename).

### 3.4 HTTP API

All calls: `Authorization: Bearer <token>`, JSON in and out. Wrong or missing token: 401. Constant-time compare.

Errors: HTTP 4xx/5xx with `{"error": "<code>", "detail": "<text without secrets>"}`. Codes: `unauthorized`, `invalid_request`, `unknown_chain`, `unknown_store`, `cap_exceeded`, `daily_cap_exceeded`, `idempotency_conflict`, `token_not_allowed`, `keystore_missing`.

1. `GET /v1/status` → `{"keystore": true, "seed_id": "...", "config_sha256": "...", "fee_wallets": {"evm": "0x..."}, "chains": {"polygon": {"chain_id": 137, "usdt": "0x..."}}, "stores": {"forkednet": {"destinations": {"polygon": "0x..."}}}}`

2. `POST /v1/derive` `{"store": "forkednet", "family": "evm", "first_index": 0, "count": 50}` → `{"seed_id": "...", "addresses": [{"index": 0, "address": "0x..."}]}`. `count` 1 to 200. The journal stores the highest index given out per (seed_id, store, family).

3. `POST /v1/sign/fund`
```json
{"idempotency_key": "string 8..64", "chain": "polygon", "store": "forkednet", "index": 12,
 "nonce": 5, "max_fee_per_gas_wei": "60000000000", "max_priority_fee_per_gas_wei": "30000000000",
 "value_wei": "4000000000000000", "replaces": null}
```
The signer builds an EIP-1559 (type 2) transaction: from = fee wallet, to = address derived from (store, index), gas 21000, value, chain id from config, no data. Refusals: `max_fee_per_gas_wei` over the cap; `value_wei` over `max_fund_value_wei`; total funding value signed for this (chain, address) over `max_fund_total_per_address_wei`; sum of `value + 21000 × max_fee` signed in the current UTC day for this chain over `fee_wallet_daily_cap_wei`; `index` higher than the highest index given out by `/v1/derive`.
Response: `{"raw_tx": "0x...", "tx_hash": "0x...", "from": "0x...", "to": "0x...", "chain_id": 137}`.

4. `POST /v1/sign/sweep`
```json
{"idempotency_key": "...", "chain": "polygon", "store": "forkednet", "index": 12,
 "nonce": 0, "gas_limit": 70000, "max_fee_per_gas_wei": "...", "max_priority_fee_per_gas_wei": "...",
 "amount": "2000000", "token": null, "replaces": null}
```
The signer builds a type 2 transaction: from = derived deposit address, to = the chain's pinned USDT contract (or, only when `token` is set, a contract listed in `recovery_tokens` for that chain), value 0, data = `transfer(address,uint256)` with the store's pinned destination for that chain and `amount`. Refusals: gas limit or max fee over the cap; `amount` 0; `token` not in the recovery list; index not given out.

Idempotency (both sign calls): the journal stores, per key, a hash of the full request and the response. Same key + same request → same response. Same key + different request → 409 `idempotency_conflict`. `replaces` names an earlier key: the new request must have the same chain, store, index, nonce and kind, the same value/amount (sweep may change the amount), and a `max_fee_per_gas_wei` at least 10% higher; caps still apply. Without `replaces`, a second different signature for the same (chain, from address, nonce) is refused with 409.

Not in the API: anything else. Unknown paths return 404. No debug endpoints.

### 3.5 Audit log

One JSON object per line in `audit.log` and on stdout: `ts`, `call`, `result` (`ok` or error code), `chain`, `store`, `index`, `nonce`, `value_wei` or `amount`, `max_fee_per_gas_wei`, `tx_hash`, `idempotency_key`, `prev` (SHA-256 of the previous line), `seq`. `python -m acctpool_signer audit-verify` checks the chain. Failed authentication is logged too (without the token that was sent).

### 3.6 Process hardening

Core dumps off (`RLIMIT_CORE` 0), `prctl(PR_SET_DUMPABLE, 0)`. The Dockerfile sets a non-root user. The compose component (section 6) sets read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, memory limit, and an `internal: true` network.

## 4. Backend plugin

Plugin name `acctpool`, module path `modules/forkedpool/acctpool/plugin.py`, class `Plugin(BasePlugin)`.

### 4.1 Files that the plugin reads (mounted read-only into backend and worker)

- `/run/acctpool/rpc.toml` (root 600 on the host; the operator writes it):
```toml
[chains.polygon]
providers = ["https://...provider-a...", "https://...provider-b..."]
finality = "finalized"        # block tag used for payout decisions
confirmations_floor = 2
poll_seconds = 15
```
- `/run/acctpool/signer.token` and env `ACCTPOOL_SIGNER_URL` (worker only).
Provider URLs contain API keys: never log a URL. Log the provider position (`provider[0]`) and host name only.

### 4.2 Tables (alembic chain in `versions/`, all names start with `plugin_acctpool_`)

- `plugin_acctpool_pools`: `id`, `wallet_id` (unique, no FK), `chain`, `store`, `enabled` bool, `invoice_cap_usd` numeric, `min_withdraw` numeric (token units), `max_fee_percent` numeric, `max_fee_usd` numeric, `always_payout` bool, `ready_target` int, `created`, `updated`.
- `plugin_acctpool_addresses`: `id`, `store`, `family`, `seed_id`, `index`, `address`, `status` (`ready`, `in_invoice`, `retired`), `invoice_id` (no FK), `allocated_at`, `retired_at`. Unique (`store`, `family`, `seed_id`, `index`) and unique (`family`, `address`). An address leaves `ready` once and never returns.
- `plugin_acctpool_address_chains`: one row per (address, chain) that an invoice uses: `id`, `address_id`, `chain`, `pool_id`, `payment_method_id`, `status` (`in_invoice`, `pending_payout`, `in_payout`, `retired`), `credited_amount` numeric, `balance` numeric, `balance_block` bigint, `last_checked_at`, `next_check_at`, `error`, `retries`. Unique (`address_id`, `chain`).
- `plugin_acctpool_deposits`: `id`, `address_chain_id`, `invoice_id`, `chain`, `tx_hash`, `log_index`, `block_number`, `block_hash`, `contract`, `from_address`, `amount` numeric, `final` bool, `credited_at`, `kind` (`normal`, `late`, `partial`, `wrong_asset`). Unique (`chain`, `tx_hash`, `log_index`).
- `plugin_acctpool_payouts`: `id` (text, also the idempotency key prefix), `address_chain_id`, `chain`, `state` (`planned`, `funding_signed`, `funding_sent`, `funded`, `sweep_signed`, `sweep_sent`, `confirmed`, `failed`), `fee_nonce`, `fund_tx_hashes` text[], `sweep_tx_hashes` text[], `fund_value` numeric, `amount` numeric, `gas_limit`, `max_fee_per_gas`, `fee_paid` numeric, `attempts`, `error`, `requested_by` (`auto`, `admin`), `created`, `updated`. Partial unique index: one row per `address_chain_id` where state is not `confirmed` and not `failed`.
- `plugin_acctpool_events`: `id`, `ts`, `kind`, `ref`, `data` jsonb. Append only.

Numeric columns use `Numeric(78, 0)` for base units and `Numeric(36, 18)` for USD values.

### 4.3 Hooks (Bitcart 0.10.3.0; read `../ref/bitcart/api/services/crud/invoices.py` and `payment_processor.py`)

- filter `create_payment_method(method, wallet, coin, amount, invoice, product, store, lightning)`: when a pool row exists for `wallet.id`, is enabled, the invoice price in USD is at most `invoice_cap_usd`, and a `ready` address exists: allocate and return `{"payment_address", "payment_url", "lookup_field", "metadata": {"acctpool": {"address_chain_id": ..., "chain": ...}}}`. In every other case return `method` unchanged (stock flow) and write an event. Never raise.
- One invoice gets ONE address per (store, family). Bitcart creates the payment methods of one invoice concurrently: take a Postgres advisory lock on (invoice id, store, family), look for an existing allocation for that invoice, otherwise take one `ready` row with `FOR UPDATE SKIP LOCKED`. The allocation uses its own session and commits before it returns.
- `payment_url`: same format as the stock daemon produces for a token (`ethereum:<contract>@<chain_id>/transfer?address=<addr>&uint256=<amount in base units>`). Read `../ref/bitcart/daemons/eth.py` `get_payment_uri`.
- `lookup_field`: `acctpool:<address_chain_id>`.
- filter `post_create_payment_method(data, invoice, wallet, ...)`: when `data["metadata"]` (or `meta`) marks the method as acctpool, set `data["user_address"] = data["payment_address"]`.
- filter `get_request(value, coin, method)`: for acctpool methods return `{"status": 0|7|3, "tx_hashes": [...], "sent_amount": "<decimal token amount>", "confirmations": n}` from the tables. Status mapping as in `../ref/bitcart/api/invoices.py`.
- hooks `invoice_expired`, `invoice_status`, `invoice_complete`: update `address_chains.status`.
- Crediting: resolve `PaymentProcessor` from the DI container and call `process_electrum_status(invoice, method, wallet, status, tx_hashes, sent_amount, di_context=container)` inside a request scope, the same way `new_payment_handler` does.

### 4.4 Chain library (`chain/`, pure Python + aiohttp, no Bitcart imports, no web3)

- `rpc.py`: JSON-RPC client for one provider: timeout, retry with backoff, error classes, no URL in any message.
- `quorum.py`: runs one read on all providers of a chain and returns a value only when all answers are equal; else raises `Disagreement`. Every provider must first report the pinned chain id.
- `evm.py`: `balanceOf` call encoding, `transfer` gas estimate call, `Transfer` log query for one recipient and one contract over a block range (split ranges to at most 2000 blocks), receipt check, fee data (`eth_feeHistory` / `eth_maxPriorityFeePerGas`), checksum encoding (Keccak from pycryptodome is NOT in the backend image; check what the backend image has and report — `eth_hash`/`pysha3` may be missing; if no Keccak is available, vendor a small pure-Python Keccak-256 with test vectors).
- Finality: balance and logs are read at the block tag from config (`finalized`). Confirmations = `latest − block_number + 1`, from both providers, lower value wins.

### 4.5 Payout engine (worker)

State machine per `plugin_acctpool_payouts` row, as in `../PLAN.md` sections 5 and 6. Every transition: write the intended next state and the idempotency key, commit, act, write the result, commit. Signer idempotency keys: `<payout id>:fund:<attempt>` and `<payout id>:sweep:<attempt>`. Broadcast to all providers; "already known" and "nonce too low" answers are checked against the chain, not treated as failure.

Fee rule (pure function, unit-tested): inputs amount (token units), token USD price (1.0 for USDT), native USD price, gas estimate, fee data, pool limits → `pay_now`, `wait`, with the reason. Native USD price source: Bitcart's own rate service (`WalletDataService.get_rate`); if it fails, `wait`.

Fee-wallet nonce: one row lock per chain (`SELECT ... FOR UPDATE` on a small `plugin_acctpool_nonces` table: `chain`, `next_nonce`), reconciled with `eth_getTransactionCount(pending)` on start.

### 4.6 REST API (`/api/plugins/acctpool/...`, scope `server_management`)

`GET pools`, `POST pools`, `PATCH pools/{id}`, `GET addresses?pool=&status=&limit=&offset=`, `GET payouts`, `GET deposits`, `GET status` (fee wallet address and native balance per chain, signer status, ready counts, stranded native total), `POST addresses/{address_chain_id}/withdraw`, `POST withdraw-batch`. Admin actions only write a request row or a `planned` payout; the worker executes. `GET ui` serves `static/pool.html` (one file, no external assets, calls the routes above with the admin token that the user pastes or that the Bitcart admin stores in local storage).

## 5. Test commands (test host)

All from `~/acctpool-build/plugin` on the test host. Builders write a script per test suite in `scripts/` and name it in the report.

- Signer unit tests: container from `docker.io/bitcart/bitcart-eth:0.10.3.0` with `signer/` mounted, `pip install pytest pytest-asyncio pytest-aiohttp` inside, `pytest signer/tests`.
- Chain library unit tests: container from `docker.io/bitcart/bitcart:0.10.3.0`, `pytest backend_tests/chain`.
- Backend plugin tests: podman pod with postgres + redis + the backend image, plugin mounted at `/app/modules/forkedpool`. Read how the `bitcart-solana-usdt` README and `backend_tests/` run tests inside the pinned image.
- Integration: anvil + signer + engine, in `integration/`.

## 6. Docker plugin folder

`components/acctpool.yml`: service `acctpool-signer`, build from `signer/`, volume `acctpool_signer_data:/data`, read-only mounts of `/root/acctpool/pools.toml`, the master key file and the token file, network `acctpool_internal` (`internal: true`), hardening options from 3.6, restart `unless-stopped`, no `ports`, no `expose` to other networks.

`rules/91_acctpool.py`: adds to `backend` and `worker`: the module mount `./plugins/docker/acctpool/backend/forkedpool:/app/modules/forkedpool:ro`, the `rpc.toml` mount. Adds to `worker` only: network `acctpool_internal`, the token mount, `ACCTPOOL_SIGNER_URL=http://acctpool-signer:7070`. It must not remove or change anything that other rules set (the Solana rule appends to `volumes` the same way).

## 7. Amendments after wave 1 (orchestrator, 2026-09-29)

These lines win where they differ from the sections above.

### 7.1 Host paths, owners, token files
- Host folder for config and tokens: `/root/acctpool/` (root, mode 700). Host folder for the master key: `/var/lib/acctpool-key/` (root, mode 700, outside the backup set).
- A secret file has mode 600 and the owner is the user id of the process that reads it (signer user, or the stock `electrum` user 1000). "Root 600" in the older text means this.
- Two token files with the same content: `signer.token` (owner signer user) and `worker.token` (owner 1000). In each container the path is `/run/acctpool/signer.token`.
- The example destination in `pools.toml.example` is a placeholder that the signer refuses.

### 7.2 The signer source and image are outside `compose/plugins`
Stock backend and worker mount `compose/plugins/docker` read-write and the stock start script runs the generator rules from that tree. A compromised backend could change signer code or a rule, and the next `./start.sh` would run it.
- The component has NO `build:` key. `image: forked/acctpool-signer:<version>`, `pull_policy: never`.
- The signer source is copied to `/root/acctpool/signer-src/` (root owner) and the image is built there by a deploy step.
- The rule makes the mount `./plugins/docker:/plugins/docker` read-only (`:ro`) on `backend` and `worker`. It changes only that one entry. If the entry is not found, the rule adds nothing and the test fails.
- All files in `compose/plugins/docker` must have owner 1000 before a start: the stock entrypoint runs `chown` on files with a different owner, and `chown` fails on a read-only mount, and then the container does not start. The deploy guide sets the owner, and the probe has a check for it.
- Effect to write in the deploy guide: the Bitcart admin page can no longer install or remove docker-type plugins. A manual copy by root is the only path.
- The checksum check before each start stays.

### 7.3 Status data for the probe and the API
- Table `plugin_acctpool_state` (key, value JSON, updated): the worker writes signer status, fee wallet balances and provider agreement. The API and the probe read this table. The probe does not use an API token.
- Table `plugin_acctpool_requests`: an admin action writes a row, the worker executes it. No API route reaches the signer or an RPC provider.
- `plugin_acctpool_address_chains` has `status_since` (timestamp, set on each status change). The probe uses it for "payout waiting > 24 h".

### 7.4 Generator order
The generator loads plugin folders in `os.listdir()` order. Rules only add or edit their own entries and never replace a list.

### 7.5 Decisions on the chain library report
- `always_payout = true`: the payout does not need a native price. A missing price does not stop it. The signer caps are the only fee limit.
- The fee rule limits come from the pool row: `max_fee_percent`, `max_fee_usd`, plus the new column `usd_rule_min_amount_usd` (default 50). Rule: pay when fee <= `max_fee_percent` of the amount, or amount >= `usd_rule_min_amount_usd` and fee <= `max_fee_usd`.
- `rpc.toml` has `chain_id` for each chain. It must be equal to the id in the code table for that chain name (section 8.1). Optional key `priority_fee_floor_wei` is accepted.
- The engine pins a block NUMBER for each read round: `head(finality)` gives the lowest finalized block of the providers, and balance and logs are read at that number on all providers.
- Test containers on the test host use `--security-opt label=disable` for mounts (SELinux) and `uv pip install --python <venv>`.

### 7.6 Decisions on the signer report
- Error codes added: `not_found` (404), `internal` (500), `rate_limited` (429). A nonce conflict is `idempotency_conflict` (409). An index that was not given out and a broken replacement rule are `invalid_request` (400).
- `/v1/derive`: `first_index` must be at most (highest index given out + 1). A call cannot make a gap. With `count` at most 200, a hostile caller can raise the index by 200 for each call only.
- Rate limit in the signer: `[signer] max_signatures_per_minute` (default 30) for the sum of the sign calls, and `max_derive_per_minute` (default 10). Over the limit: 429 `rate_limited`, one audit line.
- `GET /v1/status` with a correct token writes no audit line. All other calls and all failed authentications do.
- Signing backend: the image installs `coincurve` (exact version, hash-pinned, in the Dockerfile) so that `eth_keys` uses libsecp256k1. The image test proves which backend is active. This is the one permitted new dependency.
- The keystore has one seed in version 1. A keystore with more than one seed is refused at start. Seed rotation is a later item.
- Atomic write with link (never replace a keystore) is accepted.

## 8. Extension scope (2026-09-29): BNB, native coins, Tron, testnets, checkout

The operator asked for the items that were outside the first build. Everything is built and tested on the test host. Nothing here is a production change. Public TESTNETS are now permitted for the builders that this section names, with throwaway test keys only. Mainnet is not permitted.

### 8.1 Chain table (code table in the plugin; the signer takes its chains from `pools.toml`)

| chain | family | chain id | Bitcart currency | USDT contract | USDT decimals | native decimals |
|---|---|---|---|---|---|---|
| polygon | evm | 137 | matic | 0xc2132D05D31c914a87C6611C10748AEb04B58e8F | 6 | 18 |
| ethereum | evm | 1 | eth | 0xdAC17F958D2ee523a2206206994597C13D831ec7 | 6 | 18 |
| bnb | evm | 56 | bnb | 0x55d398326f99059fF775485246999027B3197955 | 18 | 18 |
| tron | tron | (none) | trx | TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t | 6 | 6 |
| anvil | evm | 31337 | matic | test token | 6 | 18 |
| amoy | evm | 80002 | matic | test token, from config | 6 | 18 |
| sepolia | evm | 11155111 | eth | test token, from config | 6 | 18 |
| bsctest | evm | 97 | bnb | test token, from config | 18 | 18 |
| nile | tron | (none) | trx | Nile test USDT, from config | 6 | 6 |
| trelocal | tron | (none) | trx | test token | 6 | 6 |

A test chain is usable only when `rpc.toml` lists it. The production `rpc.toml` lists mainnet chains only. Builders verify each mainnet contract address against two independent public sources and report it.

### 8.2 Assets
- `asset` is `usdt` or `native`. It is a new column (text, not null, default `usdt`) on `pools`, `address_chains`, `deposits` and `payouts`. Unique key of `address_chains` becomes (`address_id`, `chain`, `asset`).
- A Bitcart wallet with a contract that is equal to the chain's USDT contract has asset `usdt`. A wallet with no contract has asset `native`. Every other contract is refused for pool mode.
- One address per invoice per family stays: the USDT wallet and the native wallet of the same family share it.

### 8.3 Native coins (EVM)
- Detection: balance of the address at the pinned finalized block number, from all providers, equal answers only. A balance increase is one deposit row. The engine finds the block of the increase (binary search over block numbers, both providers) and the transactions in that block with `to` = address; it stores the real transaction hash when it finds one, else the synthetic id `balance:<block number>:<address>` with `log_index` = -1 (the address is in the id, because two addresses can change in one block). For a real native transaction `log_index` = -1 also.
- Before a funding transaction to an address: the engine records the expected increase, so that gas funding from the fee wallet is never counted as a customer payment. A native deposit on an address during a USDT payout is kind `wrong_asset` if the invoice has no native wallet.
- Payout: ONE transaction, no funding. New signer call `POST /v1/sign/sweep_native` `{idempotency_key, chain, store, index, nonce, gas_limit, max_fee_per_gas_wei, max_priority_fee_per_gas_wei, value_wei, replaces}`. From = derived address, to = the pinned native destination, no data. The engine sets priority fee = max fee, so the fee is exactly `gas_limit × max_fee` and the value is `balance − fee`: no leftover.
- Signer config: `[stores.<store>.native_destinations] <chain> = "0x..."`. No entry = the call is refused with `unknown_store`/`invalid_request`. Cap: `native_gas_limit_cap` per chain (default 21000; higher only when the destination is a contract wallet).
- The fee rule is the same function, with the token price = native price.
- Leftover native coin after a USDT payout (unused gas funding) is swept with the same call when its value is higher than 3 × the fee; else it stays (stranded, shown on the admin page).

### 8.4 BNB
Same code as Polygon and Ethereum. Differences are data only: chain id 56, USDT has 18 decimals, base fee can be 0 (use the priority fee floor), finality tag `finalized`. Production needs the Bitcart BNB daemon; that is a deploy item, not a build item.

### 8.5 Tron
- Family `tron`. Derivation path `m/44'/195'/<account>'/0'/<index>'`, fee wallet `m/44'/195'/9000'/0'/0'`. Addresses are Base58Check (`T...`) in every API and table.
- Signer calls, same routes, chosen by the family of the chain:
  - `/v1/sign/fund`: `TransferContract` fee wallet → derived address, `amount_sun`. Caps: `max_fund_value_sun`, `max_fund_total_per_address_sun`, `fee_wallet_daily_cap_sun`.
  - `/v1/sign/sweep`: `TriggerSmartContract` derived address → pinned USDT contract, `transfer(destination, amount)`, `fee_limit_sun` with cap `fee_limit_cap_sun`.
  - `/v1/sign/sweep_native`: `TransferContract` derived address → pinned native destination.
  - Request fields in place of nonce and gas: `ref_block_bytes`, `ref_block_hash`, `timestamp_ms`, `expiration_ms`. The signer refuses `expiration_ms − timestamp_ms` > 10 minutes.
  - Tron has no nonce. The signer refuses a second different signature for the same (chain, from address, kind) while an earlier one is not expired: `replaces` is permitted only when the new `timestamp_ms` is later than the old `expiration_ms`.
  - The signer builds the protobuf `raw_data` itself from the fields (it never signs bytes from the caller). Response: `{raw_data_hex, signature, txid, from, to}`.
- No new runtime dependency: hand-written protobuf encoding, Base58Check and SHA-256 in pure Python; secp256k1 signing with the library that the signer image has.
- Chain library `chain/tron.py`: full-node HTTP API (`/wallet/...`, `/walletsolidity/...`) only, because every provider has it. Detection by block scan with `gettransactioninfobyblocknum` on solidified blocks. A deposit is accepted when all providers give the same transaction info (result SUCCESS, Transfer log of the pinned contract to our address, same amount). Finality = solidified block.
- Flow as in PLAN section 7. Values (funding amount, fee limit, activation) come from measurements: first on a local private chain, then on Nile. The measurement report is `docs/TRON-MEASUREMENTS.md`.
- Minimum payout and "one store only" are pool settings (`min_withdraw`), not code.

### 8.6 Testnets
- Test seed and test keys exist only on the test host, in `~/acctpool-build/secrets/` (mode 700, files 600). They are never used on mainnet. They are not copied off the test host and not printed in reports.
- Public endpoints without keys, two independent operators per chain. No production RPC keys are used.
- Test USDT on EVM testnets: our own token contract, deployed by a throwaway deployer key. It must behave like mainnet USDT (no return value from `transfer`).
- Faucet funds: the builder tries public faucets for a bounded time (30 minutes in total). If a faucet needs a person (captcha, login, mainnet balance), the builder stops and reports the address and the amount that the operator must send.

### 8.7 Checkout front end
- Behaviour: when the Bitcart payment method has `metadata.acctpool` (or `user_address` equal to `payment_address`), the checkout skips the sender-address step and shows the deposit address and this line: "Send only USDT on the <network> network. Other coins or networks are not credited." For a native-coin method the line names the coin. Every other method behaves as today. No other UI change.

### 7.7 Read-only plugin mount, second decision (replaces the owner rule of 7.2)
- The rule also sets `BITCART_VOLUMES` on `backend` and `worker` to `/datadir /backups /plugins/backend /plugins/admin /plugins/store`, so that the stock entrypoint does not run `chown` in `/plugins/docker`.
- The rule makes BOTH changes (read-only mount, `BITCART_VOLUMES`) or NONE. It makes them only when the stock mount entry is found and `BITCART_VOLUMES` has exactly the stock value `/datadir /backups /plugins` on both services. In every other case it changes nothing of the two and the generator test fails.
- Files in `compose/plugins/docker` then have owner root, mode 644/755, on the host. The "owner 1000" rule, the owner probe check and the owner step of the start sequence are removed. The probe checks that no file in that tree is writable by a user that is not root.
- The checksum check before each start stays. Bytecode folders (`__pycache__`) are not in the checksum list.

## 9. Worker engine (wave 2) — decisions from the backend plugin report

### 9.1 Accepted additions of the backend builder
Column `address_chains.confirmations`; database triggers (an address never returns to `ready`, no delete, events append only); the plugin's own connection pool; a non-USD invoice and a second pool wallet on the same chain, store and asset use the stock flow; hooks registered in `setup_app`, `startup` and `worker_setup`; `ACCTPOOL_ALLOW_TEST_CHAIN=1` for test chains (all test chains of 8.1). The "no pool row" event for non-pool payment methods is removed (noise).

### 9.2 Crediting rules
- Bitcart completes an invoice at the store's transaction speed. With speed 0, status 7 completes at once. Thus the engine reports NOTHING to Bitcart (no `credited_amount`, no `credit()`, `get_request` answers status 0 with amount 0) until: the deposit is in a block, both providers agree on it at a pinned block number, and it has at least `confirmations_floor` confirmations by the lower provider value.
- After the floor: write deposit rows, `credited_amount`, `confirmations`, commit, then `credit()`. Confirmations are updated on each poll until the invoice is complete. A chain reorganization that removes a credited deposit before the payout: event `reorg_after_credit`, alert, no payout of that amount, no automatic change of the invoice.
- A deposit is `normal` when the invoice is open and the asset and chain match a payment method of the invoice. Sum of normal deposits below the invoice amount: Bitcart's own underpayment rule decides (the engine passes the real sum). Invoice not open at credit time: kind `late`. Other asset on the address: `wrong_asset`. No automatic credit and no automatic refund for late and wrong deposits; event + alert.
- The payout waits for the `finality` block tag. Late, partial and wrong-asset USDT/native amounts are paid out to the pinned destination by the same rules.

### 9.3 Reconcile loop
Bitcart's batch `mark_invalid` fires no hook, and hooks can be lost in a restart. Every 5 minutes the engine compares `address_chains` in status `in_invoice` with the invoice status in the Bitcart tables (read only) and corrects the status. An address of an expired or invalid invoice with no deposit becomes `retired` (never `ready` again) and enters the retired checks.

### 9.4 Retired checks
Every address that was given to an invoice is checked for new deposits: all chains and assets of its family that `rpc.toml` lists, daily for 12 months after retirement, then every 30 days. Use `next_check_at`. One `eth_getLogs` call can cover many addresses (topic filter with a list of recipients); keep calls bounded.

### 9.5 Known limit to document (not to fix in code)
If the plugin is not loaded while pool invoices are open, stock `check_pending` asks the stock daemon and sets them to invalid. The deploy guide says: set pool mode off and wait until open pool invoices are closed before the plugin is removed; the probe alerts when the plugin is not loaded.

### 9.6 Loops and state
All loops run in the worker only, as asyncio tasks started from `worker_setup`, each with its own error barrier and backoff; one loop failure does not stop the others or the stock worker. Loops: ready-fill (exists), scan + credit, payout, reconcile, retired checks, state writer (`fee_balance:<chain>`, `stranded_native:<chain>`, `providers:<chain>`, `signer_status`), admin requests. A Postgres advisory lock makes sure that only one worker process runs the loops.

### 7.8 Signer, second round of decisions
- Fee budget of token sweeps: the signer refuses a token sweep (`cap_exceeded`) when the sum of `gas_limit × max_fee_per_gas` of the token sweeps signed for that (chain, address) would be higher than the sum of the funding values signed for that (chain, address). A replacement counts only its difference to the highest earlier signature of the same nonce. Thus a token sweep can never spend native coin that a customer sent to the address. The engine funds before each token sweep, with at least `gas_limit × max_fee` of the sweep it will ask for.
- Leftover native coin is swept when its value is at least 10 × the fee (this agrees with `native_max_fee_share_percent = 10`). SPEC 8.3 "3 × the fee" is replaced by this.
- The image build must work with `--network none`: the Dockerfile installs `coincurve` from a wheel file in `signer/wheels/` with `--no-index --require-hashes`. The wheel file is not in git (`.gitignore`). `signer/fetch-wheels.sh` downloads it from PyPI and checks the SHA-256; the deploy guide runs that script before the build. The build fails with a clear message when the wheel is missing.
- The rate limit counts every call with a correct token. A derive gap is `invalid_request`.
- `ACCTPOOL_SIGNING_BACKEND=coincurve` with refusal to start on another backend is accepted.

### 7.9 Decisions from review A of the signer (2026-09-29)
- Fee budget of token sweeps, corrected (after review B): the signatures of a (chain, address) are counted in signing order; a funding adds its value, a token sweep takes its fee, a native sweep takes value + fee, and the budget is never below 0 after a native sweep. In every order of the calls, the sum of the token-sweep fees of an address is not more than the sum of the funding values signed for it (at most `max_fund_total_per_address`). A native sweep takes the funded coin out of the budget only when it is signed after the funding. The 7.8 sentence "a token sweep can never spend native coin that a customer sent" holds only in the order funding, then native sweep; the engine keeps the order fund, token sweep, native sweep last.
- Tron has no nonce: every Tron signature counts fully in the caps and the budget.
- `init` and `restore` run only through `exec` in a running container. They refuse to run as the main process of a container. The image test proves that no seed word reaches the container log or the host journal.
- A caller without the token cannot stop the signer, fill the volume or get it killed.
- A missing, older or foreign journal is detected at start.
- The deploy guide must: use `exec -it` for `init`, never `run`; tell the operator to use a terminal that does not record; set a log size limit for the signer service.
- Test seeds of the builders reached the journal of the test host and its syslog copy on the log server. They are throwaway seeds with no funds. No test seed is used for anything else.

### 8.8 Tron rules from the measurements (2026-09-29, `docs/TRON-MEASUREMENTS.md`)
- Funding value = dry-run energy × energy price + 350,000 sun (bandwidth reserve). Fee limit of the sweep = dry-run energy × energy price, and NEVER above the TRX balance of the address at the pinned solidified block. Reason: a balance below the energy cost burns ALL TRX of the address (OUT_OF_ENERGY); a fee limit below the energy cost is refused at the broadcast and burns nothing.
- Before it asks for the sweep signature, the engine reads the energy (dry run), the energy price and the TRX balance again. If the balance is too low (price change, second sweep in one day), it sends a second funding for the difference first.
- A failed sweep (OUT_OF_ENERGY) is recovered with a new funding of energy cost + 350,000 sun, inside the signer caps. After two failed sweeps of one address the payout goes to `failed` and an alert is raised; no third automatic try.
- Token transfers with amount 0 are ignored by the scan (no deposit row).
- The worker checks before broadcast that the signature recovers to the expected sender address, as it does for EVM.
- Signer caps for Tron (start values): `fee_limit_cap_sun` 30 TRX, `max_fund_value_sun` 30 TRX, `max_fund_total_per_address_sun` 60 TRX.
- `rpc.toml` for a Tron chain: `family = "tron"`, `genesis_block_id` (pin in place of a chain id; the code table has the value for `tron` and `nile`, the config gives it for `trelocal`), `providers`, optional `api_key_header` + key per provider, `min_request_interval_ms` per provider.
- SPEC section 0 line "Do not write Tron code" is void; section 8 wins.

### 8.9 One Tron chain per signer
A Tron signature has no chain id: it is valid on every Tron network that has the reference block. A signer refuses to start when its config has more than one chain of the family `tron`. The production signer has `tron` only. Test signers (one for `nile`, one for `trelocal`) use test seeds only.

### 8.10 Tron signer rules after round 5
- The "no second signature while an earlier one is not expired" rule of 8.5 applies: for a funding, per RECIPIENT address; for a sweep, per (sender address, kind). Fundings to different addresses can run at the same time. Every Tron signature still counts fully in all caps.
- `fee_wallet_daily_cap_sun` start value 300 TRX (about 36 normal payouts per day). Raise it only from real volume.
- The engine uses a window of 60 seconds for Tron (`expiration_ms − timestamp_ms`), as the measurements recommend; the signer maximum stays 10 minutes.

### 7.10 Decisions from review B of the signer (2026-09-29)
- H1 (more than one Tron chain): fixed in round 5 (SPEC 8.9). Tests that asserted the unsafe behaviour are removed.
- M1 (full-volume restore not detected): the signer reads an optional root-owned high-water file `/run/acctpool/audit.highwater` (read-only mount; one line: highest audit `seq` and its hash). The health probe on the host writes it from the audit lines that it reads (the probe runs as root; it already runs `audit-verify`). At start the signer refuses a journal or audit log below that value, with a clear message and the `journal-rebuild` path. Missing file = no check (first start). PLAN 10.13 must say: a volume restore needs `journal-rebuild` and the high-water check.
- M2 (`init`/`restore` in a shared PID namespace): accept only when this process is not PID 1 AND PID 1 of the same PID namespace is the signer `serve` process of this image (check its command line). Refuse in every other case. The image test covers `run -t`, `run -t --init`, `run -t --pid=host` (if the test host permits it) and a pod.
- L1: the request time is taken after the body is read and passed as an argument; body read timeout.
- L2: the journal stores the seed id; the start compares it with the keystore.
- L3: a Tron signature counts as "not expired" until `expiration_ms` + 60 s.
- L4: the fee budget text in SPEC 7.9 and THREATS.md is corrected: the bound is on value, independent of call order.
- L5: expired Tron fundings that were never broadcast: the engine reports them (event); the signer keeps counting them (safe side). Documented in THREATS.md and the README.
- README: the audit recovery text must not rely on a syslog copy (the component uses the `json-file` log driver); the volume backup and the probe's high-water file are the other copies.

### 7.11 Audit high-water file (from signer round 6)
- Host path `/root/acctpool/audit.highwater` (root, 0644), mounted read-only at `/run/acctpool/audit.highwater`. Content: `<seq> <hash>` (decimal seq without leading zeros, 64 lower-case hex SHA-256 of that audit line without its newline), optional newline, max 100 bytes. `0` + space + 64 zeros = no line yet; the deploy step writes this before the first start (docker would make a folder for a missing bind source, and the signer refuses a folder).
- The probe updates it in every run and after each volume backup: `audit-verify` must exit 0; take the last complete line of the newest audit file; if the file on the host has a higher seq, or the same seq with another hash, do not write and alert; else write a temp file, fsync, rename. Never lower the seq.
- The signer reads it only at start. Volume restore procedure: `docs/VOLUME-RESTORE.md` (PLAN 10.13).
- The component must not set `init: true`, and the docker daemon must not have `"init": true`: the seed commands then refuse to run.

### 9.7 Retired check schedule (changed after the engine report)
A retired address is checked every hour for the first 7 days after retirement, then daily until 12 months, then every 30 days. Reason: a customer who pays just after expiry must be seen in about an hour, not a day. The first check stays immediate at retirement.

### 9.8 Decisions from the plugin + engine review (2026-09-29, `review/plugin-a-REPORT.md`)
- F1: a token deposit is identified by (chain, tx hash, contract, recipient, amount); the log index is data, not identity. A deposit is marked removed only when its block hash changed AND its receipt is gone on both providers. Token rows rewind the scan after a reorganization, as native rows do. `reorg_after_credit` is set only when the amount is really gone.
- F2: the balance proof counts only deposits and sweeps whose block is at or below the checked block, confirmed payouts included.
- F5: pool addresses are given out only when the `engine` state row has `alive_at` less than 5 minutes old; else the stock flow is used and an event is written (rate-limited). Scan and credit start even when the signer config is missing (payouts then wait and alert).
- F3: SPEC 9.7 is built (hourly for 7 days).
- F4: every top-up and every fee bump runs the fee rule `decide()` again, and waits if it says wait (except `always_payout`). One top-up per payout attempt, as PLAN 6 says.
- F6 to F9: fix each, or state in ENGINE.md why not.
- The reviewer's suspicion (not verified): a native fee computed higher than the real fee leaves a positive rest that becomes a synthetic deposit and is credited. Check it; the real fee from the receipt decides; a rest from our own transactions is never a deposit.

### 8.11 Tron engine decisions (after the Tron engine report)
- Fee limit + 350,000 sun (bandwidth reserve R) must not be more than the TRX balance of the address at the pinned solidified block (stricter than 8.8).
- The engine counts only its OWN TRX on an address (fundings that arrived, minus fees paid) as fee money. A customer's TRX on the address is never used as fee money. The signer's budget counts signed fundings; the engine rule closes the gap for fundings that expired unsent.
- `rpc.toml` keys for Tron providers: `api_key_header`, `api_keys` (one entry per provider), `min_request_interval_ms`.
- For Tron payout rows, the columns `gas_limit` and `max_fee_per_gas` hold the energy and the energy price.
- Retired Tron addresses are checked by balance (TRX and pinned USDT); late USDT gets a synthetic id with log index -2. Other tokens on a retired Tron address are not seen (known limit).
- New event kind `tron_signature_expired`: in EventKind and in the probe alert list.

### 9.9 Correction of 9.5 (after the engine review fixes)
With Bitcart 0.10.3.0 and the stock eth daemon, stock `check_pending` does NOT set open pool invoices to invalid when the plugin is not loaded: the daemon answers a generic error, Bitcart logs it, and the invoice stays pending until it expires. The rule of 9.5 stays (pool mode off, wait until open pool invoices are closed, then remove the plugin), because a payment to a pool address is not credited while the plugin is not loaded. The probe alert stays.
