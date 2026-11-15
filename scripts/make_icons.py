"""Draw the web page's icons. Standard library only; the output is committed.

    python3 scripts/make_icons.py

An ink-dark square with three ruled lines, like a page of a log, and a small vermilion dot beside the
first: the agent's pilot light. The same shapes are in `icon.svg`; keep the two in step by editing
the numbers below and the SVG together.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src/personal_agent/channels/web/static"

INK = (0x1F, 0x1B, 0x14)
PAPER = (0xF4, 0xEC, 0xD8)
VERMILION = (0xE0, 0x55, 0x3A)

LINES = [((136, 190), (300, 190)), ((136, 262), (376, 262)), ((136, 334), (276, 334))]
LINE_WIDTH = 30
DOT = (376, 190)
DOT_RADIUS = 28
HALO_RADIUS = 44
HALO_WIDTH = 6
CORNER = 112


def coverage(distance: float) -> float:
    """1 inside a shape, 0 outside, a one-pixel ramp across its edge."""
    return min(1.0, max(0.0, 0.5 - distance))


def capsule(x: float, y: float, a: tuple[int, int], b: tuple[int, int], radius: float) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (ax + t * dx), y - (ay + t * dy)) - radius


def rounded_square(x: float, y: float, size: float, corner: float) -> float:
    qx = abs(x - size / 2) - (size / 2 - corner)
    qy = abs(y - size / 2) - (size / 2 - corner)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - corner


def over(base: tuple[float, float, float, float], colour: tuple[int, int, int], alpha: float):
    """`colour` laid over `base` at `alpha`."""
    r, g, b, a = base
    out_a = alpha + a * (1 - alpha)
    if out_a == 0:
        return (0.0, 0.0, 0.0, 0.0)
    cr, cg, cb = colour
    return (
        (cr * alpha + r * a * (1 - alpha)) / out_a,
        (cg * alpha + g * a * (1 - alpha)) / out_a,
        (cb * alpha + b * a * (1 - alpha)) / out_a,
        out_a,
    )


def render(size: int, *, rounded: bool) -> bytes:
    """RGBA rows of the icon at `size` pixels. `rounded` leaves the corners transparent."""
    scale = size / 512
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter: none
        for px in range(size):
            x, y = (px + 0.5) / scale, (py + 0.5) / scale
            pixel = (0.0, 0.0, 0.0, 0.0)
            background = coverage(rounded_square(x, y, 512, CORNER) * scale) if rounded else 1.0
            pixel = over(pixel, INK, background)
            for a, b in LINES:
                pixel = over(pixel, PAPER, coverage(capsule(x, y, a, b, LINE_WIDTH / 2) * scale))
            ring = abs(math.hypot(x - DOT[0], y - DOT[1]) - HALO_RADIUS) - HALO_WIDTH / 2
            pixel = over(pixel, VERMILION, coverage(ring * scale) * 0.35)
            dot = math.hypot(x - DOT[0], y - DOT[1]) - DOT_RADIUS
            pixel = over(pixel, VERMILION, coverage(dot * scale))
            r, g, b, a = pixel
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


def main() -> None:
    for name, size, rounded in [
        ("icon-192.png", 192, True),
        ("icon-512.png", 512, True),
        ("icon-maskable-512.png", 512, False),  # the system rounds it, so it must be full-bleed
        (
            "apple-touch-icon.png",
            180,
            False,
        ),  # iOS rounds it too, and fills transparency with black
    ]:
        (OUT / name).write_bytes(png(size, render(size, rounded=rounded)))
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
