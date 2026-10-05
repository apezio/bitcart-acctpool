# acctpool signer (v4): what a hostile caller can do

Attackers: (1) a hostile worker: it has the token, sends any request in any order, and decides what is broadcast; (2) a caller without the token on the internal network; (3) a party that can read the volume, a backup or the logs; (4) a party that can write the volume. Not here: root on the host (accepted in PLAN.md). No attacker can change `pools.toml` or read the master key.

Short result for the hostile worker: it cannot get a key and cannot send tokens or native coin to an address that is not pinned in `pools.toml`. It can waste the fee wallet up to the daily cap per chain (and give that waste to a block producer as fee), give up to `native_max_fee_share_percent` (10%) of a native deposit to a block producer, use native coin of a customer as token-sweep fee up to the fundings signed for that address, and block payouts until the operator acts.

## What no request can do

- Name a destination, a contract, a chain id, a gas limit for a funding, or data: an unknown field is refused (`test_fund_has_no_field_for_a_destination_or_data`, `test_sweep_has_no_field_for_a_destination_contract_chain_id_or_data`, `test_native_sweep_has_no_field_for_a_destination_or_data`).
- Select a path: it is the store account (config) and an index given out by `/v1/derive`. Account 9000 (fee wallet) cannot be a store.
- Sign a message, a legacy transaction, a contract creation, an approval, or value and data together: three code paths build every field.
- Read a key, the seed, the entropy or the master key: no call returns them, no log line has them, an internal error is logged as type and code position only (`test_secrets.py`).

## Worst outcome per call

- `status`, `derive`: public data. `derive` can raise the highest index by 200 per call, 10 calls a minute (the index rule is then weaker; it gives no money).
- `sign/fund`: fee wallet coin to deposit addresses of the seed, up to `fee_wallet_daily_cap_wei` per chain and UTC day (two days around midnight). The coin can only be swept on to the pinned native destination. A signature counts when signed, not when broadcast: using the caps without broadcasting blocks honest fundings until the next day; nothing is lost. A signed transaction has no expiry: keep the fee wallet balance small; after a compromise move the fee wallet and rotate the token. The day of a request is taken when its body is there (review B L1).
- `sign/sweep`: tokens only to the pinned destination of that store and chain, from the pinned USDT contract (or a `recovery_tokens` contract; keep that list empty). Early or split payouts cost gas inside the fee budget.
- `sign/sweep_native`: coin only to the pinned native destination; the fee is at most the share of value + fee, so every transaction that can be mined gives at least 90% to the destination (`test_a_native_sweep_cannot_sign_a_deposit_away_as_fee`).
- Nonce taken: a signature of the next nonce that is not broadcast blocks that nonce (409) until a replacement names it (key in the audit log) or the operator broadcasts the stored one from the journal.

Fee budget (SPEC 7.8, 7.9, review B L4): the budget is the SIGNED funding, not the one that arrived: a funding that is not broadcast lets token sweeps pay fees with a customer's native coin, bounded by `max_fund_total_per_address_wei` per address and the daily cap per chain. A native sweep signed before a funding does not reduce that funding's budget. Tests: `test_api_budget.py`, `test_value_bound_holds_in_any_order`, `test_evm_native_sweep_signed_before_the_funding_the_value_bound_holds`.

Replacements: 10% above the highest fee of the nonce (0 cannot replace 0), same kind/chain/store/index/nonce/sender/token (funding: same value); all caps apply; caps count only the difference. A replay signs nothing. After a destination change, old signed sweeps to the old destination stay valid while their nonce is free.

Rate limits: 30 sign, 10 derive, 120 status calls a minute; they survive a restart (from the audit log).

## A caller without the token

It gets 401 for every request. Its body is not read, no body is ever decompressed (a request with `Content-Encoding` is refused), and it writes no audit line: only one JSON line on stderr per request (method, path, status, remote), so it cannot fill the volume; the container log driver needs a size limit. 40 connections with 30 MB gzip bodies under a 300 MB memory limit: the signer runs (image test). The token compare is `hmac.compare_digest`.

What it can still do (accepted in SPEC-v4 3, which replaced the custom connection layer of review A F02 with stock aiohttp; the only peer on the `internal: true` network is the worker): hold many idle connections and use up file descriptors, or send a flood of requests, so that the worker waits. Nothing is signed and no rule changes.

## Risks that stay

- Signing: libsecp256k1 (`coincurve`), refused start on the pure-Python backend; RFC 6979 nonces (the image test compares with pure Python). Key derivation and address code are not constant-time; not measured.
- Restart or kill: no rule is lost (journal); a signature in work did not leave the process.
- Disk: each call with the token that the rate limits let through (not status) writes an audit line (~300 bytes) and each signature a journal row (~1 kB): at most about 60 MB a day (30 signatures and 10 derive calls a minute). A call that a rate limit refuses is only counted: the first refusal of each minute of a limit gets a line (at most 3 lines a minute, about 1.3 MB a day, however many calls come), the count of the others goes to stderr. No rotation. Full volume: 500, nothing signed, the process stays, a start is refused, no repair when there is space.
- A party that can WRITE the volume: the start refuses a missing, older, changed or foreign journal or `audit.log` (they must agree, the log at most one line behind), a journal of another seed, a second process, and with the high-water file a volume below the probe's mark. It can still restore an older full volume when there is no high-water file, or lose the signatures of the last probe interval (10 minutes or less). It cannot get a key; it can remove the keystore (the signer then signs nothing).
- A party that can READ the volume or a backup: every signed transaction is in the journal; it can broadcast them (pinned addresses only, already counted in the caps). The keystore is encrypted.
- The internal network: token and signed transactions are clear text; with the token a party is a hostile worker.
- Memory: master key, seed and words stay in process memory; locked only with `memlock: -1`; no core dumps, not dumpable.
- Seed words at `init`: refused as a main process in five ways (review A F01, review B M2); the signer cannot know if the terminal is recorded.
- A chain name: v4 does not record the chain id of a name in the journal (SPEC-v4 deleted `check_chains`). Renaming a chain in `pools.toml` starts its totals at zero; changing its chain id keeps them. Do not rename chains.
