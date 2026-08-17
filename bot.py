"""
bot.py — "#رای <name>" in a group shreds that person's vote, with their
profile photo as the backdrop.

Resolution order for who is being voted on:
  1. a text_mention entity (a name tapped in the compose box)
  2. an exact @username
  3. fuzzy cross-script match against everyone the bot has seen in this chat
  4. the message being replied to, if none of the above landed
  5. nobody -> the raw word goes on the ballot, no backdrop
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from collections import OrderedDict

from telegram import MessageEntity, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import roster
from animator import make_gif
from matching import Candidate, best_match

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("vote-shredder")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()

TRIGGER = re.compile(
    r"#\s*(?:رای|رأی|راي)(?![\u0600-\u06FF\u200c])[\s_:.\-،]*(.*)", re.UNICODE
)

MAX_CHARS = 24
COOLDOWN = 6.0
CACHE_SIZE = 40
BLANK = "رأی سفید"
ADMIN_REFRESH = 6 * 3600

_last_sent: dict[int, float] = {}
_admins_seeded: dict[int, float] = {}
_cache: "OrderedDict[str, bytes]" = OrderedDict()
_render_slots = asyncio.Semaphore(2)


# --------------------------------------------------------------- roster upkeep

async def track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs on every message: quietly learns who is in the group."""
    message = update.effective_message
    if not message or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    chat_id = message.chat_id

    people = [message.from_user]
    if message.reply_to_message:
        people.append(message.reply_to_message.from_user)
    if message.new_chat_members:
        people.extend(message.new_chat_members)
    for entity in message.entities or []:
        if entity.type == MessageEntity.TEXT_MENTION and entity.user:
            people.append(entity.user)

    for user in people:
        if user:
            await asyncio.to_thread(roster.remember, chat_id, user)

    if message.left_chat_member:
        await asyncio.to_thread(roster.forget, chat_id, message.left_chat_member.id)


async def seed_admins(bot, chat_id: int) -> None:
    """getChatAdministrators is the only bulk member call a bot gets — use it."""
    if time.time() - _admins_seeded.get(chat_id, 0) < ADMIN_REFRESH:
        return
    _admins_seeded[chat_id] = time.time()
    try:
        for member in await bot.get_chat_administrators(chat_id):
            await asyncio.to_thread(roster.remember, chat_id, member.user)
    except Exception as exc:
        log.debug("admin seed failed for %s: %s", chat_id, exc)


# ------------------------------------------------------------------ resolution

def _as_candidate(user) -> Candidate:
    return Candidate(user.id, user.first_name, user.last_name, user.username,
                     time.time())


async def resolve_target(message, query: str) -> tuple[Candidate | None, float]:
    for entity in message.entities or []:
        if entity.type == MessageEntity.TEXT_MENTION and entity.user:
            return _as_candidate(entity.user), 1.0

    replied = message.reply_to_message.from_user if message.reply_to_message else None

    if not query:
        return (_as_candidate(replied), 1.0) if replied else (None, 0.0)

    people = await asyncio.to_thread(roster.members, message.chat_id)
    match, score = best_match(query, people)
    if match:
        return match, score
    if replied:
        return _as_candidate(replied), 0.0
    return None, score


async def fetch_photo(bot, user_id: int) -> tuple[str | None, bytes | None]:
    """(unique_id, jpeg) for a user's current avatar, cached for a day."""
    if await asyncio.to_thread(roster.photo_is_fresh, user_id):
        return await asyncio.to_thread(roster.cached_photo, user_id)

    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
    except Exception as exc:
        log.debug("no profile photos for %s: %s", user_id, exc)
        photos = None

    if not photos or not photos.total_count:
        await asyncio.to_thread(roster.store_photo, user_id, None, None)
        return None, None

    size = photos.photos[0][-1]                       # largest available
    unique = size.file_unique_id
    known_id, known_data = await asyncio.to_thread(roster.cached_photo, user_id)
    if known_id == unique and known_data:
        await asyncio.to_thread(roster.store_photo, user_id, unique, known_data)
        return unique, known_data

    try:
        handle = await bot.get_file(size.file_id)
        data = bytes(await handle.download_as_bytearray())
    except Exception as exc:
        log.warning("avatar download failed for %s: %s", user_id, exc)
        return None, None

    await asyncio.to_thread(roster.store_photo, user_id, unique, data)
    return unique, data


# --------------------------------------------------------------------- render

def extract_word(text: str) -> str | None:
    match = TRIGGER.search(text)
    if not match:
        return None
    tail = match.group(1).split("\n")[0]
    tail = re.sub(r"#\S+", " ", tail)
    tail = re.sub(r"\s+", " ", tail).strip()
    return tail[:MAX_CHARS].strip()


async def render(label: str, photo_key: str, photo: bytes | None) -> bytes:
    key = f"{label}|{photo_key}"
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    async with _render_slots:
        data = await asyncio.to_thread(make_gif, label, None, photo)
    _cache[key] = data
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return data


async def on_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    query = extract_word(message.text or message.caption or "")
    if query is None:
        return

    chat_id = message.chat_id
    now = time.monotonic()
    if now - _last_sent.get(chat_id, 0.0) < COOLDOWN:
        return
    _last_sent[chat_id] = now

    try:
        await seed_admins(context.bot, chat_id)
        target, score = await resolve_target(message, query)

        photo = None
        if target:
            label = target.display
            _, photo = await fetch_photo(context.bot, target.user_id)
            log.info("matched %r -> %s (%.2f) photo=%s",
                     query, label, score, bool(photo))
        else:
            label = query or BLANK
            log.info("no match for %r (best %.2f)", query, score)

        photo_key = str(target.user_id) if (target and photo) else "-"

        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        gif = await render(label[:MAX_CHARS], photo_key, photo)
        await message.reply_animation(
            animation=io.BytesIO(gif),
            filename="vote.gif",
            caption=f"«{label}» با موفقیت ثبت شد ✅",
        )
    except Exception:
        log.exception("failed to answer in chat %s", chat_id)


# ------------------------------------------------------------------- commands

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "منو به گروه اضافه کن و بنویس:\n"
        "#رای علی\n\n"
        "اسم فارسی یا انگلیسی، کوتاه‌شده یا یوزرنیم — پیدا می‌کنم. 🗳️"
    )


async def who(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whois <name> — test the matcher without rendering anything."""
    message = update.effective_message
    query = " ".join(context.args or [])
    if not query:
        known = await asyncio.to_thread(roster.count, message.chat_id)
        await message.reply_text(
            f"{known} نفر در این گروه شناخته شده‌اند.\n/whois <اسم>"
        )
        return
    people = await asyncio.to_thread(roster.members, message.chat_id)
    match, score = best_match(query, people)
    if match:
        await message.reply_text(
            f"{query} → {match.display} (@{match.username or '—'}) · {score:.2f}"
        )
    else:
        await message.reply_text(f"{query} → کسی پیدا نشد (بهترین: {score:.2f})")


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not set")

    # Python 3.14 removed the implicit loop that python-telegram-bot's
    # run_polling() still expects from asyncio.get_event_loop().
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, track), group=0)
    app.add_handler(CommandHandler(["start", "help"], start), group=1)
    app.add_handler(CommandHandler("whois", who), group=1)
    app.add_handler(
        MessageHandler(filters.TEXT | filters.CAPTION, on_trigger), group=1
    )
    log.info("polling… roster db: %s", roster.DB_PATH)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
