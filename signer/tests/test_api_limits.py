"""Rate limits (SPEC 7.6): max_signatures_per_minute for the sum of the sign calls, max_derive_per_minute."""

import time

import pytest
from conftest import CONFIG, NO_LIMITS, fund_request, make_api, prefund_all

LOW = "max_signatures_per_minute = 5\nmax_derive_per_minute = 3"


@pytest.fixture
async def low(env, aiohttp_client):
    """Signer with 5 sign calls and 3 derive calls for each minute. One derive call is used here."""
    env.write_config(CONFIG.replace(NO_LIMITS, LOW))
    api = await make_api(env, aiohttp_client)
    status, _ = await api.derive()
    assert status == 200
    return prefund_all(api)


async def limited(api, call, *args, **kwargs):
    """The call is refused by the limit: 429, no signature, no journal row. Its audit line (only the first refusal of
    each 60 seconds of a limit gets one) or None."""
    rows, signed, lines = api.journal_rows(), len(api.transactions), len(api.audit_lines())
    status, result = await call(*args, **kwargs)
    assert (status, result["error"]) == (429, "rate_limited"), result
    assert set(result) == {"error", "detail"}
    assert api.journal_rows() == rows
    assert len(api.transactions) == signed
    new = api.audit_lines()[lines:]
    assert len(new) <= 1 and all(line["result"] == "rate_limited" for line in new), new
    return new[0] if new else None


async def test_limit_is_for_the_sum_of_the_sign_calls(low, env):
    # (the native sweep uses the fee budget of the address up; the funding after it has the fee of the last sweep)
    calls = [
        (low.fund, "limit-sum-1", {"nonce": 0}),
        (low.sweep, "limit-sum-2", {"nonce": 0}),
        (low.sweep_native, "limit-sum-3", {"nonce": 1}),
        (low.fund, "limit-sum-4", {"nonce": 1, "value_wei": str(5 * 10**15)}),
        (low.sweep, "limit-sum-5", {"nonce": 2}),
    ]
    for call, key, changes in calls:
        status, result = await call(key, **changes)
        assert status == 200, result
    assert (await limited(low, low.fund, "limit-sum-6", nonce=2))["call"] == "sign/fund"
    assert await limited(low, low.sweep, "limit-sum-7", nonce=3) is None  # counted, no second line in the minute
    assert await limited(low, low.sweep_native, "limit-sum-8", nonce=3) is None
    assert low.journal_rows() == 5
    # the other limit and the status call are not changed by this
    status, _ = await low.derive("shop", 20, 1)
    assert status == 200
    status, _ = await low.get("/v1/status")
    assert status == 200


async def test_refusals_without_a_line_are_counted_for_their_minute(low, env, caplog):
    """R1: the refusals after the audited one of a minute get no line; their number goes to stderr when that minute
    is over (with the next call of any kind), and at the stop, never for another minute."""
    for nonce in range(5):
        assert (await low.fund(f"limit-count-{nonce}", nonce=nonce))[0] == 200
    assert await limited(low, low.fund, "limit-count-5", nonce=5) is not None
    for _ in range(3):
        assert await limited(low, low.fund, "limit-count-5", nonce=5) is None
    env.clock.advance(30)
    assert (await low.get("/v1/status"))[0] == 200
    assert "rate_limited" not in caplog.text  # its minute is not over
    env.clock.advance(31)
    assert (await low.get("/v1/status"))[0] == 200
    assert "3 more rate_limited calls in the 60 s of the audited one, 61 s ago" in caplog.text
    caplog.clear()
    for nonce in range(5, 10):  # a new minute: its places, then a new audited refusal and one without a line
        assert (await low.fund(f"limit-count-{nonce}", nonce=nonce))[0] == 200
    assert await limited(low, low.fund, "limit-count-10", nonce=10) is not None
    assert await limited(low, low.fund, "limit-count-10", nonce=10) is None
    for limit in low.state.limits.values():
        limit.report(env.clock.monotonic(), stop=True)
    assert "1 more rate_limited calls in the 60 s of the audited one" in caplog.text


