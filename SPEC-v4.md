# acctpool v4 — Bitcart-native build specification (2026-09-29)

This replaces SPEC.md for the v4 build. SPEC.md stays as history. Where they differ, this file wins.

## 0. Goal and rules

Reproduce the FUNCTIONALITY of Bitcart's paid ETH Payments plugin as simply as possible, on top of Bitcart's own infrastructure. Bitcart (the stock daemons and backend) is the payment and blockchain platform. The plugin does not re-implement anything the stock daemon or backend already does.

Scope (EVM only): Polygon, Ethereum, BNB (data only), anvil (tests). Assets: USDT and the native coin. Tron is OUT of v4 (deleted from the tree; a later phase).

Function list: unique deposit address per invoice; no sender-address field; payments from exchanges or any wallet; native coins; USDT; automatic gas funding; automatic sweep to the pinned merchant destination; address pool lifecycle; retries and idempotency; admin list + manual and batch withdraw.

Budgets (production lines, `wc -l`, comments included): backend plugin + deploy glue <= 3,100; signer <= 1,300. Going over needs a written reason in the builder report. Tests are not budgeted; keep them focused on money safety.

Rules for every builder:
- No production host. Tests run on a separate test host with rootless podman. No mainnet. anvil only.
- No custom RPC client, block scanner, log parser, receipt parser, gas estimator or broadcaster, except `secondcheck.py` (section 4.7).
- The plugin never sees, stores, returns or logs a private key or seed.
- Plain, explicit code. No abstraction for a chain family or a feature that v4 does not have.

## 1. Proven platform facts (spike, `review/SPIKE-DAEMON-2026-09-29.md`)

- The stock eth daemon (Bitcart 0.10.3.0; matic and bnb are subclasses) watches every loaded wallet address. A diskless watch-only wallet is loaded with `batch_load(wallets=[{"xpub": <address>, "diskless": true}, {"xpub": <address>, "contract": <usdt>, "diskless": true}])`.
- For each native transfer and each ERC-20 `Transfer` log to a loaded address, the daemon emits `new_transaction` with `tx`, `from_address`, `to`, `amount` (decimal string in coin units), `contract` (None for native). No event for addresses that are not loaded. Exact, no duplicates in the spike.
- Bitcart's worker listens on the daemon websocket through `CoinService.manager` (bitcart SDK `APIManager`). A handler added with `manager.add_event_handler("new_transaction", fn)` is called as `fn(instance, event, tx, from_address, to, amount, contract)` (arguments are matched by name).
- Diskless wallets are LOST on a daemon restart. The SDK reconnects by itself and Bitcart then runs `check_pending(currency)`, which runs the plugin hook `check_pending`. Payments mined while the wallets were not loaded give NO event. So: reload on `check_pending` + a balance reconcile loop are required.
- Useful daemon calls (all through `coin.server.<method>` of a Bitcart coin object from `CoinService.get_coin`): `batch_load`, `normalizeaddress`, `getaddressbalance(address)` (native, coin units), `getaddressbalance_contract(address, contract)` → `{balance, divisibility}`, `getnonce(address, pending=True)`, `get_default_gas(tx)` (removes fee fields itself, works for an empty address), `getfeerate()` / gas price, `broadcast(raw_tx)`, `get_tx_status(tx_hash)` (receipt), `gettransaction(tx_hash)` (includes `confirmations`), `get_used_fee(tx_hash)`, `get_tx_hash(raw_tx)`. Read `ref/bitcart/daemons/eth.py` and `genericprocessor.py` before using one.
- Load 3,000 addresses (6,000 wallets) in ~21 s; idle CPU 0.2%; memory +50 MB.

## 2. Architecture

A. Stock daemon = chain layer (detection, balances, nonce, gas, fee rate, broadcast, receipts, confirmations, provider failover through its `SERVER` list).

B. Backend plugin `acctpool` (backend + worker, read-only mount, as before):
- Bitcart hooks: `create_payment_method`, `post_create_payment_method`, `get_request`, `invoice_status` / `invoice_expired` / `invoice_complete`, `check_pending`.
- Event handler `new_transaction` → deposit row → credit through Bitcart's PaymentProcessor (keep `crediting.py`).
- Loops in the worker, one leader (Postgres advisory lock): derive-ahead, watch-load + reconcile, payout.
- Admin REST under `/api/plugins/acctpool/*` (scope `server_management`) + one standalone page.

C. Signer `acctpool-signer` (separate container, internal network, only the worker reaches it): seed, derivation, policy, signing. Unchanged role; API in section 3.

## 3. Signer (target <= 1,300 lines)

Keep the reviewed core; delete the rest. Keep: `keystore.py`, `hd.py`, `tx.py`, `hardening.py`, `errors.py`, the EVM policy in `api.py`, the journal guards, `init` / `restore` / `verify` / `audit-verify` CLI.

