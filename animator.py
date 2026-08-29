"""
animator.py — a name written in sand, washed away by a red tide.

Pure Pillow: no ffmpeg, no system deps. Returns GIF bytes ready for Telegram's
send_animation().

The sand is procedural (value noise), the letters are carved into it with an
inset shadow and a lit rim, and the wash is a band with an irregular edge that
sweeps down the frame: ahead of it the name is still there, behind it the sand
is smooth and stained.
"""

from __future__ import annotations

import glob
import io
import math
import os
import random

from PIL import (Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter,
                 ImageFont, features)

# ---------------------------------------------------------------- Persian text
#
# Two mutually exclusive ways to lay out Persian. Doing BOTH mangles the word.
#
#   * Pillow built with Raqm (all official wheels are) shapes and bidi-orders
#     the text itself, given direction="rtl". Feed it the RAW string.
#   * Without Raqm, Pillow draws codepoints in logical order, so the text must
#     be pre-shaped with arabic_reshaper and reordered with python-bidi.

HAVE_RAQM = False
try:
    HAVE_RAQM = bool(features.check("raqm"))
except Exception:
    pass

try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    def _manual_shape(text: str) -> str:
        return get_display(arabic_reshaper.reshape(text))

except Exception:  # pragma: no cover - never hard-crash the bot over this
    def _manual_shape(text: str) -> str:
        return text


def shape(text: str) -> str:
    """The string to hand to Pillow — pre-shaped only when Raqm is absent."""
    return text if HAVE_RAQM else _manual_shape(text)


def text_kwargs() -> dict:
    """Layout hints Pillow only understands when Raqm is present."""
    return {"direction": "rtl", "language": "fa"} if HAVE_RAQM else {}


# Fonts are searched in this order. Drop the TTF next to these scripts OR in a
# fonts/ subfolder — both work. FONT_PATH env var overrides everything.
_NAME_HINTS = ("vazir", "sahel", "samim", "shabnam", "estedad", "iran",
               "yekan", "naskh", "arabic", "amiri", "tahoma")

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_chosen_path: str | None = None


