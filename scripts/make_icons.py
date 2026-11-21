"""Draw the web page's PNG icons from `icon.svg`. Standard library only; the output is committed.

    uv run python scripts/make_icons.py

`icon.svg` is the one source. It is read for exactly what it uses: rectangles with rounded corners
(filled with a linear gradient, or just an outline) and paths of absolute M, L, C and Z commands
filled with a linear gradient. Anything else stops the script, rather than being drawn wrongly.

The "any" icons keep the tile as the SVG draws it, with its margin and transparent rounded corners.
The maskable and iOS icons run the tile's colour out to the edge, because the system cuts its own
corners (iOS also fills transparency with black), and leave the faint outline off.
"""

from __future__ import annotations

import itertools
import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src/personal_agent/channels/web/static"
SVG_NS = "{http://www.w3.org/2000/svg}"
ROWS_PER_PIXEL = 8  # sub-scanlines per pixel; coverage along a row is exact
CURVE_STEPS = 32

Point = tuple[float, float]
Contour = list[Point]
Colour = tuple[float, float, float]
Pixel = tuple[float, float, float, float]  # straight (not premultiplied) RGBA, 0-255 and 0-1


class UnsupportedSvg(ValueError):
    pass


# ---- reading the SVG ----------------------------------------------------------------------------
def colour(text: str) -> Colour:
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        raise UnsupportedSvg(f"colour {text!r}: only #rrggbb is drawn")
    return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))


class Gradient:
    def __init__(self, element: ET.Element) -> None:
        if element.get("gradientUnits") != "userSpaceOnUse":
            raise UnsupportedSvg("gradients must use userSpaceOnUse")
        self.x1, self.y1, self.x2, self.y2 = (
            float(element.get(name, "0")) for name in ("x1", "y1", "x2", "y2")
        )
        self.stops = [
            (float(stop.get("offset", "0")), colour(stop.get("stop-color", "")))
            for stop in element.findall(f"{SVG_NS}stop")
        ]
        if len(self.stops) < 2:
            raise UnsupportedSvg("a gradient needs two stops")

    def at(self, x: float, y: float) -> Colour:
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        t = ((x - self.x1) * dx + (y - self.y1) * dy) / (dx * dx + dy * dy)
        t = min(1.0, max(0.0, t))
        for (a, ca), (b, cb) in itertools.pairwise(self.stops):
            if t <= b:
                k = 0.0 if b == a else (t - a) / (b - a)
                return (
                    ca[0] + (cb[0] - ca[0]) * k,
                    ca[1] + (cb[1] - ca[1]) * k,
                    ca[2] + (cb[2] - ca[2]) * k,
                )
        return self.stops[-1][1]


def cubic(p0: Point, p1: Point, p2: Point, p3: Point) -> Contour:
    points: Contour = []
    for i in range(1, CURVE_STEPS + 1):
        t = i / CURVE_STEPS
        u = 1 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        points.append(
            (
                a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
            )
        )
    return points


def path_contours(data: str) -> list[Contour]:
    if not re.fullmatch(r"[MLCZ0-9\s.,-]+", data):
        raise UnsupportedSvg("paths may only use the absolute commands M, L, C and Z")
    contours: list[Contour] = []
    for command, numbers in re.findall(r"([MLCZ])([^MLCZ]*)", data):
        values = [float(n) for n in re.findall(r"-?\d*\.?\d+", numbers)]
        if command == "M":
            contours.append([(values[0], values[1])])
        elif command == "L":
            contours[-1].append((values[0], values[1]))
        elif command == "C":
            for i in range(0, len(values), 6):
                v = values[i : i + 6]
                contours[-1] += cubic(contours[-1][-1], (v[0], v[1]), (v[2], v[3]), (v[4], v[5]))
    return contours


def rounded_rect(x: float, y: float, w: float, h: float, radius: float) -> Contour:
    """Clockwise outline of a rectangle with round corners."""
    points: Contour = []
    corners = [
        (x + w - radius, y + radius, -90),
        (x + w - radius, y + h - radius, 0),
        (x + radius, y + h - radius, 90),
        (x + radius, y + radius, 180),
    ]
    for cx, cy, start in corners:
        steps = max(1, CURVE_STEPS if radius else 1)
        for i in range(steps + 1):
            angle = math.radians(start + 90 * i / steps)
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


