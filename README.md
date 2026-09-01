# 🗳️ vote-shredder-bot

A Telegram group bot. Someone writes:

```
#رای ممد
```

…and the bot replies with an animation: that person's name is written into wet
sand, then a red tide sweeps down the frame and washes it away, leaving stained
ground where the name was. Their profile photo is faintly impressed into the
sand behind it.

Pure Pillow — no ffmpeg, no headless browser, no system packages.

The desert floor is built in layers: a dune height field raked by a low sun,
wind ripples stretched along one axis, grit and dark specks, broad tonal drift,
then glare from the top-left and shadow falling into the far corner. Relief
comes from lighting a height field against a shifted copy of itself, so dunes
and ripples are actually lit rather than painted. Dust drifts across every frame
— fine motes plus a few blurred near-camera ones.

The letters are carved: dark inset core, lit rim up-left, deep shadow
down-right, and a pale ridge of displaced sand along the outside. The tide is a
band with an irregular edge — ahead of it the name is still there, behind it the
ground is wet and stained.

Output is a ~800 KB GIF, about three seconds to render, cached per name.

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
   The included `Dockerfile` makes the build explicit — Railway installs
   exactly `requirements.txt` on Python 3.12. If you'd rather let nixpacks
   infer it, delete the Dockerfile and set `"builder": "NIXPACKS"` in
   `railway.json`, but then confirm the build log actually shows a
   `pip install -r requirements.txt` line.
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

| written in the group | result |
|---|---|
| `#رای ممد` | washes that person's name away, avatar in the sand |
| `#رایگیری` | leaderboard of the last 24 hours |
| `#رای` alone, as a reply | votes for the person replied to |
| `#رای <unknown>` | plain word in the sand, no backdrop |
| `#رایانه چیست` | ignored — the hashtag must end at a word boundary |
| `/delete ...` | owner only, see below |
| `/duel [name]` | attack / defend / trick, see below |
| `/travel` | roll and move across the map, see below |

Who gets matched, in order:

1. a **text_mention** (a name tapped in the compose box) — exact
2. an **@username** — exact
3. a **fuzzy cross-script match** against the seed list + everyone seen here
4. the **message being replied to**, if 1–3 found nothing
5. nobody — plain word, no photo

### The seed list

`seed.py` holds the group's actual people: a @username (or numeric id) and every
spelling they go by. Names there are matched even before that person has ever
spoken, and the first name listed is what gets written in the sand.

```python
PEOPLE = [
    ("informer_mohammad", ["اینفرمر", "ممد", "محمد پیروز", "درخراب"], False),
    ("theforgottendreamer74", ["محو", "شتر"], True),   # immune
    ("527341236", ["داریوش"], False),                  # no username, id instead
]
```

The third field is **immune**: votes for that person are struck by lightning
before the tide arrives, and never counted in `#رایگیری`. The bot says the vote was
annulled rather than pretending it registered.

A seeded person with only a @username has no user id yet, so their avatar can't
be fetched until either they speak once in the group or Telegram resolves the
handle. Numeric-id entries work immediately.

### Counting

**One vote per person per 24 hours.** Not one per target — one, total. Vote
again before the window is up and the animation still plays, but the caption
says who you already voted for and how long is left; nothing is counted.

The exception is an immune target: a vote struck by lightning never counted, so
it doesn't burn the day's allowance either. Vote for محو and you can still vote
for someone real afterwards.

Rows older than a week are pruned on write. Cooldowns: 8 seconds per person and
2.5 seconds per group on votes, 15 seconds on the tally.

Only **freshly sent** messages are acted on. Anything with an `edit_date`, older
than two minutes, or already processed is ignored, and the bot no longer
subscribes to `message_reaction` updates at all. Without that, reacting to an old
`#رای X` message re-delivered it and cast the vote again, days later.

### Deleting votes — `/delete`