async def test_replay_and_refused_calls_use_a_place(low, env):
    status, first = await low.fund("limit-replay-1")
    assert status == 200
    status, again = await low.fund("limit-replay-1")
    assert (status, again["tx_hash"]) == (200, first["tx_hash"])
    status, result = await low.fund("limit-replay-2", nonce=1, value_wei=str(10**30))
    assert (status, result["error"]) == (403, "cap_exceeded")
    status, result = await low.sweep("limit-replay-3", to="0x00000000000000000000000000000000000000ee")
    assert (status, result["error"]) == (400, "invalid_request")
    status, result = await low.post("/v1/sign/sweep_native", raw=b"{not json")
    assert (status, result["error"]) == (400, "invalid_request")
    await limited(low, low.fund, "limit-replay-1")  # a replay too
    await limited(low, low.fund, "limit-replay-4", nonce=1)
    assert low.journal_rows() == 1


async def test_call_that_the_limit_refused_uses_no_place(low, env):
    for nonce in range(5):
        status, _ = await low.fund(f"limit-free-{nonce}", nonce=nonce)
        assert status == 200
    for _ in range(20):
        await limited(low, low.fund, "limit-free-x", nonce=5)
    # 60 seconds after the 5 accepted calls, 5 places are free again; the 20 refused calls did not move that time
    env.clock.seconds += 59.9
    await limited(low, low.fund, "limit-free-x", nonce=5)
    env.clock.seconds += 0.1
    for nonce in range(5, 10):
        status, _ = await low.fund(f"limit-free-{nonce}", nonce=nonce)
        assert status == 200
    await limited(low, low.fund, "limit-free-x", nonce=10)


async def test_a_flood_of_refused_calls_writes_one_audit_line_a_minute(low, env):
    """S1: a caller with the token must not fill the volume with audit lines of refused calls (two fsyncs each)."""
    for nonce in range(5):
        assert (await low.fund(f"limit-flood-{nonce}", nonce=nonce))[0] == 200
    before = len(low.audit_lines())
    for _ in range(1000):
        await limited(low, low.fund, "limit-flood-x", nonce=5)
    assert [line["result"] for line in low.audit_lines()[before:]] == ["rate_limited"]
    env.clock.seconds += 30  # still refused, and still in the minute of the line
    assert await limited(low, low.fund, "limit-flood-x", nonce=5) is None
    env.clock.seconds += 30  # the 5 places are free again; the next refusal is in a new minute
    for nonce in range(5, 10):
        assert (await low.fund(f"limit-flood-{nonce}", nonce=nonce))[0] == 200
    assert (await limited(low, low.fund, "limit-flood-y", nonce=10))["call"] == "sign/fund"


async def test_window_moves_with_the_time(low, env):
    start = env.clock.seconds
    for nonce in range(3):
        assert (await low.fund(f"limit-window-{nonce}", nonce=nonce))[0] == 200
    env.clock.seconds = start + 30
    for nonce in range(3, 5):
        assert (await low.fund(f"limit-window-{nonce}", nonce=nonce))[0] == 200
    await limited(low, low.fund, "limit-window-x", nonce=5)
    # at 60 s the first 3 calls are out of the window, the 2 calls of second 30 are in it
    env.clock.seconds = start + 60
    for nonce in range(5, 8):
        assert (await low.fund(f"limit-window-{nonce}", nonce=nonce))[0] == 200
    await limited(low, low.fund, "limit-window-x", nonce=8)
    env.clock.seconds = start + 90
    for nonce in range(8, 10):
        assert (await low.fund(f"limit-window-{nonce}", nonce=nonce))[0] == 200
    await limited(low, low.fund, "limit-window-x", nonce=10)


async def test_derive_has_its_own_limit(low, env):
    # the fixture used 1 of 3
    assert (await low.derive("shop", 0, 5))[0] == 200
    status, result = await low.derive("shop", 500, 1)  # refused for the gap; it uses a place
    assert (status, result["error"]) == (400, "invalid_request")
    line = await limited(low, low.derive, "shop", 20, 1)
    assert line["call"] == "derive"
    assert low.state.journal.highest_index("shop") == 19
    # sign calls have their own places
    for nonce in range(5):
        assert (await low.fund(f"limit-derive-{nonce}", nonce=nonce))[0] == 200
    env.clock.seconds += 60
    assert (await low.derive("shop", 20, 200))[0] == 200


async def test_with_the_limit_a_caller_can_add_600_indexes_in_a_minute_at_most(low, env):
    env.clock.seconds += 60
    first = 20
    for _ in range(10):
        status, result = await low.derive("shop", first, 200)
        if status == 200:
            first += 200
    assert low.state.journal.highest_index("shop") == 19 + 3 * 200


