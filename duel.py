"""
duel.py — attack / defend / trick, fought out by two robed warriors.

Same procedural approach as animator.py, but a side-on desert with a horizon
instead of a top-down surface: sky, layered dunes, a ground plane, and two
figures built from polygons. Silhouette-first — at this size a clear outline
reads far better than detail would.

    ATTACK  beats TRICK
    DEFEND  beats ATTACK
    TRICK   beats DEFEND
"""

from __future__ import annotations

import io
import math
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from animator import _load_font, _value_noise, shape, text_kwargs

W, H = 520, 380
FPS = 18
HORIZON = 196                     # where sky meets sand, far away
GROUND = 338                      # where the two of them actually stand
SCALE = 1.42                      # figure height, relative to the base sketch

ATTACK, DEFEND, TRICK = "attack", "defend", "trick"
MOVES = (ATTACK, DEFEND, TRICK)
BEATS = {ATTACK: TRICK, DEFEND: ATTACK, TRICK: DEFEND}

FA_MOVE = {ATTACK: "حمله", DEFEND: "دفاع", TRICK: "حیله"}

SKY_TOP = (128, 158, 190)
SKY_LOW = (247, 226, 182)
DUNE_FAR = (198, 172, 136)
DUNE_MID = (206, 172, 124)
SAND = (214, 178, 122)
SAND_DARK = (150, 116, 74)
SAND_LIGHT = (250, 232, 192)
DUST = (255, 240, 208)

GREEN = (34, 106, 62)
GREEN_DARK = (20, 68, 42)
GREEN_TRIM = (222, 206, 150)
RED = (150, 32, 34)
RED_DARK = (98, 18, 22)
RED_TRIM = (232, 214, 168)
STEEL = (206, 208, 214)
STEEL_DARK = (128, 132, 142)
SKIN_SHADOW = (74, 54, 40)


def winner_of(a: str, b: str) -> int:
    """1 if the first move wins, -1 if the second does, 0 for a draw."""
    if a == b:
        return 0
    return 1 if BEATS[a] == b else -1


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# -------------------------------------------------------------------- scenery

def _shift(img: Image.Image, dx: int, dy: int) -> Image.Image:
    out = img.copy()
    out.paste(img, (dx, dy))
    return out


def _relief(height: Image.Image, base: Image.Image, dx: int, dy: int,
            light: float, shade: float) -> Image.Image:
    lit = ImageChops.subtract(height, _shift(height, dx, dy), scale=1, offset=128)
    hi = lit.point(lambda v: int(min(255, max(0, v - 128) * light)))
    lo = lit.point(lambda v: int(min(255, max(0, 128 - v) * shade)))
    out = Image.composite(Image.new("RGB", base.size, SAND_LIGHT), base, hi)
    return Image.composite(Image.new("RGB", base.size, SAND_DARK), out, lo)


def _dune_line(rng: random.Random, y: float, amp: float, wobble: float):
    phase = [rng.uniform(0, math.tau) for _ in range(3)]
    points = []
    for x in range(-10, W + 12, 8):
        dy = (amp * math.sin(x / 130 + phase[0])
              + amp * 0.5 * math.sin(x / 61 + phase[1])
              + wobble * math.sin(x / 23 + phase[2]))
        points.append((x, y + dy))
    return points + [(W + 12, H), (-10, H)]


