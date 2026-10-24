"""Draw the app icons (a clip between two trim handles, with a play button) as PNGs, using only numpy.

Run from the repo root: python frontend/scripts/make_icons.py
Everything stays inside the central circle that Android's maskable icons keep visible.
"""

import struct
import zlib
from pathlib import Path

import numpy as np

PUBLIC = Path(__file__).resolve().parent.parent / "public"
BACKGROUND = (9, 9, 11)  # zinc-950
CLIP = (124, 58, 237)  # violet-600
HANDLES = (196, 181, 253)  # violet-300
PLAY = (255, 255, 255)


def draw(size: int, supersample: int = 4) -> np.ndarray:
    n = size * supersample
    y, x = np.mgrid[0:n, 0:n].astype(np.float32)
    u, v = (x + 0.5) / n - 0.5, (y + 0.5) / n - 0.5  # -0.5 .. 0.5, centred
    img = np.empty((n, n, 3), np.float32)
    img[:] = BACKGROUND

    img[rounded_rect(u, v, 0.27, 0.19, 0.05)] = CLIP
    for side in (-1, 1):
        img[rounded_rect(u - side * 0.285, v, 0.018, 0.235, 0.018)] = HANDLES
    img[triangle(u, v, (-0.065, -0.095), (-0.065, 0.095), (0.1, 0.0))] = PLAY

    small = img.reshape(size, supersample, size, supersample, 3).mean(axis=(1, 3))
    return np.clip(small + 0.5, 0, 255).astype(np.uint8)


def rounded_rect(u, v, half_w, half_h, radius):
    qx, qy = np.abs(u) - (half_w - radius), np.abs(v) - (half_h - radius)
    outside = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - radius
    return outside <= 0


def triangle(u, v, a, b, c):
    def edge(p, q):
        return (u - p[0]) * (q[1] - p[1]) - (v - p[1]) * (q[0] - p[0])

    e = edge(a, b), edge(b, c), edge(c, a)
    return ((e[0] >= 0) & (e[1] >= 0) & (e[2] >= 0)) | ((e[0] <= 0) & (e[1] <= 0) & (e[2] <= 0))


def write_png(path: Path, rgb: np.ndarray) -> None:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[row].tobytes() for row in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    PUBLIC.mkdir(exist_ok=True)
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        write_png(PUBLIC / name, draw(size))
        print("wrote", PUBLIC / name)
