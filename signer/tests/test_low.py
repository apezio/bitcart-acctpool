"""The low findings of review A that have no proof test: config and journal rules at the start, one time for a request."""

import sqlite3
from datetime import UTC, datetime

import pytest
from conftest import CONFIG, DEST_SHOP_ANVIL, NATIVE_SHOP_ANVIL, make_api

from acctpool_signer.__main__ import build_state
from acctpool_signer.errors import JournalError, StartError


def start(env, config: str):
    env.write_config(config)
    return build_state(env.paths)


def test_destination_that_is_the_fee_wallet_is_refused(env):
    for config in (CONFIG.replace(DEST_SHOP_ANVIL, env.fee_address), CONFIG.replace(NATIVE_SHOP_ANVIL, env.fee_address)):
        with pytest.raises(StartError) as info:
            start(env, config)
        assert "is the fee wallet of this signer" in str(info.value)
    start(env, CONFIG).close()


def test_journal_of_another_build_is_refused(env):
    start(env, CONFIG).close()
    db = sqlite3.connect(env.paths.journal)
    assert db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone() == ("v4",)
    db.execute("UPDATE meta SET value = '2' WHERE key = 'schema'")
    db.commit()
    db.close()
    with pytest.raises(JournalError) as info:
        start(env, CONFIG)
    assert "schema 2" in str(info.value)
    # the refused start gave the lock back
    db = sqlite3.connect(env.paths.journal)
    db.execute("UPDATE meta SET value = 'v4' WHERE key = 'schema'")
    db.commit()
    db.close()
    start(env, CONFIG).close()


async def test_one_time_value_for_a_request(env, aiohttp_client):
    """A request at midnight: the check of the daily cap and the record of the signature use the same day."""
    api = await make_api(env, aiohttp_client)
    assert (await api.derive())[0] == 200
    times = [datetime(2026, 9, 29, 23, 59, 59, 999000, tzinfo=UTC), datetime(2026, 9, 30, 0, 0, 0, 1000, tzinfo=UTC)]
    calls = []

    def clock() -> datetime:
        calls.append(1)
        return times[min(len(calls), 2) - 1]

    api.state.now = clock
    status, result = await api.fund("low-midnight-1")
    assert status == 200
    assert len(calls) == 1
    assert api.state.journal.get_signature("low-midnight-1")["utc_day"] == "2026-09-29"
    assert api.audit_lines()[-1]["ts"] == "2026-09-29T23:59:59.999000Z"
    assert api.state.journal.daily_spend("anvil", "2026-09-29") == 5 * 10**15 + 21000 * 60 * 10**9
    assert api.state.journal.daily_spend("anvil", "2026-09-30") == 0


async def test_signature_self_check_stops_a_wrong_signature(api, env, monkeypatch):
    """tx.sign recovers the sender from the signed bytes before they leave: a wrong result is a 500 and no journal row."""
    from acctpool_signer import tx

    monkeypatch.setattr(tx.Account, "recover_transaction", lambda raw: "0x" + "11" * 20)
    rows = api.journal_rows()
    status, result = await api.fund("low-selfcheck-1")
    assert (status, result["error"]) == (500, "internal")
    assert api.journal_rows() == rows and api.transactions == []
    monkeypatch.undo()
    assert (await api.fund("low-selfcheck-1"))[0] == 200