def build_scene(rng: random.Random) -> Image.Image:
    scene = Image.new("RGB", (W, H), SKY_LOW)
    d = ImageDraw.Draw(scene)

    # sky: hot near the horizon, cooler overhead
    for y in range(HORIZON + 6):
        t = (y / (HORIZON + 6)) ** 1.5
        d.line([0, y, W, y], fill=tuple(
            int(lerp(SKY_TOP[i], SKY_LOW[i], t)) for i in range(3)))

    # sun, low and washed out
    sun = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sun).ellipse([W * 0.68, 34, W * 0.68 + 62, 96],
                                fill=(255, 248, 222, 210))
    scene.paste(sun.filter(ImageFilter.GaussianBlur(9)), (0, 0),
                sun.filter(ImageFilter.GaussianBlur(9)))

    # layered dunes receding into haze
    for depth, (y, amp, wob, colour) in enumerate([
        (HORIZON - 34, 11, 4, DUNE_FAR),
        (HORIZON - 14, 9, 5, DUNE_MID),
    ]):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon(_dune_line(rng, y, amp, wob),
                                      fill=colour + (255,))
        layer = layer.filter(ImageFilter.GaussianBlur(1.6 - depth * 0.6))
        scene.paste(layer, (0, 0), layer)

    # the ground the fight happens on
    floor = Image.new("RGB", (W, H), SAND)
    ripple_small = _value_noise(rng, (W, H), 60, 14)
    floor = _relief(ripple_small, floor, 6, 4, 2.0, 2.3)
    streak = Image.new("L", (18, 90))
    streak.putdata([rng.randrange(256) for _ in range(18 * 90)])
    streak = streak.resize((W, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(1.1))
    floor = _relief(streak, floor, 2, 2, 1.2, 1.4)
    grain = _value_noise(rng, (W, H), 3, 0.35)
    floor = Image.composite(Image.new("RGB", (W, H), SAND_DARK), floor,
                            grain.point(lambda v: int(v * 0.15)))

    ground_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(ground_mask).rectangle([0, HORIZON, W, H], fill=255)
    scene.paste(floor, (0, 0), ground_mask.filter(ImageFilter.GaussianBlur(1.2)))

    # heat haze along the horizon
    haze = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(haze).rectangle([0, HORIZON - 14, W, HORIZON + 12],
                                   fill=SKY_LOW + (120,))
    haze = haze.filter(ImageFilter.GaussianBlur(9))
    scene.paste(haze, (0, 0), haze)

    vign = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vign).ellipse([-W * 0.3, -H * 0.35, W * 1.3, H * 1.35], fill=255)
    vign = vign.filter(ImageFilter.GaussianBlur(70))
    dark = Image.new("RGB", (W, H), (150, 120, 88))
    return Image.composite(scene, Image.blend(scene, dark, 0.4), vign)


# -------------------------------------------------------------------- figures

class Pose:
    """Everything that changes about a warrior between frames."""

    def __init__(self, lean=0.0, crouch=0.0, arm=-40.0, offarm=150.0,
                 weapon="sword", guard=0.0, tilt=0.0, hop=0.0, fallen=0.0):
        self.lean = lean          # px forward
        self.crouch = crouch      # px down
        self.arm = arm            # weapon-arm angle, degrees (0 = forward)
        self.offarm = offarm      # other arm
        self.weapon = weapon      # "sword" | "shield" | "dagger"
        self.guard = guard        # shield raised 0..1
        self.tilt = tilt          # whole-body rotation, degrees
        self.hop = hop            # px up
        self.fallen = fallen      # 0..1 knocked down


def blend_pose(a: Pose, b: Pose, t: float) -> Pose:
    t = _clamp01(t)
    out = Pose()
    for field in ("lean", "crouch", "arm", "offarm", "guard", "tilt", "hop",
                  "fallen"):
        setattr(out, field, lerp(getattr(a, field), getattr(b, field), t))
    out.weapon = b.weapon if t > 0.5 else a.weapon
    return out


IDLE = Pose(arm=-55, offarm=150)
WINDUP = {
    ATTACK: Pose(lean=-8, arm=-118, offarm=140),
    DEFEND: Pose(lean=-4, crouch=6, arm=-30, offarm=95, weapon="shield", guard=0.4),
    TRICK: Pose(lean=-12, crouch=14, arm=-140, offarm=120, weapon="dagger"),
}
STRIKE = {
    ATTACK: Pose(lean=26, arm=-4, offarm=125),
    DEFEND: Pose(lean=-2, crouch=12, arm=-16, offarm=40, weapon="shield", guard=1.0),
    TRICK: Pose(lean=18, crouch=22, arm=14, offarm=-52, weapon="dagger"),
}
TRIUMPH = Pose(lean=2, arm=-105, offarm=-70, hop=2)
BEATEN = Pose(lean=-16, crouch=26, arm=-150, offarm=-160, tilt=-20, fallen=1.0)
STANDOFF = Pose(lean=8, crouch=6, arm=-25, offarm=120)


def _rot(px, py, cx, cy, deg):
    r = math.radians(deg)
    dx, dy = px - cx, py - cy
    return (cx + dx * math.cos(r) - dy * math.sin(r),
            cy + dx * math.sin(r) + dy * math.cos(r))


BOX_W, BOX_H = 300, 330          # local buffer a single figure is drawn into


