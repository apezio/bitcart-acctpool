"""Integration test only: a JSON-RPC proxy in front of anvil, in its own container (v4i-proxy).

:8546  provider of the stock eth daemon. With block_send on, eth_sendRawTransaction gets an error, so the worker
       keeps a payout in `signed` (the worker-kill flow).
:8547  the second-opinion provider of the plugin (second.toml). Modes: ok, lie (every balance + 1), down (HTTP 503).
:8548  control: POST /mode {"second": "ok|lie|down", "block_send": true|false}
"""

import asyncio
import json

from aiohttp import ClientSession, web

ANVIL = "http://v4i-anvil:8545"
state = {"second": "ok", "block_send": False}


async def forward(session: ClientSession, body: bytes) -> bytes:
    async with session.post(ANVIL, data=body, headers={"Content-Type": "application/json"}) as response:
        return await response.read()


def make_daemon_side(session: ClientSession) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        body = await request.read()
        data = json.loads(body)
        if state["block_send"] and isinstance(data, dict) and data.get("method") == "eth_sendRawTransaction":
            error = {"code": -32000, "message": "test proxy: broadcast blocked"}
            return web.json_response({"jsonrpc": "2.0", "id": data.get("id"), "error": error})
        return web.Response(body=await forward(session, body), content_type="application/json")

    app = web.Application()
    app.router.add_post("/", handle)
    return app


def make_second_side(session: ClientSession) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        if state["second"] == "down":
            return web.Response(status=503)
        body = await request.read()
        answer = json.loads(await forward(session, body))
        method = json.loads(body).get("method")
        if state["second"] == "lie" and method in ("eth_getBalance", "eth_call") and "result" in answer:
            answer["result"] = hex(int(answer["result"], 16) + 1)
        return web.json_response(answer)

    app = web.Application()
    app.router.add_post("/", handle)
    return app


async def control(request: web.Request) -> web.Response:
    state.update(await request.json())
    return web.json_response(state)


async def main() -> None:
    session = ClientSession()
    admin = web.Application()
    admin.router.add_post("/mode", control)
    for app, port in ((make_daemon_side(session), 8546), (make_second_side(session), 8547), (admin, 8548)):
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
    print("proxy up", flush=True)  # noqa: T201
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
