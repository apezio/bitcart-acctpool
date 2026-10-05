"""acctpool: address pool for USDT and native-coin payments on EVM chains. One deposit address per invoice, no
sender-address prompt; the stock Bitcart daemon is the chain layer. Loaded by Bitcart (api/plugins.py
load_plugins) in the backend and in the worker."""

from fastapi import FastAPI

from api.logging import get_exception_message, get_logger
from api.plugins import BasePlugin
from api.services.coins import CoinService
from api.settings import Settings

from . import api
from .constants import PLUGIN_NAME
from .db import Database
from .detect import Detector
from .hooks import Hooks
from .runner import Runner

logger = get_logger(__name__)

API_PREFIX = f"/plugins/{PLUGIN_NAME}"


class Plugin(BasePlugin):
    name = PLUGIN_NAME

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self._db: Database | None = None
        self._registered = False
        self.hooks = Hooks(self)
        self.detector = Detector(self)
        self.runner: Runner | None = None

    async def db(self) -> Database:
        if self._db is None:
            settings = await self.container.get(Settings)
            if self._db is None:  # another hook made it while this one waited for the settings
                self._db = Database(settings)
        return self._db

    def register_hooks(self) -> None:
        """From setup_app, startup and worker_setup, whichever comes first: Bitcart skips startup() of a plugin
        whose migration raised (every process but one, when many start on a new database at once). Without the
        get_request filter a pool invoice would be asked for at the stock daemon."""
        if not self._registered:
            self.hooks.register()
            self._registered = True

    def setup_app(self, app: FastAPI) -> None:
        self.register_hooks()
        if not any(getattr(route, "path", "").startswith(f"{API_PREFIX}/") for route in app.routes):
            app.include_router(api.router, prefix=API_PREFIX, tags=[PLUGIN_NAME])

    async def startup(self) -> None:
        self.register_hooks()

    async def worker_setup(self) -> None:
        """The worker only: the daemon event handler (every worker process) and the loops (one leader)."""
        self.register_hooks()
        try:
            coins = await self.container.get(CoinService)
            coins.manager.add_event_handler("new_transaction", self.detector.on_transaction)
            if self.runner is None:
                self.runner = Runner(self)
                self.runner.start()
        except Exception as e:
            logger.error(f"acctpool: the worker part did not start: {get_exception_message(e)}")

    async def shutdown(self) -> None:
        if self.runner is not None:
            await self.runner.stop()
            self.runner = None
        if self._db is not None:
            await self._db.close()
            self._db = None
