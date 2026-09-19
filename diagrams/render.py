"""Render the README diagrams as self-contained SVGs in light and dark variants.

    uv run python diagrams/render.py

The README embeds each pair with a <picture> element so GitHub picks the variant that
matches the reader's colour scheme.
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"

# fill, stroke, title, subtitle — per ramp, per theme.
PALETTE = {
    "light": {
        "purple": ("#EEEDFE", "#534AB7", "#3C3489", "#534AB7"),
        "teal": ("#E1F5EE", "#0F6E56", "#085041", "#0F6E56"),
        "coral": ("#FAECE7", "#993C1D", "#712B13", "#993C1D"),
        "gray": ("#F1EFE8", "#5F5E5A", "#444441", "#5F5E5A"),
        "text": "#2C2C2A",
        "muted": "#5F5E5A",
        "line": "#888780",
    },
    "dark": {
        "purple": ("#3C3489", "#AFA9EC", "#CECBF6", "#AFA9EC"),
        "teal": ("#085041", "#5DCAA5", "#9FE1CB", "#5DCAA5"),
        "coral": ("#712B13", "#F0997B", "#F5C4B3", "#F0997B"),
        "gray": ("#444441", "#B4B2A9", "#D3D1C7", "#B4B2A9"),
        "text": "#D3D1C7",
        "muted": "#B4B2A9",
        "line": "#888780",
    },
}

ARROW = (
    '<marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" '
    'orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="{line}" '
    'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></marker>'
)


class Canvas:
    def __init__(self, theme: str, height: int, title: str):
        self.p = PALETTE[theme]
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="680" height="{height}" '
            f'viewBox="0 0 680 {height}" role="img" font-family="{FONT}">',
            f"<title>{title}</title>",
            "<defs>" + ARROW.format(line=self.p["line"]) + "</defs>",
        ]

    def node(self, x, y, w, h, ramp, title, sub=None):
        fill, stroke, t1, t2 = self.p[ramp]
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="0.75"/>'
        )
        cx = x + w / 2
        if sub:
            self.text(cx, y + 18, title, t1, 14, 500)
            self.text(cx, y + 38, sub, t2, 12)
        else:
            self.text(cx, y + h / 2, title, t1, 14, 500)

    def container(self, x, y, w, h, ramp, title, sub):
        fill, stroke, t1, t2 = self.p[ramp]
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="20" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="0.75"/>'
        )
        self.text(x + 24, y + 24, title, t1, 14, 500, "start")
        self.text(x + 24, y + 42, sub, t2, 12, 400, "start")

    def text(self, x, y, value, color, size=12, weight=400, anchor="middle"):
        self.parts.append(
            f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" dominant-baseline="central">{value}</text>'
        )

    def label(self, x, y, value, anchor="start"):
        self.text(x, y, value, self.p["muted"], 12, 400, anchor)

    def arrow(self, points, start=False, end=True):
        d = " ".join(f"{'M' if i == 0 else 'L'}{x} {y}" for i, (x, y) in enumerate(points))
        markers = (' marker-start="url(#a)"' if start else "") + (
            ' marker-end="url(#a)"' if end else ""
        )
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{self.p["line"]}" stroke-width="1.25"{markers}/>'
        )

    def swatch(self, x, y, ramp, value):
        fill, stroke, _, _ = self.p[ramp]
        self.parts.append(
            f'<rect x="{x}" y="{y - 6}" width="12" height="12" rx="2" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="0.75"/>'
        )
        self.label(x + 20, y, value)

    def dashed(self, x, y, w, h, ramp):
        stroke = self.p[ramp][1]
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="20" fill="none" '
            f'stroke="{stroke}" stroke-width="0.75" stroke-dasharray="4 3"/>'
        )

    def run_line(self, x0, x1, y, head=True):
        stroke = self.p["teal"][1]
        self.parts.append(
            f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="{stroke}" stroke-width="3" '
            'stroke-linecap="round"/>'
        )
        if head:
            self.parts.append(f'<circle cx="{x1}" cy="{y}" r="6" fill="{stroke}"/>')

    def dead_line(self, x0, x1, y):
        self.parts.append(
            f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="{self.p["line"]}" '
            'stroke-width="2" stroke-linecap="round"/>'
        )

    def cross(self, x, y, r):
        red = "#E24B4A"
        for dx, dy in ((r, r), (r, -r)):
            self.parts.append(
                f'<line x1="{x - dx}" y1="{y - dy}" x2="{x + dx}" y2="{y + dy}" stroke="{red}" '
                'stroke-width="2.5" stroke-linecap="round"/>'
            )

    def gate_mark(self, x, y):
        fill = self.p["purple"][1]
        for dx in (-6, 2):
            self.parts.append(
                f'<rect x="{x + dx}" y="{y - 10}" width="4" height="20" fill="{fill}"/>'
            )

    def path(self, d, stroke, width):
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{width}" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    def square(self, x, y, ramp):
        fill, stroke, _, _ = self.p[ramp]
        self.parts.append(
            f'<rect x="{x - 7}" y="{y - 7}" width="14" height="14" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.5"/>'
        )

    def save(self, name: str, theme: str):
        self.parts.append("</svg>")
        (HERE / f"{name}-{theme}.svg").write_text("\n".join(self.parts) + "\n", encoding="utf-8")


def system(theme):
    c = Canvas(theme, 460, "Mario Never Dies components")
    c.node(240, 40, 200, 56, "gray", "Browser", "watch, rewind, replay")
    c.arrow([(340, 98), (340, 148)], start=True)
    c.label(352, 123, "frames and state")
    c.node(220, 150, 240, 56, "purple", "Orchestrator", "Python on the host · microsandbox SDK")
    c.node(40, 150, 160, 56, "coral", "Jev", "TypeSafe API, remote")
    c.arrow([(145, 208), (145, 308)], start=True)
    c.label(155, 236, "a move every 8 frames")
    c.arrow([(340, 208), (340, 258)], start=True)
    c.label(352, 233, "create · branch · pause · kill")
    c.dashed(40, 260, 600, 150, "teal")
    c.text(64, 284, "microVMs", c.p["teal"][2], 14, 500, "start")
    c.label(150, 284, "126 MiB and 1 vCPU each")
    c.node(60, 310, 170, 56, "teal", "Running Mario", "emulator + Jev client")
    c.node(255, 310, 170, 56, "purple", "Frozen copies", "paused, ready to fork")
    c.node(450, 310, 170, 56, "teal", "Race trials", "4 at once, during a fork")
    c.swatch(60, 436, "teal", "running VM")
    c.swatch(180, 436, "purple", "microsandbox, frozen copy")
    c.swatch(390, 436, "coral", "Jev")
    c.save("system", theme)


def gate(theme):
    c = Canvas(theme, 280, "Checkpoints along a run")
    c.label(180, 62, "gate: no Jev request in flight", "middle")
    c.label(330, 62, "150 frames later", "middle")
    c.label(480, 62, "150 frames later", "middle")
    c.run_line(60, 600, 110)
    c.label(60, 134, "stage start")
    c.label(600, 134, "Mario keeps playing", "end")
    for x in (180, 330, 480):
        c.gate_mark(x, 110)
        c.arrow([(x, 122), (x, 158)])
        c.node(x - 60, 160, 120, 56, "purple", "Frozen copy", "whole VM, paused")
    c.label(
        340,
        250,
        "each drop is branch() in about 150 ms, then pause(); the copies wait as save slots",
        "middle",
    )
    c.save("gate", theme)


def race(theme):
    """The browser's timeline shape: lanes curve out of a copy, the survivor curves back."""
    c = Canvas(theme, 290, "A death becomes a fork")
    teal, red, grey = c.p["teal"][1], "#E24B4A", c.p["line"]
    c.label(250, 44, "frozen copy", "middle")
    c.label(352, 44, "death", "middle")
    c.run_line(40, 250, 70, head=False)
    c.path("M257 70 L340 70", red, 2)
    c.cross(348, 70, 6)
    c.square(250, 70, "purple")
    lanes = (
        ("early jump", 120, "cancelled", 520),
        ("slower jump", 150, "cancelled", 520),
        ("wait, then jump", 180, "survived", 560),
        ("retreat, then jump", 210, "died", 500),
    )
    for name, y, outcome, end in lanes:
        colour = {"survived": teal, "died": red, "cancelled": grey}[outcome]
        width = 3 if outcome == "survived" else 2
        c.path(f"M250 77 C250 {y}, 268 {y}, 300 {y}", colour, width)
        c.label(308, y, name)
        x0 = 308 + int(len(name) * 6.6) + 8
        c.path(f"M{x0} {y} L{end} {y}", colour, width)
        if outcome == "died":
            c.cross(end + 6, y, 5)
        elif outcome == "cancelled":
            c.path(f"M{end} {y - 5} L{end} {y + 5}", grey, 2)
        else:
            c.path(f"M{end} {y} C600 {y}, 580 70, 610 70 L640 70", teal, 3)
            c.parts.append(f'<circle cx="640" cy="70" r="6" fill="{teal}"/>')
    c.path("M40 254 L64 254", teal, 3)
    c.label(72, 254, "survived, now the present")
    c.path("M250 254 L268 254", red, 2)
    c.cross(276, 254, 5)
    c.label(290, 254, "died")
    c.path("M350 254 L374 254", grey, 2)
    c.path("M374 249 L374 259", grey, 2)
    c.label(382, 254, "stopped once a winner was found")
    c.save("race", theme)


