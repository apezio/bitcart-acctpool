"""The plugin migration through the stock migration environment (migrations/env.py), on a database that has
the stock tables and no data."""

from typing import Any

import pytest
from conftest import PLUGIN_MIGRATION, STOCK_MIGRATION, admin_sql, run_python
from sqlalchemy import text

from api.db import create_async_engine
from api.settings import Settings

pytestmark = pytest.mark.anyio

DATABASE = "v4u_migration"

PLUGIN_TABLES = {
    "plugin_acctpool_pools",
    "plugin_acctpool_addresses",
    "plugin_acctpool_deposits",
    "plugin_acctpool_payouts",
    "plugin_acctpool_balances",
    "plugin_acctpool_events",
    "plugin_acctpool_state",
    "plugin_acctpool_alembic_version",
}

# everything that the database knows about objects that are not the plugin's
STOCK_STATE = {
    "columns": "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns"
    " WHERE table_schema = 'public' AND table_name NOT LIKE 'plugin\\_acctpool\\_%' ORDER BY 1, 2",
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
    " AND tablename NOT LIKE 'plugin\\_acctpool\\_%' ORDER BY 1, 2",
    "constraints": "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint"
    " WHERE connamespace = 'public'::regnamespace AND conrelid::regclass::text NOT LIKE 'plugin\\_acctpool\\_%'"
    " ORDER BY 1, 2",
    "triggers": "SELECT tgrelid::regclass::text, tgname FROM pg_trigger WHERE NOT tgisinternal"
    " AND tgrelid::regclass::text NOT LIKE 'plugin\\_acctpool\\_%' ORDER BY 1, 2",
    "functions": "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
    " AND proname NOT LIKE 'plugin\\_acctpool\\_%' ORDER BY 1",
    "stock_version": "SELECT version_num FROM alembic_version",
    "rows": "SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE relname NOT LIKE 'plugin\\_acctpool\\_%' ORDER BY 1",
}

CHECK = """
from alembic import command
from alembic.config import Config
config = Config("alembic.ini")
config.set_main_option("plugin_name", "acctpool")
config.set_main_option("version_locations", "modules/forkedpool/acctpool/versions")
config.set_main_option("no_logs", "true")
command.{call}
print("DONE")
"""


async def query(settings: Settings, statement: str) -> list[tuple[Any, ...]]:
    engine = create_async_engine(settings, "test", dsn=settings.build_postgres_dsn(db_name=DATABASE))
    async with engine.connect() as conn:
        result = [tuple(row) for row in (await conn.execute(text(statement))).all()]
    await engine.dispose()
    return result


async def stock_state(settings: Settings) -> dict[str, list[tuple[Any, ...]]]:
    return {name: await query(settings, statement) for name, statement in STOCK_STATE.items()}


