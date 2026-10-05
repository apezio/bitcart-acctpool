# Signer volume: restore procedure

"Restore the volume from the backup" is not sufficient. The rules of the signer (nonces, per-address totals, fee budgets, daily spend, highest index) have their memory in the journal of the volume. A backup is older than the journal. A signer that starts from the backup alone forgets every signature after the backup, and a hostile worker that kept those signatures gets the caps a second time (review B, M1). The start refuses such a volume when the high-water file of the probe is there (signer README, "High-water file"). The v4 signer has NO journal rebuild: `journal.sqlite3` and `audit.log` are always restored together, from the same backup.

Short rule: "Keystore damaged or master key missing: the signer does not start. Put back `keystore.json` only (from the backup, or make it again from the offline seed with `restore`); do not put back the journal or `audit.log`. When the full volume must be restored, follow docs/VOLUME-RESTORE.md case 2: the start is refused while the backup is older than the high-water mark, and going on is a decision for the operator."

## Case 1: only the keystore is damaged

1. Stop the signer.
2. Put `keystore.json` of the backup into the volume (owner uid 10001, mode 600), or make it again with `docker exec -it <container> python -I -m acctpool_signer restore` in a signer that runs without a keystore.
3. Do not put back `journal.sqlite3` or `audit.log`. They are newer than the backup.
4. Start the signer. The start compares the seed id of the keystore with the seed id of the journal.

## Case 2: the full volume must be restored

1. Stop the worker first (`docker compose stop worker`): nothing may ask for a signature during the restore. Then stop the signer: `docker compose stop acctpool-signer`. Do not remove the signer container: its log (`json-file`, stdout) has the newest audit lines as evidence.
2. Keep the old volume: copy all its files to `/root/acctpool/restore/volume-before/` (mode 700). Do not delete a file.
3. Restore the volume from the backup: `keystore.json`, `journal.sqlite3` and `audit.log` of the SAME backup.
4. Start the signer: `docker compose up -d acctpool-signer`. When the start is refused with "audit.log ends with line K, and the high-water file ... has line M" (or "... does not agree with the high-water file"), the backup is older than what the probe saw: the lines K+1 to M are lost for the rules. Stop here; see the next section.
5. When the start works: `docker exec <container> python -I -m acctpool_signer audit-verify` gives exit code 0. Start the worker. The next run of the probe writes a new mark. A payout row of the worker whose signature is not in the restored journal gets its answer again only if the request is the same; otherwise the payout waits with `payout_failed` for an admin.

## When lines K+1 to M are lost

The start stays refused. Do not delete the high-water file and do not write a lower value into it to go around the refusal. That is a decision for the operator, with these facts:

- What is lost: the signatures between line K and line M (and after M, up to the failure). They are not in the rules any more. The plugin tables (`plugin_acctpool_payouts`: raw bytes, nonces, hashes) and the chains have the transactions of that time.
- A fee wallet nonce or a deposit address nonce of that time can be signed a second time, to another address of the seed. Only one transaction of a nonce can go into a block. The per-address funding totals, the fee budgets and the daily spend of that time are forgotten: a hostile worker could use them a second time, inside the caps of the config.
- The highest index is lower. The worker derives again from the new highest index + 1 before it signs for higher indexes.
- If the operator accepts this (signer README): the worker stays stopped; check on chain that the fee wallet and the deposit addresses have no pending transaction of the missing time; then root writes `0` and 64 zeros to the high-water file and starts the signer; the probe writes the new mark. Write the date, K, M and the reason into the operations log. Rotate the worker token when a compromise of the worker is possible. Then start the worker.
