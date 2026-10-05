"""Unit tests of the acctpool plugin (SPEC-v4 5). They run inside docker.io/bitcart/bitcart:0.10.3.0 with the plugin
mounted at /app/modules/forkedpool (read-only, as in production), cwd /app. See scripts/test-backend.sh.

The daemon, the signer and the second-opinion provider are fakes in this process (fakes.py); postgres and redis
are real. Bitcart's load_plugins returns nothing when BITCART_ENV is "testing"; the tests do by hand what it does
(Bitcart's own load_module on the path that the stock glob finds) and the stock PluginRegistry does the rest.
"""

import os
import secrets
import subprocess
import sys
import tempfile

os.environ["BITCART_ENV"] = "testing"
os.environ["BITCART_CRYPTOS"] = "matic,eth,bnb"
os.environ["DB_DATABASE"] = "v4u"  # Bitcart adds _test in the testing environment
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ["MATIC_HOST"] = os.environ["ETH_HOST"] = os.environ["BNB_HOST"] = "127.0.0.1"
os.environ["MATIC_PORT"] = "15008"
os.environ["ETH_PORT"] = "15002"
os.environ["BNB_PORT"] = "15006"
os.environ["BITCART_DATADIR"] = tempfile.mkdtemp(prefix="v4u-data-")
os.environ["BITCART_BACKUPS_DIR"] = tempfile.mkdtemp(prefix="v4u-backups-")
# the anvil test chain on the eth daemon (constants.py), and the second-opinion file (secondcheck.py)
os.environ["ACCTPOOL_TEST_CHAIN"] = "31337:0x1111111111111111111111111111111111111111:6"
os.environ["ACCTPOOL_SECOND_OPINION"] = os.path.join(os.environ["BITCART_DATADIR"], "second.toml")

import glob
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from dishka import Provider, Scope, decorate, provide
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher
from sqlalchemy import text

from api.bootstrap import get_app
from api.db import create_async_engine
from api.ioc import build_container, setup_dishka
from api.logging import configure as configure_logging
from api.plugins import PluginObjects, load_module
from api.services.exchange_rate import ExchangeRateService
from api.services.plugin_registry import PluginRegistry
from api.settings import Settings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakes

PORTS = {"matic": 15008, "eth": 15002, "bnb": 15006}
with open(os.environ["ACCTPOOL_SECOND_OPINION"], "w") as second_file:
    for name, chain_id in (("polygon", 137), ("anvil", 31337), ("bnb", 56)):
        second_file.write(f'[{name}]\nurl = "http://127.0.0.1:17080/{chain_id}"\n')

PLUGIN_PATH = "modules/forkedpool/acctpool/plugin.py"
TEST_DB = "v4u_test"
TEMPLATE_DB = "v4u_test_template"
STORE_KEY = "teststore"


@pytest.fixture(scope="session")
def anyio_backend() -> tuple[str, dict[str, Any]]:
    return ("asyncio", {"use_uvloop": True})


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


def run_python(code: str, database: str, env_name: str = "production") -> subprocess.CompletedProcess[str]:
    """A new process, as the backend and the worker are: the stock code reads its settings from the environment."""
    env = {**os.environ, "BITCART_ENV": env_name, "DB_DATABASE": database}
    return subprocess.run([sys.executable, "-c", code], env=env, cwd="/app", capture_output=True, text=True, timeout=300)


STOCK_MIGRATION = """
from alembic import command
from alembic.config import Config
command.upgrade(Config("alembic.ini"), "head")
"""

# load_plugins + init_plugins + PluginRegistry.run_migrations: the stock path of a plugin migration
PLUGIN_MIGRATION = """
import asyncio
from api.plugins import init_plugins, load_plugins
from api.services.plugin_registry import PluginRegistry
from api.settings import Settings
classes, providers = load_plugins(Settings())
assert list(classes) == ["acctpool"], classes
plugin = init_plugins(classes)["acctpool"]
assert plugin.path == "modules/forkedpool/acctpool", plugin.path
asyncio.run(PluginRegistry.run_migrations(None, plugin))
print("PLUGIN MIGRATION DONE")
"""


