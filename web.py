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
import json
import logging
import os

from aiohttp import web as aioweb

log = logging.getLogger("vote-shredder.web")

HERE = os.path.dirname(os.path.abspath(__file__))

# Looked for in this order, so it works whether ride.html sits in webapp/ or
# was dropped next to the Python files.
RIDE_CANDIDATES = [
    os.environ.get("RIDE_HTML", ""),
    os.path.join(HERE, "webapp", "ride.html"),
    os.path.join(HERE, "ride.html"),
    os.path.join(os.getcwd(), "webapp", "ride.html"),
    os.path.join(os.getcwd(), "ride.html"),
]


def ride_path() -> str | None:
    for path in RIDE_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


RIDE = RIDE_CANDIDATES[1]        # what we report when nothing is found

_runner: aioweb.AppRunner | None = None
bot = None                       # set by bot.py at boot, for hunt announcements

FA = {"lion": "شیر", "tiger": "ببر", "deer": "آهو", "zebra": "گورخر",
      "eagle": "عقاب"}


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

    It used to be served with FileResponse, which streams via sendfile with no
    declared charset — Railway's edge mangled that into ERR_INVALID_RESPONSE.
    An ordinary Response with an explicit charset goes through untouched.
    """
    global _ride_html
    if _ride_html is None:
        path = ride_path()
        if path is None:
            raise FileNotFoundError(RIDE)
        with open(path, encoding="utf-8") as fh:
            _ride_html = fh.read()
        log.info("serving ride from %s", path)
    return _ride_html


async def _ride(request: aioweb.Request) -> aioweb.StreamResponse:
    try:
        body = _load_ride()
    except FileNotFoundError:
        looked = "\n".join(f"  {p}" for p in RIDE_CANDIDATES if p)
        log.error("ride.html not found. Looked in:\n%s", looked)
        return aioweb.Response(
            status=500, content_type="text/plain", charset="utf-8",
            text=("ride.html is not in the deployed image.\n\nLooked in:\n"
                  f"{looked}\n\nCommit it (git add webapp/ride.html) and "
                  "redeploy, or set RIDE_HTML to its path."))
    return aioweb.Response(body=body.encode("utf-8"),
                           content_type="text/html", charset="utf-8",
                           headers={"Cache-Control": "no-store"})


async def _health(request: aioweb.Request) -> aioweb.StreamResponse:
    found = ride_path()
    return aioweb.json_response({
        "ok": True,
        "ride_html": found or None,
        "looked_in": [p for p in RIDE_CANDIDATES if p],
        "cwd_listing": sorted(os.listdir(os.getcwd()))[:40],
    })


async def _trip(request: aioweb.Request) -> aioweb.StreamResponse:
    """Details for one ride. A Direct Link Mini App can only carry a short
    alphanumeric start_param, so the payload is fetched by token instead."""
    import roster

    token = request.match_info.get("token", "")
    payload = await asyncio.to_thread(roster.load_trip, token)
    if not payload:
        return aioweb.json_response({"error": "expired"}, status=404)

    trip = json.loads(payload)
    public = {k: v for k, v in trip.items() if k not in ("chat_id", "user_id")}
    return aioweb.json_response(public, headers={"Cache-Control": "no-store"})


async def _hunt(request: aioweb.Request) -> aioweb.StreamResponse:
    """The ride reports how the hunt went; the bot announces it in the group.

    The animal is whatever the bot chose for this trip, not whatever the page
    sends — the page is only trusted for hit or miss.
    """
    import roster

    token = request.match_info.get("token", "")
    payload = await asyncio.to_thread(roster.load_trip, token)
    if not payload:
        return aioweb.json_response({"error": "expired"}, status=404)
    if await asyncio.to_thread(roster.hunt_recorded, token):
        return aioweb.json_response({"ok": True, "already": True})

    try:
        body = await request.json()
    except Exception:
        body = {}
    hit = bool(body.get("hit"))
    shots = int(body.get("shots") or 0)

    trip = json.loads(payload)
    animal = trip.get("quarry", "deer")
    chat_id, user_id = trip.get("chat_id"), trip.get("user_id")
    name = trip.get("name", "؟")

    await asyncio.to_thread(roster.save_hunt, token, chat_id, user_id, name,
                            animal, hit, shots)

    if bot is not None and chat_id:
        fa = FA.get(animal, animal)
        text = (f"🏹 <b>{name}</b> یک {fa} شکار کرد!"
                if hit else
                f"🏹 {fa} از دست <b>{name}</b> فرار کرد.")
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as exc:
            log.info("hunt announcement failed: %s", exc)

    return aioweb.json_response({"ok": True})


async def start() -> None:
    """Bind to $PORT. Failure here must not take the bot down with it."""
    global _runner
    port = int(os.environ.get("PORT", "8080"))

    app = aioweb.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/ride", _ride)
    app.router.add_get("/trip/{token}", _trip)
    app.router.add_post("/hunt/{token}", _hunt)

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
        log.error("ride.html MISSING — looked in %s; cwd holds %s",
                  [p for p in RIDE_CANDIDATES if p],
                  sorted(os.listdir(os.getcwd()))[:40])

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
