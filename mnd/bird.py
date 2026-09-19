"""Flappy: a one-button, one-life game written for this demo.

Classic physics (30 fps, gravity 1, a flap sets the speed to -9, pipes scroll 4 px a frame
through a 100 px gap), our own pixel art in the microsandbox colours, and one seeded
``random.Random``. The whole game is plain Python state, so a branched microVM continues
the same flight towards the same pipes. No emulator, no ROM, no third-party game assets.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

WIDTH, HEIGHT, GROUND = 288, 512, 412
FPS = 30
SCROLL = 4  # pixels the world moves per frame; also the unit of progress (x_pos)
GRAVITY, FLAP_SPEED, MAX_FALL = 1, -9, 10
PIPE_WIDTH, PIPE_GAP, PIPE_SPACING, FIRST_PIPE = 52, 100, 144, 388
REACH = 90  # how far one gap can sit above or below the last
AIM = 0.55  # where in the gap the bird is steered, as a share of the gap from its top
BIRD_X, BIRD_W, BIRD_H = 62, 32, 26
ACTIONS = ("flap", "glide")
FRAMES_PER_DECISION = 6  # Jev is asked five times a second
FORESIGHT = 3  # and its outlook reaches this many decisions ahead: 18 frames

SPRITE = (
    "....OOOOOO......",
    "..OOWWWWWWOO....",
    ".OWWWWWWWWWWO...",
    ".OWWWWWWWEEWO...",
    "OWWWWWWWWEEWWOOO",
    "OLLLLWWWWWWWBBBO",
    "OLDDLLWWWWWBBBBO",
    "OLLLLLWWWWWWBBO.",
    ".OLLLWWWWWWWOO..",
    ".OGWWWWWWWWGO...",
    "..OGGWWWWGGO....",
    "...OOGGGGOO.....",
    ".....OOOO.......",
)
PALETTE = {
    "sky": "#10131f",
    "star": "#3a4160",
    "skyline": "#171b2b",
    "pipe": "#d5f45c",
    "pipe_hi": "#e6ff85",
    "pipe_lo": "#a9c43a",
    "edge": "#131708",
    "ground": "#1a1d29",
    "ground_top": "#a9c43a",
    "hatch": "#232738",
    "ink": "#f4f6ff",
    "O": "#07080d",
    "W": "#f4f6ff",
    "L": "#d5f45c",
    "D": "#a9c43a",
    "B": "#ff9f43",
    "E": "#07080d",
    "G": "#c3c9dc",
}
DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


@dataclass
class Pipe:
    x: int
    gap_top: int
    passed: bool = False

    @property
    def gap_bottom(self) -> int:
        return self.gap_top + PIPE_GAP


@dataclass
class Game:
    seed: int = 123
    frame: int = 0
    flown: int = 0  # frames flown alive; the crash's fall adds pictures, not distance
    y: float = 200.0
    velocity: float = 0.0
    score: int = 0
    dead: bool = False
    pipes: list[Pipe] = field(default_factory=list)

    def __post_init__(self):
        self.random = random.Random(self.seed)
        if not self.pipes:
            for index in range(3):
                self.pipes.append(self.new_pipe(FIRST_PIPE + index * PIPE_SPACING))

    def new_pipe(self, x: int) -> Pipe:
        low, high = 60, GROUND - PIPE_GAP - 60
        if self.pipes:
            # Between two pipes the bird can climb roughly 110 px; keep the next gap reachable.
            previous = self.pipes[-1].gap_top
            low, high = max(low, previous - REACH), min(high, previous + REACH)
        return Pipe(x, self.random.randrange(low, high + 1))

    @property
    def x_pos(self) -> int:
        """Distance flown. The controller measures progress, hazards and races in it."""
        return self.flown * SCROLL

    def step(self, flap: bool) -> bool:
        """Advance one frame. Returns True while the bird is alive."""
        if self.dead:
            return False
        self.velocity = FLAP_SPEED if flap else min(self.velocity + GRAVITY, MAX_FALL)
        self.y = max(0.0, self.y + self.velocity)
        for pipe in self.pipes:
            pipe.x -= SCROLL
            if not pipe.passed and pipe.x + PIPE_WIDTH < BIRD_X:
                pipe.passed = True
                self.score += 1
        if self.pipes[0].x < -PIPE_WIDTH - 8:
            self.pipes.pop(0)
            self.pipes.append(self.new_pipe(self.pipes[-1].x + PIPE_SPACING))
        self.frame += 1
        self.flown += 1
        self.dead = self.collides()
        return not self.dead

    def collides(self) -> bool:
        left, right = BIRD_X + 3, BIRD_X + BIRD_W - 3  # a forgiving hitbox, as in the classic
        top, bottom = self.y + 3, self.y + BIRD_H - 3
        if bottom >= GROUND:
            return True
        for pipe in self.pipes:
            overlaps = right > pipe.x and left < pipe.x + PIPE_WIDTH
            if overlaps and (top < pipe.gap_top or bottom > pipe.gap_bottom):
                return True
        return False

    def fall(self) -> bool:
        """One frame of the crash: the dead bird drops to the ground. True while falling."""
        self.velocity = min(self.velocity + 2 * GRAVITY, 2 * MAX_FALL)
        self.y = min(self.y + max(self.velocity, 4), GROUND - BIRD_H + 4)
        self.frame += 1
        return self.y < GROUND - BIRD_H + 4

    # ------------------------------------------------------------------ what Jev sees

    def ahead(self) -> list[Pipe]:
        return [pipe for pipe in self.pipes if pipe.x + PIPE_WIDTH >= BIRD_X]

    def observe(self, aim: float = AIM) -> dict:
        centre = int(self.y + BIRD_H / 2)
        gaps = []
        for pipe in self.ahead()[:2]:
            middle = pipe.gap_top + PIPE_GAP // 2
            gaps.append(
                {
                    "distance_ahead": max(0, pipe.x - (BIRD_X + BIRD_W)),
                    "frames_until_inside": max(0, (pipe.x - (BIRD_X + BIRD_W)) // SCROLL),
                    "frames_until_clear": max(0, (pipe.x + PIPE_WIDTH - BIRD_X) // SCROLL + 1),
                    "gap_top": pipe.gap_top,
                    "gap_bottom": pipe.gap_bottom,
                    "gap_centre": middle,
                    "bird_below_centre_by": centre - middle,
                }
            )
        return {
            "goal": "Fly through the gaps. Touching a pipe or the ground ends the run.",
            "units": "pixels; y grows downward; 30 frames a second",
            "frames_per_decision": FRAMES_PER_DECISION,
            "bird": {
                "centre_y": centre,
                "vertical_speed": self.velocity,
                "height_above_ground": int(GROUND - (self.y + BIRD_H)),
                "height": BIRD_H,
            },
            "gaps_ahead": gaps,
            "steer_for_y": int(target_line(self, aim)),
            "outlook": outlook(self, aim=aim),
            "score": self.score,
        }

    # ------------------------------------------------------------------ the picture

    def render(self):
        from PIL import ImageDraw

        image = _backdrop().copy()
        draw = ImageDraw.Draw(image)
        for pipe in self.pipes:
            _draw_pipe(draw, pipe)
        draw.rectangle([0, GROUND, WIDTH, HEIGHT], fill=PALETTE["ground"])
        draw.rectangle([0, GROUND, WIDTH, GROUND + 5], fill=PALETTE["ground_top"])
        offset = (self.flown * SCROLL) % 14
        for x in range(-20 - offset, WIDTH + 14, 14):
            draw.polygon(
                [(x, GROUND + 8), (x + 7, GROUND + 8), (x + 1, GROUND + 20), (x - 6, GROUND + 20)],
                fill=PALETTE["hatch"],
            )
        tilt = -80 if self.dead else 22 if self.velocity < 0 else -35 if self.velocity > 6 else 0
        sprite = _sprite(tilt)
        image.paste(sprite, (BIRD_X - (sprite.width - BIRD_W) // 2, int(self.y)), sprite)
        _draw_score(draw, self.score)
        return image


_CACHE: dict = {}


def _backdrop():
    from PIL import Image, ImageDraw

    if "backdrop" not in _CACHE:
        image = Image.new("RGB", (WIDTH, HEIGHT), PALETTE["sky"])
        draw = ImageDraw.Draw(image)
        rng = random.Random(7)
        for _ in range(46):
            x, y = rng.randrange(WIDTH), rng.randrange(GROUND - 120)
            draw.rectangle([x, y, x + 1, y + 1], fill=PALETTE["star"])
        x = 0
        while x < WIDTH:
            height, width = rng.choice((26, 40, 54, 34, 62, 46)), rng.choice((18, 24, 30))
            draw.rectangle([x, GROUND - height, x + width - 2, GROUND], fill=PALETTE["skyline"])
            x += width
        _CACHE["backdrop"] = image
    return _CACHE["backdrop"]


def _sprite(tilt: int):
    from PIL import Image, ImageDraw

    if ("sprite", tilt) not in _CACHE:
        sprite = Image.new("RGBA", (len(SPRITE[0]) * 2, len(SPRITE) * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(sprite)
        for row, line in enumerate(SPRITE):
            for column, key in enumerate(line):
                if key != ".":
                    box = [column * 2, row * 2, column * 2 + 1, row * 2 + 1]
                    draw.rectangle(box, fill=PALETTE[key])
        if tilt:
            sprite = sprite.rotate(tilt, resample=Image.NEAREST, expand=True)
        _CACHE["sprite", tilt] = sprite
    return _CACHE["sprite", tilt]


def _draw_pipe(draw, pipe: Pipe):
    x = pipe.x
    for top, bottom in ((0, pipe.gap_top), (pipe.gap_bottom, GROUND)):
        draw.rectangle([x, top, x + 51, bottom], fill=PALETTE["pipe"])
        draw.rectangle([x + 4, top, x + 11, bottom], fill=PALETTE["pipe_hi"])
        draw.rectangle([x + 40, top, x + 49, bottom], fill=PALETTE["pipe_lo"])
        draw.rectangle([x, top, x + 1, bottom], fill=PALETTE["edge"])
        draw.rectangle([x + 50, top, x + 51, bottom], fill=PALETTE["edge"])
    for lip in (pipe.gap_top - 24, pipe.gap_bottom):
        draw.rectangle([x - 3, lip, x + 54, lip + 23], fill=PALETTE["pipe"])
        draw.rectangle([x + 1, lip, x + 9, lip + 23], fill=PALETTE["pipe_hi"])
        draw.rectangle([x + 42, lip, x + 52, lip + 23], fill=PALETTE["pipe_lo"])
        draw.rectangle([x - 3, lip, x + 54, lip + 1], fill=PALETTE["edge"])
        draw.rectangle([x - 3, lip + 22, x + 54, lip + 23], fill=PALETTE["edge"])
        draw.rectangle([x - 3, lip, x - 2, lip + 23], fill=PALETTE["edge"])
        draw.rectangle([x + 53, lip, x + 54, lip + 23], fill=PALETTE["edge"])


def _draw_score(draw, score: int):
    text, scale = str(score), 5
    width = len(text) * 4 * scale - scale
    x = (WIDTH - width) // 2
    for character in text:
        for row, line in enumerate(DIGITS[character]):
            for column, bit in enumerate(line):
                if bit == "1":
                    box = [x + column * scale, 36 + row * scale]
                    box += [box[0] + scale - 1, box[1] + scale - 1]
                    draw.rectangle(
                        [box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1], fill=PALETTE["O"]
                    )
        for row, line in enumerate(DIGITS[character]):
            for column, bit in enumerate(line):
                if bit == "1":
                    box = [x + column * scale, 36 + row * scale]
                    draw.rectangle(
                        [*box, box[0] + scale - 1, box[1] + scale - 1], fill=PALETTE["ink"]
                    )
        x += 4 * scale


# ---------------------------------------------------------------------- playing without Jev


def fly(game: Game, plan) -> tuple[int, float, str | None]:
    """Fly a plan (one flap-or-glide per decision) on a copy of the physics. Returns the
    frames it lasted, where the bird ended, and what it hit (None if it is still flying)."""
    pipes = [(pipe.x, pipe.gap_top, pipe.gap_bottom) for pipe in game.pipes]
    y, velocity, frames = game.y, game.velocity, 0
    for flap in plan:
        for index in range(FRAMES_PER_DECISION):
            velocity = FLAP_SPEED if flap and index == 0 else min(velocity + GRAVITY, MAX_FALL)
            y = max(0.0, y + velocity)
            frames += 1
            top, bottom = y + 3, y + BIRD_H - 3
            if bottom >= GROUND:
                return frames, y, "the ground"
            for x, gap_top, gap_bottom in pipes:
                x -= SCROLL * frames
                inside = x < BIRD_X + BIRD_W - 3 and x + PIPE_WIDTH > BIRD_X + 3
                if inside and top < gap_top:
                    return frames, y, "the upper pipe"
                if inside and bottom > gap_bottom:
                    return frames, y, "the lower pipe"
    return frames + 1, y, None


def target_line(game: Game, aim: float = AIM) -> float:
    ahead = game.ahead()
    return ahead[0].gap_top + PIPE_GAP * aim if ahead else GROUND / 2


def outlook(game: Game, depth: int = FORESIGHT, aim: float = AIM) -> dict:
    """What each choice leaves open. For flap and for glide: taking it now, is there still
    any way to fly the next few decisions without hitting something? Jev reads this the way
    the Mario guest reads its terrain flags; the choice stays Jev's."""
    from itertools import product

    target, views = target_line(game, aim), {}
    for action in ACTIONS:
        plans = ((action == "flap", *rest) for rest in product((False, True), repeat=depth - 1))
        flights = (fly(game, plan) for plan in plans)
        frames, y, hit = max(flights, key=lambda f: (f[0], -abs(f[1] + BIRD_H / 2 - target)))
        views[action] = {
            "survivable": hit is None,
            "frames_until_crash": None if hit is None else frames,
            "crashes_into": hit,
            "ends_from_aim": int(abs(y + BIRD_H / 2 - target)),
        }
    return views


