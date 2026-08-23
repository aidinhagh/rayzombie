"""
bot.py — ballot shredder for a Telegram group.

  #رای <name>     shred that person's vote (their avatar as the backdrop)
  #رایگیری        results of the last 24 hours
  /whois <name>   what the matcher would pick, and their id
  /photo <name>   why an avatar did or didn't load

Some people are marked immune in seed.py: their votes get struck by lightning
and are never counted.
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

import matching
import roster
import seed
from animator import make_gif
from matching import Candidate, best_match

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("vote-shredder")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# #رایگیری must be tested first — otherwise "#رای گیری" votes for "گیری"
TALLY = re.compile(r"#\s*(?:رای|رأی|راي)[\s\u200c_]*گیری", re.UNICODE)
TRIGGER = re.compile(
    r"#\s*(?:رای|رأی|راي)(?![\u0600-\u06FF\u200c])[\s_:.\-،]*(.*)", re.UNICODE
)

MAX_CHARS = 24
USER_COOLDOWN = 8.0
CHAT_COOLDOWN = 2.5
TALLY_COOLDOWN = 15.0
CACHE_SIZE = 40
BLANK = "رأی سفید"
ADMIN_REFRESH = 6 * 3600
TOP_N = 10

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

_last_user: dict[tuple[int, int], float] = {}
_last_chat: dict[int, float] = {}
_last_tally: dict[int, float] = {}
_admins_seeded: dict[int, float] = {}
_cache: "OrderedDict[str, bytes]" = OrderedDict()
_render_slots = asyncio.Semaphore(2)


def fa(number: int) -> str:
    return str(number).translate(_FA_DIGITS)


def hours_left(seconds: float) -> str:
    if seconds >= 3600:
        return f"{fa(int(seconds // 3600))} ساعت"
    return f"{fa(max(1, int(seconds // 60)))} دقیقه"


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
            if user.username:
                await asyncio.to_thread(roster.store_handle, user.username, user.id)

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
            if member.user.username:
                await asyncio.to_thread(roster.store_handle,
                                        member.user.username, member.user.id)
    except Exception as exc:
        log.debug("admin seed failed for %s: %s", chat_id, exc)


# ------------------------------------------------------------------ resolution

def _as_candidate(user) -> Candidate:
    return Candidate(user.id, user.first_name, user.last_name, user.username,
                     time.time())


async def chat_candidates(chat_id: int) -> list[Candidate]:
    """Everyone the bot has seen here, plus the seed list for those it hasn't."""
    people = await asyncio.to_thread(roster.members, chat_id)
    ids = {p.user_id for p in people}
    handles = {matching.normalize(p.username).replace(" ", "")
               for p in people if p.username}

    for cand in seed.candidates():
        if cand.user_id and cand.user_id in ids:
            continue
        if cand.username:
            if cand.username in handles:
                continue
            resolved = await asyncio.to_thread(roster.known_handle, cand.username)
            if resolved:
                if resolved in ids:
                    continue
                cand.user_id = resolved
        people.append(cand)
    return people


async def ensure_user_id(bot, cand: Candidate) -> int:
    """Seed entries only carry a @handle. Try to turn it into a real user id."""
    if cand.user_id:
        return cand.user_id
    if not cand.username:
        return 0

    cached = await asyncio.to_thread(roster.known_handle, cand.username)
    if cached:
        cand.user_id = cached
        return cached

    try:
        chat = await bot.get_chat(f"@{cand.username}")
    except Exception as exc:
        log.info("could not resolve @%s: %s", cand.username, exc)
        return 0

    if chat and chat.id:
        await asyncio.to_thread(roster.store_handle, cand.username, chat.id)
        cand.user_id = chat.id
        return chat.id
    return 0