async def test_status_has_the_limit_of_the_other_calls(env, aiohttp_client):
    """SPEC 7.8: every call with a correct token that reaches a handler counts; status: 120 in a minute (default).
    v4: an unknown path or a wrong method never reaches a handler; it has no effect but the request log line."""
    env.write_config(CONFIG.replace(NO_LIMITS, ""))
    api = await make_api(env, aiohttp_client)
    assert api.state.config.max_other_per_minute == 120
    before = len(api.audit_lines())
    assert [(await api.get("/v1/status"))[0] for _ in range(121)] == [200] * 120 + [429]
    assert (await api.get("/v1/nothing"))[0] == 404
    assert (await api.call("PUT", "/v1/sign/fund", None))[0] == 405
    # the sign calls and derive have their own places
    api.prefund("anvil", "shop", [3])
    assert (await api.derive())[0] == 200
    assert (await api.fund("limit-other-1"))[0] == 200
    env.clock.advance(60)
    assert (await api.get("/v1/status"))[0] == 200
    assert [line["result"] for line in api.audit_lines()[before:]] == ["rate_limited", "ok", "ok"]


async def test_limits_are_not_empty_after_a_restart(env, aiohttp_client):
    """The calls of the last 60 seconds are in the journal. A restart (or a kill) does not give new places."""
    env.write_config(CONFIG.replace(NO_LIMITS, LOW))
    first = prefund_all(await make_api(env, aiohttp_client))
    assert (await first.derive())[0] == 200
    for nonce in range(5):
        assert (await first.fund(f"limit-restart-{nonce}", nonce=nonce))[0] == 200
    await limited(first, first.fund, "limit-restart-x", nonce=5)
    first.close()

    second = await make_api(env, aiohttp_client)
    await limited(second, second.fund, "limit-restart-x", nonce=5)
    await limited(second, second.sweep, "limit-restart-y", nonce=0)
    assert (await second.derive())[0] == 200
    assert (await second.derive())[0] == 200
    await limited(second, second.derive)
    env.clock.advance(60)
    assert (await second.fund("limit-restart-5", nonce=5))[0] == 200


async def test_call_without_the_token_uses_no_place(low, env):
    for _ in range(10):
        status, result = await low.post(
            "/v1/sign/fund", fund_request("limit-auth-1"), token="wrong-token-wrong-token-wrong-token-xx"
        )
        assert (status, result["error"]) == (401, "unauthorized")
        status, result = await low.post(
            "/v1/derive", {"store": "shop", "family": "evm", "first_index": 0, "count": 1}, token=None
        )
        assert (status, result["error"]) == (401, "unauthorized")
    for nonce in range(5):
        assert (await low.fund(f"limit-auth-{nonce}", nonce=nonce))[0] == 200
    assert (await low.derive("shop", 0, 1))[0] == 200


async def test_default_limits_are_30_and_10(env, aiohttp_client):
    env.write_config(CONFIG.replace(NO_LIMITS, ""))
    api = prefund_all(await make_api(env, aiohttp_client))
    assert (api.state.config.max_signatures_per_minute, api.state.config.max_derive_per_minute) == (30, 10)
    for number in range(10):
        assert (await api.derive("shop", 0, 20))[0] == 200, number
    await limited(api, api.derive, "shop", 0, 20)
    for nonce in range(30):
        call = (api.fund, api.sweep, api.sweep_native)[nonce % 3]
        # value of the funding: the fee of the token sweep that comes after it
        changes = {"value_wei": str(5 * 10**15)} if call == api.fund else {}
        assert (await call(f"limit-default-{nonce}", nonce=nonce, **changes))[0] == 200, nonce
    await limited(api, api.fund, "limit-default-x", nonce=30)
    await limited(api, api.sweep, "limit-default-x", nonce=30)
    assert api.journal_rows() == 30


def test_limits_use_a_clock_that_cannot_go_back(env):
    from acctpool_signer.__main__ import build_state
    from acctpool_signer.api import RateLimit

    state = build_state(env.paths)
    assert state.monotonic is time.monotonic
    state.close()
    limit = RateLimit(2)
    assert [limit.allow(now) for now in (10.0, 10.0, 10.0, 69.9, 70.0, 70.0, 70.0, 130.0)] == [
        True, True, False, False, True, True, False, True,
    ]  # fmt: skip
