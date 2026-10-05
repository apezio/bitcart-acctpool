"""acctpool v4 tables (SPEC-v4 4.3)

Revision ID: acctpool0001
Revises:
Create Date: 2026-09-30

Only tables with the prefix plugin_acctpool_ are made. No stock table is read or changed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "acctpool0001"
down_revision = None
branch_labels = None
depends_on = None

AMOUNT = sa.Numeric(78, 18)
WEI = sa.Numeric(78, 0)
TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("now()")
TABLES = ("pools", "addresses", "deposits", "payouts", "balances", "events", "state")


def col(name, kind, nullable=False, default=None):
    return sa.Column(name, kind, nullable=nullable, server_default=sa.text(default) if default else None)


def ident():
    return sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "plugin_acctpool_pools",
        ident(),
        col("wallet_id", sa.Text()),
        col("chain", sa.Text()),
        col("asset", sa.Text()),
        col("store", sa.Text()),
        col("enabled", sa.Boolean(), default="false"),
        col("min_withdraw", AMOUNT, default="0"),
        sa.UniqueConstraint("wallet_id"),
        sa.UniqueConstraint("store", "chain", "asset"),
        sa.CheckConstraint("asset IN ('usdt', 'native')", name="plugin_acctpool_pools_asset"),
    )
    op.create_table(
        "plugin_acctpool_addresses",
        ident(),
        col("store", sa.Text()),
        col("index", sa.Integer()),
        col("address", sa.Text()),
        col("status", sa.Text(), default="'ready'"),
        col("invoice_id", sa.Text(), True),
        col("assigned_at", TS, True),
        col("retired_at", TS, True),
        col("watch_until", TS, True),
        col("withdraw_requested", sa.Boolean(), default="false"),
        col("checked", sa.Boolean(), default="false"),
        sa.UniqueConstraint("address"),
        sa.UniqueConstraint("store", "index"),
        sa.CheckConstraint(
            "status IN ('ready', 'in_invoice', 'pending_payout', 'in_payout', 'retired')",
            name="plugin_acctpool_addresses_status",
        ),
    )
    op.create_index("plugin_acctpool_addresses_ready", "plugin_acctpool_addresses", ["store", "id"],
                    postgresql_where=sa.text("status = 'ready'"))  # fmt: skip
    op.create_index("plugin_acctpool_addresses_lower", "plugin_acctpool_addresses", [sa.text("lower(address)")])
    op.create_index("plugin_acctpool_addresses_invoice", "plugin_acctpool_addresses", ["invoice_id"], unique=True)
    op.create_table(
        "plugin_acctpool_deposits",
        ident(),
        col("address_id", sa.BigInteger()),
        col("chain", sa.Text()),
        col("asset", sa.Text()),
        col("tx_hash", sa.Text()),
        col("amount", AMOUNT),
        col("from_address", sa.Text(), True),
        col("source", sa.Text()),
        col("height", sa.BigInteger(), True),
        col("invoice_id", sa.Text(), True),
        col("credited", sa.Boolean(), default="false"),
        col("late", sa.Boolean(), default="false"),
        col("created", TS, default="now()"),
        sa.UniqueConstraint("chain", "tx_hash", "address_id", "asset"),
    )
    op.create_index("plugin_acctpool_deposits_address", "plugin_acctpool_deposits", ["address_id", "chain"])
    op.create_table(
        "plugin_acctpool_payouts",
        ident(),
        col("address_id", sa.BigInteger()),
        col("chain", sa.Text()),
        col("asset", sa.Text()),
        col("kind", sa.Text()),
        col("state", sa.Text(), default="'planned'"),
        col("idempotency_key", sa.Text()),
        col("nonce", sa.BigInteger()),
        col("gas_limit", sa.BigInteger()),
        col("max_fee_wei", WEI),
        col("value", WEI),
        col("raw_tx", sa.Text(), True),
        col("tx_hash", sa.Text(), True),
        col("replaces_id", sa.BigInteger(), True),
        col("attempts", sa.Integer(), default="0"),
        col("error", sa.Text(), True),
        col("created", TS, default="now()"),
        col("updated", TS, default="now()"),
        sa.UniqueConstraint("idempotency_key"),
        sa.CheckConstraint("kind IN ('fund', 'sweep', 'sweep_native')", name="plugin_acctpool_payouts_kind"),
        sa.CheckConstraint(
            "state IN ('planned', 'signed', 'broadcast', 'confirmed', 'failed')", name="plugin_acctpool_payouts_state"
        ),
    )
    # one open transaction per address and chain (the nonces of the address), one open funding per chain (the
    # nonces of the fee wallet), and one open replacement per transaction (its original stays open until it is sent)
    open_state = "state IN ('planned', 'signed', 'broadcast')"
    op.create_index("plugin_acctpool_payouts_open", "plugin_acctpool_payouts", ["address_id", "chain"], unique=True,
                    postgresql_where=sa.text(f"kind <> 'fund' AND replaces_id IS NULL AND {open_state}"))  # fmt: skip
    op.create_index("plugin_acctpool_payouts_fund", "plugin_acctpool_payouts", ["chain"], unique=True,
                    postgresql_where=sa.text(f"kind = 'fund' AND replaces_id IS NULL AND {open_state}"))  # fmt: skip
    op.create_index("plugin_acctpool_payouts_replaces", "plugin_acctpool_payouts", ["replaces_id"], unique=True,
                    postgresql_where=sa.text(open_state))  # fmt: skip
    op.create_table(
        "plugin_acctpool_balances",
        col("address_id", sa.BigInteger()),
        col("chain", sa.Text()),
        col("asset", sa.Text()),
        col("baseline", AMOUNT),
        sa.PrimaryKeyConstraint("address_id", "chain", "asset"),
    )
    op.create_table(
        "plugin_acctpool_events",
        ident(),
        col("kind", sa.Text()),
        col("chain", sa.Text(), True),
        col("address", sa.Text(), True),
        col("detail", postgresql.JSONB(), default="'{}'"),
        col("created", TS, default="now()"),
    )
    op.create_index("plugin_acctpool_events_kind", "plugin_acctpool_events", ["kind", "created"])
    op.create_table(
        "plugin_acctpool_state",
        col("key", sa.Text()),
        col("value", postgresql.JSONB()),
        col("updated", TS, default="now()"),
        sa.PrimaryKeyConstraint("key"),
    )
    # Money rules in the database too: an address row is never deleted, keeps its identity, and never goes back
    # to 'ready' (a used address is never given out again). The event log is append only.
    op.execute(
        """
        CREATE FUNCTION plugin_acctpool_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR TG_TABLE_NAME = 'plugin_acctpool_events' THEN
                RAISE EXCEPTION 'acctpool: % of % is not allowed', TG_OP, TG_TABLE_NAME;
            END IF;
            IF NEW.store <> OLD.store OR NEW.index <> OLD.index OR NEW.address <> OLD.address
               OR (NEW.status = 'ready' AND OLD.status <> 'ready')
               OR (OLD.invoice_id IS NOT NULL AND NEW.invoice_id IS DISTINCT FROM OLD.invoice_id) THEN
                RAISE EXCEPTION 'acctpool: this change of an address row is not allowed';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("addresses", "events"):
        op.execute(
            f"CREATE TRIGGER plugin_acctpool_{table}_guard BEFORE UPDATE OR DELETE ON plugin_acctpool_{table}"
            " FOR EACH ROW EXECUTE FUNCTION plugin_acctpool_guard()"
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(f"plugin_acctpool_{table}")
    op.execute("DROP FUNCTION plugin_acctpool_guard()")
