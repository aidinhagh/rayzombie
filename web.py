"""
web.py — serves the ride Web App.

Telegram will only open a Web App over HTTPS, so this has to be reachable from
the internet. Railway gives the service a domain as soon as it listens on
$PORT; the bot picks that up automatically via RAILWAY_PUBLIC_DOMAIN.

It runs inside the bot's own event loop (started from Application.post_init),
so there is still exactly one process and one poller.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web as aioweb

log = logging.getLogger("vote-shredder.web")

HERE = os.path.dirname(os.path.abspath(__file__))
RIDE = os.path.join(HERE, "webapp", "ride.html")

_runner: aioweb.AppRunner | None = None


def public_url() -> str | None:
    """Base URL of this service, if it has one."""
    explicit = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}" if domain else None


async def _ride(request: aioweb.Request) -> aioweb.StreamResponse:
    return aioweb.FileResponse(RIDE, headers={"Cache-Control": "no-store"})


async def _health(request: aioweb.Request) -> aioweb.StreamResponse:
    return aioweb.json_response({"ok": True})


async def start() -> None:
    """Bind to $PORT. Failure here must not take the bot down with it."""
    global _runner
    port = int(os.environ.get("PORT", "8080"))

    app = aioweb.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/ride", _ride)

    _runner = aioweb.AppRunner(app)
    await _runner.setup()
    await aioweb.TCPSite(_runner, "0.0.0.0", port).start()

    base = public_url()
    log.info("web app on :%s — %s", port,
             f"{base}/ride" if base else "no public URL set, rides disabled")


async def stop() -> None:
    if _runner is not None:
        await _runner.cleanup()
