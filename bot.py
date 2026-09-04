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
import datetime as dt
import json
import random
import secrets
import time
from collections import OrderedDict

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      MessageEntity, Update)
from telegram.constants import ChatAction, ChatMemberStatus, ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import duel
import matching
import roster
import seed
import web
import worldmap
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
VERSION = "2026-09-04.nick-preassign-2"

# A message older than this is history, not a new command. Reactions, edits and
# any backlog Telegram replays after downtime all arrive attached to the
# ORIGINAL message — without this, reacting to a week-old "#رای X" re-votes.
MAX_MESSAGE_AGE = 120.0
SEEN_LIMIT = 400

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

_last_user: dict[tuple[int, int], float] = {}
_last_chat: dict[int, float] = {}
_last_tally: dict[int, float] = {}
_admins_seeded: dict[int, float] = {}
_cache: "OrderedDict[str, bytes]" = OrderedDict()
_render_slots = asyncio.Semaphore(2)
_seen: "OrderedDict[tuple[int, int], float]" = OrderedDict()


def is_stale(message) -> bool:
    """True for anything that is not a freshly sent message."""
    if message.edit_date is not None:
        return True
    # a forwarded "#رای X" is someone showing an old vote, not casting one
    if getattr(message, "forward_origin", None) is not None:
        return True
    if message.date is not None:
        age = time.time() - message.date.timestamp()
        if age > MAX_MESSAGE_AGE:
            return True

    key = (message.chat_id, message.message_id)
    if key in _seen:
        return True
    _seen[key] = time.time()
    if len(_seen) > SEEN_LIMIT:
        _seen.popitem(last=False)
    return False


def fa(number: int) -> str:
    return str(number).translate(_FA_DIGITS)


def hours_left(seconds: float) -> str:
    if seconds >= 3600:
        return f"{fa(int(seconds // 3600))} ساعت"
    return f"{fa(max(1, int(seconds // 60)))} دقیقه"


# --------------------------------------------------------------- roster upkeep

# /nick may be assigned before this board has ever seen the user.
# Numeric IDs can be stored immediately.  Telegram does not reliably resolve
# arbitrary private-user @usernames for bots, so unknown usernames are kept
# here as pending settings and bound the instant Telegram exposes username+id.
PENDING_NICK_ID = "pending_nick_id:"
PENDING_NICK_USER = "pending_nick_user:"
_pending_nick_checked: set[tuple[int, int, str]] = set()


def _pending_nick_id_key(user_id: int) -> str:
    return f"{PENDING_NICK_ID}{user_id}"


def _pending_nick_user_key(username: str) -> str:
    return f"{PENDING_NICK_USER}{username.lstrip('@').casefold()}"


async def bind_pending_nick(chat_id: int, user) -> str | None:
    """Attach a pre-assigned nickname as soon as Telegram reveals the user."""
    if not user or not getattr(user, "id", 0):
        return None

    username = (getattr(user, "username", None) or "").strip()
    check_key = (chat_id, user.id, username.casefold())
    if check_key in _pending_nick_checked:
        return None

    # Numeric ID is authoritative if both an id and username pending value exist.
    id_key = _pending_nick_id_key(user.id)
    nick = await asyncio.to_thread(roster.get_setting, GAME, id_key)

    user_key = _pending_nick_user_key(username) if username else None
    if not nick and user_key:
        nick = await asyncio.to_thread(roster.get_setting, GAME, user_key)

    _pending_nick_checked.add(check_key)
    if not nick:
        return None

    try:
        await asyncio.to_thread(roster.set_nick, GAME, user.id, nick)
    except Exception:
        # Keep the pending settings intact so the next restart/message can retry.
        log.exception("failed to bind pending nickname for %s in %s",
                      user.id, chat_id)
        _pending_nick_checked.discard(check_key)
        return None

    # It is bound now; these are only temporary lookup records.
    await asyncio.to_thread(roster.set_setting, GAME, id_key, "")
    if user_key:
        await asyncio.to_thread(roster.set_setting, GAME, user_key, "")

    log.info("bound pending nickname in %s: %s (@%s) -> %r",
             chat_id, user.id, username or "—", nick)
    return nick


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

    await asyncio.to_thread(roster.remember_group, chat_id, message.chat.title)

    for user in people:
        if user:
            await asyncio.to_thread(roster.remember, GAME, user)
            if user.username:
                if await asyncio.to_thread(roster.merge_handle, GAME,
                                           user.username, user.id):
                    log.info("bound placeholder @%s -> %s", user.username, user.id)
            if user.username:
                await asyncio.to_thread(roster.store_handle, user.username, user.id)
            await bind_pending_nick(chat_id, user)

    if message.left_chat_member:
        await asyncio.to_thread(roster.forget, GAME, message.left_chat_member.id)


async def seed_admins(bot, chat_id: int) -> None:
    """getChatAdministrators is the only bulk member call a bot gets — use it."""
    if time.time() - _admins_seeded.get(chat_id, 0) < ADMIN_REFRESH:
        return
    _admins_seeded[chat_id] = time.time()
    try:
        for member in await bot.get_chat_administrators(chat_id):
            await asyncio.to_thread(roster.remember, GAME, member.user)
            if member.user.username:
                await asyncio.to_thread(roster.store_handle,
                                        member.user.username, member.user.id)
            await bind_pending_nick(chat_id, member.user)
    except Exception as exc:
        log.debug("admin seed failed for %s: %s", chat_id, exc)


# ------------------------------------------------------------------ resolution

def _as_candidate(user) -> Candidate:
    return Candidate(user.id, user.first_name, user.last_name, user.username,
                     time.time())


async def chat_candidates(chat_id: int) -> list[Candidate]:
    """Everyone the bot has seen here, plus the seed list for those it hasn't."""
    people = await asyncio.to_thread(roster.members, GAME)
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
    if not (TALLY.search(text) or TRIGGER.search(text)):
        return
    if is_stale(message):
        log.debug("ignoring stale/edited message %s", message.message_id)
        return

    if TALLY.search(text):
        await show_tally(update, context)
        return

    query = extract_word(text)
    if query is None:
        return

    chat_id = message.chat_id
    voter = message.from_user.id if message.from_user else 0
    if voter and await asyncio.to_thread(roster.is_dead, GAME, voter):
        await message.reply_text("مرده‌ها رأی نمی‌دهند. ⚰️")
        return
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

        if target and target.user_id and await asyncio.to_thread(
                roster.is_dead, GAME, target.user_id):
            label = seed.display_for(target) or target.display
            await message.reply_text(f"«{label}» از بازی خارج شده. ⚰️")
            return

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
            already = await asyncio.to_thread(roster.last_vote, GAME, voter)
            if already is None:
                await asyncio.to_thread(roster.record_vote, GAME, voter,
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

    rows, voters = await asyncio.to_thread(roster.tally, GAME)
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
        "#رایگیری — نتیجه ۲۴ ساعت گذشته\n"
        "/duel — مبارزه: حمله / دفاع / حیله\n"
        "/travel — سفر روی نقشه\n"
        "/hunts — جدول شکار\n"
        "تا ساعت ۲۰ به وقت تهران وقت داری؛ بعدش قرعه می‌افتد.\n"
        "/delete — فقط مالک گروه: حذف رأی\n\n"
        "اسم فارسی یا انگلیسی، کوتاه‌شده یا یوزرنیم — پیدا می‌کنم. 🗳️\n"
        "/help — همهٔ دستورها"
    )