Owner only (`ChatMemberStatus.OWNER` — admins can't, deliberately).

| command | effect |
|---|---|
| `/delete ممد` | removes every vote cast **for** ممد in the window |
| `/delete` as a reply | removes that person's **own** vote, freeing them to vote again |
| `/delete all` | clears the whole 24-hour window |

The two directions do genuinely different things: deleting *for* a name fixes a
wrong tally, deleting *by* reply gives someone their vote back.

**Anonymous admins can't use it.** If you post with "Remain Anonymous" on,
Telegram replaces your identity with @GroupAnonymousBot and there is no way to
check who you are — the bot says so instead of failing silently. Turn anonymous
off for the one message.

### When something does nothing at all

`/ping` reports the running version and whether Telegram considers you the
owner. If the version isn't the one you just pushed, the deploy didn't land —
that alone explains most "the new command doesn't work" cases.

A global error handler is registered, so a crash inside any handler is logged
with a traceback *and* answered in the chat. Before it existed, an exception
mid-command looked exactly like the bot ignoring you.

### Matching across scripts

People write Persian names while Telegram profiles are in English. `matching.py`
reduces both to the same consonant skeleton — no vowels, and the sounds that
transliterate inconsistently collapse to one symbol:

| written | skeleton | | profile | skeleton |
|---|---|---|---|---|
| محمد | `mhmd` | | Mohammad / Muhammed / Mohamad | `mhmd` |
| شهرام | `1hrm` | | Shahram | `1hrm` |
| قاسمی | `4sm` | | Ghasemi | `4sm` |

Shortened names work by prefix (`علی` → Alireza, `mreza` → Mohammadreza).

Three hand-maintained tables cover what phonetics can't. They do different jobs
— mixing them up is a syntax error:

**`seed.py` → `PEOPLE`** — the main one, described above: person, spellings,
immunity.

**`ALIASES`** — one nickname, one canonical name. General, not tied to a person:

```python
ALIASES = {
    "ممد": "محمد",
    "زری": "زهرا",
}
```

**`EXTRA_NAMES`** — one person, many spellings. Use this when someone's Telegram
name gives the matcher nothing to work with (a handle like `BellaCia0o7` for a
person everyone calls صادق). Key it by their @username without the @, or by
their numeric user id; the value is a **list**:

```python
EXTRA_NAMES = {
    "bellacia0o7": ["صادق", "صادخ", "sadegh", "sadekh"],
    "123456789":   ["حاج آقا", "hajagha"],
}
```

`/whois <name>` prints the matched person's user id, which is what you key on
when someone has no username at all.

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

Fetched with `getUserProfilePhotos`, cached in SQLite, then blurred, desaturated
and blended into the sand at low opacity, so the face reads as an impression
rather than a picture behind glass.

A user with no visible avatar just gets no backdrop. That result is cached for 2
hours (vs 24 for a real photo) so people who add a picture aren't stuck. API
*failures* are never cached — otherwise one timeout would hide someone's photo
for a whole day.

**`photo=False` in the logs when the person clearly has a picture** is almost
always Telegram privacy: Settings → Privacy and Security → **Profile photo** set
to *My Contacts* or *Nobody* hides it from bots, and `total_count` comes back 0.
There is no way around that from the bot side — the person has to allow
Everybody, or you accept no backdrop for them.

To see which case you're in, in the group:

```
/photo             → your own avatar status
/photo صادخ        → that person's
(reply to someone) /photo
```

It bypasses the cache and prints `total_count` and how many bytes actually
downloaded, plus the raw API error if the call itself failed.

## Duelling — attack / defend / trick

```
ATTACK beats TRICK      TRICK
DEFEND beats ATTACK      ↙  ↖
TRICK  beats DEFEND  DEFEND ← ATTACK
```

`/duel` opens a challenge; `/duel صادق` or a reply aims it at one person.
Otherwise the first eligible person to answer takes it.

**The first move stays hidden.** Both players tap the same inline keyboard, and
Telegram delivers the answer to a tap as a private toast that only the tapper
sees. The challenger gets *"انتخاب تو: حمله — مخفی ماند"*; the group message only
says a choice was made. The move lives server-side until the second player
commits, so there is nothing in the chat to read ahead.

Guards on the callback: only the challenger can make the first move, they can't
also make the second, a named opponent is enforced, and challenges expire after
ten minutes.

Then a duel animation is sent — challenger in green, opponent in red, 79 frames
of the same procedural desert. Whoever played حیله throws a fistful of sand at
the other's eyes. The loser is knocked flat; a draw ends with crossed swords.

### Who is allowed to play

Owner only, same check as `/delete`:

| command | effect |
|---|---|
| `/challengers` | show who may start (default: everyone) |
| `/challengers ممد، میثم` | only these people may start |
| `/challengers all` | reopen to everyone |
| `/responders ...` | same, for answering a challenge |

Names go through the same fuzzy matcher as voting, comma or space separated. A
person can only be added once the bot knows their user id — if they've never
spoken, the bot says so rather than storing a name it can't check against.

## The map — `/travel`

31 places and 48 roads, transcribed from the hand-drawn map into
`worldmap.py`. Every box and every blank circle on that drawing is a place;
every line is a road. The blank circles have no name, so they are the oases —
واحهٔ ۱ through واحهٔ ۱۵.

**Moving.** Your first trip is a free choice of anywhere. After that `/travel`
rolls Telegram's own 🎲 in the chat and the buttons offer only what that roll
reaches. `EXACT_STEPS = True` in `bot.py` means a 4 is *four roads*, not "up to
four" — the map's diameter is 6, so "up to" would make a 6 open the whole board
and the dice would stop mattering. Each roll currently offers between 2 and 6
destinations. Flip the flag if you'd rather it be forgiving.

**The ride.** Choosing a destination opens a Telegram Web App: a first-person
camel ride down a desert road, drawn with a pseudo-3D projection on a 2D canvas
— no WebGL, no libraries. The road bends and rolls over real hills, built from a
seeded sequence of curves and elevations, so each destination has its own route.

**The terrain.** Four dune bands run down each side, all of them far out and
low. A ridge is filled from its crest down to the bottom of the frame, so a
band that is both close and tall projects as a wall across the side of the
screen — keeping every band distant and shallow holds the crest line near the
horizon, and the desert reads as flat with swells in the distance. Heights come
from four octaves of noise, smoothstepped so troughs flatten into saddles, and
crests are drawn as curves through the sample midpoints rather than straight
lines.

Tones between bands stay close together: distance is carried by haze, not by a
darker swatch, so the swells are felt rather than outlined. A 128px noise tile
generated at load is laid over the ground in `overlay` mode — one `fillRect` a
frame, and the biggest single thing separating this from flat vector fills.

**The track** is a worn caravan road, not paving: no kerb, no rumble strip, no
repeating courses. Its width varies ±16% and it drifts sideways segment by
segment, so the edges wander. A translucent shoulder blends packed dirt into
loose sand, two shallow ruts run where hooves have cut in, and blown sand
straddles the edge in patches so the boundary never reads as a drawn line.

**The hunt.** One animal is out on the road every ride — آهو 34%, گورخر 26%,
شیر 20%, ببر 15%, **عقاب 5%**. The bot picks it when the trip is created, not
the page, so the outcome can be announced in the group and kept on a scoreboard
(`/hunts`).

Four arrows. Hold the bow button to draw (a fuller draw gets there sooner),
release to loose. You aim by looking — the crosshair is fixed at the centre —
and **the arrow lands on the crosshair**: it is lofted just enough to cancel the
drop over the distance to the animal. Leading a moving target is the skill;
inventing a hold-over by feel, with nothing on screen to judge it against, was
not, and it made shots that looked dead-on land short.

Drag right to look right. Sensitivity is about 0.009 rad per pixel — the earlier
version had the signs written for dragging the world rather than turning the
camera, and moved about three times too slowly.

On a mouse, **clicking anywhere draws the bow** — you already aim by moving the
cursor, so the button is only needed on touch, where dragging is what aims.

While a hunt is live the view **holds where you left it** and opens up to ±77°
of yaw and ±31° of pitch (±43° and ±13° otherwise), with slower travel per pixel
for fine correction. Recentring during a hunt made the shot impossible: you
would aim, let go to reach the bow, and the view would slide off the animal
before you could loose. The quarry also turns back if it drifts outside the arc
you can aim through, and the hunt ends once it draws level with you.
The animal weaves *and* drifts steadily sideways, so it is never briefly
motionless, and it bolts once the first arrow goes past. The eagle is small,
fast and climbing.

Balance, measured by playing it headlessly: with four arrows, careful aim takes
everything and rough aim that just tracks the animal takes most things. It is
meant to be a pleasant thing you do on the way somewhere, not a test. Two bugs came
out of that testing — a spooked animal used to run *faster* than the rider so no
second arrow could ever reach it, and arrow velocity was set against absolute
speed rather than closing speed, which made every shot fly ~1.7× steeper than
where the crosshair pointed.

**Looking around.** Drag anywhere to turn your head: yaw up to ±35°, pitch ±9°.
On a desktop the mouse free-looks without holding a button. Let go and the view
eases back to the road. Yaw and pitch shift the projected image by a constant at
every depth — that falls out of the pinhole projection, so perspective stays
correct and it costs nothing per point.

**Weather.** The destination fixes the base conditions, so a place feels like
itself: 25% clear, 19% blazing heat, then dusk, wind, night, and a thin chance
of overcast, dust or desert rain. On top of that, roughly **29% of rides get
weather rolling in partway** — usually a dust storm, sometimes wind or rain —
which blows through for a stretch and clears before you arrive. A storm cuts
visibility hard (the fog term jumps from 1.1 to 5.5), smears the airborne grit
into streaks, and dims the sun. The name of the weather fades in when it
changes.

**Other travellers** already at your destination ride the same road with their
names above them. Each has a speed of its own relative to yours, so they drift
back past you or pull away ahead; whoever leaves the scene reappears at the
other end, so you pass each other more than once. Who is at a place is never
announced in the chat — seeing them on the road is how you find out.

**Reports.** Every move is DMed to `@Aidinhagh` (change with the `ADMIN_HANDLE`
env var). Telegram forbids a bot from opening a private chat, so **that account
has to press Start in the bot's DM once** or the reports silently go nowhere.

| command | who | effect |
|---|---|---|
| `/travel` | anyone | roll and move |
| `/webcheck` | anyone | why the ride button is or isn't showing |
| `/hunts` | anyone | who has hit anything this week |
| `/where` | anyone | everyone's position |
| `/where ممد` | anyone | one person's position |
| `/map` | anyone | every place and its roads |
| `/setloc ممد = کعبه` | owner | move someone by hand |
| `/clearlocs ممد` | owner | wipe one player |
| `/clearlocs all` | owner | wipe the board |

Positions, travel history and settings all live in the same SQLite file as the
votes, so nothing is lost on restart — **as long as `DB_PATH` points at a
Railway volume.** Without one, a redeploy resets the whole board.

### Hosting the Web App

Telegram only opens Web Apps over HTTPS, so the service now serves one:
`web.py` runs a small aiohttp server inside the bot's own event loop (still one
process, one poller) bound to `$PORT`.

