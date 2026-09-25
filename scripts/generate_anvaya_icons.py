"""Render the code-native Anvaya connection mark for PWA icon sizes."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


OUT = Path(__file__).resolve().parents[1] / "app/static/icons"
BACKGROUND = (16, 24, 32)
CORAL = (255, 139, 104)
MINT = (184, 233, 223)


def segment_distance(x: float, y: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - (a[0] + t * dx), y - (a[1] + t * dy))


def mix(base: tuple[int, int, int], ink: tuple[int, int, int], opacity: float) -> tuple[int, int, int]:
    opacity = max(0.0, min(1.0, opacity))
    return tuple(round(a + (b - a) * opacity) for a, b in zip(base, ink))


def png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def render(filename: str, size: int, *, maskable: bool = False) -> None:
    rows = bytearray()
    scale = 0.76 if maskable else 1.0
    edge = 64 / size
    for py in range(size):
        rows.append(0)
        for px in range(size):
            x = 32 + ((px + 0.5) * edge - 32) / scale
            y = 32 + ((py + 0.5) * edge - 32) / scale
            segments = [((9, 51), (32, 10)), ((32, 10), (55, 51)), ((18, 39), (46, 39))]
            line_distance = min(segment_distance(x, y, a, b) for a, b in segments)
            coral_alpha = max(0.0, min(1.0, (2.5 - line_distance) / edge + 0.5))
            for cx, cy in ((9, 51), (55, 51)):
                coral_alpha = max(coral_alpha, max(0.0, min(1.0, (5 - math.hypot(x - cx, y - cy)) / edge + 0.5)))
            mint_alpha = 0.0
            for cx, cy, radius in ((32, 10, 5), (32, 39, 4)):
                mint_alpha = max(mint_alpha, max(0.0, min(1.0, (radius - math.hypot(x - cx, y - cy)) / edge + 0.5)))
            rgb = mix(mix(BACKGROUND, CORAL, coral_alpha), MINT, mint_alpha)
            rows.extend((*rgb, 255))
    image = b"\x89PNG\r\n\x1a\n"
    image += png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    image += png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    image += png_chunk(b"IEND", b"")
    (OUT / filename).write_bytes(image)


if __name__ == "__main__":
    render("anvaya-192.png", 192)
    render("anvaya-512.png", 512)
    render("anvaya-maskable-512.png", 512, maskable=True)
    render("anvaya-apple-touch-icon.png", 180)