async def admin_sql(settings: Settings, *statements: str) -> None:
    engine = create_async_engine(settings, "test", dsn=settings.build_postgres_dsn(db_name="postgres"))
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        for statement in statements:
            await conn.execute(text(statement))
    await engine.dispose()


def migrate(database: str) -> None:
    for code in (STOCK_MIGRATION, PLUGIN_MIGRATION):
        done = run_python(code, database)
        assert done.returncode == 0, done.stderr[-3000:]


@pytest.fixture(scope="session")
async def template_database(settings: Settings, anyio_backend: Any) -> None:
    """The test databases are made by the real migrations (stock, then plugin), not by create_all: the
    triggers and the partial indexes are part of what is tested."""
    await admin_sql(
        settings,
        f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)",
        f"DROP DATABASE IF EXISTS {TEMPLATE_DB} WITH (FORCE)",
        f"CREATE DATABASE {TEMPLATE_DB}",
    )
    migrate(TEMPLATE_DB)


class TestingProvider(Provider):
    @provide(scope=Scope.RUNTIME)
    def get_password_context(self) -> PasswordHash:
        return PasswordHash((BcryptHasher(rounds=4),))

    @decorate
    async def get_exchange_rate_service(self, service: ExchangeRateService) -> AsyncIterator[ExchangeRateService]:
        await service.init()
        yield service


@pytest.fixture(scope="session")
def plugin_module() -> Any:
    assert PLUGIN_PATH in glob.glob("modules/**/**/plugin.py")  # the stock loader finds it with this glob
    module = load_module(PLUGIN_PATH)
    load_module(PLUGIN_PATH.replace("plugin.py", "models.py"))
    module.Plugin.path = os.path.dirname(PLUGIN_PATH)
    return module


@pytest.fixture(scope="session")
def app(settings: Settings, plugin_module: Any) -> FastAPI:
    container = build_container(
        settings, extra_providers=(TestingProvider(),), include_plugins=False, start_scope=Scope.RUNTIME
    )
    app = get_app(settings)
    setup_dishka(container=container, app=app)
    configure_logging(settings=settings)
    app.state.root_container = container
    return app


@pytest.fixture(scope="session")
async def daemons(anyio_backend: Any) -> AsyncIterator[dict[str, fakes.FakeDaemon]]:
    result = {coin: fakes.FakeDaemon(coin) for coin in PORTS}
    runners = [await fakes.start(daemon.app, PORTS[coin]) for coin, daemon in result.items()]
    yield result
    for runner in runners:
        await runner.cleanup()


@pytest.fixture(scope="session")
async def second_server(anyio_backend: Any) -> AsyncIterator[fakes.SecondOpinion]:
    server = fakes.SecondOpinion({})
    runner = await fakes.start(server.app, 17080)
    yield server
    await runner.cleanup()


async def fake_rates(*args: Any, **kwargs: Any) -> Any:
    url = args[1]
    if "simple/supported_vs_currencies" in url:
        return ["usd", "eur"]
    if "coins/list" in url:
        return [
            {
                "id": "tether",
                "symbol": "usdt",
                "name": "Tether",
                "platforms": {"polygon-pos": fakes.USDT["matic"], "ethereum": fakes.USDT["eth"]},
            },
            {"id": "matic-network", "symbol": "matic", "name": "Polygon", "platforms": {}},
            {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "platforms": {}},
            {"id": "binancecoin", "symbol": "bnb", "name": "BNB", "platforms": {}},
        ]
    if "simple/price" in url:
        return {
            "tether": {"usd": 1, "eur": 0.9},
            "matic-network": {"usd": 0.5},
            "ethereum": {"usd": 2000},
            "binancecoin": {"usd": 500},
        }
    return {}