On Railway: open **Settings → Networking → Generate Domain**. That sets
`RAILWAY_PUBLIC_DOMAIN`, which the bot reads on its own — there is nothing to
paste anywhere. Override with `WEBAPP_URL` only if you host it elsewhere.

**A `web_app` button only works in private chats.** In a group Telegram rejects
it, so the ride opens through a *Direct Link Mini App* instead:

1. @BotFather → `/newapp` → pick the bot → give it a short name, e.g. `ride`
2. Web App URL: `https://<your-domain>/ride`
3. Railway → Variables → `MINIAPP_SHORT_NAME=ride`

The button then links to `t.me/<bot>/ride?startapp=<token>`, which opens inside
Telegram from a group. Without `MINIAPP_SHORT_NAME` the button still appears but
opens the ride in the phone's browser — playable, just not embedded.

A `startapp` token can only be short and alphanumeric, so it carries no data
itself: the bot stores the trip (destination, who else is there) against that
token and the page fetches it from `/trip/<token>`. Trips expire after 6 hours.

### "Hmmm... can't reach this page"

That means Telegram opened the right URL and nothing answered. Work down:

1. **Open `https://<your-domain>/health` in a browser.** `{"ok": true}` means
   the server is fine and the problem is elsewhere. Anything else — timeout,
   404, 502 — is routing.
