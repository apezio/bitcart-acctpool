"""The plugin's own small connection pool. A hook runs inside a Bitcart request that already holds a connection of
Bitcart's pool; with the same pool, many invoices at once would wait for each other until the pool timeout."""

import hashlib
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.logging import get_exception_message, get_logger
from api.settings import Settings

from . import models
from .constants import DEFAULT_SETTINGS, STATE_SETTINGS

logger = get_logger(__name__)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = create_async_engine(
            str(settings.postgres_dsn),
            pool_size=2,
            max_overflow=8,
            pool_timeout=10,
            pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
            connect_args={"server_settings": {"application_name": f"{settings.ENV.value}.acctpool"}},
        )
        self.session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def close(self) -> None:
        await self.engine.dispose()

    async def set_state(self, key: str, value: dict[str, Any]) -> None:
        table = models.AcctpoolState
        async with self.session() as session, session.begin():
            await session.execute(
                insert(table)
                .values(key=key, value=value, updated=func.now())
                .on_conflict_do_update(index_elements=[table.key], set_={"value": value, "updated": func.now()})
            )

    async def get_state(self, key: str, max_age: float | None = None) -> dict[str, Any] | None:
        """The value, or None when missing or older than max_age seconds (clock of the database)."""
        table = models.AcctpoolState
        age = func.extract("epoch", func.now() - table.updated)
        async with self.session() as session:
            row = (await session.execute(select(table.value, age).where(table.key == key))).first()
        if row is None or (max_age is not None and float(row[1]) > max_age):
            return None
        return row[0]

    async def settings(self) -> dict[str, Any]:
        return {**DEFAULT_SETTINGS, **(await self.get_state(STATE_SETTINGS) or {})}

    async def event(
        self,
        kind: str,
        chain: str | None = None,
        address: str | None = None,
        detail: dict[str, Any] | None = None,
        every: float = 0,
    ) -> bool:
        """Append one event. every > 0: only when no event of this kind (and address) is newer than that many
        seconds (all processes; the table decides). An event that cannot be written never breaks the caller."""
        table = models.AcctpoolEvent
        try:
            async with self.session() as session, session.begin():
                if every:
                    await session.execute(select(func.pg_advisory_xact_lock(lock_key("event", kind))))
                    newest = (
                        await session.execute(
                            select(func.extract("epoch", func.now() - func.max(table.created)))
                            .where(table.kind == kind)
                            .where(table.address.is_(None) if address is None else table.address == address)
                        )
                    ).scalar()
                    if newest is not None and float(newest) < every:
                        return False
                session.add(table(kind=kind, chain=chain, address=address, detail=detail or {}))
            return True
        except Exception as e:
            logger.error(f"acctpool: could not write event {kind}: {get_exception_message(e)}")
            return False


def short_error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:300]


def lock_key(*parts: str) -> int:
    """Key for the Postgres advisory locks: a signed 64-bit number from the parts."""
    digest = hashlib.sha256("\x1f".join(("acctpool", *parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)
