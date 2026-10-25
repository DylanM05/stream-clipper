"""Show overlays: telling a frame from a logo, saving uploads, and placing the clip."""

import shutil
import subprocess

import numpy as np
import pytest

from clipper.overlay import Overlay, design_box, filter_graph, find_screen, save


def frame_alpha(screen=(160, 110, 1600, 900)) -> np.ndarray:
    alpha = np.full((1080, 1920), 255, np.uint8)
    x, y, w, h = screen
    alpha[y : y + h, x : x + w] = 0
    return alpha


def test_a_frame_closed_on_all_sides_has_its_screen_found():
    assert find_screen(frame_alpha()) == (160, 110, 1600, 900)
    glare = frame_alpha()
    glare[300:400, 300:700] = 90  # see-through glare on the screen doesn't count as the frame
    assert find_screen(glare) == (160, 110, 1600, 900)
    assert find_screen(frame_alpha((161, 111, 1597, 899))) == (162, 112, 1596, 898)  # even, and inside


def test_a_logo_or_a_frame_open_on_one_side_has_no_screen():
    logo = np.zeros((1080, 1920), np.uint8)
    logo[40:180, 1440:1880] = 255
    assert find_screen(logo) is None
    assert design_box(logo) == (1440, 40, 440, 140)

    open_bottom = frame_alpha()
    open_bottom[1010:, :] = 0
    assert find_screen(open_bottom) is None
    assert find_screen(frame_alpha((900, 500, 120, 80))) is None  # a peephole, not a screen


def test_the_clip_fits_a_frames_screen_and_a_logo_goes_over_the_full_clip(tmp_path):
    framed = filter_graph(Overlay(tmp_path / "o.png", (0, 0, 1920, 1080), (160, 110, 1600, 900)))
    assert "scale=1600:900:force_original_aspect_ratio=decrease" in framed and "pad=1920:1080:160+(1600-iw)/2" in framed
    logo = filter_graph(Overlay(tmp_path / "o.png", (1440, 40, 440, 140), None))
    assert "[1:v]crop=440:140:1440:40[design];[clip][design]overlay=1440:40[v]" in logo


def png(tmp_path, rgba: np.ndarray, name: str):
    raw = tmp_path / f"{name}.raw"
    rgba.tofile(raw)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba",
                    "-s", f"{rgba.shape[1]}x{rgba.shape[0]}", "-i", str(raw), "-frames:v", "1", str(tmp_path / name)], check=True)
    return tmp_path / name


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_uploads_are_saved_at_1920x1080_and_need_transparency(tmp_path):
    small_frame = np.zeros((540, 960, 4), np.uint8)
    small_frame[:, :, 3] = 255
    small_frame[55:505, 80:880, 3] = 0  # half size: scaled up to 1920x1080
    overlay = save(png(tmp_path, small_frame, "tv.png"), tmp_path / "overlay.png")
    assert overlay.screen == (160, 110, 1600, 900) and overlay.path.exists()

    with pytest.raises(ValueError, match="no transparent parts"):
        save(png(tmp_path, np.full((1080, 1920, 4), 255, np.uint8), "solid.png"), tmp_path / "solid-overlay.png")
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="isn't an image"):
        save(junk, tmp_path / "junk-overlay.png")