def heuristic(game: Game, depth: int = 7) -> str:
    """A stand-in controller for tests and keyless runs: try every flap/glide plan for the
    next few decisions on a copy of the physics and take the first move of the plan that
    survives longest, ending nearest the middle of the gap ahead."""
    from itertools import product

    target = target_line(game)

    def worth(plan):
        frames, y, _ = fly(game, plan)
        return frames, -abs(y + BIRD_H / 2 - target), -sum(plan)

    best = max(product((False, True), repeat=depth), key=worth)
    return "flap" if best[0] else "glide"


# ---------------------------------------------------------------------- fork experiments


# Cover both first-frame choices: gliding even one frame can doom a late checkpoint.
# The other two openings delay a flap, shifting subsequent Jev decisions off the old beat.
# Once the short opening ends, Jev chooses again with this verse's aim in the visible gap.
VERSES = (
    ((0.30, "flap", 1), (0.80, "glide", 1), (0.42, "delay", 2), (0.68, "delay", 4)),
    ((0.20, "flap", 2), (0.90, "glide", 2), (0.36, "delay", 3), (0.74, "delay", 5)),
    ((0.25, "flap", 4), (0.85, "glide", 4), (0.48, "delay", 1), (0.62, "delay", 3)),
)


def aim_words(aim: float) -> str:
    if aim < 0.35:
        return "aims high"
    if aim < 0.5:
        return "aims a little high"
    if aim <= 0.62:
        return "aims for the middle"
    return "aims a little low" if aim < 0.78 else "aims low"