def _candidate_paths() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    found: list[str] = []
    env = os.environ.get("FONT_PATH")
    if env:
        found.append(env)

    for root in (os.path.join(here, "fonts"), here, os.getcwd()):
        if not os.path.isdir(root):
            continue
        for pattern in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
            found.extend(sorted(glob.glob(os.path.join(root, pattern))))

    found.extend([
        "/usr/share/fonts/truetype/vazir/Vazir-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ])

    def rank(path: str) -> tuple[int, int]:
        name = os.path.basename(path).lower()
        hinted = 0 if any(h in name for h in _NAME_HINTS) else 1
        bold = 0 if "bold" in name else 1
        return (hinted, bold)

    seen, ordered = set(), []
    for path in sorted(found, key=rank):
        if path not in seen and os.path.exists(path):
            seen.add(path)
            ordered.append(path)
    return ordered


def _draw_probe(font: ImageFont.FreeTypeFont, ch: str) -> bytes:
    img = Image.new("L", (56, 56), 0)
    ImageDraw.Draw(img).text((6, 6), ch, font=font, fill=255)
    return img.tobytes()


def _has_persian_glyphs(font: ImageFont.FreeTypeFont) -> bool:
    """A font missing Persian draws .notdef (an empty box) or nothing at all.
    Compare against a private-use codepoint that is certainly .notdef."""
    try:
        notdef = _draw_probe(font, "\ue000")
        # arabic_reshaper emits presentation forms, so test those — and test a
        # Persian-only letter (gaf), which many Arabic-only fonts lack.
        for ch in ("\ufedf", "\ufb94"):
            drawn = _draw_probe(font, ch)
            if not any(drawn) or drawn == notdef:
                return False
        return True
    except Exception:
        return False


def _resolve_font() -> str | None:
    global _chosen_path
    if _chosen_path is not None:
        return _chosen_path
    fallback = None
    for path in _candidate_paths():
        try:
            probe = ImageFont.truetype(path, 24)
        except Exception:
            continue
        if _has_persian_glyphs(probe):
            _chosen_path = path
            print(f"[animator] font: {path}")
            return path
        fallback = fallback or path
    if fallback:
        print(f"[animator] WARNING: no font with Persian glyphs found; "
              f"falling back to {fallback}. Persian will render as boxes. "
              f"Put Vazirmatn-Bold.ttf next to animator.py.")
    else:
        print("[animator] WARNING: no usable TTF found at all.")
    _chosen_path = fallback or ""
    return _chosen_path or None


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    if size in _font_cache:
        return _font_cache[size]
    path = _resolve_font()
    font = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
    _font_cache[size] = font
    return font


# ------------------------------------------------------------------ dimensions

W, H = 420, 520
FPS = 20

SAND_BASE = (211, 172, 116)
SAND_DARK = (118, 87, 52)
SAND_LIGHT = (252, 233, 190)
SAND_SHADOW = (96, 70, 52)        # dune shadow, slightly cool against the sun
SUN = (255, 236, 186)
DUST = (255, 240, 208)
BLOOD = (176, 20, 22)
BLOOD_BRIGHT = (222, 44, 36)
BLOOD_DARK = (104, 8, 14)
STAIN = (126, 44, 38)

TEXT_BOX = (W - 56, 190)          # room the name may occupy

# ------------------------------------------------------------------- timeline

T_SETTLE = (0, 3)
T_WRITE = (4, 28)                 # the name is drawn into the sand
T_HOLD = (29, 37)
T_WASH = (38, 72)                 # the tide crosses the frame
T_END = 94
TOTAL = T_END + 1

BAND = 200                        # depth of the wash, in pixels


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ----------------------------------------------------------------------- sand

def _value_noise(rng: random.Random, size: tuple[int, int], cell: int,
                 blur: float) -> Image.Image:
    """Cheap smooth noise: random low-res image scaled up and blurred."""
    small = Image.new("L", (max(2, size[0] // cell), max(2, size[1] // cell)))
    small.putdata([rng.randrange(256) for _ in range(small.width * small.height)])
    return small.resize(size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(blur))


def _streak_noise(rng: random.Random, cols: int, rows: int,
                  blur: float) -> Image.Image:
    """Noise stretched along X: few columns, many rows, so features come out
    wide and shallow — the ripple lines wind leaves across a dune."""
    small = Image.new("L", (max(2, cols), max(2, rows)))
    small.putdata([rng.randrange(256) for _ in range(small.width * small.height)])
    return small.resize((W, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(blur))


def _shift(img: Image.Image, dx: int, dy: int) -> Image.Image:
    """Shift without wrapping. ImageChops.offset wraps around, which draws a
    bright seam down two edges of every relief pass."""
    out = img.copy()
    out.paste(img, (dx, dy))
    return out


def _relief(height: Image.Image, base: Image.Image, dx: int, dy: int,
            light: float, shade: float) -> Image.Image:
    """Light a height field from the top-left, the way a low sun rakes dunes."""
    lit = ImageChops.subtract(height, _shift(height, dx, dy), scale=1, offset=128)
    highlight = lit.point(lambda v: int(min(255, max(0, v - 128) * light)))
    shadow = lit.point(lambda v: int(min(255, max(0, 128 - v) * shade)))
    out = Image.composite(Image.new("RGB", (W, H), SAND_LIGHT), base, highlight)
    return Image.composite(Image.new("RGB", (W, H), SAND_SHADOW), out, shadow)


def build_sand(rng: random.Random, photo: Image.Image | None = None) -> Image.Image:
    """Desert floor: dunes lit by a low sun, wind ripples, grit and glare."""
    base = Image.new("RGB", (W, H), SAND_BASE)

    # 1. dunes — big slow height field, raked by the sun from the upper left
    dunes = _value_noise(rng, (W, H), 78, 24)
    base = _relief(dunes, base, 7, 7, 2.4, 2.8)

    # 2. wind ripples — stretched horizontally, much finer, shallower relief
    ripples = _streak_noise(rng, 26, 105, 1.3)
    base = _relief(ripples, base, 2, 3, 1.5, 1.8)

    fine_ripples = _streak_noise(rng, 44, 190, 0.7)
    base = _relief(fine_ripples, base, 1, 2, 0.7, 0.85)

    # broad tonal drift, so the surface is not one flat colour edge to edge
    patches = _value_noise(rng, (W, H), 120, 40)
    base = Image.composite(Image.new("RGB", (W, H), (190, 150, 98)), base,
                           patches.point(lambda v: int(max(0, v - 118) * 0.75)))
    base = Image.composite(Image.new("RGB", (W, H), (238, 214, 168)), base,
                           patches.point(lambda v: int(max(0, 118 - v) * 0.70)))

    if photo is not None:
        base = Image.blend(base, photo, 0.30)

    # 3. grit: coarse grains, then scattered dark specks of grit and pebble
    grain = _value_noise(rng, (W, H), 3, 0.35)
    base = Image.composite(Image.new("RGB", (W, H), SAND_DARK), base,
                           grain.point(lambda v: int(v * 0.16)))
    speck = _value_noise(rng, (W, H), 2, 0.2)
    base = Image.composite(Image.new("RGB", (W, H), (78, 58, 40)), base,
                           speck.point(lambda v: 255 if v > 243 else 0))

    # 4. sunlight pouring in from the top-left corner
    glare = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glare).ellipse([-W * 0.9, -H * 0.8, W * 0.85, H * 0.55],
                                  fill=120)
    glare = glare.filter(ImageFilter.GaussianBlur(110))
    base = Image.composite(Image.new("RGB", (W, H), SUN), base, glare)

    # 5. the far corner falls into shadow
    shade = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shade).ellipse([W * 0.35, H * 0.5, W * 1.9, H * 1.7], fill=175)
    shade = shade.filter(ImageFilter.GaussianBlur(120))
    return Image.composite(Image.new("RGB", (W, H), (132, 98, 68)), base, shade)


def prepare_background(photo: bytes | None) -> Image.Image | None:
    """A profile photo, softened enough to survive being buried in sand."""
    if not photo:
        return None
    try:
        img = Image.open(io.BytesIO(photo)).convert("RGB")
    except Exception:
        return None

    scale = max(W / img.width, H / img.height)
    img = img.resize((max(W, int(img.width * scale) + 1),
                      max(H, int(img.height * scale) + 1)), Image.LANCZOS)
    left = (img.width - W) // 2
    top = (img.height - H) // 2
    img = img.crop((left, top, left + W, top + H))

    img = img.filter(ImageFilter.GaussianBlur(3))
    img = ImageEnhance.Color(img).enhance(0.25)
    img = ImageEnhance.Contrast(img).enhance(0.8)
    return Image.blend(img, Image.new("RGB", (W, H), SAND_BASE), 0.45)


def build_stain(rng: random.Random, clean: Image.Image) -> Image.Image:
    """Sand the tide has already crossed: darker, wet, streaked with red."""
    wet = ImageEnhance.Brightness(clean).enhance(0.84)
    wet = Image.blend(wet, Image.new("RGB", (W, H), STAIN), 0.15)

    # streaks left by the water draining away
    streaks = _value_noise(rng, (W * 3, H), 6, 1.3).resize((W, H), Image.BICUBIC)
    wet = Image.composite(Image.new("RGB", (W, H), (104, 22, 26)), wet,
                          streaks.point(lambda v: int(min(255, max(0, v - 168) * 1.6))))

    patches = _value_noise(rng, (W, H), 30, 9)
    return Image.composite(Image.new("RGB", (W, H), (96, 48, 42)), wet,
                           patches.point(lambda v: int(max(0, v - 178) * 0.8)))


# ------------------------------------------------------------- carved letters

def _fit_font(text: str, max_w: int, max_h: int):
    for size in range(86, 13, -1):
        font = _load_font(size)
        box = font.getbbox(text, **text_kwargs())
        if (box[2] - box[0]) <= max_w and (box[3] - box[1]) <= max_h:
            return font
    return _load_font(14)


def build_name(word: str):
    """(carved_layer, x0, x1) — the name as it appears cut into the sand."""
    visual = shape(word)
    font = _fit_font(visual, *TEXT_BOX)

    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).text((W // 2, H // 2), visual, font=font, fill=255,
                              anchor="mm", **text_kwargs())

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # groove: dark core, offset down-right; lit rim offset up-left
    rim = ImageChops.subtract(mask, ImageChops.offset(mask, -4, -4))
    shadow = ImageChops.subtract(mask, ImageChops.offset(mask, 4, 4))

    # sand pushed out of the groove piles along the edges
    ridge = ImageChops.subtract(ImageChops.offset(mask, -5, -5), mask)
    pile = Image.new("RGBA", (W, H), SAND_LIGHT + (150,))
    pile.putalpha(ImageChops.multiply(pile.split()[3],
                                      ridge.filter(ImageFilter.GaussianBlur(3))))
    layer.alpha_composite(pile)

    core = Image.new("RGBA", (W, H), (118, 90, 58) + (235,))
    core.putalpha(ImageChops.multiply(core.split()[3],
                                      mask.filter(ImageFilter.GaussianBlur(0.6))))
    layer.alpha_composite(core)

    lit = Image.new("RGBA", (W, H), (252, 241, 219) + (230,))
    lit.putalpha(ImageChops.multiply(lit.split()[3],
                                     rim.filter(ImageFilter.GaussianBlur(1.1))))
    layer.alpha_composite(lit)

    deep = Image.new("RGBA", (W, H), (74, 54, 34, 205))
    deep.putalpha(ImageChops.multiply(deep.split()[3],
                                      shadow.filter(ImageFilter.GaussianBlur(1.4))))
    layer.alpha_composite(deep)

    box = mask.getbbox() or (0, 0, W, H)
    return layer, box[0], box[2]


def name_at(layer: Image.Image, x0: int, x1: int, progress: float):
    """Reveal the name right-to-left, the way it is written."""
    if progress >= 1:
        return layer
    if progress <= 0:
        return None
    cut = x1 - (x1 - x0) * progress
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rectangle([cut, 0, W, H], fill=255)
    out = layer.copy()
    out.putalpha(ImageChops.multiply(out.split()[3],
                                     mask.filter(ImageFilter.GaussianBlur(2))))
    return out


# ----------------------------------------------------------------- airborne

def build_dust(rng: random.Random):
    """Grit blowing across the lens: many fine motes, a few near-camera blurs."""
    motes = []
    for _ in range(52):
        motes.append({
            "x": rng.uniform(-30, W + 30), "y": rng.uniform(-30, H + 30),
            "r": rng.uniform(0.7, 2.4), "a": rng.randint(50, 130),
            "vx": rng.uniform(1.0, 3.2), "vy": rng.uniform(-0.45, 0.55),
            "amp": rng.uniform(2, 9), "ph": rng.uniform(0, math.tau),
            "big": False,
        })
    for _ in range(7):
        motes.append({
            "x": rng.uniform(-30, W + 30), "y": rng.uniform(-30, H + 30),
            "r": rng.uniform(4.5, 10.0), "a": rng.randint(26, 52),
            "vx": rng.uniform(0.4, 1.3), "vy": rng.uniform(-0.3, 0.35),
            "amp": rng.uniform(4, 13), "ph": rng.uniform(0, math.tau),
            "big": True,
        })
    return motes


def draw_dust(canvas: Image.Image, motes, frame: int) -> None:
    near = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    far = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    nd, fd = ImageDraw.Draw(near), ImageDraw.Draw(far)

    for m in motes:
        x = (m["x"] + m["vx"] * frame) % (W + 60) - 30
        y = (m["y"] + m["vy"] * frame
             + m["amp"] * math.sin(frame * 0.17 + m["ph"])) % (H + 60) - 30
        r = m["r"]
        box = [x - r, y - r, x + r, y + r]
        (nd if m["big"] else fd).ellipse(box, fill=DUST + (m["a"],))

    near = near.filter(ImageFilter.GaussianBlur(3.5))
    canvas.paste(near, (0, 0), near)
    canvas.paste(far, (0, 0), far)


# ----------------------------------------------------------------- the wash

def _edge_profile(rng: random.Random):
    """A repeatable wobble, so the tide edge is never a straight line."""
    phase = [rng.uniform(0, math.tau) for _ in range(4)]
    bumps = [(rng.uniform(0, W), rng.uniform(18, 46), rng.uniform(26, 70))
             for _ in range(5)]

    def offset(x: float) -> float:
        value = (13 * math.sin(x / 41 + phase[0])
                 + 7 * math.sin(x / 17 + phase[1])
                 + 4 * math.sin(x / 7 + phase[2]))
        for cx, depth, width in bumps:            # longer runs, like fingers
            value += depth * math.exp(-((x - cx) / width) ** 2)
        return value

    return offset


def _band_polygon(edge, y_at, top: bool):
    points = []
    for x in range(-10, W + 12, 6):
        points.append((x, y_at + edge(x)))
    if top:
        points = [(W + 12, -H)] + points[::-1] + [(-10, -H)]
    return points


def draw_wash(canvas: Image.Image, clean: Image.Image, front_y: float,
              back_y: float, front_edge, back_edge) -> None:
    """Paint one frame of the tide: clean sand behind it, blood in the band."""
    if back_y > -H:
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).polygon(_band_polygon(back_edge, back_y, True),
                                     fill=255)
        canvas.paste(clean, (0, 0), mask.filter(ImageFilter.GaussianBlur(1.2)))

    if front_y < -20:
        return

    band = Image.new("L", (W, H), 0)
    bd = ImageDraw.Draw(band)
    bd.polygon(_band_polygon(front_edge, front_y, True), fill=255)
    bd.polygon(_band_polygon(back_edge, back_y, True), fill=0)
    band = band.filter(ImageFilter.GaussianBlur(1.0))

    fill = Image.new("RGB", (W, H), BLOOD)
    sheen = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sheen).polygon(_band_polygon(front_edge, front_y - 26, True),
                                  fill=90)
    fill = Image.composite(Image.new("RGB", (W, H), BLOOD_DARK), fill,
                           sheen.filter(ImageFilter.GaussianBlur(14)))

    lead = Image.new("L", (W, H), 0)
    ld = ImageDraw.Draw(lead)
    ld.polygon(_band_polygon(front_edge, front_y, True), fill=255)
    ld.polygon(_band_polygon(front_edge, front_y - 11, True), fill=0)
    fill = Image.composite(Image.new("RGB", (W, H), BLOOD_BRIGHT), fill,
                           lead.filter(ImageFilter.GaussianBlur(2)))

    # wet gloss: a soft specular streak a little behind the leading edge
    gloss = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(gloss)
    gd.polygon(_band_polygon(front_edge, front_y - 26, True), fill=78)
    gd.polygon(_band_polygon(front_edge, front_y - 48, True), fill=0)
    fill = Image.composite(Image.new("RGB", (W, H), (228, 78, 62)), fill,
                           gloss.filter(ImageFilter.GaussianBlur(12)))

    canvas.paste(fill, (0, 0), band)


def draw_droplets(canvas: Image.Image, drops, front_y: float, back_y: float,
                  front_edge, back_edge) -> None:
    """Spatter thrown ahead of the tide, and what it leaves behind."""
    d = ImageDraw.Draw(canvas, "RGBA")
    for x, ahead, radius in drops:
        lead = front_y + front_edge(x)
        tail = back_y + back_edge(x)

        y = lead + ahead
        if -10 < y < H + 10:
            d.ellipse([x - radius, y - radius * 0.75,
                       x + radius, y + radius * 0.75], fill=BLOOD + (230,))

        settled = tail - ahead * 0.6
        if -10 < settled < H + 10 and settled < tail:
            r = radius * 1.5
            d.ellipse([x - r, settled - r * 0.6, x + r, settled + r * 0.6],
                      fill=(96, 20, 24, 150))


# ------------------------------------------------------------------ lightning

def _bolt_points(rng, start_x: float, end_x: float, end_y: float, steps=8):
    points = [(start_x, 0.0)]
    for i in range(1, steps + 1):
        t = i / steps
        jitter = rng.uniform(-30, 30) * (1 - t) + rng.uniform(-7, 7)
        points.append((start_x + (end_x - start_x) * t + jitter, end_y * t))
    return points


def draw_lightning(canvas: Image.Image, rng: random.Random, power: float) -> None:
    """Storm-darken the frame, then strike the sand. The darkening is the
    point: a white bolt on pale sand is invisible."""
    storm = Image.new("RGB", (W, H), (17, 22, 34))
    canvas.paste(Image.blend(canvas.convert("RGB"), storm, 0.55 * power), (0, 0))

    start = rng.uniform(W * 0.25, W * 0.75)
    points = _bolt_points(rng, start, W / 2 + rng.uniform(-30, 30), H * 0.62)
    fork_at = rng.randrange(2, len(points) - 2)
    fx, fy = points[fork_at]
    branch = [(fx, fy)]
    for i in range(1, 4):
        branch.append((fx + rng.uniform(-46, 46) * i, fy + i * 26))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.line(points, fill=(120, 190, 255, 255), width=11, joint="curve")
    gd.line(branch, fill=(120, 190, 255, 210), width=7, joint="curve")
    canvas.paste(Image.alpha_composite(
        canvas.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(9))
    ).convert("RGB"), (0, 0))

    core = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    cd.line(points, fill=(238, 248, 255, 255), width=5, joint="curve")
    cd.line(branch, fill=(238, 248, 255, 235), width=3, joint="curve")
    cd.line(points, fill=(255, 255, 255, 255), width=2, joint="curve")
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), core).convert("RGB"),
                 (0, 0))


