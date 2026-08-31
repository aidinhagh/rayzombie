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
import json
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
VERSION = "2026-08-27.1"

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
        "#رایگیری — نتیجه ۲۴ ساعت گذشته\n"
        "/duel — مبارزه: حمله / دفاع / حیله\n"
        "/travel — سفر روی نقشه\n"
        "/delete — فقط مالک گروه: حذف رأی\n\n"
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


ANONYMOUS_ADMIN_ID = 1087968824      # @GroupAnonymousBot


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
    if not sender or not await is_owner(context.bot, chat_id, sender.id):
        await message.reply_text("فقط مالک گروه می‌تواند رأی حذف کند. ⛔️")
        return

    query = " ".join(context.args or [])

    if query.strip().lower() in ("all", "همه"):
        removed = await asyncio.to_thread(roster.clear_votes, chat_id)
        await message.reply_text(f"همهٔ {fa(removed)} رأی ۲۴ ساعت گذشته پاک شد. 🧹")
        return

    if message.reply_to_message and not query:
        person = message.reply_to_message.from_user
        removed = await asyncio.to_thread(roster.delete_votes_by, chat_id, person.id)
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
    removed = await asyncio.to_thread(roster.delete_votes_for, chat_id, key, label)

    if removed:
        await message.reply_text(f"{fa(removed)} رأی داده‌شده به «{label}» حذف شد. 🗑")
    else:
        await message.reply_text(f"رأیی به «{label}» در ۲۴ ساعت گذشته ثبت نشده.")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ping — is the deployed code the code you think it is?"""
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
    raw = await asyncio.to_thread(roster.get_setting, chat_id, key)
    if not raw or raw == "all":
        return True
    return str(user_id) in raw.split(",")


def display_name(user) -> str:
    return (user.first_name or user.username or str(user.id))[:16]


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

    raw = await asyncio.to_thread(roster.get_setting, chat_id, key)

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

    if not await is_owner(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مالک گروه می‌تواند این را تنظیم کند. ⛔️")
        return

    if context.args[0].lower() in ("all", "همه"):
        await asyncio.to_thread(roster.set_setting, chat_id, key, "all")
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

    await asyncio.to_thread(roster.set_setting, chat_id, key, ",".join(ids))
    text = f"اجازهٔ {label}: " + "، ".join(named)
    if missed:
        text += "\nپیدا نشد: " + "، ".join(missed)
    await message.reply_text(text + " ✅")




# ================================================================ the journey

ADMIN_HANDLE = os.environ.get("ADMIN_HANDLE", "Aidinhagh").lstrip("@")
# BotFather → /newapp → short name. Needed because a web_app button only works
# in private chats; a Direct Link Mini App opens from a group too.
MINIAPP = os.environ.get("MINIAPP_SHORT_NAME", "").strip()
_bot_username: str | None = None
EXACT_STEPS = True          # a 4 means four roads, not "up to four"
PAGE = 8                    # destinations per keyboard page
DICE_PAUSE = 4.2            # let the dice animation land before answering

# token -> {chat_id, user_id, name, origin, roll, options, page}
_journeys: dict[str, dict] = {}


def fa_num(n: int) -> str:
    return str(n).translate(_FA_DIGITS)


HOME_KEY = "home"          # settings row (chat_id 0) remembering a DM's board


async def board_for(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Which group's board does this message act on?

    In a group: that group. In a DM there is no board of its own, so it acts on
    the group the person plays in — remembered after the first time, and asked
    about only when they belong to more than one.
    """
    message = update.effective_message
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return message.chat_id, None

    user_id = message.from_user.id
    saved = await asyncio.to_thread(roster.get_setting, 0, f"{HOME_KEY}:{user_id}")
    if saved:
        return int(saved), None

    chats = await asyncio.to_thread(roster.chats_for_user, user_id)
    if not chats:
        await message.reply_text(
            "اول باید در گروه یک پیام بدهی تا بشناسمت، بعد اینجا هم می‌شود بازی کرد."
        )
        return None, None
    if len(chats) == 1:
        await asyncio.to_thread(roster.set_setting, 0, f"{HOME_KEY}:{user_id}",
                                str(chats[0]))
        return chats[0], None

    rows = []
    for cid in chats[:6]:
        try:
            title = (await context.bot.get_chat(cid)).title or str(cid)
        except Exception:
            title = str(cid)
        rows.append([InlineKeyboardButton(title, callback_data=f"t|home|0|{cid}")])
    await message.reply_text("کدام گروه؟", reply_markup=InlineKeyboardMarkup(rows))
    return None, "asked"


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