def draw_warrior(canvas: Image.Image, x: float, facing: int, pose: Pose,
                 robe, robe_dark, trim, bob: float = 0.0) -> None:
    """One robed figure, feet planted at (x, GROUND).

    Drawn into a small buffer rather than a full-canvas layer: the outline and
    blur passes cost about eight times less that way, which matters when a
    single duel is 79 frames of two figures.
    """
    base_y = GROUND + pose.crouch * SCALE - pose.hop + pose.fallen * 34
    cx = x + facing * pose.lean * SCALE
    top = base_y - 132 * SCALE + bob

    ox = int(cx) - BOX_W // 2
    oy = int(base_y) - BOX_H + 60

    layer = Image.new("RGBA", (BOX_W, BOX_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    def P(dx, dy):
        """Body-local coords: +dx is forward, +dy is down from the head."""
        px, py = cx + facing * dx * SCALE, top + dy * SCALE
        if pose.tilt:
            px, py = _rot(px, py, cx, base_y, facing * pose.tilt)
        return px - ox, py - oy

    # ground shadow, straight onto the canvas
    shadow = Image.new("RGBA", (BOX_W, BOX_H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        [cx - 52 - ox, GROUND - 6 - oy, cx + 52 - ox, GROUND + 16 - oy],
        fill=(84, 62, 42, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))
    canvas.paste(shadow, (ox, oy), shadow)

    # robe
    d.polygon([P(-15, 34), P(15, 34), P(30, 128), P(-32, 128)], fill=robe)
    d.polygon([P(2, 34), P(15, 34), P(30, 128), P(8, 128)], fill=robe_dark)
    d.polygon([P(-16, 72), P(16, 72), P(17, 82), P(-17, 82)], fill=trim)
    d.polygon([P(-14, 30), P(14, 30), P(12, 52), P(-12, 52)], fill=robe)

    # head: cloth over the whole skull, a slit for the face
    hx, hy = P(0, 15)
    d.ellipse([hx - 11 * SCALE, hy - 13 * SCALE,
               hx + 11 * SCALE, hy + 11 * SCALE], fill=(188, 148, 108))
    d.polygon([P(-13, 1), P(13, 1), P(17, 18), P(12, 42), P(-12, 42), P(-17, 18)],
              fill=trim)
    d.polygon([P(-13, 1), P(-3, 1), P(-6, 40), P(-12, 42), P(-17, 18)],
              fill=tuple(int(c * 0.82) for c in trim))
    d.polygon([P(5, 11), P(13, 13), P(13, 25), P(5, 24)], fill=(206, 170, 128))
    d.polygon([P(7, 15), P(12, 16), P(12, 19), P(7, 18)], fill=SKIN_SHADOW)
    d.polygon([P(-14, 3), P(14, 3), P(14, 9), P(-14, 9)], fill=(46, 38, 30))

    shoulder = P(0, 40)

    oa = math.radians(pose.offarm)
    ohand = (shoulder[0] + facing * math.cos(oa) * 40 * SCALE,
             shoulder[1] + math.sin(oa) * 40 * SCALE)
    d.line([shoulder, ohand], fill=robe_dark, width=int(9 * SCALE))
    if pose.guard > 0.05:
        r = (15 + 13 * pose.guard) * SCALE
        d.ellipse([ohand[0] - r, ohand[1] - r * 1.15,
                   ohand[0] + r, ohand[1] + r * 1.15], fill=robe_dark)
        d.ellipse([ohand[0] - r * 0.55, ohand[1] - r * 0.62,
                   ohand[0] + r * 0.55, ohand[1] + r * 0.62], fill=trim)

    wa = math.radians(pose.arm)
    hand = (shoulder[0] + facing * math.cos(wa) * 44 * SCALE,
            shoulder[1] + math.sin(wa) * 44 * SCALE)
    d.line([shoulder, hand], fill=robe, width=int(10 * SCALE))

    if pose.weapon in ("sword", "dagger"):
        length = (62 if pose.weapon == "sword" else 30) * SCALE
        tip = (hand[0] + facing * math.cos(wa) * length,
               hand[1] + math.sin(wa) * length)
        d.line([hand, tip], fill=STEEL,
               width=int((6 if pose.weapon == "sword" else 5) * SCALE))
        d.line([hand, tip], fill=STEEL_DARK, width=max(2, int(2 * SCALE)))
        guard_a = wa + math.pi / 2
        d.line([(hand[0] + math.cos(guard_a) * 9 * SCALE,
                 hand[1] + math.sin(guard_a) * 9 * SCALE),
                (hand[0] - math.cos(guard_a) * 9 * SCALE,
                 hand[1] - math.sin(guard_a) * 9 * SCALE)],
               fill=trim, width=int(5 * SCALE))

    # A dark outline lifts the figure off a busy desert: grow the silhouette,
    # subtract the original, and the difference is a clean rim.
    alpha = layer.split()[3]
    rim = ImageChops.subtract(alpha.filter(ImageFilter.MaxFilter(5)), alpha)
    outline = Image.new("RGBA", (BOX_W, BOX_H), (42, 30, 22, 255))
    outline.putalpha(rim.point(lambda v: int(v * 0.82)))
    canvas.paste(outline, (ox, oy), outline)
    canvas.paste(layer, (ox, oy), layer)


# ----------------------------------------------------------------------- dust

def build_dust(rng: random.Random, count=44):
    return [{"x": rng.uniform(-30, W + 30), "y": rng.uniform(40, H),
             "r": rng.uniform(0.8, 2.6), "a": rng.randint(45, 120),
             "vx": rng.uniform(0.9, 2.8), "vy": rng.uniform(-0.4, 0.4),
             "amp": rng.uniform(2, 8), "ph": rng.uniform(0, math.tau)}
            for _ in range(count)]


def draw_dust(canvas: Image.Image, motes, frame: int) -> None:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for m in motes:
        x = (m["x"] + m["vx"] * frame) % (W + 60) - 30
        y = (m["y"] + m["vy"] * frame
             + m["amp"] * math.sin(frame * 0.16 + m["ph"])) % (H + 40) - 20
        d.ellipse([x - m["r"], y - m["r"], x + m["r"], y + m["r"]],
                  fill=DUST + (m["a"],))
    canvas.paste(layer, (0, 0), layer)


def draw_kickup(canvas: Image.Image, rng: random.Random, cx: float,
                strength: float) -> None:
    """Sand thrown up where the two meet."""
    if strength <= 0.02:
        return
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(int(34 * strength)):
        a = rng.uniform(0, math.tau)
        dist = rng.uniform(4, 90) * strength
        px = cx + math.cos(a) * dist * 1.5
        py = GROUND + math.sin(a) * dist * 0.5 - dist * 0.4
        r = rng.uniform(1.5, 5.5)
        d.ellipse([px - r, py - r, px + r, py + r],
                  fill=(206, 178, 130, int(190 * strength)))
    layer = layer.filter(ImageFilter.GaussianBlur(1.4))
    canvas.paste(layer, (0, 0), layer)


# -------------------------------------------------------------------- captions

def _fit(text: str, max_w: int, start=26):
    for size in range(start, 9, -1):
        font = _load_font(size)
        box = font.getbbox(text, **text_kwargs())
        if box[2] - box[0] <= max_w:
            return font
    return _load_font(10)


_label_cache: dict[tuple, Image.Image] = {}


def label_sprite(text: str, max_w=180, size=24,
                 colour=(38, 28, 20)) -> Image.Image:
    """Text never changes between frames, so render it once and reuse it."""
    key = (text, max_w, size, colour)
    if key in _label_cache:
        return _label_cache[key]

    visual = shape(text)
    font = _fit(visual, max_w, size)
    box = font.getbbox(visual, **text_kwargs())
    w = box[2] - box[0] + 12
    h = box[3] - box[1] + 12

    sprite = Image.new("RGBA", (max(8, w), max(8, h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(sprite)
    cx, cy = sprite.width / 2, sprite.height / 2
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        d.text((cx + dx, cy + dy), visual, font=font,
               fill=(252, 240, 214, 235), anchor="mm", **text_kwargs())
    d.text((cx, cy), visual, font=font, fill=colour + (255,), anchor="mm",
           **text_kwargs())

    _label_cache[key] = sprite
    return sprite


def draw_label(canvas: Image.Image, text: str, cx: float, cy: float,
               max_w=180, size=24, colour=(38, 28, 20)) -> None:
    sprite = label_sprite(text, max_w, size, colour)
    canvas.paste(sprite, (int(cx - sprite.width / 2),
                          int(cy - sprite.height / 2)), sprite)


# ------------------------------------------------------------------- assembly

T_IDLE = (0, 12)
T_WINDUP = (13, 26)
T_STRIKE = (27, 38)
T_IMPACT = (39, 45)
T_RESULT = (46, 78)
TOTAL = 79

GREEN_X, RED_X = 168, 352


def _phase_pose(move: str, f: int, outcome: int, mine: int) -> Pose:
    """mine: +1 for the green fighter, -1 for red. outcome as winner_of()."""
    if f <= T_IDLE[1]:
        return IDLE
    if f <= T_WINDUP[1]:
        t = ease((f - T_WINDUP[0]) / (T_WINDUP[1] - T_WINDUP[0]))
        return blend_pose(IDLE, WINDUP[move], t)
    if f <= T_STRIKE[1]:
        t = ease((f - T_STRIKE[0]) / (T_STRIKE[1] - T_STRIKE[0]))
        return blend_pose(WINDUP[move], STRIKE[move], t)
    if f <= T_IMPACT[1]:
        return STRIKE[move]

    t = ease(_clamp01((f - T_RESULT[0]) / 14))
    if outcome == 0:
        return blend_pose(STRIKE[move], STANDOFF, t)
    won = (outcome > 0) == (mine > 0)
    return blend_pose(STRIKE[move], TRIUMPH if won else BEATEN, t)


def _throw_sand(canvas, rng, from_x, to_x, t) -> None:
    """حیله — a fistful of sand flung at the other one's eyes."""
    if not 0 < t < 1:
        return
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    head_y = GROUND - 120 * SCALE
    for _ in range(26):
        spread = rng.uniform(0.0, 1.0)
        px = lerp(from_x, to_x, t * (0.6 + 0.4 * spread))
        py = head_y + rng.uniform(-26, 26) * (0.3 + t)
        r = rng.uniform(1.5, 5.0)
        d.ellipse([px - r, py - r, px + r, py + r],
                  fill=(214, 186, 138, int(210 * (1 - t * 0.5))))
    layer = layer.filter(ImageFilter.GaussianBlur(1.3))
    canvas.paste(layer, (0, 0), layer)


def render_duel(green_name: str, red_name: str, green_move: str, red_move: str,
                seed: int | None = None):
    rng = random.Random(seed if seed is not None else 0)
    scene = build_scene(random.Random(4))
    motes = build_dust(random.Random(9))
    outcome = winner_of(green_move, red_move)

    frames = []
    for f in range(TOTAL):
        canvas = scene.copy()
        bob = math.sin(f * 0.28) * 2.0

        gp = _phase_pose(green_move, f, outcome, +1)
        rp = _phase_pose(red_move, f, outcome, -1)

        draw_warrior(canvas, GREEN_X, +1, gp, GREEN, GREEN_DARK, GREEN_TRIM, bob)
        draw_warrior(canvas, RED_X, -1, rp, RED, RED_DARK, RED_TRIM, -bob)

        # sand in the face, for whoever played the trick
        if T_STRIKE[0] <= f <= T_IMPACT[1]:
            t = (f - T_STRIKE[0]) / (T_IMPACT[1] - T_STRIKE[0])
            if green_move == TRICK:
                _throw_sand(canvas, random.Random(f), GREEN_X + 60, RED_X - 10, t)
            if red_move == TRICK:
                _throw_sand(canvas, random.Random(f + 99), RED_X - 60,
                            GREEN_X + 10, t)

        if T_IMPACT[0] <= f <= T_IMPACT[1] + 6:
            k = _clamp01(1 - (f - T_IMPACT[0]) / 9)
            draw_kickup(canvas, random.Random(f * 7), (GREEN_X + RED_X) / 2, k)
            if f <= T_IMPACT[0] + 1:
                flash = Image.new("RGBA", (W, H), (255, 246, 220, 70))
                canvas.paste(flash, (0, 0), flash)

        draw_dust(canvas, motes, f)

        draw_label(canvas, green_name, GREEN_X, 34, max_w=190, size=23,
                   colour=GREEN_DARK)
        draw_label(canvas, red_name, RED_X, 34, max_w=190, size=23,
                   colour=RED_DARK)
        if f >= T_WINDUP[1]:
            draw_label(canvas, FA_MOVE[green_move], GREEN_X, 62, max_w=150,
                       size=18, colour=(60, 46, 34))
            draw_label(canvas, FA_MOVE[red_move], RED_X, 62, max_w=150,
                       size=18, colour=(60, 46, 34))

        if f >= T_RESULT[0] + 6:
            if outcome == 0:
                text = "مساوی"
            else:
                text = f"برنده: {green_name if outcome > 0 else red_name}"
            draw_label(canvas, text, W // 2, H - 30, max_w=W - 60, size=30,
                       colour=(58, 38, 26))

        frames.append(canvas.convert("RGB"))
    return frames


def make_duel_gif(green_name: str, red_name: str, green_move: str,
                  red_move: str, seed: int | None = None) -> bytes:
    frames = render_duel(green_name, red_name, green_move, red_move, seed)
    master = frames[0].convert("P", palette=Image.ADAPTIVE, colors=110)
    pal = [master] + [f.quantize(palette=master, dither=Image.Dither.NONE)
                      for f in frames[1:]]
    buf = io.BytesIO()
    pal[0].save(buf, format="GIF", save_all=True, append_images=pal[1:],
                duration=int(1000 / FPS), loop=0, optimize=True, disposal=1)
    return buf.getvalue()
