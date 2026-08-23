"""
animator.py — builds the "vote goes in, confetti comes out" animation.

Pure Pillow: no ffmpeg, no system deps. Returns GIF bytes ready for
Telegram's send_animation().
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

BOX_W, BOX_H = 212, 140
BOX_X = (W - BOX_W) // 2
BOX_Y = 210
BOX_BOTTOM = BOX_Y + BOX_H          # 350 — where the confetti sprays out
LEG_H = 26
FLOOR_Y = 478

SLOT_Y = BOX_Y - 5                  # the "swallow" line
PAPER_W, PAPER_H = 170, 116
PAPER_X = (W - PAPER_W) // 2
PAPER_REST_Y = 96                   # centre y while the word is written

COLS, ROWS = 12, 3                  # cross-cut shredder grid

BG = (243, 240, 233)
BOX_BODY = (47, 72, 88)
BOX_LID = (34, 55, 68)
BOX_EDGE = (28, 45, 56)
INK = (30, 34, 41)
PAPER = (255, 253, 246)
PAPER_EDGE = (206, 199, 185)
ACCENT = (198, 60, 60)

# ------------------------------------------------------------------- timeline

T_DROP = (0, 4)         # sheet flutters into frame
T_WRITE = (5, 26)       # the word gets written on it
T_HOLD = (27, 33)
T_INSERT = (34, 56)     # sheet is swallowed by the slot
T_SHAKE = (57, 64)      # the box chews
T_SHRED = (65, 98)      # confetti sprays out and falls
T_END = 108
TOTAL = T_END + 1


def _ease_out(p: float) -> float:
    return 1 - (1 - p) ** 3


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ------------------------------------------------------------------- the sheet

def _fit_font(text: str, max_w: int, max_h: int):
    for size in range(38, 9, -1):
        font = _load_font(size)
        box = font.getbbox(text, **text_kwargs())
        if (box[2] - box[0]) <= max_w and (box[3] - box[1]) <= max_h:
            return font
    return _load_font(10)


def build_sheet(word: str):
    """Return (blank_sheet, text_layer, text_x0, text_x1) — all RGBA."""
    sheet = Image.new("RGBA", (PAPER_W, PAPER_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(sheet)
    d.rounded_rectangle([0, 0, PAPER_W - 1, PAPER_H - 1], radius=5,
                        fill=PAPER + (255,), outline=PAPER_EDGE + (255,))
    # a couple of faint ruled lines so it reads as a ballot
    d.line([16, 20, PAPER_W - 16, 20], fill=(228, 222, 208, 255), width=2)
    d.line([16, PAPER_H - 20, PAPER_W - 16, PAPER_H - 20],
           fill=(228, 222, 208, 255), width=2)
    d.rectangle([PAPER_W - 34, PAPER_H - 34, PAPER_W - 22, PAPER_H - 22],
                outline=(196, 190, 176, 255), width=2)

    visual = shape(word)
    font = _fit_font(visual, PAPER_W - 30, PAPER_H - 46)

    layer = Image.new("RGBA", (PAPER_W, PAPER_H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((PAPER_W // 2, PAPER_H // 2 - 2), visual, font=font,
            fill=INK + (255,), anchor="mm", **text_kwargs())

    bbox = layer.getbbox() or (0, 0, PAPER_W, PAPER_H)
    return sheet, layer, bbox[0], bbox[2]


def sheet_at(sheet, layer, x0, x1, progress: float) -> Image.Image:
    """Sheet with the word revealed right-to-left (Persian writing order)."""
    out = sheet.copy()
    if progress <= 0:
        return out
    if progress >= 1:
        out.alpha_composite(layer)
        return out

    cut = x1 - (x1 - x0) * progress
    mask = Image.new("L", (PAPER_W, PAPER_H), 0)
    ImageDraw.Draw(mask).rectangle([cut, 0, PAPER_W, PAPER_H], fill=255)
    partial = layer.copy()
    partial.putalpha(ImageChops.multiply(partial.split()[3], mask))
    out.alpha_composite(partial)
    return out


# --------------------------------------------------------------------- the box

def draw_scene(d: ImageDraw.ImageDraw, shake: float = 0.0) -> None:
    dx = int(round(shake))
    # floor + soft shadow
    d.line([30, FLOOR_Y, W - 30, FLOOR_Y], fill=(222, 217, 205), width=3)
    d.ellipse([BOX_X - 24, BOX_BOTTOM + LEG_H - 8,
               BOX_X + BOX_W + 24, BOX_BOTTOM + LEG_H + 12],
              fill=(230, 226, 214))

    # legs
    for lx in (BOX_X + 14, BOX_X + BOX_W - 32):
        d.rounded_rectangle([lx + dx, BOX_BOTTOM - 4, lx + 18 + dx,
                             BOX_BOTTOM + LEG_H], radius=4, fill=BOX_EDGE)

    # body
    d.rounded_rectangle([BOX_X + dx, BOX_Y, BOX_X + BOX_W + dx, BOX_BOTTOM],
                        radius=10, fill=BOX_BODY)
    # front panel: a little ballot icon
    px, py = BOX_X + BOX_W // 2 - 30 + dx, BOX_Y + 40
    d.rounded_rectangle([px, py, px + 60, py + 62], radius=5, fill=(238, 235, 228))
    d.line([px + 10, py + 14, px + 50, py + 14], fill=(190, 185, 174), width=3)
    d.line([px + 10, py + 26, px + 50, py + 26], fill=(190, 185, 174), width=3)
    d.line([px + 14, py + 44, px + 24, py + 54], fill=ACCENT, width=4)
    d.line([px + 24, py + 54, px + 46, py + 34], fill=ACCENT, width=4)

    # output mouth
    d.rounded_rectangle([BOX_X + 20 + dx, BOX_BOTTOM - 12,
                         BOX_X + BOX_W - 20 + dx, BOX_BOTTOM - 2],
                        radius=4, fill=(20, 32, 40))

    # lid + input slot
    d.rounded_rectangle([BOX_X - 10 + dx, BOX_Y - 18, BOX_X + BOX_W + 10 + dx,
                         BOX_Y + 8], radius=7, fill=BOX_LID)
    d.rounded_rectangle([W // 2 - 90 + dx, SLOT_Y - 5, W // 2 + 90 + dx,
                         SLOT_Y + 5], radius=5, fill=(14, 22, 28))


# ----------------------------------------------------------------- the shreds

def build_shreds(full_sheet: Image.Image, rng: random.Random):
    xs = [round(i * PAPER_W / COLS) for i in range(COLS + 1)]
    ys = [round(j * PAPER_H / ROWS) for j in range(ROWS + 1)]
    shreds = []
    for r in range(ROWS):
        for c in range(COLS):
            piece = full_sheet.crop((xs[c], ys[r], xs[c + 1], ys[r + 1]))
            shreds.append({
                "img": piece,
                "x": PAPER_X + xs[c],
                "start": T_SHRED[0] + r * 4 + rng.randint(0, 3),
                "drift": rng.uniform(-72, 72),
                "spin": rng.uniform(-190, 190),
                "land": rng.uniform(404, 468),
                "sway": rng.uniform(0.18, 0.42),
            })
    return shreds


def paste_rotated(canvas, img, cx, cy, angle):
    rot = img.rotate(angle, expand=True, resample=Image.BICUBIC)
    canvas.alpha_composite(rot, (int(cx - rot.width / 2), int(cy - rot.height / 2)))


def draw_shred(canvas, s, frame):
    t = frame - s["start"]
    if t < 0:
        return
    w, h = s["img"].size
    extrude = 4
    fall = 18

    if t < extrude:                       # sliding out of the mouth
        p = (t + 1) / extrude
        vis = max(1, int(h * p))
        part = s["img"].crop((0, h - vis, w, h))
        canvas.alpha_composite(part, (int(s["x"]), BOX_BOTTOM - 6))
        return

    q = _clamp01((t - extrude) / fall)
    g = q * q                              # gravity
    y0 = BOX_BOTTOM - 6 + h / 2
    cy = y0 + (s["land"] - y0) * g
    cx = s["x"] + w / 2 + s["drift"] * q + math.sin(q * 7) * s["sway"] * 14
    paste_rotated(canvas, s["img"], cx, cy, s["spin"] * q)


# ------------------------------------------------------------------ lightning

def _bolt_points(rng, start_x: float, end_x: float, end_y: float, steps=8):
    points = [(start_x, 0.0)]
    for i in range(1, steps + 1):
        t = i / steps
        jitter = rng.uniform(-30, 30) * (1 - t) + rng.uniform(-7, 7)
        points.append((start_x + (end_x - start_x) * t + jitter, end_y * t))
    return points


def draw_lightning(canvas: Image.Image, rng: random.Random, power: float) -> None:
    """Storm-darken the frame, then strike into the top of the box.

    The darkening is the point: a white bolt on a pale background is invisible,
    so the strike has to bring its own night with it.
    """
    storm = Image.new("RGB", (W, H), (17, 24, 38))
    darkened = Image.blend(canvas.convert("RGB"), storm, 0.55 * power)
    canvas.paste(darkened.convert("RGBA"), (0, 0))

    start = rng.uniform(W * 0.25, W * 0.75)
    points = _bolt_points(rng, start, W / 2 + rng.uniform(-30, 30), BOX_Y + 6)
    fork_at = rng.randrange(2, len(points) - 2)
    fx, fy = points[fork_at]
    branch = [(fx, fy)]
    for i in range(1, 4):
        branch.append((fx + rng.uniform(-46, 46) * i, fy + i * 26))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.line(points, fill=(120, 190, 255, 255), width=11, joint="curve")
    gd.line(branch, fill=(120, 190, 255, 210), width=7, joint="curve")
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(9)))

    core = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    cd.line(points, fill=(238, 248, 255, 255), width=5, joint="curve")
    cd.line(branch, fill=(238, 248, 255, 235), width=3, joint="curve")
    cd.line(points, fill=(255, 255, 255, 255), width=2, joint="curve")
    canvas.alpha_composite(core)

    # light spilling onto the box lid where the bolt lands
    spill = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(spill).ellipse(
        [W / 2 - 120, BOX_Y - 60, W / 2 + 120, BOX_Y + 40],
        fill=(170, 215, 255, int(90 * power)))
    canvas.alpha_composite(spill.filter(ImageFilter.GaussianBlur(18)))


def flash(canvas: Image.Image, strength: float) -> None:
    """Afterglow frame: a dimmer echo of the strike."""
    storm = Image.new("RGB", (W, H), (17, 24, 38))
    canvas.paste(Image.blend(canvas.convert("RGB"), storm,
                             0.30 * strength).convert("RGBA"), (0, 0))
    canvas.alpha_composite(
        Image.new("RGBA", (W, H), (200, 225, 255, int(60 * strength))))


# ----------------------------------------------------------------- background

def prepare_background(photo: bytes | None):
    """Turn a profile photo into a backdrop that never fights the foreground:
    cropped to fill, blurred, desaturated and washed toward the paper colour."""
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

    img = img.filter(ImageFilter.GaussianBlur(6))
    img = ImageEnhance.Color(img).enhance(0.5)
    img = ImageEnhance.Contrast(img).enhance(0.85)
    img = Image.blend(img, Image.new("RGB", (W, H), BG), 0.55)

    # fewer distinct colours in the backdrop => much smaller GIF
    img = img.quantize(colors=48, method=Image.MEDIANCUT).convert("RGB")

    # soft top-down scrim so the ballot stays readable over busy photos
    column = Image.new("L", (1, H))
    for y in range(H):
        column.putpixel((0, y), int(105 * max(0.0, 1 - y / 300) ** 1.4))
    scrim = Image.new("RGBA", (W, H), BG + (255,))
    scrim.putalpha(column.resize((W, H), Image.BILINEAR))
    return Image.alpha_composite(img.convert("RGBA"), scrim).convert("RGB")


# ------------------------------------------------------------------- assembly

def render_frames(word: str, seed: int | None = None,
                  photo: bytes | None = None, lightning: bool = False):
    rng = random.Random(seed)
    bolt_frames = {T_INSERT[1] - 2, T_SHAKE[0] + 3, T_SHRED[0] + 1}
    flash_frames = {f + 1 for f in bolt_frames} | {T_SHAKE[0] + 8, T_SHRED[0] + 9}
    backdrop = prepare_background(photo)
    blank = Image.new("RGBA", (W, H), BG + (255,))
    base = backdrop.convert("RGBA") if backdrop else blank
    sheet, layer, x0, x1 = build_sheet(word)
    full = sheet_at(sheet, layer, x0, x1, 1.0)
    shreds = build_shreds(full, rng)

    frames = []
    for f in range(TOTAL):
        canvas = base.copy()
        d = ImageDraw.Draw(canvas)

        shake = 0.0
        if T_SHAKE[0] <= f <= T_SHRED[1]:
            decay = max(0.0, 1 - (f - T_SHAKE[0]) / 26)
            shake = math.sin(f * 2.1) * 4.5 * decay
        draw_scene(d, shake)

        # --- the sheet, phase by phase
        if f <= T_INSERT[1]:
            if f <= T_DROP[1]:
                p = _ease_out((f + 1) / (T_DROP[1] - T_DROP[0] + 1))
                cy = 24 + (PAPER_REST_Y - 24) * p
                reveal = 0.0
            elif f <= T_WRITE[1]:
                cy = PAPER_REST_Y
                reveal = _clamp01((f - T_WRITE[0] + 1) /
                                  (T_WRITE[1] - T_WRITE[0] + 1))
            elif f <= T_HOLD[1]:
                cy, reveal = PAPER_REST_Y, 1.0
            else:
                p = ((f - T_INSERT[0]) / (T_INSERT[1] - T_INSERT[0])) ** 1.55
                cy = PAPER_REST_Y + (SLOT_Y + PAPER_H / 2 + 6 - PAPER_REST_Y) * p
                reveal = 1.0

            img = sheet_at(sheet, layer, x0, x1, reveal)
            top = int(cy - PAPER_H / 2)
            visible = SLOT_Y - top          # only what is above the slot shows
            if visible > 0:
                canvas.alpha_composite(img.crop((0, 0, PAPER_W, min(PAPER_H, visible))),
                                       (PAPER_X, top))

        # --- confetti
        if f >= T_SHRED[0]:
            for s in shreds:
                draw_shred(canvas, s, f)

        if lightning:
            if f in bolt_frames:
                draw_lightning(canvas, rng, 1.0 if f != T_SHRED[0] + 1 else 0.7)
            elif f in flash_frames:
                flash(canvas, 0.45)

        frames.append(canvas.convert("RGB"))
    return frames


def make_gif(word: str, seed: int | None = None, photo: bytes | None = None,
             lightning: bool = False) -> bytes:
    frames = render_frames(word, seed, photo, lightning)

    # One shared palette for every frame. Per-frame palettes would force the
    # encoder to rewrite the whole canvas each time; with a common palette it
    # only stores the rectangle that actually changed, which matters a lot when
    # the background is a photo.
    master = frames[0].convert("P", palette=Image.ADAPTIVE,
                               colors=128 if (photo or lightning) else 96)
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
    print(f"still.png — check the word here")
    print(f"preview.gif — {len(data) / 1024:.0f} KB")
