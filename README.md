# 🗳️ vote-shredder-bot

A Telegram group bot. Someone writes:

```
#رای آزادی
```

…and the bot replies with an animation: the word gets written onto a ballot
paper, the paper slides into the ballot box, the box rattles, and the vote
comes back out as confetti.

Pure Pillow — no ffmpeg, no headless browser, no system packages. Output is a
~380 KB GIF, rendered in about 2 seconds and cached per word.

---

## 1. Create the bot

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. **This step is not optional:** `/setprivacy` → pick your bot → **Disable**.
   With privacy *enabled* (the default) a bot only receives commands and
   replies addressed to it, so it will never see a plain `#رای` message in a
   group. If the bot is already in the group, remove it and re-add it after
   flipping this.
3. Add the bot to your group as a normal member.

## 2. Add the font

Download **Vazirmatn-Bold.ttf** (see `fonts/README.md`) and drop it either next
to `animator.py` or in a `fonts/` subfolder — both are searched, along with the
current directory. `animator.py` prints which font it picked on the first
render, and warns loudly if the font it found has no Persian glyphs. To force a
specific file, set `FONT_PATH=C:\path\to\Vazirmatn-Bold.ttf`.

Commit the font, otherwise Railway builds without it and words render as boxes.

## 3. Run it locally

**Python 3.12 is the safe choice.** On 3.14 two things break: Pillow has no
prebuilt Windows wheel (pip tries to compile it and fails), and
`asyncio.get_event_loop()` no longer creates a loop, which python-telegram-bot
21.x relies on (`RuntimeError: There is no current event loop`). `bot.py` works
around the second one, but `py -3.12` avoids both:

```bat
py -3.12 -m pip install -r requirements.txt
py -3.12 bot.py
```

Windows CMD uses `set`, not `export`:

```bat
set BOT_TOKEN=123456:ABC...
python bot.py
```

PowerShell:

```powershell
$env:BOT_TOKEN = "123456:ABC..."
python bot.py
```

Preview the animation without Telegram (no token needed):

```bat
python animator.py "آزادی"
```

### The word looks scrambled or the letters are disconnected

Persian needs shaping (letters change form depending on position) and bidi
reordering. There are two ways to get it, and doing both mangles the result —
`animator.py` picks one automatically and prints which:

- `raqm shaping: True` — Pillow does it, the raw string is passed through.
- `raqm shaping: False` — arabic_reshaper + python-bidi do it first.

Run `python animator.py "رای مردم"` and open `still.png` — that's the frame
with the finished word, quicker to check than scrubbing the GIF.

If `pip install` fails partway, nothing after the failing line gets installed —
that's why `ModuleNotFoundError: No module named 'telegram'` shows up right
after a Pillow build error. Fix the Pillow problem, re-run the install, and
`telegram` appears too.

## 4. Deploy on Railway

```bash
git init && git add -A && git commit -m "vote shredder bot"
# push to a GitHub repo
```

Then on [railway.app](https://railway.app):

1. **New Project → Deploy from GitHub repo** → pick the repo.
2. **Variables** → add `BOT_TOKEN` = your token.
   Add a **Volume** mounted at `/data` and set `DB_PATH=/data/roster.db`, or the
   roster is wiped on every deploy.
3. Deploy. `railway.json` already sets the start command (`python bot.py`) and
   restart policy; nixpacks reads `requirements.txt` and `.python-version`.

Notes:

- This is a **worker**, not a web service. It uses long polling, so it needs no
  public URL and no `PORT`. Railway will not give it a domain — that's correct.
  Ignore any "no ports detected" hint.
- Run exactly **one** instance. Two replicas polling the same token fight each
  other and you get duplicate or dropped replies.
- Railway sleeps free-tier services; if the bot goes quiet, check the plan.

---

## How it behaves

`#رای <name>` looks up who is meant, then shreds their vote with their profile
photo blurred into the background. If nobody matches, the raw text goes on the
ballot with no backdrop — nothing breaks.

Who gets matched, in order:

1. a **text_mention** (a name tapped in the compose box) — exact
2. an **@username** — exact
3. a **fuzzy cross-script match** against everyone the bot has seen here
4. the **message being replied to**, if 1–3 found nothing
5. nobody — plain word, no photo

### Matching across scripts

People write Persian names while Telegram profiles are in English. `matching.py`
reduces both to the same consonant skeleton — no vowels, and the sounds that
transliterate inconsistently collapse to one symbol:

| written | skeleton | | profile | skeleton |
|---|---|---|---|---|
| محمد | `mhmd` | | Mohammad / Muhammed / Mohamad | `mhmd` |
| شهرام | `1hrm` | | Shahram | `1hrm` |
| قاسمی | `4sm` | | Ghasemi | `4sm` |

Shortened names work by prefix (`علی` → Alireza, `mreza` → Mohammadreza). Purely
colloquial nicknames that no phonetic rule recovers — ممد for محمد, زری for زهرا
— live in the `ALIASES` dict at the bottom of `matching.py`. **Add your group's
own nicknames there**; it's the one part that needs local knowledge.

Test the matcher in the group without spamming animations:

```
/whois ممد        →  ممد → Mohammadreza Nouri (@mrezaa) · 0.80
/whois            →  how many members the bot currently knows
```

`THRESHOLD` in `matching.py` (0.74) is the confidence floor. Raise it if the bot
matches the wrong person; lower it if it misses people it should find.

### The roster — read this one

**The Bot API has no "list all group members" call.** `getChatAdministrators` is
the only bulk lookup a bot gets. So the roster is built by watching: every person
who sends a message, joins, gets replied to, or is tapped as a mention is
recorded in SQLite. Admins are pulled in automatically.

Consequences:

- A member who has never spoken since the bot joined **cannot be matched.** The
  group fills itself in over a day or two of normal use.
- Privacy mode must stay disabled, or the bot sees nothing to learn from.
- The roster lives in `roster.db`. **Railway's filesystem is wiped on every
  deploy**, so add a Volume (mount path `/data`) and set `DB_PATH=/data/roster.db`,
  otherwise the bot forgets everyone each time you push.

### Profile photos

Fetched with `getUserProfilePhotos`, cached in SQLite for 24 hours, then blurred,
desaturated and washed out behind a soft scrim so the ballot stays readable. A
user whose privacy settings hide their photo simply gets no backdrop — that path
is cached too, so the bot doesn't retry on every vote.

## Files

```
bot.py            Telegram side: tracking, resolution, cooldown, sending
matching.py       Cross-script name matching + the ALIASES nickname table
roster.py         SQLite: who has been seen here, and their cached avatar
animator.py       Frame-by-frame drawing, GIF encoding (runs standalone too)
fonts/            Drop Vazirmatn-Bold.ttf here
requirements.txt  python-telegram-bot, Pillow, arabic-reshaper, python-bidi
railway.json      Start command + restart policy
Procfile          Same thing for other hosts
```