async def who(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whois <name> — test the matcher without rendering. Admin only."""
    if not await admin_only(update, context):
        return
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
    """/photo [name] — what Telegram says about an avatar. Admin only."""
    if not await admin_only(update, context):
        return
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


ANONYMOUS_ADMIN_ID = 1087968824      # @GroupAnonymousBot


async def is_admin(bot, chat_id: int, user_id: int) -> bool:
    """Strictly the bot's admin. Being a group's owner is NOT enough.

    The owner fallback was a mistake: every group has an owner, anyone can make
    a group and add the bot, and in a private chat the check was resolved
    against whichever group the person happened to play in. Admin commands
    answer to one identity now — ADMIN_IDS if set, otherwise the account behind
    ADMIN_HANDLE — and nothing else.
    """
    if not user_id:
        return False
    if ADMIN_IDS:
        allowed = user_id in ADMIN_IDS
    else:
        allowed = user_id == await admin_id(bot)
    if not allowed:
        log.info("admin command refused for %s in %s", user_id, chat_id)
    return allowed


async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Refuse and say so. Checked before any lookup, so a refused command
    never reveals anything — not even which groups the bot knows about."""
    message = update.effective_message
    user = message.from_user if message else None
    if user and await is_admin(context.bot, 0, user.id):
        return True
    if message:
        await message.reply_text("فقط مدیر. ⛔️")
    return False


async def is_owner(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:
        log.warning("get_chat_member failed in %s: %s", chat_id, exc)
        return False
    log.info("owner check in %s for %s -> %s", chat_id, user_id, member.status)
    return member.status == ChatMemberStatus.OWNER


async def delete_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delete <name> | /delete all | (as a reply) /delete — owner only.

    Two different deletions, both useful:
      · by NAME   — drop every vote cast for that person
      · by REPLY  — drop that person's own vote, so they can vote again
    """
    message = update.effective_message
    chat_id = message.chat_id
    log.info("/delete from %s in %s args=%r",
             message.from_user.id if message.from_user else "?", chat_id,
             context.args)

    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("این دستور فقط داخل گروه کار می‌کند.")
        return

    sender = message.from_user
    if message.sender_chat or (sender and sender.id == ANONYMOUS_ADMIN_ID):
        # Posting anonymously hides who you are, so ownership can't be checked.
        await message.reply_text(
            "پیام ناشناس فرستاده شده و نمی‌توانم مالک بودن را تأیید کنم.\n"
            "لطفاً حالت ناشناس (Remain Anonymous) را موقتاً خاموش کن."
        )
        return
    if not sender or not await is_admin(context.bot, chat_id, sender.id):
        await message.reply_text("فقط مدیر می‌تواند رأی حذف کند. ⛔️")
        return

    query = " ".join(context.args or [])

    if query.strip().lower() in ("all", "همه"):
        removed = await asyncio.to_thread(roster.clear_votes, GAME)
        await message.reply_text(f"همهٔ {fa(removed)} رأی ۲۴ ساعت گذشته پاک شد. 🧹")
        return

    if message.reply_to_message and not query:
        person = message.reply_to_message.from_user
        removed = await asyncio.to_thread(roster.delete_votes_by, GAME, person.id)
        if removed:
            await message.reply_text(
                f"رأی {person.first_name} حذف شد — می‌تواند دوباره رأی بدهد. ✅"
            )
        else:
            await message.reply_text(f"{person.first_name} در ۲۴ ساعت گذشته رأی نداده.")
        return

    if not query:
        await message.reply_text(
            "/delete <اسم> — حذف رأی‌های داده‌شده به یک نفر\n"
            "/delete (ریپلای) — حذف رأی خودِ آن شخص\n"
            "/delete all — پاک کردن کل ۲۴ ساعت"
        )
        return

    target, score = best_match(query, await chat_candidates(chat_id))
    label = (seed.display_for(target) or target.display) if target else query
    key = vote_key(target, label)
    removed = await asyncio.to_thread(roster.delete_votes_for, GAME, key, label)

    if removed:
        await message.reply_text(f"{fa(removed)} رأی داده‌شده به «{label}» حذف شد. 🗑")
    else:
        await message.reply_text(f"رأیی به «{label}» در ۲۴ ساعت گذشته ثبت نشده.")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ping — is the deployed code the code you think it is? Admin only."""
    if not await admin_only(update, context):
        return
    message = update.effective_message
    sender = message.from_user
    owner = "—"
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and sender:
        owner = "بله" if await is_owner(context.bot, message.chat_id, sender.id) \
                else "خیر"
    await message.reply_text(
        f"pong · نسخه {VERSION}\n"
        f"مالک گروه: {owner}\n"
        f"دستورها: /whois /photo /delete /result"
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Without this, a crash inside a handler is silent — the user just sees
    nothing happen, which is exactly how /delete looked when it broke."""
    log.exception("handler error on update: %s", update, exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                f"خطا در اجرای دستور: {type(context.error).__name__}"
            )
    except Exception:
        pass




# =========================================================== attack/defend/trick

DUEL_TTL = 10 * 60
CHALLENGERS, RESPONDERS = "challengers", "responders"

# token -> {chat_id, message_id, challenger, challenger_name, move, opponent, born}
_duels: dict[str, dict] = {}

MOVE_BUTTONS = [
    (duel.ATTACK, "⚔️ حمله"),
    (duel.DEFEND, "🛡 دفاع"),
    (duel.TRICK, "🌀 حیله"),
]


def move_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"d|{token}|{move}")
        for move, label in MOVE_BUTTONS
    ]])


async def may(chat_id: int, user_id: int, key: str) -> bool:
    raw = await asyncio.to_thread(roster.get_setting, GAME, key)
    if not raw or raw == "all":
        return True
    return str(user_id) in raw.split(",")


def display_name(user) -> str:
    return (user.first_name or user.username or str(user.id))[:16]


async def game_name(chat_id: int, user_id: int, fallback: str) -> str:
    """What everyone sees in the game: the nickname if one is set.

    Real names only ever appear in /rollcall, which is admin-only.
    """
    nick = await asyncio.to_thread(roster.get_nick, GAME, user_id)
    return nick or fallback


def _expire() -> None:
    now = time.time()
    for token in [t for t, g in _duels.items() if now - g["born"] > DUEL_TTL]:
        _duels.pop(token, None)


async def start_duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/duel [name] — open a challenge. The first move stays hidden."""
    message = update.effective_message
    chat_id = message.chat_id
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("مبارزه فقط داخل گروه.")
        return

    challenger = message.from_user
    if not await may(chat_id, challenger.id, CHALLENGERS):
        await message.reply_text("تو اجازهٔ شروع مبارزه نداری. ⛔️")
        return

    _expire()

    opponent = None
    if message.reply_to_message and message.reply_to_message.from_user:
        opponent = message.reply_to_message.from_user.id
        opponent_name = display_name(message.reply_to_message.from_user)
    elif context.args:
        target, _ = best_match(" ".join(context.args),
                               await chat_candidates(chat_id))
        if target:
            opponent = await ensure_user_id(context.bot, target)
            opponent_name = seed.display_for(target) or target.display

    token = secrets.token_urlsafe(6)
    _duels[token] = {
        "chat_id": chat_id,
        "challenger": challenger.id,
        "challenger_name": display_name(challenger),
        "move": None,
        "opponent": opponent,
        "born": time.time(),
    }

    who = f"حریف: {opponent_name}" if opponent else "هر کسی می‌تواند جواب بدهد"
    sent = await message.reply_text(
        f"🏜 <b>مبارزه</b>\n{display_name(challenger)} چالش داد.\n{who}\n\n"
        f"اول {display_name(challenger)} حرکتش را مخفیانه انتخاب کند:",
        reply_markup=move_keyboard(token), parse_mode="HTML")
    _duels[token]["message_id"] = sent.message_id


async def on_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Both taps land here. The answer to the tap is a private toast, which is
    what keeps the first move hidden from the second player."""
    query = update.callback_query
    try:
        _, token, move = (query.data or "").split("|")
    except ValueError:
        await query.answer()
        return

    _expire()
    match = _duels.get(token)
    if not match:
        await query.answer("این مبارزه منقضی شده.", show_alert=True)
        return

    user = query.from_user
    chat_id = match["chat_id"]

    # --- first move: only the challenger, and nobody gets to see it
    if match["move"] is None:
        if user.id != match["challenger"]:
            await query.answer("این چالش مال تو نیست — صبر کن.", show_alert=True)
            return
        match["move"] = move
        await query.answer(f"انتخاب تو: {duel.FA_MOVE[move]} — مخفی ماند 🤫",
                           show_alert=True)
        await query.edit_message_text(
            f"🏜 <b>مبارزه</b>\n{match['challenger_name']} حرکتش را انتخاب کرد "
            f"(مخفی).\n\nحالا حریف انتخاب کند:",
            reply_markup=move_keyboard(token), parse_mode="HTML")
        return

    # --- second move
    if user.id == match["challenger"]:
        await query.answer("تو که انتخاب کردی. منتظر حریف بمان.", show_alert=True)
        return
    if match["opponent"] and user.id != match["opponent"]:
        await query.answer("این مبارزه برای تو نیست.", show_alert=True)
        return
    if not await may(chat_id, user.id, RESPONDERS):
        await query.answer("تو اجازهٔ جواب دادن نداری. ⛔️", show_alert=True)
        return

    _duels.pop(token, None)
    await query.answer(f"انتخاب تو: {duel.FA_MOVE[move]}")

    green_name = match["challenger_name"]
    red_name = display_name(user)
    green_move, red_move = match["move"], move
    outcome = duel.winner_of(green_move, red_move)

    await query.edit_message_text(
        f"🏜 <b>مبارزه</b>\n{green_name} ({duel.FA_MOVE[green_move]}) "
        f"در برابر {red_name} ({duel.FA_MOVE[red_move]})\n\nدر حال نبرد…",
        parse_mode="HTML")

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        async with _render_slots:
            gif = await asyncio.to_thread(duel.make_duel_gif, green_name,
                                          red_name, green_move, red_move,
                                          secrets.randbelow(9999))
        if outcome == 0:
            caption = (f"🤝 مساوی — هر دو {duel.FA_MOVE[green_move]} "
                       f"انتخاب کردند." if green_move == red_move
                       else "🤝 مساوی")
        else:
            winner = green_name if outcome > 0 else red_name
            loser = red_name if outcome > 0 else green_name
            wmove = green_move if outcome > 0 else red_move
            lmove = red_move if outcome > 0 else green_move
            caption = (f"🏆 <b>{winner}</b> برد!\n"
                       f"{duel.FA_MOVE[wmove]} حریفِ {duel.FA_MOVE[lmove]} را شکست داد "
                       f"({loser} باخت)")
        await context.bot.send_animation(
            chat_id, animation=io.BytesIO(gif), filename="duel.gif",
            caption=caption, parse_mode="HTML",
            reply_to_message_id=match.get("message_id"))
    except Exception:
        log.exception("duel render failed in %s", chat_id)


async def set_roles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/challengers | /responders — owner only. `all` reopens it to everyone."""
    message = update.effective_message
    chat_id = message.chat_id
    key = CHALLENGERS if (message.text or "").lstrip("/").startswith("challeng") \
        else RESPONDERS
    label = "شروع مبارزه" if key == CHALLENGERS else "جواب دادن"

    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("این دستور فقط داخل گروه کار می‌کند.")
        return

    raw = await asyncio.to_thread(roster.get_setting, GAME, key)

    if not context.args:
        if not raw or raw == "all":
            await message.reply_text(f"اجازهٔ {label}: همه\n"
                                     f"/{key} <اسم‌ها> یا /{key} all")
            return
        people = await chat_candidates(chat_id)
        by_id = {c.user_id: c for c in people}
        names = [(seed.display_for(by_id[int(i)]) or by_id[int(i)].display)
                 if int(i) in by_id else i for i in raw.split(",")]
        await message.reply_text(f"اجازهٔ {label}: " + "، ".join(names))
        return

    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر می‌تواند این را تنظیم کند. ⛔️")
        return

    if context.args[0].lower() in ("all", "همه"):
        await asyncio.to_thread(roster.set_setting, GAME, key, "all")
        await message.reply_text(f"اجازهٔ {label}: همه ✅")
        return

    people = await chat_candidates(chat_id)
    ids, named, missed = [], [], []
    for raw_name in " ".join(context.args).replace("،", ",").split(","):
        raw_name = raw_name.strip()
        if not raw_name:
            continue
        target, _ = best_match(raw_name, people)
        if not target:
            missed.append(raw_name)
            continue
        uid = await ensure_user_id(context.bot, target)
        if not uid:
            missed.append(raw_name)
            continue
        ids.append(str(uid))
        named.append(seed.display_for(target) or target.display)

    if not ids:
        await message.reply_text("کسی پیدا نشد: " + "، ".join(missed))
        return

    await asyncio.to_thread(roster.set_setting, GAME, key, ",".join(ids))
    text = f"اجازهٔ {label}: " + "، ".join(named)
    if missed:
        text += "\nپیدا نشد: " + "، ".join(missed)
    await message.reply_text(text + " ✅")




# ================================================================ the journey

ADMIN_HANDLE = os.environ.get("ADMIN_HANDLE", "Aidinhagh").lstrip("@")

# Numeric ids are the authoritative list. A username has to be resolved through
# Telegram and cached, and anything that depends on a lookup can fail open if
# the lookup misbehaves — an id cannot. Set ADMIN_IDS=123,456 and be certain.
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
# BotFather → /newapp → short name. Needed because a web_app button only works
# in private chats; a Direct Link Mini App opens from a group too.
MINIAPP = os.environ.get("MINIAPP_SHORT_NAME", "").strip()
_bot_username: str | None = None
EXACT_STEPS = True          # a 4 means four roads, not "up to four"

# One animal is out there on every ride. The bot picks it, not the page, so the
# result can be trusted enough to announce and to keep score.
QUARRY = [("deer", 34), ("zebra", 26), ("lion", 20), ("tiger", 15), ("eagle", 5)]
QUARRY_FA = {"lion": "شیر", "tiger": "ببر", "deer": "آهو",
             "zebra": "گورخر", "eagle": "عقاب"}


def pick_quarry() -> str:
    roll = secrets.randbelow(sum(w for _, w in QUARRY))
    for animal, weight in QUARRY:
        roll -= weight
        if roll < 0:
            return animal
    return "deer"
PAGE = 8                    # destinations per keyboard page

# Everyone has until 20:00 Tehran to pick. Iran dropped daylight saving in
# 2022, so +03:30 holds all year and a fixed offset is safe.
TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))
DEADLINE_HOUR = 20

# Board state — who is where, and who is behind which nickname — is the admin's
# alone. With this on, nothing that names a player alongside a place is ever
# sent to a group: the player is told privately, the admin is told, and the
# group sees only that something happened.
PRIVATE_BOARD = True
DART_EMOJI = "🎯"
DICE_EMOJI = "🎲"
DICE_PAUSE = 4.2            # let the Telegram randomizer animation land

# token -> {chat_id, user_id, name, origin, roll, options, page}
_journeys: dict[str, dict] = {}


def fa_num(n: int) -> str:
    return str(n).translate(_FA_DIGITS)


# Every group is the same game. Positions, nicknames, votes, roads, hunts and
# the dead all live on one board, so it makes no difference which chat someone
# happens to be typing in. Real chat ids are still used to SEND messages — this
# constant only ever keys stored data.
GAME = 0

HOME_KEY = "home"          # kept so old settings rows do not confuse anything


async def board_for(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """There is one board. Groups and private chats all read and write it.

    This used to resolve which group a private chat referred to, remembered per
    user and sticky — which meant a DM could answer about a group nobody was
    playing in any more, and look like lost data.
    """
    return GAME, None


async def admin_id(bot) -> int | None:
    """Numeric id for the person the reports go to."""
    cached = await asyncio.to_thread(roster.known_handle, ADMIN_HANDLE)
    if cached:
        return cached
    try:
        chat = await bot.get_chat(f"@{ADMIN_HANDLE}")
    except Exception as exc:
        log.info("cannot resolve admin @%s: %s", ADMIN_HANDLE, exc)
        return None
    if chat and chat.id:
        await asyncio.to_thread(roster.store_handle, ADMIN_HANDLE, chat.id)
        return chat.id
    return None


async def report_to_admin(bot, chat_title: str, name: str, origin: str | None,
                          place: str, roll: int | None) -> None:
    """DM the move. Telegram forbids bots from opening a chat, so this only
    works once the admin has pressed Start in the bot's private chat."""
    target = await admin_id(bot)
    if not target:
        return
    line = (f"🐫 <b>{name}</b>\n"
            f"{worldmap.name_of(origin) + ' ← ' if origin else 'شروع: '}"
            f"<b>{worldmap.describe(place)}</b>")
    if roll:
        line += f"\nتاس: {fa_num(roll)}"
    line += f"\nگروه: {chat_title}"
    try:
        await bot.send_message(target, line, parse_mode="HTML")
    except Exception as exc:
        log.info("admin DM failed (has @%s started the bot?): %s",
                 ADMIN_HANDLE, exc)


async def ride_button(bot, token: str, base: str | None):
    """A link the group can actually open.

    web_app buttons are private-chat only, so in a group we need either a
    Direct Link Mini App (t.me/<bot>/<app>?startapp=<token>) or, failing that,
    a plain URL that opens the ride in the browser.
    """
    global _bot_username
    if MINIAPP:
        if _bot_username is None:
            try:
                _bot_username = (await bot.get_me()).username
            except Exception as exc:
                log.warning("get_me failed: %s", exc)
                _bot_username = ""
        if _bot_username:
            url = f"https://t.me/{_bot_username}/{MINIAPP}?startapp={token}"
            return InlineKeyboardMarkup([[
                InlineKeyboardButton("🐫 سوار شو", url=url)]])

    if base:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🐫 سوار شو", url=f"{base}/ride?t={token}")]])
    return None


async def report_roll(bot, chat_id: int, label: str, real: str,
                      origin: str | None, dart_roll: int,
                      dice_roll: int | None) -> None:
    """Tell the admin about the dart and, after the first trip, the dice."""
    target = await admin_id(bot)
    if not target:
        return
    where = worldmap.name_of(origin) if origin else "—"
    line = (f"🎯 <b>{label}</b> ({real})\n"
            f"از {where} · دارت {fa_num(dart_roll)}")
    if dice_roll is None:
        line += " · اولین سفر"
    else:
        line += f" · تاس {fa_num(dice_roll)}"
    try:
        await bot.send_message(target, line, parse_mode="HTML")
    except Exception as exc:
        log.info("roll report failed: %s", exc)


def destination_keyboard(token: str, options: list[str], page: int,
                         ride_url: str | None = None) -> InlineKeyboardMarkup:
    pages = max(1, (len(options) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for pid in options[page * PAGE:(page + 1) * PAGE]:
        rows.append([InlineKeyboardButton(worldmap.short_describe(pid),
                                          callback_data=f"t|go|{token}|{pid}")])
    if pages > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=f"t|pg|{token}|{page - 1}"),
            InlineKeyboardButton(f"{fa_num(page + 1)}/{fa_num(pages)}",
                                 callback_data="t|nop|0|0"),
            InlineKeyboardButton("▶️", callback_data=f"t|pg|{token}|{page + 1}"),
        ])
    return InlineKeyboardMarkup(rows)


async def chat_graph(chat_id: int = GAME):
    closed, extra = await asyncio.to_thread(roster.roadwork, GAME)
    return worldmap.build_graph(closed, extra)


async def visible_options(bot, chat_id: int, user_id: int, origin: str | None,
                          options: list[str]) -> list[str]:
    """Strip out anywhere this player has not earned the right to see.

    The silo is never offered, never listed and never mentioned — the only way
    to find it is to have taken an eagle and then stand in the cemetery.
    """
    allowed = []
    earned = await asyncio.to_thread(roster.has_killed, GAME, user_id, "eagle")
    for pid in options:
        if pid in worldmap.SECRET:
            continue
        allowed.append(pid)
    if earned and origin == "baqi" and "missile_silo" not in allowed:
        allowed.append("missile_silo")          # any roll will do, from there
    return allowed


async def travel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/travel — first trip is a free choice; after that, roll for range."""
    message = update.effective_message
    chat_id, asked = await board_for(update, context)
    if chat_id is None:
        return

    user = message.from_user

    if await asyncio.to_thread(roster.is_dead, GAME, user.id):
        await message.reply_text("تو از بازی خارج شده‌ای. ⚰️")
        return
    if past_deadline():
        await message.reply_text(
            f"مهلت امروز تمام شد (ساعت {fa_num(DEADLINE_HOUR)} تهران).\n"
            f"{time_until_open()} دیگر دوباره باز می‌شود."
        )
        return
    if await asyncio.to_thread(roster.last_move, GAME, user.id) >= day_start():
        here = await asyncio.to_thread(roster.get_place, GAME, user.id)
        await message.reply_text(
            f"امروز جابه‌جا شدی — {worldmap.describe(here) if here else ''}\n"
            f"روزی یک بار. {time_until_open()} دیگر دوباره می‌توانی."
        )
        return

    real = display_name(user)
    # kept for the admin's report only; players never see each other's nicknames
    name = await game_name(chat_id, user.id, real)
    origin = await asyncio.to_thread(roster.get_place, GAME, user.id)

    adj = await chat_graph(chat_id)

    # Every /travel throws a dart, including the very first trip.
    dart_sent = await context.bot.send_dice(
        message.chat_id, emoji=DART_EMOJI,
        reply_to_message_id=message.message_id
    )
    dart_roll = dart_sent.dice.value

    if origin is None:
        # First trip: dart only. Starting location is still a free choice.
        roll = None
        await asyncio.sleep(DICE_PAUSE)
        options = list(worldmap.IDS)
        head = (f"🗺 <b>{real}</b> هنوز جایی نیست.\n"
                f"برای شروع هر جای نقشه را می‌توانی انتخاب کنی:")
    else:
        # Later trips: dart + normal dice. Only the dice controls travel range.
        sent = await context.bot.send_dice(
            message.chat_id, emoji=DICE_EMOJI,
            reply_to_message_id=message.message_id
        )
        roll = sent.dice.value
        await asyncio.sleep(DICE_PAUSE)
        options = worldmap.reachable(origin, roll, exact=EXACT_STEPS, adj=adj)
        if not options:
            options = worldmap.reachable(origin, roll, exact=False, adj=adj)
        head = (f"🎯 <b>{real}</b> عدد {fa_num(roll)} آورد.\n"
                f"از <b>{worldmap.describe(origin)}</b> می‌توانی بروی به:")

    await report_roll(context.bot, chat_id, name, real, origin, dart_roll, roll)

    options = await visible_options(context.bot, chat_id, user.id, origin, options)
    if not options:
        await message.reply_text("از اینجا راهی باز نیست. 🚧")
        return

    token = secrets.token_urlsafe(6)
    _journeys[token] = {"chat_id": chat_id, "user_id": user.id, "name": name,
                        "real": real, "origin": origin, "roll": roll,
                        "options": options, "born": time.time()}
    await message.reply_text(head, parse_mode="HTML",
                             reply_markup=destination_keyboard(token, options, 0))


async def on_travel_button(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split("|")
    if len(parts) != 4:
        await query.answer()
        return
    _, action, token, arg = parts

    if action == "nop":
        await query.answer()
        return

    if action == "home":
        # one board now: nothing to choose
        await query.answer()
        return

    trip = _journeys.get(token)
    if not trip:
        await query.answer("این سفر منقضی شده. دوباره /travel بزن.",
                           show_alert=True)
        return
    if query.from_user.id != trip["user_id"]:
        await query.answer("این سفر مال تو نیست.", show_alert=True)
        return

    if action == "pg":
        await query.answer()
        await query.edit_message_reply_markup(
            destination_keyboard(token, trip["options"], int(arg)))
        return

    if arg not in trip["options"]:
        await query.answer("از اینجا نمی‌شود به آنجا رفت.", show_alert=True)
        return

    _journeys.pop(token, None)
    chat_id, user_id, name = trip["chat_id"], trip["user_id"], trip["name"]
    origin, roll = trip["origin"], trip["roll"]
    real = trip.get("real") or name

    # keep the real name in the record: the nickname is a display layer, and
    # writing it into players.name wiped the only copy of who this actually is
    await asyncio.to_thread(roster.set_place, GAME, user_id, real, arg)
    await asyncio.to_thread(roster.log_travel, GAME, user_id, real, arg,
                            origin, roll)
    # Real names on the road. A nickname is something only the admin knows, so
    # showing other riders' nicknames would hand them out to everyone who
    # travels to the same place.
    others = await asyncio.to_thread(roster.others_at, GAME, arg, user_id, 3)

    await query.answer(f"راهیِ {worldmap.name_of(arg)} شدی 🐫")

    # Who is already there is deliberately NOT announced — you find that out by
    # seeing them on the road during the ride.
    full = (f"🐫 <b>{real}</b> از "
            f"<b>{worldmap.describe(origin) if origin else 'ناکجا'}</b> "
            f"راهی <b>{worldmap.describe(arg)}</b> شد.")
    in_group = query.message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    # In a group the destination is withheld: the rider gets it privately, the
    # admin gets it, and everyone else sees only that a move was made.
    text = ("🐫 <b>" + real + "</b> حرکتش را انتخاب کرد."
            if (PRIVATE_BOARD and in_group) else full)

    ride_token = secrets.token_urlsafe(8).replace("-", "_")
    await asyncio.to_thread(roster.save_trip, ride_token, json.dumps({
        "to": arg, "kind": worldmap.KIND.get(arg, "oasis"),
        "title": worldmap.name_of(arg), "name": real,
        "from": worldmap.name_of(origin) if origin else "",
        "others": others,
        "quarry": pick_quarry(),
        # server-side only; stripped before the page ever sees it
        "chat_id": GAME, "user_id": user_id,
        # where the trip was actually started. A ride begun in the bot's DM
        # should not announce its hunt to the group.
        "from_chat": query.message.chat_id if query.message else chat_id,
    }))
    markup = await ride_button(context.bot, ride_token, web.public_url())
    if markup is None:
        text += ("\n\n<i>سواری باز نمی‌شود: دامنه یا MINIAPP_SHORT_NAME "
                 "تنظیم نشده — /webcheck</i>")
        log.warning("no ride button: public_url=%r MINIAPP=%r",
                    web.public_url(), MINIAPP)

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

    # chat_id is the shared board (0) — say where they actually did it
    title = (query.message.chat.title
             or ("پیوی" if query.message.chat.type == ChatType.PRIVATE
                 else str(query.message.chat_id)))
    await report_to_admin(context.bot, title, name, origin, arg, roll)

    # tell the rider where they went, out of everyone else's sight
    if PRIVATE_BOARD and in_group:
        try:
            await context.bot.send_message(user_id, full, parse_mode="HTML",
                                           reply_markup=markup)
        except Exception as exc:
            log.info("private move note failed for %s: %s", user_id, exc)


# ------------------------------------------------------- looking after it all

async def where(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/where [name] — one player, or everyone. Admin only."""
    if not await admin_only(update, context):
        return
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    query = " ".join(context.args or [])

    if query:
        target, _ = best_match(query, await chat_candidates(chat_id))
        if not target:
            await message.reply_text("کسی پیدا نشد.")
            return
        place = await asyncio.to_thread(roster.get_place, GAME,
                                        target.user_id)
        label = await game_name(chat_id, target.user_id,
                                seed.display_for(target) or target.display)
        where = "نامعلوم" if place in worldmap.SECRET else worldmap.describe(place)
        await message.reply_text(
            f"{label}: {where}" if place else f"{label} هنوز وارد نقشه نشده."
        )
        return

    rows = await asyncio.to_thread(roster.all_players, GAME)
    gone = await asyncio.to_thread(roster.dead_ids, GAME)
    nicks = await asyncio.to_thread(roster.all_nicks, GAME)
    rows = [r for r in rows if r[0] not in gone]
    if not rows:
        await message.reply_text("هنوز کسی روی نقشه نیست.")
        return
    lines = ["🗺 <b>موقعیت‌ها</b>", ""]
    for uid, name, place, _ts in rows:
        # a player standing in the silo shows as unknown: naming it publicly
        # would give the secret away to everyone at once
        where = "نامعلوم" if place in worldmap.SECRET else worldmap.describe(place)
        lines.append(f"• {nicks.get(uid, name)} — {where}")
    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def set_location(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setloc <person> = <place> — admin only, and works in the bot's DM."""
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return

    raw = " ".join(context.args or [])
    if "=" not in raw:
        await message.reply_text("/setloc <اسم> = <مکان>")
        return
    who, _, where_text = raw.partition("=")

    target, _ = best_match(who.strip(), await chat_candidates(chat_id))
    if not target:
        await message.reply_text(f"کسی به اسم «{who.strip()}» پیدا نشد.")
        return
    place = worldmap.find(where_text.strip())
    if not place:
        await message.reply_text(f"مکانی به اسم «{where_text.strip()}» نداریم.")
        return

    uid = await ensure_user_id(context.bot, target)
    if not uid:
        await message.reply_text("این نفر هنوز آی‌دی ندارد — یک پیام بدهد.")
        return
    label = seed.display_for(target) or target.display
    await asyncio.to_thread(roster.set_place, GAME, uid, label, place)
    shown = await game_name(chat_id, uid, label)
    await message.reply_text(f"{shown} → {worldmap.name_of(place)} ✅")


async def clear_locations(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clearlocs [name|all] — admin only, and works in the bot's DM."""
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return

    arg = " ".join(context.args or []).strip()
    if arg.lower() in ("all", "همه", ""):
        removed = await asyncio.to_thread(roster.clear_all_places, GAME)
        await message.reply_text(
            f"{fa_num(removed)} موقعیت پاک شد. همه از اول شروع می‌کنند. 🧹")
        return

    target, _ = best_match(arg, await chat_candidates(chat_id))
    if not target:
        await message.reply_text("کسی پیدا نشد.")
        return
    uid = await ensure_user_id(context.bot, target)
    removed = await asyncio.to_thread(roster.clear_place, GAME, uid or 0)
    label = seed.display_for(target) or target.display
    await message.reply_text(
        f"موقعیت {label} پاک شد." if removed else f"{label} روی نقشه نبود.")


async def webcheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/webcheck — why the ride button is or isn't there. Admin only."""
    if not await admin_only(update, context):
        return
    global _bot_username
    if _bot_username is None:
        try:
            _bot_username = (await context.bot.get_me()).username
        except Exception:
            _bot_username = ""
    base = web.public_url()
    counts = await asyncio.to_thread(roster.counts)
    lines = [
        f"نسخه: {VERSION}",
        f"دیتابیس: <code>{roster.DB_PATH}</code>"
        f"{' ⚠️ موقتی' if not roster.DB_PATH.startswith('/data') else ' ✅'}",
        f"بازیکن‌ها: {counts.get('players', 0)} · لقب‌ها: {counts.get('nicknames', 0)}"
        f" · رأی‌ها: {counts.get('votes', 0)}",
        f"RAILWAY_PUBLIC_DOMAIN: {os.environ.get('RAILWAY_PUBLIC_DOMAIN') or '—'}",
        f"WEBAPP_URL: {os.environ.get('WEBAPP_URL') or '—'}",
        f"public_url(): {base or '—'}",
        f"MINIAPP_SHORT_NAME: {MINIAPP or '—'}",
        f"bot: @{_bot_username or '—'}",
    ]
    if MINIAPP and _bot_username:
        lines.append(f"لینک: https://t.me/{_bot_username}/{MINIAPP}?startapp=test")
    elif base:
        lines.append(f"مرورگر: {base}/ride?t=test")
    else:
        lines.append("هیچ آدرسی تنظیم نشده — دکمهٔ سواری ساخته نمی‌شود.")
    await update.effective_message.reply_text("\n".join(lines))


async def hunts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hunts — who has actually hit anything this week."""
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    rows = await asyncio.to_thread(roster.hunt_tally, GAME)
    if not rows:
        await message.reply_text("این هفته کسی شکاری نکرده. 🏹")
        return
    lines = ["🏹 <b>شکار هفته</b>", ""]
    for i, (name, kills, tries) in enumerate(rows[:10]):
        rank = MEDALS[i] if i < 3 else f"{fa_num(i+1)}."
        lines.append(f"{rank} {name} — {fa_num(kills or 0)} از {fa_num(tries)}")
    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def resolve_person(bot, chat_id: int, text: str):
    """Find someone by @username, numeric id, or name.

    An id or a handle is exact, which matters for admin commands — you do not
    want a fuzzy match deciding who just died.
    """
    text = (text or "").strip()
    if not text:
        return None, None

    if text.isdigit():
        uid = int(text)
        rows = await asyncio.to_thread(roster.all_players, GAME)
        for row_id, name, *_ in rows:
            if row_id == uid:
                return uid, name
        people = await asyncio.to_thread(roster.members, GAME)
        for cand in people:
            if cand.user_id == uid:
                return uid, cand.display
        return uid, str(uid)          # unseen, but the id is unambiguous

    handle = text.lstrip("@")
    if handle:
        cached = await asyncio.to_thread(roster.known_handle, handle)
        if cached:
            people = await asyncio.to_thread(roster.members, GAME)
            for cand in people:
                if cand.user_id == cached:
                    return cached, cand.display
            return cached, f"@{handle}"
        if text.startswith("@"):
            # Never seen. Give them a placeholder so they can be nicknamed and
            # placed now; the rows move onto the real account the first time
            # Telegram shows the bot that username.
            return roster.placeholder_id(handle), f"@{handle}"

    target, _score = best_match(text, await chat_candidates(chat_id))
    if not target:
        return None, None
    uid = await ensure_user_id(bot, target)
    return (uid or None), (seed.display_for(target) or target.display)


async def set_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/nick <person> = <nickname> — admin only. `/nick <person> =` clears it.

    Examples:
      /nick @SomeUsername = شمر
      /nick 123456789 = شمر

    Numeric IDs work even before the user appears in the group.  If Telegram has
    never exposed an @username's numeric ID to this bot, the username assignment
    is stored and attached automatically the first time that user is observed.
    """
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return

    raw = " ".join(context.args or [])
    if "=" not in raw:
        await message.reply_text(
            "/nick @username = <لقب>\n"
            "/nick 123456789 = <لقب>")
        return

    who, _, nick = raw.partition("=")
    who = who.strip()
    nick = nick.strip()[:20]

    # --- exact numeric Telegram ID -----------------------------------------
    # Store a pending copy first. If roster.set_nick can already accept this
    # unseen ID it becomes active immediately; otherwise track() will bind it
    # after roster.remember() sees the user for the first time.
    if who.isdigit():
        uid = int(who)
        id_key = _pending_nick_id_key(uid)
        await asyncio.to_thread(roster.set_setting, GAME, id_key, nick)

        try:
            await asyncio.to_thread(roster.set_nick, GAME, uid, nick or None)
        except Exception as exc:
            log.info("nickname for unseen id %s saved pending: %s", uid, exc)

        # If this id had already been checked earlier in this process, allow a
        # future observation to check the newly-written pending value.
        for key in list(_pending_nick_checked):
            if key[0] == chat_id and key[1] == uid:
                _pending_nick_checked.discard(key)

        if nick:
            await message.reply_text(f"{uid} → «{nick}» ✅")
        else:
            await message.reply_text(f"لقب {uid} پاک شد.")
        return

    # --- exact @username ---------------------------------------------------
    if who.startswith("@") and len(who) > 1 and not any(c.isspace() for c in who):
        handle = who[1:]
        user_key = _pending_nick_user_key(handle)

        # This makes an unknown username assignable immediately as a pending
        # identity, instead of answering "not found".
        await asyncio.to_thread(roster.set_setting, GAME, user_key, nick)

        cached = await asyncio.to_thread(roster.known_handle, handle)
        if cached:
            # We already know this username's numeric ID from any previous
            # observation, so activate the nickname now as well.
            await asyncio.to_thread(
                roster.set_setting, GAME, _pending_nick_id_key(cached), nick
            )
            try:
                await asyncio.to_thread(
                    roster.set_nick, GAME, cached, nick or None
                )
            except Exception as exc:
                log.info("nickname for @%s/%s saved pending: %s",
                         handle, cached, exc)

            for key in list(_pending_nick_checked):
                if key[0] == chat_id and key[1] == cached:
                    _pending_nick_checked.discard(key)

            if nick:
                await message.reply_text(f"@{handle} → «{nick}» ✅")
            else:
                await message.reply_text(f"لقب @{handle} پاک شد.")
        else:
            if nick:
                await message.reply_text(
                    f"@{handle} → «{nick}» ✅\n"
                    "ذخیره شد؛ لازم نیست قبلاً در گروه پیام داده باشد. "
                    "به محض اینکه تلگرام آی‌دی این یوزرنیم را به ربات نشان بدهد، "
                    "لقب خودکار به همان کاربر وصل می‌شود.")
            else:
                await message.reply_text(f"لقب ذخیره‌شده برای @{handle} پاک شد.")
        return

    # --- ordinary known name ----------------------------------------------
    # Keep the old fuzzy/name behaviour for people the bot already knows.
    uid, real = await resolve_person(context.bot, chat_id, who)
    if not uid:
        await message.reply_text(
            f"«{who}» پیدا نشد. از @یوزرنیم یا آی‌دی عددی استفاده کن.")
        return

    await asyncio.to_thread(roster.set_nick, GAME, uid, nick or None)
    await message.reply_text(f"{real} → «{nick}» ✅" if nick
                             else f"لقب {real} پاک شد.")


async def rollcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rollcall — real names, nicknames and positions. Admin only."""
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return

    rows = await asyncio.to_thread(roster.all_players, GAME)
    nicks = await asyncio.to_thread(roster.all_nicks, GAME)
    gone = await asyncio.to_thread(roster.dead_ids, GAME)
    rows = [r for r in rows if r[0] not in gone]
    if not rows:
        await message.reply_text("هنوز کسی روی نقشه نیست.")
        return
    lines = ["🗒 <b>اسامی واقعی</b>", ""]
    for uid, name, place, _ts in rows:
        nick = nicks.get(uid)
        who = f"{name} ({nick})" if nick else name
        lines.append(f"• {who} — {worldmap.describe(place)}")
    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def board(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/board — nicknames and positions only. Admin only, but safe to forward."""
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return

    rows = await asyncio.to_thread(roster.all_players, GAME)
    nicks = await asyncio.to_thread(roster.all_nicks, GAME)
    gone = await asyncio.to_thread(roster.dead_ids, GAME)
    rows = [r for r in rows if r[0] not in gone]
    if not rows:
        await message.reply_text("هنوز کسی روی نقشه نیست.")
        return
    lines = ["🗺 <b>موقعیت‌ها</b>", ""]
    for uid, name, place, _ts in rows:
        lines.append(f"• {nicks.get(uid, '؟')} — {worldmap.describe(place)}")
    unnamed = [uid for uid, *_ in rows if uid not in nicks]
    if unnamed:
        lines.append(f"\n({fa_num(len(unnamed))} نفر هنوز لقب ندارند)")
    await message.reply_text("\n".join(lines), parse_mode="HTML")


def day_start(now: dt.datetime | None = None) -> float:
    """Midnight Tehran of the current day.

    A round is a calendar day: you may move once, any time between 00:00 and
    20:00. Anyone whose last move predates this has not chosen today.
    """
    now = now or dt.datetime.now(TEHRAN)
    return now.replace(hour=0, minute=0, second=0,
                       microsecond=0).timestamp()


def past_deadline(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(TEHRAN)
    return now.hour >= DEADLINE_HOUR


def time_until_open(now: dt.datetime | None = None) -> str:
    """How long until the window opens again, in Persian."""
    now = now or dt.datetime.now(TEHRAN)
    nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    left = nxt - now
    hours = int(left.total_seconds() // 3600)
    mins = int((left.total_seconds() % 3600) // 60)
    if hours:
        return f"{fa_num(hours)} ساعت و {fa_num(mins)} دقیقه"
    return f"{fa_num(mins)} دقیقه"


async def assign_missing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """20:00 Tehran: anyone who has not chosen gets sent somewhere at random.

    A player with a position is moved somewhere a dice roll could have taken
    them, so an automatic move obeys the same rules as a chosen one. A player
    with no position at all is simply dropped anywhere on the map, exactly like
    a first turn.
    """
    since = day_start()                  # anyone who has not moved today
    try:
        stale = await asyncio.to_thread(roster.stale_players, GAME, since)
    except Exception:
        stale = []
    if stale:

        nicks = await asyncio.to_thread(roster.all_nicks, GAME)
        gone = await asyncio.to_thread(roster.dead_ids, GAME)
        adj = await chat_graph()
        moved = []
        for user_id, name, place in stale:
            if user_id in gone:
                continue
            if place:
                roll = secrets.randbelow(6) + 1
                options = worldmap.reachable(place, roll, exact=EXACT_STEPS,
                                             adj=adj) \
                    or worldmap.reachable(place, roll, exact=False, adj=adj)
            else:
                roll, options = None, list(worldmap.IDS)
            options = [p for p in options if p not in worldmap.SECRET]
            if not options:
                continue
            dest = random.choice(options)
            label = nicks.get(user_id, name)
            await asyncio.to_thread(roster.set_place, GAME, user_id, name, dest)
            await asyncio.to_thread(roster.log_travel, GAME, user_id, name,
                                    dest, place, roll)
            moved.append((label, place, dest, roll))
            await report_to_admin(context.bot, "قرعهٔ روزانه", label, place,
                                  dest, roll)

        if not moved:
            return
        lines = ["⏰ <b>مهلت تمام شد</b>", "برای این افراد قرعه انداختم:", ""]
        for label, origin, dest, roll in moved[:25]:
            piece = f"• {label} → {worldmap.describe(dest)}"
            if roll:
                piece += f" (تاس {fa_num(roll)})"
            lines.append(piece)

        # The full draw goes to the admin only — it is a list of who is where,
        # which is exactly what must not appear in a group.
        target = await admin_id(context.bot)
        if target:
            try:
                await context.bot.send_message(target, "\n".join(lines),
                                               parse_mode="HTML")
            except Exception as exc:
                log.info("deadline report to admin failed: %s", exc)

        if not PRIVATE_BOARD:
            for group in await asyncio.to_thread(roster.groups):
                try:
                    await context.bot.send_message(group, "\n".join(lines),
                                                   parse_mode="HTML")
                except Exception as exc:
                    log.info("deadline announcement failed in %s: %s", group, exc)
        else:
            # the group learns only that the deadline passed
            for group in await asyncio.to_thread(roster.groups):
                try:
                    await context.bot.send_message(
                        group, f"⏰ مهلت امروز تمام شد. برای "
                               f"{fa_num(len(moved))} نفر قرعه افتاد.")
                except Exception as exc:
                    log.info("deadline note failed in %s: %s", group, exc)


async def deadline_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/draw — run the 20:00 draw immediately. Admin only, for testing."""
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return
    await assign_missing(context)
    await message.reply_text("قرعه انجام شد.")


async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kill <person> — out of the game. /revive brings them back."""
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return

    arg = " ".join(context.args or [])
    if message.reply_to_message and not arg:
        uid = message.reply_to_message.from_user.id
        real = display_name(message.reply_to_message.from_user)
    else:
        uid, real = await resolve_person(context.bot, chat_id, arg)
    if not uid:
        await message.reply_text("/kill <اسم یا @یوزرنیم یا آی‌دی>")
        return

    reviving = (message.text or "").lstrip("/").startswith("revive")
    await asyncio.to_thread(roster.set_dead, GAME, uid, not reviving)
    shown = await game_name(chat_id, uid, real)
    await message.reply_text(f"{shown} برگشت به بازی. ✅" if reviving
                             else f"{shown} از بازی خارج شد. ⚰️")


async def dead_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dead — who is out. Admin only."""
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return

    gone = await asyncio.to_thread(roster.dead_ids, GAME)
    if not gone:
        await message.reply_text("همه زنده‌اند.")
        return
    rows = {r[0]: r[1] for r in await asyncio.to_thread(roster.all_players, GAME)}
    nicks = await asyncio.to_thread(roster.all_nicks, GAME)
    lines = ["⚰️ <b>خارج‌شده‌ها</b>", ""]
    for uid in gone:
        name = rows.get(uid, str(uid))
        nick = nicks.get(uid)
        lines.append(f"• {name}" + (f" ({nick})" if nick else ""))
    await message.reply_text("\n".join(lines), parse_mode="HTML")


PUBLIC_HELP = """📖 <b>راهنما</b>

<b>رأی</b>
#رای ممد — رأی به یک نفر (روزی یک رأی)
#رایگیری — نتیجهٔ ۲۴ ساعت گذشته

<b>سفر روی نقشه</b>
/travel — تاس بینداز و جابه‌جا شو
  · روزی فقط یک بار، از ۰۰:۰۰ تا {deadline}:۰۰ به وقت تهران
  · بعد از {deadline} قرعه می‌افتد و جای کسانی که انتخاب نکرده‌اند تصادفی تعیین می‌شود
  · اولین سفرت هر جای نقشه می‌تواند باشد؛ بعد از آن تاس تعیین می‌کند تا کجا
<b>مبارزه</b>
/duel — چالش حمله / دفاع / حیله
  · حمله ← حیله، دفاع ← حمله، حیله ← دفاع
  · حرکت اول مخفی می‌ماند تا حریف انتخاب کند
/duel ممد یا ریپلای — چالش به یک نفر مشخص

<b>شکار</b>
هر سفر یک حیوان سر راهت هست. با کمان بزنش.
/hunts — جدول شکار هفته

<b>بقیه</b>
/help — همین راهنما
/whoami — آی‌دی خودت
"""

ADMIN_HELP = """

🔑 <b>فقط مدیر</b>
/nick ممد = زامبی — لقب (با @یوزرنیم یا آی‌دی عددی هم می‌شود)
/rollcall — اسم واقعی + لقب + موقعیت
/board — فقط لقب + موقعیت
/setloc ممد = کعبه — جابه‌جایی دستی
/clearlocs ممد | all — پاک کردن موقعیت
/kill ممد — خارج از بازی (نه رأی می‌دهد، نه رأی می‌گیرد، نه سفر)
/revive ممد — برگرداندن · /dead — لیست خارج‌شده‌ها
/draw — اجرای فوری قرعهٔ {deadline}
/where — موقعیت همه · /where ممد — یک نفر
/map — همهٔ مکان‌ها و جاده‌ها
/whois ممد — تست تشخیص اسم
/photo — چرا عکس پروفایل نمی‌آید
/ping · /webcheck — وضعیت ربات
/delete ممد | all — حذف رأی
/challengers · /responders — اجازهٔ مبارزه
/road خراب کعبه - واحه ۸ — خراب کردن جاده
/road بساز A - B · /road بازگردان A - B · /road لیست
همهٔ این‌ها در پیوی ربات هم کار می‌کنند."""


async def help_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — everything the bot does, with the admin half only for admins."""
    message = update.effective_message
    text = PUBLIC_HELP.replace("{deadline}", fa_num(DEADLINE_HOUR))
    chat_id, _ = (message.chat_id, None) if message.chat.type in (
        ChatType.GROUP, ChatType.SUPERGROUP) else (None, None)
    try:
        if await is_admin(context.bot, chat_id or 0, message.from_user.id):
            text += ADMIN_HELP.replace("{deadline}", fa_num(DEADLINE_HOUR))
    except Exception:
        pass
    await message.reply_text(text, parse_mode="HTML")


ROAD_SPLIT = re.compile(r"\s*(?:[-–—]|تا|به)\s*")


async def roadwork_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/road خراب کعبه - واحه ۸   ·   /road بساز A - B   ·   /road لیست

    Destroying a road removes it for everyone; building one adds a road the
    original map never had. Both persist and both are admin only.
    """
    message = update.effective_message
    chat_id, _ = await board_for(update, context)
    if chat_id is None:
        return
    if not await is_admin(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مدیر. ⛔️")
        return

    args = list(context.args or [])
    verb = (args[0].lower() if args else "")
    rest = " ".join(args[1:])

    if verb in ("list", "لیست", ""):
        closed, extra = await asyncio.to_thread(roster.roadwork, GAME)
        if not closed and not extra:
            await message.reply_text(
                "همهٔ جاده‌ها سالم‌اند.\n"
                "/road خراب <جا> - <جا>\n/road بساز <جا> - <جا>\n"
                "/road بازگردان <جا> - <جا>")
            return
        lines = []
        for a, b in closed:
            lines.append(f"🚧 {worldmap.name_of(a)} — {worldmap.name_of(b)}")
        for a, b in extra:
            lines.append(f"🛤 {worldmap.name_of(a)} — {worldmap.name_of(b)} (جدید)")
        await message.reply_text("\n".join(lines))
        return

    parts = [p for p in ROAD_SPLIT.split(rest) if p.strip()]
    if len(parts) != 2:
        await message.reply_text("دو مکان لازم است: /road خراب کعبه - واحه ۸")
        return
    a = worldmap.find(parts[0].strip())
    b = worldmap.find(parts[1].strip())
    if not a or not b:
        bad = parts[0] if not a else parts[1]
        await message.reply_text(f"مکانی به اسم «{bad.strip()}» نداریم.")
        return
    if a == b:
        await message.reply_text("یک مکان به خودش جاده ندارد.")
        return

    if verb in ("خراب", "destroy", "close", "ببند"):
        await asyncio.to_thread(roster.set_road, GAME, a, b, "closed")
        verdict = f"🚧 جادهٔ {worldmap.name_of(a)} — {worldmap.name_of(b)} خراب شد."
    elif verb in ("بساز", "build", "open", "بازکن"):
        await asyncio.to_thread(roster.set_road, GAME, a, b, "open")
        verdict = f"🛤 جادهٔ {worldmap.name_of(a)} — {worldmap.name_of(b)} ساخته شد."
    elif verb in ("بازگردان", "reset", "revert"):
        await asyncio.to_thread(roster.set_road, GAME, a, b, None)
        verdict = f"↩️ {worldmap.name_of(a)} — {worldmap.name_of(b)} به حالت اول برگشت."
    else:
        await message.reply_text("خراب / بساز / بازگردان / لیست")
        return

    adj = await chat_graph(chat_id)
    stranded = [worldmap.name_of(p) for p in worldmap.IDS
                if p not in worldmap.SECRET and not
                [n for n in adj[p] if n not in worldmap.SECRET]]
    if stranded:
        verdict += "\n⚠️ حالا این جاها هیچ راهی ندارند: " + "، ".join(stranded)
    await message.reply_text(verdict)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whoami — your id, and whether the bot treats you as its admin."""
    message = update.effective_message
    user = message.from_user
    chat_id = message.chat_id if message.chat_id < 0 else 0
    ok = await is_admin(context.bot, chat_id, user.id)
    lines = [f"id: <code>{user.id}</code>",
             f"@{user.username or '—'}",
             f"مدیر: {'بله ✅' if ok else 'خیر'}"]
    if ok:
        # only the admin sees the configuration — telling everyone who the
        # admin is, is itself information not worth handing out
        resolved = await admin_id(context.bot)
        lines.append(f"ADMIN_IDS: "
                     f"{', '.join(str(i) for i in sorted(ADMIN_IDS)) or '—'}")
        lines.append(f"ADMIN_HANDLE: @{ADMIN_HANDLE} → {resolved or 'حل نشد'}")
    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def show_map(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/map — the roads as they stand right now, closures included. Admin only."""
    if not await admin_only(update, context):
        return
    chat_id, _ = await board_for(update, context)
    text = worldmap.summary(await chat_graph(chat_id) if chat_id else None)
    for chunk in [text[i:i + 3500] for i in range(0, len(text), 3500)]:
        await update.effective_message.reply_text(chunk)


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not set")

    seed.install()

    folded = roster.fold_onto_board(GAME)
    if any(folded.values()):
        log.info("folded onto the shared board: %s",
                 {k: v for k, v in folded.items() if v})
    else:
        log.info("board already unified")

    # Python 3.14 removed the implicit loop that python-telegram-bot's
    # run_polling() still expects from asyncio.get_event_loop().
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    async def _boot(_app) -> None:
        web.bot = _app.bot          # so a hunt result can be announced in chat
        await web.start()

    app = Application.builder().token(TOKEN).post_init(_boot).build()
    fresh = filters.UpdateType.MESSAGE
    app.add_handler(MessageHandler(filters.ALL & fresh, track), group=0)
    app.add_handler(CommandHandler("start", start), group=1)
    app.add_handler(CommandHandler(["help", "commands", "rahnama"], help_all),
                    group=1)
    app.add_handler(CommandHandler("whois", who), group=1)
    app.add_handler(CommandHandler("photo", photo_check), group=1)
    app.add_handler(CommandHandler(["result", "natije"], show_tally), group=1)
    app.add_handler(CommandHandler("delete", delete_vote), group=1)
    app.add_handler(CommandHandler("ping", ping), group=1)
    app.add_handler(CommandHandler(["duel", "mobareze"], start_duel), group=1)
    app.add_handler(CommandHandler("challengers", set_roles), group=1)
    app.add_handler(CommandHandler("responders", set_roles), group=1)
    app.add_handler(CallbackQueryHandler(on_move, pattern=r"^d\|"), group=1)
    app.add_handler(CommandHandler(["travel", "safar"], travel), group=1)
    app.add_handler(CommandHandler(["where", "locations"], where), group=1)
    app.add_handler(CommandHandler("setloc", set_location), group=1)
    app.add_handler(CommandHandler("clearlocs", clear_locations), group=1)
    app.add_handler(CommandHandler("map", show_map), group=1)
    app.add_handler(CommandHandler("webcheck", webcheck), group=1)
    app.add_handler(CommandHandler("hunts", hunts), group=1)
    app.add_handler(CommandHandler("nick", set_nick), group=1)
    app.add_handler(CommandHandler("rollcall", rollcall), group=1)
    app.add_handler(CommandHandler("board", board), group=1)
    app.add_handler(CommandHandler("draw", deadline_now), group=1)
    app.add_handler(CommandHandler(["kill", "revive"], kill), group=1)
    app.add_handler(CommandHandler("dead", dead_list), group=1)
    app.add_handler(CommandHandler(["road", "roads"], roadwork_cmd), group=1)
    app.add_handler(CommandHandler("whoami", whoami), group=1)
    app.add_handler(CallbackQueryHandler(on_travel_button, pattern=r"^t\|"),
                    group=1)
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & fresh, on_trigger),
        group=1,
    )
    web.admin_id_getter = admin_id
    app.add_error_handler(on_error)

    if app.job_queue:
        app.job_queue.run_daily(
            assign_missing,
            time=dt.time(hour=DEADLINE_HOUR, minute=0, tzinfo=TEHRAN),
            name="deadline",
        )
        log.info("nightly draw scheduled for %02d:00 Tehran", DEADLINE_HOUR)
    else:
        log.warning("no job queue — install python-telegram-bot[job-queue] or "
                    "the 20:00 draw will never run")
    problems = worldmap.sanity()
    if problems:
        log.warning("map problems: %s", problems)
    log.info("mini app: %s", MINIAPP or "not set — rides open in the browser")
    log.info("admin: %s", f"ids {sorted(ADMIN_IDS)}" if ADMIN_IDS
             else f"@{ADMIN_HANDLE} (set ADMIN_IDS to be certain)")
    log.info("polling… version %s, db %s, %d seeded people, %d places",
             VERSION, roster.DB_PATH, len(seed.PEOPLE), len(worldmap.PLACES))
    # deliberately NOT Update.ALL_TYPES: message_reaction updates are noise
    # here, and asking for them only invites the bug they caused.
    app.run_polling(
        allowed_updates=["message", "callback_query", "chat_member",
                         "my_chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()