async def tables(settings: Settings) -> set[str]:
    return {row[0] for row in await query(settings, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}


def run(code: str) -> str:
    done = run_python(code, DATABASE)
    assert done.returncode == 0, done.stderr[-3000:]
    return done.stdout


async def test_migration_on_empty_bitcart_database(settings: Settings) -> None:
    await admin_sql(settings, f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)", f"CREATE DATABASE {DATABASE}")
    run(STOCK_MIGRATION)
    stock_tables = await tables(settings)
    assert {"invoices", "paymentmethods", "wallets", "stores", "alembic_version"} <= stock_tables
    assert not {name for name in stock_tables if name.startswith("plugin_")}
    before = await stock_state(settings)

    # the stock loader finds the plugin, the stock registry runs the migration through the stock env.py
    assert "PLUGIN MIGRATION DONE" in run(PLUGIN_MIGRATION)

    assert await tables(settings) == stock_tables | PLUGIN_TABLES
    assert await query(settings, "SELECT version_num FROM plugin_acctpool_alembic_version") == [("acctpool0001",)]
    assert await stock_state(settings) == before

    # the models and the migration have the same columns, types and NULL rules
    from modules.forkedpool.acctpool import models

    columns = await query(
        settings,
        "SELECT table_name, column_name, is_nullable FROM information_schema.columns"
        " WHERE table_name LIKE 'plugin\\_acctpool\\_%' AND table_name <> 'plugin_acctpool_alembic_version'",
    )
    modelled = set()
    for model in (models.AcctpoolPool, models.AcctpoolAddress, models.AcctpoolDeposit, models.AcctpoolPayout,
                  models.AcctpoolBalance, models.AcctpoolEvent, models.AcctpoolState):  # fmt: skip
        for column in model.__table__.columns:
            modelled.add((model.__tablename__, column.name, "YES" if column.nullable else "NO"))
    assert set(columns) == modelled

    # a second start finds nothing to do
    assert "PLUGIN MIGRATION DONE" in run(PLUGIN_MIGRATION)
    assert await stock_state(settings) == before

    # down and up again: nothing of the plugin stays, nothing of the stock changes
    run(CHECK.format(call='downgrade(config, "base")'))
    assert await tables(settings) == stock_tables | {"plugin_acctpool_alembic_version"}
    assert await query(settings, "SELECT proname FROM pg_proc WHERE proname LIKE 'plugin\\_acctpool\\_%'") == []
    assert await stock_state(settings) == before
    run(PLUGIN_MIGRATION)
    assert await tables(settings) == stock_tables | PLUGIN_TABLES
    await admin_sql(settings, f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")


async def test_database_rules(bitcart: Any, signer: Any) -> None:
    """The money rules that the migration puts into the database itself."""
    import helpers
    from sqlalchemy.exc import DBAPIError

    await helpers.add_ready(bitcart, signer, 2)
    await helpers.sql(
        bitcart, "UPDATE plugin_acctpool_addresses SET status = 'in_invoice', invoice_id = 'inv1' WHERE index = 0"
    )
    for statement in (
        "UPDATE plugin_acctpool_addresses SET status = 'ready', invoice_id = NULL WHERE index = 0",
        "UPDATE plugin_acctpool_addresses SET invoice_id = 'inv2' WHERE index = 0",
        "UPDATE plugin_acctpool_addresses SET address = '0x' || repeat('9', 40) WHERE index = 1",
        "UPDATE plugin_acctpool_addresses SET index = 7 WHERE index = 1",
        "DELETE FROM plugin_acctpool_addresses WHERE index = 1",
        "INSERT INTO plugin_acctpool_addresses (store, index, address, status) VALUES ('s', 0, '0x01', 'free')",
        "INSERT INTO plugin_acctpool_addresses (store, index, address) SELECT 'other', 0, address"
        " FROM plugin_acctpool_addresses WHERE index = 1",
        "UPDATE plugin_acctpool_addresses SET invoice_id = 'inv1' WHERE index = 1",  # one address per invoice
    ):
        with pytest.raises(DBAPIError):
            await helpers.sql(bitcart, statement)
    # a late payment brings a retired address back to pending_payout; never to ready
    await helpers.sql(bitcart, "UPDATE plugin_acctpool_addresses SET status = 'retired' WHERE index = 0")
    await helpers.sql(bitcart, "UPDATE plugin_acctpool_addresses SET status = 'pending_payout' WHERE index = 0")
    # one open transaction per address and chain, one open funding per chain
    row = (
        "INSERT INTO plugin_acctpool_payouts (address_id, chain, asset, kind, idempotency_key, nonce, gas_limit,"
        " max_fee_wei, value)"
    )
    await helpers.sql(bitcart, row + " VALUES (1, 'anvil', 'usdt', 'sweep', 'k1', 0, 1, 1, 1)")
    await helpers.sql(bitcart, row + " VALUES (1, 'anvil', 'native', 'fund', 'k2', 0, 1, 1, 1)")
    for statement in (
        row + " VALUES (1, 'anvil', 'native', 'sweep_native', 'k3', 1, 1, 1, 1)",
        row + " VALUES (2, 'anvil', 'native', 'fund', 'k4', 1, 1, 1, 1)",
        row + " VALUES (2, 'anvil', 'native', 'sweep', 'k1', 1, 1, 1, 1)",
    ):
        with pytest.raises(DBAPIError):
            await helpers.sql(bitcart, statement)
    db = await bitcart.plugin.db()
    await db.event("test", None, None, {"n": 1})
    for statement in ("UPDATE plugin_acctpool_events SET kind = 'x'", "DELETE FROM plugin_acctpool_events"):
        with pytest.raises(DBAPIError, match="not allowed"):
            await helpers.sql(bitcart, statement)