2. **Check the deploy log** for `LISTENING on 0.0.0.0:<port>` and
   `self-check ... -> 200`. Both present means the process is serving correctly
   *inside* the container, so the gap is Railway's edge, not the code.
3. **Match the target port.** When Railway generates a domain it asks which port
   to route to. If that number is not the one in the `LISTENING` line, nothing
   reaches the app. Settings → Networking → set the target port to match.
4. **`No open ports detected`** in the build log means the deploy that generated
   the domain predates the web server. Redeploy.

## Files

```
bot.py            Telegram side: tracking, resolution, tally, sending
duel.py           Desert duel: scene, warriors, poses, the fight animation
worldmap.py       The board: 31 places, 48 roads, dice reachability
web.py            aiohttp server for the Web App, runs in the bot's loop
webapp/ride.html  The camel ride: landmarks, scenery, other travellers
seed.py           The group's people, their spellings, and who is immune
matching.py       Cross-script name matching + the ALIASES nickname table
roster.py         SQLite: who has been seen here, and their cached avatar
animator.py       Sand, carved letters, the tide, GIF encoding (standalone too)
fonts/            Drop Vazirmatn-Bold.ttf here
requirements.txt  python-telegram-bot, Pillow, arabic-reshaper, python-bidi
Dockerfile        Explicit build: python:3.12-slim + requirements.txt
railway.json      Builder, start command, restart policy
Procfile          Same thing for other hosts
```