def experiments(checkpoint_x: int, death_x: int) -> list[dict]:
    """Four short, different openings per race, followed by fresh Jev decisions."""
    import hashlib
    import json

    plans = []
    for race in VERSES:
        for aim, opening, frames in race:
            steps = [{"action": "glide" if opening == "delay" else opening, "frames": frames}]
            if opening == "delay":
                steps.append({"action": "flap", "frames": 1})
            label = f"glides {frames}f then flaps" if opening == "delay" else f"{opening}s now"
            signature = json.dumps([checkpoint_x, death_x, aim, steps])
            plans.append(
                {
                    "experiment_id": hashlib.sha256(signature.encode()).hexdigest()[:12],
                    "label": f"{aim_words(aim)}, {label}",
                    "aim": aim,
                    "x_min": checkpoint_x,
                    "x_max": death_x + 16,
                    "steps": steps,
                    "status": "waiting",
                    "step_index": 0,
                }
            )
    return plans


def validate_sequence(plan: dict):
    aim = plan.get("aim", AIM)
    if type(aim) not in (int, float) or not 0.1 <= aim <= 0.9:
        raise ValueError("A verse aims inside the gap")
    steps = plan.get("steps", [])
    if not 1 <= len(steps) <= 4:
        raise ValueError("Recovery sequences require one to four steps")
    for step in steps:
        frames = step.get("frames")
        if step.get("action") not in ACTIONS or type(frames) is not int or not 1 <= frames <= 48:
            raise ValueError("Invalid recovery step")