async def travel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/travel — first trip is a free choice; after that, roll for range."""
    message = update.effective_message
    chat_id, asked = await board_for(update, context)
    if chat_id is None:
        return

    user = message.from_user
    name = display_name(user)
    origin = await asyncio.to_thread(roster.get_place, chat_id, user.id)

    if origin is None:
        options, roll = list(worldmap.IDS), None
        head = (f"🗺 <b>{name}</b> هنوز جایی نیست.\n"
                f"برای شروع هر جای نقشه را می‌توانی انتخاب کنی:")
    else:
        sent = await context.bot.send_dice(message.chat_id, emoji="🎲",
                                           reply_to_message_id=message.message_id)
        roll = sent.dice.value
        await asyncio.sleep(DICE_PAUSE)
        options = worldmap.reachable(origin, roll, exact=EXACT_STEPS)
        if not options:
            options = worldmap.reachable(origin, roll, exact=False)
        head = (f"🎲 <b>{name}</b> عدد {fa_num(roll)} آورد.\n"
                f"از <b>{worldmap.describe(origin)}</b> می‌توانی بروی به:")

    token = secrets.token_urlsafe(6)
    _journeys[token] = {"chat_id": chat_id, "user_id": user.id, "name": name,
                        "origin": origin, "roll": roll, "options": options,
                        "born": time.time()}
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
        await asyncio.to_thread(roster.set_setting, 0,
                                f"{HOME_KEY}:{query.from_user.id}", arg)
        await query.answer("ثبت شد.")
        await query.edit_message_text("گروه ثبت شد. حالا /travel بزن.")
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

    await asyncio.to_thread(roster.set_place, chat_id, user_id, name, arg)
    await asyncio.to_thread(roster.log_travel, chat_id, user_id, name, arg,
                            origin, roll)
    others = await asyncio.to_thread(roster.others_at, chat_id, arg, user_id, 3)

    await query.answer(f"راهیِ {worldmap.name_of(arg)} شدی 🐫")

    text = (f"🐫 <b>{name}</b> از "
            f"<b>{worldmap.describe(origin) if origin else 'ناکجا'}</b> "
            f"راهی <b>{worldmap.describe(arg)}</b> شد.")
    if others:
        text += "\nاینجا هستند: " + "، ".join(others)

    ride_token = secrets.token_urlsafe(8).replace("-", "_")
    await asyncio.to_thread(roster.save_trip, ride_token, json.dumps({
        "to": arg, "kind": worldmap.KIND.get(arg, "oasis"),
        "title": worldmap.name_of(arg), "name": name,
        "from": worldmap.name_of(origin) if origin else "",
        "others": others,
    }))
    markup = await ride_button(context.bot, ride_token, web.public_url())
    if markup is None:
        text += ("\n\n<i>سواری باز نمی‌شود: دامنه یا MINIAPP_SHORT_NAME "
                 "تنظیم نشده — /webcheck</i>")
        log.warning("no ride button: public_url=%r MINIAPP=%r",
                    web.public_url(), MINIAPP)

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)

    title = query.message.chat.title or str(chat_id)
    await report_to_admin(context.bot, title, name, origin, arg, roll)


# ------------------------------------------------------- looking after it all

async def where(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/where [name] — one player, or everyone."""
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
        place = await asyncio.to_thread(roster.get_place, chat_id,
                                        target.user_id)
        label = seed.display_for(target) or target.display
        await message.reply_text(
            f"{label}: {worldmap.describe(place)}" if place
            else f"{label} هنوز وارد نقشه نشده."
        )
        return

    rows = await asyncio.to_thread(roster.all_players, chat_id)
    if not rows:
        await message.reply_text("هنوز کسی روی نقشه نیست.")
        return
    lines = ["🗺 <b>موقعیت‌ها</b>", ""]
    for _uid, name, place, _ts in rows:
        lines.append(f"• {name} — {worldmap.describe(place)}")
    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def set_location(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setloc <person> = <place> — owner only."""
    message = update.effective_message
    chat_id = message.chat_id
    if not await is_owner(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مالک گروه. ⛔️")
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
    await asyncio.to_thread(roster.set_place, chat_id, uid, label, place)
    await message.reply_text(f"{label} → {worldmap.name_of(place)} ✅")


async def clear_locations(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clearlocs [name|all] — owner only."""
    message = update.effective_message
    chat_id = message.chat_id
    if not await is_owner(context.bot, chat_id, message.from_user.id):
        await message.reply_text("فقط مالک گروه. ⛔️")
        return

    arg = " ".join(context.args or []).strip()
    if arg.lower() in ("all", "همه", ""):
        removed = await asyncio.to_thread(roster.clear_all_places, chat_id)
        await message.reply_text(
            f"{fa_num(removed)} موقعیت پاک شد. همه از اول شروع می‌کنند. 🧹")
        return

    target, _ = best_match(arg, await chat_candidates(chat_id))
    if not target:
        await message.reply_text("کسی پیدا نشد.")
        return
    uid = await ensure_user_id(context.bot, target)
    removed = await asyncio.to_thread(roster.clear_place, chat_id, uid or 0)
    label = seed.display_for(target) or target.display
    await message.reply_text(
        f"موقعیت {label} پاک شد." if removed else f"{label} روی نقشه نبود.")


async def webcheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/webcheck — why the ride button is or isn't there."""
    global _bot_username
    if _bot_username is None:
        try:
            _bot_username = (await context.bot.get_me()).username
        except Exception:
            _bot_username = ""
    base = web.public_url()
    lines = [
        f"نسخه: {VERSION}",
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


async def show_map(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/map — what the bot believes the roads are, so it can be corrected."""
    text = worldmap.summary()
    for chunk in [text[i:i + 3500] for i in range(0, len(text), 3500)]:
        await update.effective_message.reply_text(chunk)


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

    async def _boot(_app) -> None:
        await web.start()

    app = Application.builder().token(TOKEN).post_init(_boot).build()
    fresh = filters.UpdateType.MESSAGE
    app.add_handler(MessageHandler(filters.ALL & fresh, track), group=0)
    app.add_handler(CommandHandler(["start", "help"], start), group=1)
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
    app.add_handler(CallbackQueryHandler(on_travel_button, pattern=r"^t\|"),
                    group=1)
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & fresh, on_trigger),
        group=1,
    )
    app.add_error_handler(on_error)
    problems = worldmap.sanity()
    if problems:
        log.warning("map problems: %s", problems)
    log.info("mini app: %s", MINIAPP or "not set — rides open in the browser")
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
