# v4 integration test (SPEC-v4 5)

`scripts/test-integration.sh` on your workstation runs it on the test host (`$TEST_HOST`, rootless podman, prefix `v4i-`).

Real parts: Bitcart 0.10.3.0 backend and worker with the plugin (stock loader, production mode), the STOCK
`bitcart/bitcart-eth:0.10.3.0` daemon, anvil (chain id 31337, a block per second), postgres + redis, the signer
image built from `git archive <commit> signer`. Test parts: `proxy.py` in front of anvil (the daemon's provider,
which can block broadcasts, and the second-opinion provider, which can lie or be down), `TestUSDT.sol` (6
decimals, `transfer` returns nothing), and `driver.py` (customer, admin and block producer; no private key).

Flows: `usdt` (exact USDT, credited by the plugin's own `new_transaction` handler in the real worker, fund +
sweep), `native` (one `sweep_native`), `expiry` (part payment that expires and is swept; a payment after expiry
is late and swept), `two-at-once`, `daemon-restart` (payment while the daemon is stopped is credited by the
reconcile; a new payment to the old address comes as an event again: the wallets were reloaded on
check_pending), `worker-kill` (broadcast blocked, funding stays `signed`, SIGKILL of the worker, the new worker
broadcasts the same bytes: one funding, no replacement), `second-opinion` (lie, then down: no signature, event;
then it agrees and the payout runs). `worker-restart` (SIGKILL of the worker, a new one at once; when it listens on Bitcart's task channel, a
pool invoice is made and paid at once and paid out; the only wait is that stock condition, see SPEC-v4 section 1).

Cleanup: a trap removes every `v4i-` container, network, volume, the signer image and the work folder, and the
script checks it (`--keep` leaves them; `--clean` removes them).