async def resolve_target(message, query: str) -> tuple[Candidate | None, float]:
    for entity in message.entities or []:
        if entity.type == MessageEntity.TEXT_MENTION and entity.user:
            return _as_candidate(entity.user), 1.0

    replied = message.reply_to_message.from_user if message.reply_to_message else None

    if not query:
        return (_as_candidate(replied), 1.0) if replied else (None, 0.0)

    match, score = best_match(query, await chat_candidates(message.chat_id))
    if match:
        return match, score
    if replied:
        return _as_candidate(replied), 0.0
    return None, score


async def fetch_photo(bot, user_id: int,
                      force: bool = False) -> tuple[str | None, bytes | None]:
    """(unique_id, jpeg) for a user's current avatar, cached.

    A failed API call is NOT cached as "no avatar" — that would make one
    transient error hide someone's photo for as long as the cache lives.
    """
    if not user_id:
        return None, None
    if force:
        await asyncio.to_thread(roster.drop_photo, user_id)
    elif await asyncio.to_thread(roster.photo_is_fresh, user_id):
        return await asyncio.to_thread(roster.cached_photo, user_id)

    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
    except Exception as exc:
        log.warning("getUserProfilePhotos failed for %s: %s", user_id, exc)
        return None, None

    if not photos.total_count or not photos.photos:
        # Usually the user's "Profile photo" privacy is Contacts / Nobody.
        log.info("user %s has no avatar visible to the bot", user_id)
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


async def render(label: str, photo_key: str, photo: bytes | None,
                 lightning: bool) -> bytes:
    key = f"{label}|{photo_key}|{int(lightning)}"
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    async with _render_slots:
        data = await asyncio.to_thread(make_gif, label, None, photo, lightning)
    _cache[key] = data
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return data


def vote_key(target: Candidate | None, label: str) -> str:
    if target and target.user_id:
        return f"id:{target.user_id}"
    if target and target.username:
        return f"@{target.username}"
    return f"t:{matching.normalize(label)}"


async def on_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    text = message.text or message.caption or ""

    if TALLY.search(text):
        await show_tally(update, context)
        return

    query = extract_word(text)
    if query is None:
        return

    chat_id = message.chat_id
    voter = message.from_user.id if message.from_user else 0
    now = time.monotonic()
    if now - _last_user.get((chat_id, voter), 0.0) < USER_COOLDOWN:
        return
    if now - _last_chat.get(chat_id, 0.0) < CHAT_COOLDOWN:
        return
    _last_user[(chat_id, voter)] = now
    _last_chat[chat_id] = now

    try:
        await seed_admins(context.bot, chat_id)
        target, score = await resolve_target(message, query)

        photo = None
        immune = False
        if target:
            label = seed.display_for(target) or target.display
            immune = seed.is_immune(target)
            user_id = await ensure_user_id(context.bot, target)
            _, photo = await fetch_photo(context.bot, user_id)
            log.info("matched %r -> %s (%.2f) photo=%s immune=%s",
                     query, label, score, bool(photo), immune)
        else:
            label = query or BLANK
            log.info("no match for %r (best %.2f)", query, score)

        # One vote per person per 24h. A vote annulled by lightning does not
        # burn that allowance — it never counted, so it costs nothing.
        already = None
        if not immune:
            already = await asyncio.to_thread(roster.last_vote, chat_id, voter)
            if already is None:
                await asyncio.to_thread(roster.record_vote, chat_id, voter,
                                        vote_key(target, label), label)

        photo_key = str(target.user_id) if (target and photo) else "-"
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        gif = await render(label[:MAX_CHARS], photo_key, photo, immune)

        if immune:
            caption = f"⚡️ رأی به «{label}» صاعقه زد و باطل شد"
        elif already:
            previous, remaining = already
            caption = (f"«{label}» — ولی رأی شما امروز به «{previous}» ثبت شده "
                       f"و این یکی شمرده نشد ⛔️\n"
                       f"{hours_left(remaining)} دیگر دوباره می‌توانید رأی بدهید.")
        else:
            caption = f"«{label}» با موفقیت ثبت شد ✅"
        await message.reply_animation(animation=io.BytesIO(gif),
                                      filename="vote.gif", caption=caption)
    except Exception:
        log.exception("failed to answer in chat %s", chat_id)


