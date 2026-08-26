#!/usr/bin/env python3
import json
import math
import os

from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPLASH_DIR = os.path.join(BASE_DIR, "splash_frames")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

CANVAS = 700
CENTER = CANVAS // 2
RADIUS = 220
DOT_COUNT = 10
DOT_SIZE_MAX = 22
DOT_SIZE_MIN = 12

DEFAULT_BG = (10, 10, 11)
DEFAULT_DOT = (91, 140, 255)


def hex_to_rgb(hex_color, default):
    try:
        hex_color = hex_color.lstrip("#")
        return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


def load_theme():
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except Exception:
        config = {}

    bg = hex_to_rgb(config.get("band_background_color"), DEFAULT_BG)
    dot = hex_to_rgb(config.get("text_color") or config.get("accent_color"), DEFAULT_DOT)
    return bg, dot


def clear_old_frames():
    if not os.path.isdir(SPLASH_DIR):
        return
    for name in os.listdir(SPLASH_DIR):
        if name.startswith("frame_") and name.endswith(".png"):
            os.remove(os.path.join(SPLASH_DIR, name))


def make_frame(active_index, bg, dot_color):
    img = Image.new("RGB", (CANVAS, CANVAS), bg)
    draw = ImageDraw.Draw(img)

    for i in range(DOT_COUNT):
        angle = (2 * math.pi / DOT_COUNT) * i - math.pi / 2
        x = CENTER + RADIUS * math.cos(angle)
        y = CENTER + RADIUS * math.sin(angle)

        offset = (i - active_index) % DOT_COUNT
        fade = max(0.2, 1 - offset / (DOT_COUNT * 0.55))
        size = DOT_SIZE_MIN + (DOT_SIZE_MAX - DOT_SIZE_MIN) * fade
        color = tuple(int(bg[c] + (dot_color[c] - bg[c]) * fade) for c in range(3))

        draw.ellipse([x - size / 2, y - size / 2, x + size / 2, y + size / 2], fill=color)

    return img


def main():
    os.makedirs(SPLASH_DIR, exist_ok=True)
    clear_old_frames()

    bg, dot_color = load_theme()

    for i in range(DOT_COUNT):
        frame = make_frame(i, bg, dot_color)
        frame.save(os.path.join(SPLASH_DIR, f"frame_{i:02d}.png"))

    print(f"Generated {DOT_COUNT} spinner frames in {SPLASH_DIR}")


if __name__ == "__main__":
    main()