def hero():
    """The README's opening picture: the map of the multiverse from the closing card, drawing
    itself. The lime line grows left to right; at each fork the discarded verses burst out and
    die while the line carries on. Same wave and fan rule as ``drawMultiverse`` in web/app.js.

    One file, dark in both themes (it is a picture of the product). It is a CSS-animated SVG,
    which GitHub plays inside an <img>; with reduced motion it is the finished still."""
    import hashlib
    import math

    def noise(key) -> float:
        return int(hashlib.md5(str(key).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF

    width, height, seed = 1200, 300, 0.37
    reach, left, right = height * 0.3, 24, 60
    loop, drawn = 10.0, 0.75  # seconds per loop; the share of it spent drawing the line
    forks = ((0.1, 4), (0.2, 6), (0.31, 3), (0.43, 8), (0.55, 5), (0.66, 14), (0.78, 4), (0.89, 7))

    def x_at(c):
        return left + c * (width - left - right)

    def y_at(c):
        wave = math.sin(c * math.tau * 1.3 + seed * 6.28) * 0.6
        wave += math.sin(c * math.tau * 3.1 + seed * 17.3) * 0.4
        return height / 2 + wave * height * 0.13

    # The line is drawn at constant speed along its length, so a fork fires when the pen
    # reaches it: measure how far along the curve each fork sits.
    steps = 600
    lengths = [0.0]
    for step in range(1, steps + 1):
        a, b = (step - 1) / steps, step / steps
        lengths.append(lengths[-1] + math.hypot(x_at(b) - x_at(a), y_at(b) - y_at(a)))

    def along(c):
        return lengths[round(c * steps)] / lengths[-1]

    line = " ".join(
        f"{'M' if step == 0 else 'L'}{x_at(step / 240):.1f} {y_at(step / 240):.1f}"
        for step in range(241)
    )
    end = drawn * 100  # percent of the loop at which the line is complete
    css = [
        f"*{{animation-duration:{loop:g}s;animation-iteration-count:infinite;"
        "animation-timing-function:linear}",
        "#all{animation-name:all}",
        "@keyframes all{0%,93%{opacity:1}99%,100%{opacity:0}}",
        ".pen{stroke-dasharray:1;animation-name:pen}",
        f"@keyframes pen{{0%{{stroke-dashoffset:1}}{end:g}%,100%{{stroke-dashoffset:0}}}}",
        f".head{{offset-path:path('{line}');offset-distance:100%;animation-name:head}}",
        f"@keyframes head{{0%{{offset-distance:0%}}{end:g}%,100%{{offset-distance:100%}}}}",
        ".ring{transform-box:fill-box;transform-origin:center;animation-name:ring}",
        f"@keyframes ring{{0%,{end:g}%{{opacity:0;transform:scale(.4)}}"
        f"{end + 4:g}%{{opacity:.5;transform:scale(1)}}"
        f"{end + 11:g}%{{opacity:.25;transform:scale(1.25)}}"
        "100%{opacity:.5;transform:scale(1)}}",
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}",
    ]
    body = []
    for number, (c, count) in enumerate(forks):
        fired = along(c) * drawn * 100  # percent of the loop at which the pen arrives
        css += [
            f".s{number}{{stroke-dasharray:1;animation-name:s{number}}}",
            # Hidden until it fires: a round cap draws an undrawn strand as a speck.
            f"@keyframes s{number}{{0%,{fired:.2f}%{{stroke-dashoffset:1;opacity:0}}"
            f"{fired + 0.2:.2f}%{{stroke-dashoffset:.96;opacity:.5}}"
            f"{fired + 4.5:.2f}%{{stroke-dashoffset:0;opacity:.5}}"
            f"{fired + 14:.2f}%,100%{{stroke-dashoffset:0;opacity:.24}}}}",
            f".x{number}{{animation-name:x{number}}}",
            f"@keyframes x{number}{{0%,{fired + 3.8:.2f}%{{opacity:0}}"
            f"{fired + 5:.2f}%{{opacity:1}}{fired + 14:.2f}%,100%{{opacity:.75}}}}",
            f".n{number}{{animation-name:n{number}}}",
            f"@keyframes n{number}{{0%,{max(0, fired - 0.4):.2f}%{{opacity:0}}"
            f"{fired:.2f}%,100%{{opacity:1}}}}",
        ]
        px, py = x_at(c), y_at(c)
        tangent = math.atan2(y_at(c + 0.005) - y_at(c - 0.005), x_at(c + 0.005) - x_at(c - 0.005))
        step = min(0.2, (1.45 - 0.42) / max(1, math.ceil(count / 2) - 1))
        for index in range(count):
            side = 1 if index % 2 else -1
            angle = tangent + side * (0.42 + step * (index // 2))
            uneven = 0.84 + 0.3 * noise(f"{number}:u{index}") if count > 10 else 1
            length = (0.35 + 0.65 * noise(f"{number}:r{index}")) * reach * uneven
            ex, ey = px + math.cos(angle) * length, py + math.sin(angle) * length
            mx, my = px + math.cos(tangent) * length * 0.5, py + math.sin(tangent) * length * 0.5
            body.append(
                f'<path class="s{number}" pathLength="1" stroke="#fff" stroke-opacity=".9" '
                f'stroke-width="1.8" opacity=".24" '
                f'd="M{px:.1f} {py:.1f} Q{mx:.1f} {my:.1f} {ex:.1f} {ey:.1f}"/>'
            )
            body.append(
                f'<path class="x{number}" stroke="#d97a85" stroke-width="2" opacity=".75" '
                f'd="M{ex - 3.4:.1f} {ey - 3.4:.1f}l6.8 6.8m0-6.8l-6.8 6.8"/>'
            )
    body.append(
        f'<path class="pen" pathLength="1" d="{line}" stroke="#d5f45c" stroke-width="4" '
        'stroke-linejoin="round"/>'
    )
    for number, (c, _) in enumerate(forks):
        body.append(
            f'<circle class="n{number}" cx="{x_at(c):.1f}" cy="{y_at(c):.1f}" r="4" '
            'fill="#0e0e12" stroke="#d5f45c" stroke-width="2"/>'
        )
    body.append('<circle class="head ring" r="15" stroke="#d5f45c" stroke-width="3" opacity=".5"/>')
    body.append('<circle class="head" r="8.5" fill="#d5f45c"/>')
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" fill="none" stroke-linecap="round">',
        "<title>One run of Mario Never Dies: the line that survived, and every future "
        "that was thrown away</title>",
        "<style>" + "".join(css) + "</style>",
        f'<rect width="{width}" height="{height}" rx="14" fill="#0e0e12"/>',
        '<g id="all">',
        *body,
        "</g>",
        "</svg>",
    ]
    (HERE / "map.svg").write_text("\n".join(parts) + "\n", encoding="utf-8")


MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
BRAND = {"light": "#9333ea", "dark": "#c084fc"}  # microsandbox's purple, as on msbx.fyi


def powered(theme):
    """The README's last line: microsandbox in its brand colour, the rest neutral, framed by
    the four corners of a box whose sides are not drawn."""
    p = PALETTE[theme]
    width, height, inset, arm, size = 372, 46, 3, 6, 18
    x0, y0, x1, y1 = inset, inset, width - inset, height - inset
    corners = (
        f"M{x0} {y0 + arm}V{y0}H{x0 + arm}M{x1 - arm} {y0}H{x1}V{y0 + arm}"
        f"M{x1} {y1 - arm}V{y1}H{x1 - arm}M{x0 + arm} {y1}H{x0}V{y1 - arm}"
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" font-family="{MONO}" font-size="{size}" '
        'letter-spacing=".3">'
        "<title>powered by microsandbox + jev</title>"
        f'<path d="{corners}" fill="none" stroke="{p["muted"]}" stroke-width="1.5" '
        'shape-rendering="crispEdges"/>'
        f'<text x="{width / 2:g}" y="{height / 2 + 6:g}" text-anchor="middle">'
        f'<tspan fill="{p["muted"]}">powered by </tspan>'
        f'<tspan fill="{BRAND[theme]}">microsandbox</tspan>'
        f'<tspan fill="{p["muted"]}"> + jev</tspan></text></svg>'
    )
    (HERE / f"powered-{theme}.svg").write_text(svg + "\n", encoding="utf-8")


def favicon():
    """The page's tab icon: a pixel yin-yang. Lime is the timeline that lives, red the ones
    that die, and each carries a dot of the other: every death forks a life."""
    lime, red, ink, size = "#d5f45c", "#d97a85", "#0e0e12", 16
    centre = size / 2

    def colour(col, row):
        x, y = col + 0.5, row + 0.5
        from_centre = ((x - centre) ** 2 + (y - centre) ** 2) ** 0.5
        if from_centre > 8:
            return None
        if from_centre > 7:
            return ink
        if col in (7, 8) and row in (4, 5):
            return red  # death inside life
        if col in (7, 8) and row in (10, 11):
            return lime  # life inside death
        # A touch over half the radius, so no one-pixel sliver of the other colour is left
        # between a head and the rim.
        if ((x - centre) ** 2 + (y - 4.5) ** 2) ** 0.5 <= 4:
            return lime
        if ((x - centre) ** 2 + (y - 11.5) ** 2) ** 0.5 <= 4:
            return red
        return red if x < centre else lime

    runs = []
    for row in range(size):
        col = 0
        while col < size:
            fill = colour(col, row)
            end = col
            while end < size and colour(end, row) == fill:
                end += 1
            if fill:
                runs.append(
                    f'<rect x="{col}" y="{row}" width="{end - col}" height="1" fill="{fill}"/>'
                )
            col = end
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges">{"".join(runs)}</svg>'
    )
    (HERE.parent / "web" / "assets" / "favicon.svg").write_text(svg + "\n", encoding="utf-8")


if __name__ == "__main__":
    favicon()
    hero()
    for theme in PALETTE:
        powered(theme)
        system(theme)
        gate(theme)
        race(theme)
    print("\n".join(sorted(p.name for p in HERE.glob("*.svg"))))