Delete: all Tron code (`tronapi.py`, `trontx.py`, `tronproto.py`, Tron config/journal/api branches, the one-Tron-chain rule); `rebuild.py` and the `journal-rebuild` command; audit rotation, repair and the audit copy inside the journal table; the "older build" upgrade code; the custom connection/flood layer in `server.py` (use stock `aiohttp.web` with: token check, body size limit, request timeout, one JSON log line per request); `check_chains`; multi-seed keystore handling beyond "one active seed" (keep the file format).

Audit: ONE append-only file `audit.log`, one JSON line per signature or refusal, hash-chained (`prev`, `seq`), also printed to stdout. `audit-verify` checks the chain. The high-water check stays (SPEC 7.11 file format, read at start; refuse a journal/audit below it). No rotation (a line is ~300 bytes; 1 million signatures = 300 MB; document it). Review fix 2026-09-29: only the first `rate_limited` refusal of a limit in 60 s gets an audit line (the rest are counted on stderr); `audit-verify` reads the journal line first and scans once, and accepts the in-progress tail of the running signer; a failed cut-back after a failed write stops the process.

API (unchanged from SPEC 3.4 unless said here):
- `GET /v1/status` (each store also has `highest_index`, the highest index given out, or null), `POST /v1/derive` (family `evm` only), `POST /v1/sign/fund`, `POST /v1/sign/sweep`, `POST /v1/sign/sweep_native` (SPEC 8.3 shape).
- Transactions stay EIP-1559 type 2. The engine passes `max_priority_fee_per_gas_wei = max_fee_per_gas_wei` = the daemon gas price × speed factor, so the fee equals `gas_limit × max_fee` exactly.
- Keep every guard: request fields cannot name destination, contract, chain id or data; unknown fields refused; destination / chain id / USDT contract only from `pools.toml`; the signer builds every transaction; gas limit cap, max fee cap, per-transaction funding value cap, per-address funding total cap, fee-wallet daily cap; idempotency with stored response; one signature per (chain, from, nonce) unless `replaces` with fee +10% or more; sign only indexes given out by derive; journal commit before the answer; constant-time token compare; no exception text in logs; signature self-check; encrypted keystore; hardening.
- Native destination per store and chain: `[stores.<store>.native_destinations]` (SPEC 8.3).

Tests: keep and adapt the policy, keystore, journal, idempotency, caps, replacement, audit-chain and high-water tests; delete the Tron, rebuild, rotation and flood tests. Keep the image test (non-root, read-only root, `init`/`restore` refusal rules, one start/sign/verify round).

## 4. Backend plugin (target <= 3,100 lines with deploy glue)

### 4.1 Chains (code table, `constants.py`)
`polygon`: Bitcart currency `matic`, chain id 137, USDT `0xc2132D05D31c914a87C6611C10748AEb04B58e8F` (6). `ethereum`: `eth`, 1, `0xdAC17F958D2ee523a2206206994597C13D831ec7` (6). `bnb`: `bnb`, 56, `0x55d398326f99059fF775485246999027B3197955` (18). `anvil`: currency `eth`, chain id 137 or 31337 from the test config, USDT from the test config. Per chain: `payout_confirmations` (polygon 64, ethereum 64, bnb 15, anvil 2), `native_gas_limit` 21000, `token_gas_limit` fallback 100000.

### 4.2 Pool wallets
A Bitcart wallet is in pool mode when an admin enables it (table `pools`: wallet_id, chain, asset, store, enabled). Asset `usdt` when the wallet contract equals the chain's USDT; `native` when the wallet has no contract; anything else refused. Invoices over the cap (setting `max_invoice_usd`, default 500) and invoices when the engine is not alive (state row older than 5 minutes) use the stock flow and write an event (rate-limited).

### 4.3 Tables (all `plugin_acctpool_*`, one alembic migration, as few columns as the flow needs)
- `pools` (above).
- `addresses`: id, store, index, address (unique), status (`ready`, `in_invoice`, `pending_payout`, `in_payout`, `retired`), invoice_id, assigned_at, retired_at, watch_until, withdraw_requested (bool).
- `deposits`: id, address_id, chain, asset, tx_hash (or `balance:<chain>:<address>:<asset>:<random>` when found by balance), amount (numeric, coin units), from_address, source (`event`|`balance`), invoice_id, credited (bool), late (bool), created. Unique (chain, tx_hash, address, asset).
- `payouts`: id, address_id, chain, asset, kind (`fund`|`sweep`|`sweep_native`), state (`planned`, `signed`, `broadcast`, `confirmed`, `failed`), idempotency_key (unique), nonce, gas_limit, max_fee_wei, value (numeric), raw_tx, tx_hash, replaces_id, attempts, error, created, updated.
- `balances`: address_id, chain, asset, baseline (numeric) — the sum of what our own mined transactions changed on that address, from their receipts (section 4.5): balance = baseline + all deposits.
- `events`: id, kind, chain, address, detail (json), created. Kinds feed the probe: `fee_wallet_low`, `payout_waiting_24h`, `late_payment`, `second_opinion_mismatch`, `second_opinion_down`, `ready_low`, `unswept_high`, `signer_down`, `payout_failed`, `stock_fallback` (and `address_mismatch`, `address_in_use`).
- `state`: one row per key (leader alive_at, last reconcile per chain).