class Bitcart:
    """One test's Bitcart: the app, its DI container of the APP scope, and the plugin object in it."""

    def __init__(self, app: FastAPI, plugin_module: Any) -> None:
        self.app = app
        self.plugin_module = plugin_module
        self.scope: Any = None
        self.container: Any = None
        self.plugin: Any = None
        self.registry: PluginRegistry | None = None

    async def start(self, with_plugin: bool = True) -> None:
        objects: dict[str, Any] = {}
        if with_plugin:
            objects["acctpool"] = self.plugin_module.Plugin(self.plugin_module.Plugin.path)
        self.scope = self.app.state.root_container(scope=Scope.APP, context={PluginObjects: cast(PluginObjects, objects)})
        self.container = await self.scope.__aenter__()
        self.app.state.dishka_container = self.container
        self.plugin = objects.get("acctpool")
        self.registry = await self.container.get(PluginRegistry)
        # what api/bootstrap.py lifespan does at the start of the backend
        self.registry.setup_app(self.app)
        await self.registry.startup()
        if self.plugin is not None:
            await engine_alive(self)  # also after a new database (the crash tests make one per run)

    async def stop(self) -> None:
        if self.scope is None:
            return
        assert self.registry is not None
        await self.registry.shutdown()
        await self.scope.__aexit__(None, None, None)
        self.app.state.dishka_container = self.app.state.root_container
        self.scope = None

    async def restart(self, with_plugin: bool) -> None:
        """A new Bitcart process on the same database, with or without the plugin."""
        await self.stop()
        await self.start(with_plugin)


@pytest.fixture
async def bitcart(
    app: FastAPI,
    settings: Settings,
    plugin_module: Any,
    template_database: None,
    daemons: dict[str, fakes.FakeDaemon],
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: Any,
) -> AsyncIterator[Bitcart]:
    monkeypatch.setattr("api.ext.exchanges.coingecko.fetch_delayed", fake_rates)
    for daemon in daemons.values():  # a new chain for every test
        daemon.calls.clear()
        daemon.fail.clear()
        daemon.loaded.clear()
        daemon.delay.clear()
        daemon.chain = fakes.Chain(daemon.coin)
    await admin_sql(
        settings,
        f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)",
        f"CREATE DATABASE {TEST_DB} TEMPLATE {TEMPLATE_DB}",
    )
    result = Bitcart(app, plugin_module)
    await result.start()
    yield result
    await result.stop()


async def engine_alive(bitcart: Bitcart) -> None:
    """The state row of a running worker leader: no pool address is given out without it (SPEC-v4 4.2)."""
    db = await bitcart.plugin.db()
    await db.set_state("leader", {"worker": "test"})


@pytest.fixture
async def client(bitcart: Bitcart) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=bitcart.app), base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def token(client: AsyncClient) -> str:
    """Token of a superuser with full_control."""
    import helpers

    return await helpers.superuser_token(client)


@pytest.fixture
async def signer(
    bitcart: Bitcart, daemons: dict[str, fakes.FakeDaemon], tmp_path: Any, anyio_backend: Any
) -> AsyncIterator[fakes.FakeSigner]:
    token = secrets.token_urlsafe(32)
    token_file = tmp_path / "signer.token"
    token_file.write_text(token + "\n")
    chains = {coin: daemon.chain for coin, daemon in daemons.items()}
    fake = fakes.FakeSigner(token, secrets.token_bytes(32), chains, stores=(STORE_KEY, "otherstore"))
    runner = await fakes.start(fake.app, 17070)
    fake.url = "http://127.0.0.1:17070"  # type: ignore[attr-defined]
    fake.token_file = str(token_file)  # type: ignore[attr-defined]
    yield fake
    await runner.cleanup()


@pytest.fixture
def second(second_server: fakes.SecondOpinion, daemons: dict[str, fakes.FakeDaemon], bitcart: Bitcart) -> fakes.SecondOpinion:
    second_server.chains = {fakes.CHAIN_IDS[coin]: daemon.chain for coin, daemon in daemons.items()}
    second_server.lie = second_server.down = False
    return second_server