def flash(canvas: Image.Image, strength: float) -> None:
    storm = Image.new("RGB", (W, H), (17, 22, 34))
    canvas.paste(Image.blend(canvas.convert("RGB"), storm, 0.30 * strength), (0, 0))


# ------------------------------------------------------------------- assembly

def render_frames(word: str, seed: int | None = None,
                  photo: bytes | None = None, lightning: bool = False):
    rng = random.Random(seed if seed is not None else 0)

    backdrop = prepare_background(photo)
    clean = build_sand(random.Random(11), backdrop)
    stained = build_stain(random.Random(23), clean)

    layer, x0, x1 = build_name(word)
    written = clean.copy()
    written.paste(layer, (0, 0), layer)

    front_edge = _edge_profile(random.Random(5))
    back_edge = _edge_profile(random.Random(6))
    drops = [(rng.uniform(0, W), rng.uniform(10, 90), rng.uniform(2.5, 7.0))
             for _ in range(26)]
    motes = build_dust(random.Random(31))

    bolt_frames = {T_HOLD[0] + 2, T_WASH[0] + 1, T_WASH[0] + 14}
    flash_frames = {f + 1 for f in bolt_frames}

    span = H + BAND + 150
    frames = []
    for f in range(TOTAL):
        if f <= T_WRITE[1]:
            canvas = clean.copy()
            progress = _clamp01((f - T_WRITE[0] + 1) /
                                (T_WRITE[1] - T_WRITE[0] + 1))
            partial = name_at(layer, x0, x1, progress)
            if partial is not None:
                canvas.paste(partial, (0, 0), partial)
        else:
            canvas = written.copy()

        if f >= T_WASH[0]:
            p = _clamp01((f - T_WASH[0]) / (T_WASH[1] - T_WASH[0]))
            front_y = -70 + p * span
            back_y = front_y - BAND
            draw_wash(canvas, stained, front_y, back_y, front_edge, back_edge)
            draw_droplets(canvas, drops, front_y, back_y, front_edge, back_edge)

        draw_dust(canvas, motes, f)

        if lightning:
            if f in bolt_frames:
                draw_lightning(canvas, rng, 1.0)
            elif f in flash_frames:
                flash(canvas, 0.45)

        frames.append(canvas.convert("RGB"))
    return frames


def make_gif(word: str, seed: int | None = None, photo: bytes | None = None,
             lightning: bool = False) -> bytes:
    frames = render_frames(word, seed, photo, lightning)

    # One shared palette for every frame: with per-frame palettes the encoder
    # has to rewrite the whole canvas each time, which is ruinous for a grainy
    # sand texture that barely changes.
    master = frames[0].convert("P", palette=Image.ADAPTIVE, colors=110)
    pal = [master] + [f.quantize(palette=master, dither=Image.Dither.NONE)
                      for f in frames[1:]]

    buf = io.BytesIO()
    pal[0].save(buf, format="GIF", save_all=True, append_images=pal[1:],
                duration=int(1000 / FPS), loop=0, optimize=True, disposal=1)
    return buf.getvalue()


if __name__ == "__main__":
    import sys

    text = sys.argv[1] if len(sys.argv) > 1 else "رای"
    print(f"raqm shaping: {HAVE_RAQM}")
    render_frames(text, seed=7)[T_HOLD[0]].save("still.png")
    data = make_gif(text, seed=7)
    with open("preview.gif", "wb") as fh:
        fh.write(data)
    print("still.png — check the word here")
    print(f"preview.gif — {len(data) / 1024:.0f} KB")