### 4.4 Invoice flow
1. Derive-ahead: keep `ready_target` (default 50) `ready` rows per store; fill from signer `/v1/derive`, from max(highest index in the database, the signer's `highest_index`) + 1 (a restored older database never re-issues an index). At allocation, re-derive that one index and compare (tamper check, replaces the daily re-derive). A ready address with a nonce or a balance on a chain of the store's pools (a restore) is not given out: it is `retired` (so watched: its money comes in as a late payment), event `address_in_use`, and the next one is taken.
2. `create_payment_method`: pool wallet + under cap + engine alive → take ONE ready address per invoice (`FOR UPDATE SKIP LOCKED`); every pool method of that invoice (all chains, both assets) uses it. Set it as the payment address; load the watch-only wallets for this method's currency now (`batch_load`). Else return unchanged (stock flow).
3. `post_create_payment_method`: `user_address` = deposit address (no sender prompt; keep the `metadata.acctpool` marker that checkout front ends read).
4. `get_request`: status, sent amount, tx hashes, confirmations (daemon `gettransaction` of the newest deposit tx; for a balance-found deposit use the block count since it was seen) for pool methods.
5. Expired with no payment → `retired`, watch 30 days. Paid → `pending_payout`.

### 4.5 Detection
- Event handler: currency + `to` → address row (any status that is still watched). Ignore a transaction whose hash is one of our own payouts or whose sender is the fee wallet. Insert the deposit (idempotent on the unique key; events are exact, no balance check), then credit when the invoice is open (`crediting.py`); otherwise `late` + event `late_payment`. A deleted invoice counts as closed; money that Bitcart does not take for a `confirmed` invoice is late.
- Watch-load: on `check_pending` for a currency (daemon reconnect) and every 10 minutes, `batch_load` all watched addresses of that currency (status not `retired`, or `watch_until` in the future), in batches of 500.
- Reconcile (every 60 s per chain, watched addresses with no payout in flight): daemon balance − (baseline + all deposits) > 0 at a first reading is kept with the provider head (`server_height`) of that reading; when the daemon's processed height (`blockchain_height`) has passed that head (every event of the reading is in, so an event always wins), the difference against the accounting of that moment is one deposit row with `source=balance` (its `height` = that head; its confirmations count from the provider head), then credit/late as above. A payout that is open or finished since the first reading (baseline moved) drops the reading. One address never stops the round. Retired addresses past `watch_until`: one reading every 30 days (not loaded, no event can come). Baseline: each mined transaction of ours changes it by what its receipt shows — a funding +value; a sweep −gasUsed×effectiveGasPrice and, unless reverted, −the amount it moved — in the same database transaction as its final state. This covers missed events, daemon downtime and internal native transfers without trace.

### 4.6 Payout (worker loop, per chain)
1. An address is due when its deposits have >= `payout_confirmations` (daemon `gettransaction`) and it is `pending_payout`, or an admin asked for a withdraw.
2. Fee rule (keep, trimmed): Polygon pays at once; Ethereum when fee <= 5% of amount, or amount >= $50 and fee <= $3; else hourly retry, event after 24 h. `min_withdraw` per pool. USD prices from Bitcart's rates service.
3. Second opinion (section 4.7) before EVERY signature request. Mismatch or down → no signature, event, retry next round.
4. USDT: gas = daemon `get_default_gas` for the transfer (fallback `token_gas_limit`) × 1.2; price = daemon gas price × speed factor (setting, default 1.25). If the native balance is below `gas × price`: `fund` (fee wallet → address, value = shortfall), wait until confirmed, then `sweep` the full token balance. Fee-wallet transactions on one chain are serialized (one in flight). Native: `sweep_native` with value = balance − 21000 × price. Native coin (a payment or the leftover after a USDT sweep) is swept only when the value is >= 10 × its fee (the signer refuses a fee over 10%, `native_max_fee_share_percent`), else kept (shown as stranded).
5. Every signature: write the `payouts` row `planned` with its idempotency key, call the signer, store `raw_tx` + `tx_hash` (`signed`), broadcast through the daemon (`broadcast`), then `broadcast`; confirm by `get_tx_status` → `confirmed` or `failed`. After a crash: `signed`/`broadcast` rows are re-broadcast with the SAME bytes ("already known" / "nonce too low" → look up the receipts of every attempt of that nonce). A receipt of any row of a nonce group (all rows of one sender and nonce) finishes the whole group in ONE database transaction (final state, the other rows `failed/replaced`, baseline). The signer's answer must have the expected `from` and `chain_id`. Signer refusals: `daily_cap_exceeded` → the row waits for the next UTC day (event `fee_wallet_low`); `idempotency_conflict` → the daemon nonce is read again (a stale nonce), or, when a signature of ours for that nonce never came into a block (an abandoned row), the row becomes its replacement; any other refusal → `failed`, event `payout_failed`, an admin withdraw starts a new try. The fee budget of a token sweep is the signer's rule exactly (one funding per fee-wallet nonce).
6. Not mined after 10 minutes (or signed and not taken by any node for 10 minutes) → replacement with fee +20% through the signer's `replaces` (max 3), then event `payout_failed`, no further automatic try. The original stays `broadcast` (followed and re-sent) until its replacement is broadcast; before a replacement is signed, the receipts of the whole nonce group are read.
7. Sweep confirmed and token balance 0 → `retired` (watch 30 days), baseline updated.

### 4.7 Second opinion (`secondcheck.py`, target ~80 lines)
One independent RPC URL per chain from a root-owned file mounted read-only (`/run/acctpool/second.toml`: `[polygon] url = "https://..."`), a different provider from the daemon's. Before every `fund`, `sweep` and `sweep_native` signature: `eth_chainId` must equal the chain id; the deposit address balance (native `eth_getBalance`, token `eth_call balanceOf`) at `latest` must be >= the amount to be moved and must equal the daemon's balance reading taken in the same round (tolerate a difference only if the second provider is AHEAD, i.e. higher head block, and re-read once). For `fund`, check the token balance of the address it funds. Plain aiohttp JSON-RPC, 10 s timeout, the URL never logged. Missing file for a chain = payouts on that chain wait with event `second_opinion_down` (never skip the check).

### 4.8 Admin
REST: list addresses (status/chain filter, balances from the daemon on demand), deposits and payouts of an address, fee wallet addresses and balances, events, settings (read/write: ready_target, max_invoice_usd, speed factor, min_withdraw per pool), manual withdraw (one address), batch withdraw (all `pending_payout` and `in_payout`), abandon (an open payout whose nonce has no receipt for any attempt: its open rows `failed/abandoned`, event `payout_failed`; the exit for a stuck transaction). A withdraw of an `in_invoice` address whose invoice is deleted or closed closes it first. Admin actions set `withdraw_requested`; the worker does the work. Page: one static HTML file, no build step, same features.

### 4.9 Keep / delete
Keep and trim: `hooks.py`, `crediting.py`, `allocation.py`, `lifecycle.py`, derive-ahead from `readyfill.py`, the state-machine ideas of `engine_payout.py`, `signer_client.py` (one call function), `feerule.py` (trimmed), `plugin.py`, `db.py`, `api.py`/`schemas.py`, `static/pool.html`, `rules/`, `components/`.
Delete: `chain/` except what `feerule` keeps, `engine_scan.py`, `engine_verify.py`, `engine_prices.py`, `chains.py`, every `engine_tron*`, crash-test instrumentation in production code, testnet chain rows except anvil, their tests. Delete integration/tron*, integration/testnet, backend_tests for deleted modules.

## 5. Tests
- Unit (fake daemon + fake signer + Postgres): allocation (one address per invoice, SKIP LOCKED), hooks, event handler idempotency and own-transaction filter, reconcile math with baseline, watch-load on `check_pending`, payout state machine (crash after each state, re-broadcast, replacement, nonce-too-low), second-opinion mismatch/down → no signature, fee rule.
- Integration on dev (podman, prefix `v4i-`): real Bitcart 0.10.3.0 backend + worker with the plugin, REAL stock `bitcart/bitcart-eth:0.10.3.0` daemon, anvil, the real v4 signer image, and a small second-opinion proxy in front of anvil that a test can switch to "lie" or "down". Flows: USDT exact → credit → complete → fund → sweep → destination has it; native → sweep_native; underpayment; payment after expiry (late); two invoices at once; daemon restart during an open invoice (reload + reconcile credit); worker kill after `signed` (re-broadcast, no double spend); second opinion lies → payout waits, then passes when it agrees; this also proves the PLUGIN's `new_transaction` handler in the real worker.
- Cleanup: every container, network and volume of a test is removed at the end; the script checks it.