# ---------------------------------------------------------------------- tally

MEDALS = ["🥇", "🥈", "🥉"]


async def show_tally(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat_id = message.chat_id

    now = time.monotonic()
    if now - _last_tally.get(chat_id, 0.0) < TALLY_COOLDOWN:
        return
    _last_tally[chat_id] = now

    rows, voters = await asyncio.to_thread(roster.tally, chat_id)
    if not rows:
        await message.reply_text("در ۲۴ ساعت گذشته رأیی ثبت نشده. 🗳")
        return

    lines = ["🗳 <b>نتیجه رأی‌گیری ۲۴ ساعت گذشته</b>", ""]
    for index, (label, count) in enumerate(rows[:TOP_N]):
        rank = MEDALS[index] if index < 3 else f"{fa(index + 1)}."
        lines.append(f"{rank} {label} — {fa(count)} رأی")

    lines += ["", f"مجموع {fa(voters)} نفر رأی داده‌اند"]
    if len(rows) > TOP_N:
        lines.append(f"({fa(len(rows) - TOP_N)} نفر دیگر هم رأی گرفتند)")

    await message.reply_text("\n".join(lines), parse_mode="HTML")


# ------------------------------------------------------------------- commands

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "منو به گروه اضافه کن و بنویس:\n"
        "#رای علی — رأی به یک نفر\n"
        "#رایگیری — نتیجه ۲۴ ساعت گذشته\n\n"
        "اسم فارسی یا انگلیسی، کوتاه‌شده یا یوزرنیم — پیدا می‌کنم. 🗳️"
    )


async def who(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whois <name> — test the matcher without rendering anything."""
    message = update.effective_message
    query = " ".join(context.args or [])
    if not query:
        known = await asyncio.to_thread(roster.count, message.chat_id)
        await message.reply_text(
            f"{fa(known)} نفر در این گروه شناخته شده‌اند.\n/whois <اسم>"
        )
        return
    match, score = best_match(query, await chat_candidates(message.chat_id))
    if match:
        label = seed.display_for(match) or match.display
        await message.reply_text(
            f"{query} → {label} (@{match.username or '—'}) · {score:.2f}"
            f"\nid: {match.user_id or '؟'}"
            + ("\n⚡️ مصون — رأیش شمرده نمی‌شود" if seed.is_immune(match) else "")
        )
    else:
        await message.reply_text(f"{query} → کسی پیدا نشد (بهترین: {score:.2f})")


async def photo_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/photo [name] — bypass the cache and report exactly what Telegram says."""
    message = update.effective_message
    query = " ".join(context.args or [])

    if message.reply_to_message and not query:
        target = _as_candidate(message.reply_to_message.from_user)
    elif query:
        target, _ = best_match(query, await chat_candidates(message.chat_id))
    else:
        target = _as_candidate(message.from_user)

    if not target:
        await message.reply_text(f"{query} → کسی پیدا نشد")
        return

    user_id = await ensure_user_id(context.bot, target)
    if not user_id:
        await message.reply_text(
            f"{target.display}: هنوز آی‌دی ندارد — یک بار در گروه پیام بدهد."
        )
        return

    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        total = photos.total_count
    except Exception as exc:
        await message.reply_text(f"{target.display} (id {user_id})\n"
                                 f"getUserProfilePhotos error: {exc}")
        return

    _, data = await fetch_photo(context.bot, user_id, force=True)
    await message.reply_text(
        f"{target.display} (id {user_id})\n"
        f"total_count: {total}\n"
        f"downloaded: {len(data) if data else 0} bytes"
    )


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not set")

    seed.install()

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
    app.add_handler(CommandHandler("photo", photo_check), group=1)
    app.add_handler(CommandHandler(["result", "natije"], show_tally), group=1)
    app.add_handler(
        MessageHandler(filters.TEXT | filters.CAPTION, on_trigger), group=1
    )
    log.info("polling… roster db: %s, %d seeded people",
             roster.DB_PATH, len(seed.PEOPLE))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
