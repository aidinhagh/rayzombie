"""
web.py — serves the ride Web App.

Telegram will only open a Web App over HTTPS, so this has to be reachable from
the internet. Railway gives the service a domain as soon as it listens on
$PORT; the bot picks that up automatically via RAILWAY_PUBLIC_DOMAIN.

It runs inside the bot's own event loop (started from Application.post_init),
so there is still exactly one process and one poller.
"""

from __future__ import annotations

import asyncio
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


_ride_html: str | None = None


def _load_ride() -> str:
    """Read the page once, into memory.

    It used to be served with FileResponse, which streams via sendfile and no
    explicit charset — Railway's edge mangled that into ERR_INVALID_RESPONSE.
    An ordinary Response with a declared charset goes through untouched.
    """
    global _ride_html
    if _ride_html is None:
        with open(RIDE, encoding="utf-8") as fh:
            _ride_html = fh.read()
    return _ride_html


async def _ride(request: aioweb.Request) -> aioweb.StreamResponse:
    try:
        body = _load_ride()
    except FileNotFoundError:
        log.error("ride.html not found at %s", RIDE)
        return aioweb.Response(status=500, text="ride.html missing on the server",
                               content_type="text/plain", charset="utf-8")
    return aioweb.Response(body=body.encode("utf-8"),
                           content_type="text/html", charset="utf-8",
                           headers={"Cache-Control": "no-store"})


async def _health(request: aioweb.Request) -> aioweb.StreamResponse:
    return aioweb.json_response({"ok": True})


async def _trip(request: aioweb.Request) -> aioweb.StreamResponse:
    """Details for one ride. A Direct Link Mini App can only carry a short
    alphanumeric start_param, so the payload is fetched by token instead."""
    import json

    import roster

    token = request.match_info.get("token", "")
    payload = await asyncio.to_thread(roster.load_trip, token)
    if not payload:
        return aioweb.json_response({"error": "expired"}, status=404)
    return aioweb.json_response(json.loads(payload),
                                headers={"Cache-Control": "no-store"})


async def start() -> None:
    """Bind to $PORT. Failure here must not take the bot down with it."""
    global _runner
    port = int(os.environ.get("PORT", "8080"))

    app = aioweb.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/ride", _ride)
    app.router.add_get("/trip/{token}", _trip)

    try:
        _runner = aioweb.AppRunner(app)
        await _runner.setup()
        await aioweb.TCPSite(_runner, "0.0.0.0", port).start()
    except Exception:
        log.exception("WEB SERVER FAILED TO BIND on port %s — no rides", port)
        return

    try:
        log.info("ride.html loaded, %d bytes", len(_load_ride()))
    except FileNotFoundError:
        log.error("ride.html MISSING at %s — is webapp/ committed to git and "
                  "copied into the image?", RIDE)

    # Prove it from inside the container. If this succeeds but the public URL
    # 404s or times out, the process is fine and the problem is Railway's
    # routing — check the domain's target port matches this one.
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            for path in ("/health", "/ride?t=selfcheck"):
                async with session.get(
                        f"http://127.0.0.1:{port}{path}",
                        timeout=aiohttp.ClientTimeout(total=5)) as r:
                    body = await r.read()
                    log.info("self-check %s -> %s (%d bytes, %s)",
                             path, r.status, len(body), r.content_type)
    except Exception as exc:
        log.warning("self-check failed on port %s: %s", port, exc)

    base = public_url()
    log.info("LISTENING on 0.0.0.0:%s — public %s", port,
             f"{base}/ride" if base else "(none set)")
    log.info("Railway: the generated domain must point at target port %s", port)


async def stop() -> None:
    if _runner is not None:
        await _runner.cleanup()
