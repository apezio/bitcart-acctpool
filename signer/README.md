# acctpool signer (v4)

The signer holds the seed and signs three EVM transaction shapes (SPEC-v4 section 3; SPEC.md 3, 7.x, 8.3 are the history of the rules): gas funding from the fee wallet to a derived deposit address, a USDT transfer from a deposit address to the pinned destination, and a native coin transfer from a deposit address to the pinned native destination. All are EIP-1559 (type 2). It has no network access and never broadcasts. `THREATS.md` has the analysis of a hostile caller. Tron is not in v4.

## Commands

All commands run in the container: `python -I -m acctpool_signer <command>`. The image has no shell.

- `serve`: the service (default command of the image).
- `init`: makes a 24-word seed from `os.urandom`, shows the words one time, asks for 3 of them (no echo), and only then writes the keystore. A wrong word or a lost terminal: nothing is written.
- `restore`: reads 24 words from the terminal without echo and writes the keystore.
- `verify`: prints the seed id and the fee wallet address.
- `audit-verify`: checks the hash chain of `audit.log`, that it agrees with the journal (the log may be one line behind: the running signer writes that line next; a journal older than the log is refused), and that it has the line of the high-water file. Exit code 1 on a fault. The probe runs it.

`init` and `restore` run only through `docker exec -it <container> ...` in a running signer: they refuse when this process is process 1, or when process 1 of the PID namespace is not the `serve` command of the image in the same cgroup (`run -t`, `--init`, `--pid=host`, a pod, `--pid=container:` are all refused). The reason: the container log keeps what the main process shows. Use a terminal that is not recorded (not a web console that records the session). The container must run without an init program. Steps: start the signer (without a keystore it serves `/v1/status` only), `exec -it ... init`, restart the container.

## Calls

Every call: `Authorization: Bearer <token>`, JSON. The token check is first and constant-time; a request without the token gets 401, its body is not read, and it writes no audit line.

- `GET /v1/status` → `keystore`, `seed_id`, `config_sha256`, `fee_wallets: {"evm": ...}`, `chains: {name: {family, chain_id, usdt}}`, `stores: {name: {destinations, native_destinations, highest_index}}` (`highest_index`: the highest index given out by derive, null before the first one). No audit line.
- `POST /v1/derive` `{store, family: "evm", first_index, count}`; `count` 1..200; `first_index` at most (highest index given out + 1).
- `POST /v1/sign/fund` `{idempotency_key, chain, store, index, nonce, max_fee_per_gas_wei, max_priority_fee_per_gas_wei, value_wei, replaces}`: fee wallet → deposit address, gas 21000, no data.
- `POST /v1/sign/sweep` `{..., gas_limit, amount, token}`: deposit address → USDT contract (or a `recovery_tokens` contract when `token` is set), `transfer(pinned destination, amount)`.
- `POST /v1/sign/sweep_native` `{..., gas_limit, value_wei}`: deposit address → pinned native destination, no data. The engine sets priority fee = max fee, so the fee is exactly `gas_limit x max_fee` and the value is `balance - fee`.

Answer of a sign call: `{raw_tx, tx_hash, from, to, chain_id}`. Errors: `{"error", "detail"}`. 400 `invalid_request`, `unknown_chain`, `unknown_store`; 401 `unauthorized`; 403 `cap_exceeded`, `daily_cap_exceeded`, `token_not_allowed`; 404 `not_found`; 405 wrong method and 413 body over 16 kB (`invalid_request`); 409 `idempotency_conflict`; 429 `rate_limited`; 500 `internal`; 503 `keystore_missing`. An error answer closes the connection. A body that does not come in 10 seconds is refused. 500 means nothing left the signer; send the same request with the same key again (a signature that is in the journal gives its stored answer).

## Rules (all in `api.py`)

- A request has no field for a destination, a contract, a chain id or data; an unknown field is refused. Chain id, USDT contract and destinations come from `pools.toml` only. The signer builds every transaction and checks that the signature recovers to the expected sender before the bytes leave the process.
- Caps per chain: `gas_limit_cap` (token sweep), `native_gas_limit_cap` (native sweep, default 21000), `max_fee_per_gas_cap_wei`, `max_fund_value_wei` (one funding), `max_fund_total_per_address_wei` (all fundings of one address), `fee_wallet_daily_cap_wei` (value + 21000 x max fee of the fundings of one UTC day), `native_max_fee_share_percent` (default 10: a native sweep is refused when its fee is more than this share of value + fee).
- Fee budget of token sweeps (SPEC 7.8, 7.9): per (chain, address), in signing order, a funding adds its value, a token sweep takes `gas_limit x max_fee`, a native sweep takes value + fee (never below 0). A token sweep over the budget is refused. In every order, the token-sweep fees of an address are at most its signed fundings. The engine funds first, then the token sweep, then a native sweep.
- Only indexes given out by `/v1/derive` are signed.
- Idempotency: the journal stores a hash of the validated request and the answer per key. Same key + same request → the stored answer (no new signature); same key + another request → 409.
- One signature per (chain, sender, nonce), all kinds, unless `replaces` names an earlier signature of the same kind, chain, store, index, nonce, sender and token (a funding also the same value), with `max_fee_per_gas_wei` at least 10% above (and above) the highest fee signed for that nonce. Caps count a replacement only with its difference to the highest signature of its nonce.
- Rate limits (`[signer]`): `max_signatures_per_minute` (30, all sign calls), `max_derive_per_minute` (10), `max_other_per_minute` (120, status). Every call with the token that reaches its handler counts, also a replay or a refused one; a call that the limit refused does not. A call that the limit refused gets an audit line only when it is the first refusal of that limit in 60 seconds; the others are counted (a warning line on stderr with the count, after those 60 seconds with the next call, or at the stop). After a restart the sign and derive limits are filled again from the newest audit lines.
- Journal commit before the answer; the audit line is in `audit.log` before the answer. No exception text is logged or sent (type and code position only).

