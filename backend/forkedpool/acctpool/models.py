"""Tables of the acctpool plugin (SPEC-v4 4.3). The migration in versions/ makes them; a test compares the two.

Class names carry the Acctpool prefix: Bitcart keeps every model in one registry keyed by the class name.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import TIMESTAMP, BigInteger, Boolean, Identity, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models import Model

AMOUNT = Numeric(78, 18)  # coin units
WEI = Numeric(78, 0)
TS = TIMESTAMP(timezone=True)


class AcctpoolPool(Model):
    __tablename__ = "plugin_acctpool_pools"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    wallet_id: Mapped[str] = mapped_column(Text, unique=True)  # Bitcart wallets.id
    chain: Mapped[str] = mapped_column(Text)
    asset: Mapped[str] = mapped_column(Text)
    store: Mapped[str] = mapped_column(Text)  # store key of the signer config
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    min_withdraw: Mapped[Decimal] = mapped_column(AMOUNT, server_default=text("0"))


class AcctpoolAddress(Model):
    __tablename__ = "plugin_acctpool_addresses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    store: Mapped[str] = mapped_column(Text)
    index: Mapped[int] = mapped_column(Integer)
    address: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'ready'"))
    invoice_id: Mapped[str | None] = mapped_column(Text)
    assigned_at: Mapped[datetime | None] = mapped_column(TS)
    retired_at: Mapped[datetime | None] = mapped_column(TS)
    watch_until: Mapped[datetime | None] = mapped_column(TS)
    withdraw_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    checked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))  # re-derived after allocation


class AcctpoolDeposit(Model):
    __tablename__ = "plugin_acctpool_deposits"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    address_id: Mapped[int] = mapped_column(BigInteger)
    chain: Mapped[str] = mapped_column(Text)
    asset: Mapped[str] = mapped_column(Text)
    tx_hash: Mapped[str] = mapped_column(Text)  # or balance:<chain>:<address>:<asset>:<n>
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    from_address: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)  # event | balance
    height: Mapped[int | None] = mapped_column(BigInteger)  # source=balance: provider head of the balance reading
    invoice_id: Mapped[str | None] = mapped_column(Text)
    credited: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    late: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class AcctpoolPayout(Model):
    __tablename__ = "plugin_acctpool_payouts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    address_id: Mapped[int] = mapped_column(BigInteger)
    chain: Mapped[str] = mapped_column(Text)
    asset: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # fund | sweep | sweep_native
    state: Mapped[str] = mapped_column(Text, server_default=text("'planned'"))
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    nonce: Mapped[int] = mapped_column(BigInteger)
    gas_limit: Mapped[int] = mapped_column(BigInteger)
    max_fee_wei: Mapped[Decimal] = mapped_column(WEI)
    value: Mapped[Decimal] = mapped_column(WEI)  # fund, sweep_native: wei; sweep: token base units
    raw_tx: Mapped[str | None] = mapped_column(Text)
    tx_hash: Mapped[str | None] = mapped_column(Text)
    replaces_id: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    created: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    updated: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class AcctpoolBalance(Model):
    __tablename__ = "plugin_acctpool_balances"

    address_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chain: Mapped[str] = mapped_column(Text, primary_key=True)
    asset: Mapped[str] = mapped_column(Text, primary_key=True)
    # the sum of what our own mined transactions changed (receipts): balance = baseline + all deposits
    baseline: Mapped[Decimal] = mapped_column(AMOUNT)


class AcctpoolEvent(Model):
    __tablename__ = "plugin_acctpool_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    chain: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    created: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class AcctpoolState(Model):
    __tablename__ = "plugin_acctpool_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated: Mapped[datetime] = mapped_column(TS, server_default=func.now())
