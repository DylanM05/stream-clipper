"""A show's overlay for its landscape videos: a 1920x1080 PNG with transparent parts, uploaded in the app.

Two kinds, told apart by the image itself:
- a frame (a TV set, say) whose design goes all the way round a transparent screen in the middle: the clip
  is shrunk to fit that screen, so none of the stream is hidden;
- anything else, such as a logo in a corner: laid on top of the full-size clip.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import ffmpeg

WIDTH, HEIGHT = 1920, 1080
SOLID = 200  # alpha above this is part of the design; less is see-through (a screen's glare, say)
MIN_SCREEN = 0.4  # of the width and height: smaller than this and it's not a screen to play the clip in


@dataclass
class Overlay:
    path: Path
    box: tuple[int, int, int, int]  # x, y, w, h of the part of the image with anything in it
    screen: tuple[int, int, int, int] | None  # x, y, w, h of a frame's screen, where the clip goes

    @classmethod
    def load(cls, path: Path, box: str, screen: str) -> "Overlay | None":
        """The overlay saved at `path`, with the box and screen `save` found (stored as JSON), if there is one."""
        if not path.exists() or not box:
            return None
        return cls(path, tuple(json.loads(box)), tuple(json.loads(screen)) if screen else None)


def save(upload: Path, out: Path) -> Overlay:
    """Save an uploaded image as an overlay: 1920x1080 (scaled to fill, cropped to fit) with its transparency.
    Raises ValueError, saying what's wrong, for an image that can't be one."""
    tmp, raw = out.with_suffix(".tmp.png"), out.with_suffix(".rgba")
    size = f"{WIDTH}:{HEIGHT}"
    try:
        ffmpeg.run(["-i", str(upload), "-vf", f"scale={size}:force_original_aspect_ratio=increase,crop={size},format=rgba",
                    "-frames:v", "1", "-update", "1", str(tmp)])
        ffmpeg.run(["-i", str(tmp), "-f", "rawvideo", "-pix_fmt", "rgba", str(raw)])
        alpha = np.fromfile(raw, dtype=np.uint8).reshape(HEIGHT, WIDTH, 4)[:, :, 3]
    except ffmpeg.FFmpegError:
        tmp.unlink(missing_ok=True)
        raise ValueError("That file isn't an image the app can read. Use a PNG.") from None
    finally:
        raw.unlink(missing_ok=True)
    if alpha.min() > SOLID:
        tmp.unlink(missing_ok=True)
        raise ValueError("The image has no transparent parts, so it would cover the whole video. Use a PNG with a "
                         "transparent background (a JPG can't have one).")
    if not alpha.any():
        tmp.unlink(missing_ok=True)
        raise ValueError("The image is completely transparent.")
    tmp.replace(out)
    return Overlay(out, design_box(alpha), find_screen(alpha))


def find_screen(alpha: np.ndarray) -> tuple[int, int, int, int] | None:
    """A frame's screen: the clear area around the middle, when the design closes it in on all four sides."""
    height, width = alpha.shape
    cy, cx = height // 2, width // 2
    if alpha[cy, cx] > SOLID:
        return None
    row, col = alpha[cy] > SOLID, alpha[:, cx] > SOLID
    left, right, up, down = np.flatnonzero(row[:cx]), np.flatnonzero(row[cx:]), np.flatnonzero(col[:cy]), np.flatnonzero(col[cy:])
    if not (len(left) and len(right) and len(up) and len(down)):
        return None  # open on a side: a logo, not a frame
    x0, x1, y0, y1 = left[-1] + 1, cx + right[0], up[-1] + 1, cy + down[0]
    if x1 - x0 < width * MIN_SCREEN or y1 - y0 < height * MIN_SCREEN:
        return None
    return _even(x0, y0, x1 - x0, y1 - y0)


def design_box(alpha: np.ndarray) -> tuple[int, int, int, int]:
    """The smallest area holding everything visible, so only that part is laid over each frame."""
    ys, xs = np.nonzero(alpha)
    x0, y0 = int(xs.min()) // 2 * 2, int(ys.min()) // 2 * 2
    x1, y1 = min(alpha.shape[1], -(-(int(xs.max()) + 1) // 2) * 2), min(alpha.shape[0], -(-(int(ys.max()) + 1) // 2) * 2)
    return x0, y0, x1 - x0, y1 - y0


def _even(x, y, w, h) -> tuple[int, int, int, int]:
    """Inside the given area, with even numbers: video sizes and positions need them."""
    x2, y2 = -(-int(x) // 2) * 2, -(-int(y) // 2) * 2
    return x2, y2, (int(x) + int(w) - x2) // 2 * 2, (int(y) + int(h) - y2) // 2 * 2


def filter_graph(overlay: Overlay) -> str:
    """ffmpeg filter graph from the clip ([0:v]) and the overlay image ([1:v]) to the 1920x1080 picture [v]."""
    if overlay.screen:
        x, y, w, h = overlay.screen
        # The clip fills the screen (black around it if it isn't 16:9), and the frame goes over it, which also
        # covers its corners where a screen is rounded.
        return (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:{x}+({w}-iw)/2:{y}+({h}-ih)/2:black,setsar=1[clip];"
                "[clip][1:v]overlay=0:0[v]")
    x, y, w, h = overlay.box
    return (f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[clip];"
            f"[1:v]crop={w}:{h}:{x}:{y}[design];[clip][design]overlay={x}:{y}[v]")