# ---- drawing ------------------------------------------------------------------------------------
def coverage(contours: list[Contour], size: int, scale: float) -> list[list[float]]:
    """How much of each pixel the shape covers (non-zero winding), row by row."""
    edges = []
    for contour in contours:
        for (x0, y0), (x1, y1) in zip(contour, contour[1:] + contour[:1], strict=True):
            if y0 != y1:
                edges.append((x0 * scale, y0 * scale, x1 * scale, y1 * scale))
    rows: list[list[float]] = []
    for row in range(size):
        full = [0.0] * (size + 2)  # difference array for runs of whole pixels
        part = [0.0] * (size + 1)
        for k in range(ROWS_PER_PIXEL):
            y = row + (k + 0.5) / ROWS_PER_PIXEL
            crossings = []
            for x0, y0, x1, y1 in edges:
                if y0 <= y < y1:
                    crossings.append((x0 + (y - y0) * (x1 - x0) / (y1 - y0), 1))
                elif y1 <= y < y0:
                    crossings.append((x0 + (y - y0) * (x1 - x0) / (y1 - y0), -1))
            crossings.sort()
            winding = 0
            for (xa, direction), (xb, _) in itertools.pairwise(crossings):
                winding += direction
                xa, xb = max(xa, 0.0), min(xb, float(size))
                if winding == 0 or xb <= xa:
                    continue
                first, last = int(xa), min(int(xb), size - 1)
                if first == last:
                    part[first] += (xb - xa) / ROWS_PER_PIXEL
                    continue
                part[first] += (first + 1 - xa) / ROWS_PER_PIXEL
                part[last] += (xb - last) / ROWS_PER_PIXEL
                full[first + 1] += 1 / ROWS_PER_PIXEL
                full[last] -= 1 / ROWS_PER_PIXEL
        run, line = 0.0, []
        for x in range(size):
            run += full[x]
            line.append(min(1.0, part[x] + run))
        rows.append(line)
    return rows


def over(base: Pixel, paint: Colour, alpha: float) -> Pixel:
    r, g, b, a = base
    out = alpha + a * (1 - alpha)
    if out == 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        (paint[0] * alpha + r * a * (1 - alpha)) / out,
        (paint[1] * alpha + g * a * (1 - alpha)) / out,
        (paint[2] * alpha + b * a * (1 - alpha)) / out,
        out,
    )


def render(svg: Path, size: int, *, bleed: bool) -> bytes:
    """RGBA rows of the icon at `size` pixels. `bleed` runs the tile to the edge, no outline."""
    root = ET.parse(svg).getroot()  # noqa: S314 - the repo's own icon.svg, not untrusted input
    view = [float(n) for n in root.get("viewBox", "").split()]
    if len(view) != 4 or view[0] != 0 or view[1] != 0 or view[2] != view[3]:
        raise UnsupportedSvg("viewBox must be a square starting at 0 0")
    scale = size / view[2]
    gradients = {g.get("id", ""): Gradient(g) for g in root.iter(f"{SVG_NS}linearGradient")}
    canvas: list[list[Pixel]] = [[(0.0, 0.0, 0.0, 0.0)] * size for _ in range(size)]

    def lay(contours: list[Contour], paint, opacity: float = 1.0) -> None:
        for y, line in enumerate(coverage(contours, size, scale)):
            for x, amount in enumerate(line):
                if amount > 0:
                    tone = paint((x + 0.5) / scale, (y + 0.5) / scale)
                    canvas[y][x] = over(canvas[y][x], tone, amount * opacity)

    def fill_of(element: ET.Element):
        reference = re.fullmatch(r"url\(#([^)]+)\)", element.get("fill", ""))
        if not reference:
            raise UnsupportedSvg("fills must be a gradient")
        gradient = gradients[reference.group(1)]
        return gradient.at

    for element in root:
        tag = element.tag.removeprefix(SVG_NS)
        if tag in ("title", "defs"):
            continue
        if tag == "rect":
            x, y, w, h = (float(element.get(n, "0")) for n in ("x", "y", "width", "height"))
            radius = float(element.get("rx", "0"))
            if element.get("stroke"):  # the faint outline round the tile
                if bleed:
                    continue
                edge = float(element.get("stroke-width", "1")) / 2
                outer = rounded_rect(x - edge, y - edge, w + 2 * edge, h + 2 * edge, radius + edge)
                inner = rounded_rect(x + edge, y + edge, w - 2 * edge, h - 2 * edge, radius - edge)
                tone = colour(element.get("stroke", ""))
                lay(
                    [outer, inner[::-1]],
                    lambda _x, _y, tone=tone: tone,
                    float(element.get("stroke-opacity", "1")),
                )
            elif bleed:
                lay([[(0, 0), (view[2], 0), (view[2], view[3]), (0, view[3])]], fill_of(element))
            else:
                lay([rounded_rect(x, y, w, h, radius)], fill_of(element))
        elif tag == "path":
            lay(path_contours(element.get("d", "")), fill_of(element))
        else:
            raise UnsupportedSvg(f"<{tag}> is not drawn")

    rows = bytearray()
    for line in canvas:
        rows.append(0)  # PNG filter: none
        for r, g, b, a in line:
            rows += bytes((round(r), round(g), round(b), round(a * 255)))
    return bytes(rows)


def png(size: int, data: bytes) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(data, 9))
        + chunk(b"IEND", b"")
    )


ICONS = [
    ("icon-192.png", 192, False),
    ("icon-512.png", 512, False),
    ("icon-maskable-512.png", 512, True),  # the system rounds it, so it must be full-bleed
    ("apple-touch-icon.png", 180, True),  # iOS rounds it too, and fills transparency with black
]


def main() -> None:
    for name, size, bleed in ICONS:
        (STATIC / name).write_bytes(png(size, render(STATIC / "icon.svg", size, bleed=bleed)))
        print("wrote", STATIC / name)


if __name__ == "__main__":
    main()