## Config (`pools.toml`)

As SPEC 3.2 with `[stores.<store>.native_destinations]` (SPEC 8.3). Every chain has `family = "evm"`. An unknown key is refused. The start refuses: addresses without EIP-55 checksum, addresses below `0x10000` (the zero address, precompiles), a destination that is a token contract or the fee wallet of this seed, two chains with one chain id, a store account that is 9000 or used twice, caps with `max_fund_value > max_fund_total_per_address > fee_wallet_daily_cap`.

## Files of the volume

- `keystore.json`: AES-256-GCM of the BIP39 entropy, header `version|seed_id|created` authenticated, one seed (file format of SPEC 3.3; a file with more than one seed is refused). Mode 600, written with link (never replaces a file).
- `journal.sqlite3` (WAL): the rules' memory: signatures (request, answer, nonce, value, fee, daily spend), highest index per store, the seed id, and the newest audit line. `journal.lock`: one writing process.
- `audit.log`: one JSON line per signature or refusal of a call with the token (a rate-limit refusal: one line per limit and minute), and `start`/`stop`; the same line on stdout. Fields of SPEC 3.5, always present (null when not applicable), then optional `replaces`, `replay`, `count`, `family`, `token`, `gas_limit`, `seed_id`, `config_sha256`, `backend`, `detail`, then `prev` (SHA-256 of the line before without its newline; 64 zeros for line 1) and `seq` (from 1). No rotation: a line is about 300 bytes, one million lines about 300 MB; set a volume size alert.

Write order of a request: checks, signature row and audit line in one journal transaction; commit; append the line to `audit.log` (fsync) and stdout; answer. So `audit.log` is equal to the journal or one line behind (a crash or a failed write after the commit); the next write or start appends that line, and a part of a line at the end of the file is removed at the start. Every other difference refuses the start.

Backup: copy the volume when the signer is stopped, or `sqlite3 journal.sqlite3 ".backup <file>"`.

## High-water file

`/run/acctpool/audit.highwater` (`ACCTPOOL_HIGHWATER_FILE`), root-owned, mounted read-only; format SPEC 7.11: one line `<seq> <sha256 of that audit line>`, at most 100 bytes; `0` and 64 zeros = no line yet (the deploy step writes this before the first start: docker would make a folder for a missing bind source, and a folder is refused). The probe writes it after `audit-verify` exits 0, from the last complete line of `audit.log`, never lower, temp file + fsync + rename. The signer reads it at start: no file = no check; a file that cannot be read or has another form = refused; `audit.log` ending below `<seq>`, or its line `<seq>` with another hash = refused.

## Recovery

- "the journal ends with audit line N and audit.log with line M, and they do not agree": the journal or `audit.log` is missing, older, changed, or of another signer. Put the right file back. Do not delete a file.
- "audit.log ends with line N, and the high-water file has line M" or "... does not agree with the high-water file": the volume is older than the lines the probe saw (a restore of a backup, a new volume), or of another signer. The signatures after the backup are not in the rules (nonces, caps, budgets). v4 has no journal rebuild. If you must go on with the restored volume: stop the worker; check on chain that the fee wallet and the deposit addresses have no pending transaction of the missing time; then root writes `0` and 64 zeros to the high-water file and starts the signer; the probe writes the new mark. The caps of the missing time are counted again.
- "the journal is of the seed X, the keystore has the seed Y": put the volume of this seed in place.
- "another signer process has the journal open": a second container on the same volume.
- A full volume: calls get 500, nothing is signed, the process stays; a start is refused. Make space; no other step.

## Image

`Dockerfile`: base image by tag and digest, `coincurve` 21.0.0 from `signer/wheels/` (hash-pinned in `requirements.txt`, downloaded and checked by `fetch-wheels.sh`; the build has no network: `podman build --network none signer/`). Non-root uid 10001, no shell, no package tools, isolated mode (`python -I -u -m acctpool_signer serve`). `ACCTPOOL_SIGNING_BACKEND=coincurve`: the signer refuses to start when `eth_keys` would sign with its pure-Python backend. Compose: read-only root, `cap_drop: ALL`, `no-new-privileges`, memory limit (300 MB tested), `ulimits: memlock: -1` (else the memory is not locked and a warning is logged), `internal: true` network, a log size limit, no `init: true`.

## Tests

On your workstation, with `TEST_HOST` set to the ssh name of a test host with rootless podman: `scripts/test-signer.sh` (unit tests, image test and lint on the test host, containers `v4s-*`, build folder `~/acctpool-build`), `scripts/test-signer.sh unit -k fund`, `scripts/test-signer.sh image`, `scripts/test-signer.sh lint`. `SGN_BACKEND=native scripts/test-signer.sh unit` runs the unit tests on the pure-Python backend. The image test: fetch-wheels refusals, build without network, `init`/`restore` refusals in five ways with no word in any log, `init` by `exec -it`, a start/derive/fund/sweep/sweep_native round, 40 gzip bodies without the token under a 300 MB limit, the backend proof, the high-water refusal, the pure-Python backend refusal, and what the image does not contain.
