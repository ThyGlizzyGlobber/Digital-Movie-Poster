#!/usr/bin/env python3
"""
Boot splash + animated loading spinner, drawn directly to the Linux
framebuffer.

Why not fbi: fbi's -t (slideshow delay) is parsed as whole seconds, so
any sub-second value becomes 0, which fbi treats as "no slideshow" - it
shows the first frame and stops. That's why the fbi-based spinner was
always static.

All sizing/timing is read from config.json so it's controlled from the
web UI rather than by editing this file.
"""
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
BOOT_IMAGE_PATH = os.path.join(BASE_DIR, "static", "boot_image.png")

FB_DEV = "/dev/fb0"
FB_SYS = "/sys/class/graphics/fb0"

DOT_COUNT = 12
MAX_WIDTH_RATIO = 0.92   # keep a wide logo from touching the screen edges

# The spinner itself is deliberately radially symmetric, so it needs no
# rotation handling - only the logo does.
ROTATION_MAP = {
    90: Image.ROTATE_90,
    180: Image.ROTATE_180,
    270: Image.ROTATE_270,
}

DEFAULTS = {
    "boot_image_seconds": 3.0,
    "boot_image_height_pct": 25,
    "boot_image_rotation": 0,
    "spinner_height_pct": 17,
    "spinner_fps": 8,
}


def load_settings():
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except Exception:
        config = {}

    settings = {}
    for key, fallback in DEFAULTS.items():
        value = config.get(key, fallback)
        try:
            settings[key] = float(value)
        except (TypeError, ValueError):
            settings[key] = fallback
    return settings


def read_fb_info():
    with open(os.path.join(FB_SYS, "virtual_size")) as f:
        width, height = (int(v) for v in f.read().strip().split(","))

    with open(os.path.join(FB_SYS, "bits_per_pixel")) as f:
        bpp = int(f.read().strip())

    stride_path = os.path.join(FB_SYS, "stride")
    if os.path.exists(stride_path):
        with open(stride_path) as f:
            stride = int(f.read().strip())
    else:
        stride = width * (bpp // 8)

    return width, height, bpp, stride


def to_fb_bytes(img, bpp):
    arr = np.array(img.convert("RGB"))

    if bpp == 16:
        r = (arr[:, :, 0] >> 3).astype(np.uint16)
        g = (arr[:, :, 1] >> 2).astype(np.uint16)
        b = (arr[:, :, 2] >> 3).astype(np.uint16)
        return ((r << 11) | (g << 5) | b).astype("<u2")

    if bpp == 32:
        h, w, _ = arr.shape
        out = np.zeros((h, w, 4), dtype=np.uint8)
        out[:, :, 0] = arr[:, :, 2]
        out[:, :, 1] = arr[:, :, 1]
        out[:, :, 2] = arr[:, :, 0]
        out[:, :, 3] = 255
        return out

    raise RuntimeError(f"Unsupported framebuffer depth: {bpp}bpp")


def render_boot_image(width, height, height_pct, rotation=0):
    """Full-screen frame with the splash PNG centred on black, sized to a
    fraction of screen height. Returns None if there's no usable image."""
    if not os.path.exists(BOOT_IMAGE_PATH):
        return None

    try:
        logo = Image.open(BOOT_IMAGE_PATH)
        logo.load()
    except Exception as e:
        print(f"Could not read {BOOT_IMAGE_PATH}: {e}", file=sys.stderr)
        return None

    # Rotate before scaling, so "% of screen height" always describes the
    # final on-screen result rather than the pre-rotation orientation.
    transpose_method = ROTATION_MAP.get(int(rotation))
    if transpose_method is not None:
        logo = logo.transpose(transpose_method)

    target_h = height * (height_pct / 100.0)
    scale = target_h / logo.height

    # A wide logo at a given height could still overflow the screen
    # width, so clamp that case rather than cropping it.
    if logo.width * scale > width * MAX_WIDTH_RATIO:
        scale = (width * MAX_WIDTH_RATIO) / logo.width

    target_w = max(1, round(logo.width * scale))
    target_h = max(1, round(logo.height * scale))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    pos = ((width - target_w) // 2, (height - target_h) // 2)

    if logo.mode in ("RGBA", "LA", "P"):
        logo = logo.convert("RGBA")
        canvas.paste(logo, pos, logo)
    else:
        canvas.paste(logo.convert("RGB"), pos)

    return canvas


def write_full_screen(fb, img, bpp, stride, width, height):
    data = to_fb_bytes(img, bpp).tobytes()
    row_bytes = width * (bpp // 8)
    for row in range(height):
        fb.seek(row * stride)
        fb.write(data[row * row_bytes:(row + 1) * row_bytes])


def make_frame(active_index, diameter):
    img = Image.new("RGB", (diameter, diameter), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    center = diameter / 2
    ring_radius = diameter * 0.5 * 0.78
    dot_max = diameter * 0.5 * 0.13

    for i in range(DOT_COUNT):
        angle = (2 * math.pi / DOT_COUNT) * i - math.pi / 2
        x = center + ring_radius * math.cos(angle)
        y = center + ring_radius * math.sin(angle)

        offset = (active_index - i) % DOT_COUNT
        fade = max(0.15, 1 - offset / (DOT_COUNT * 0.75))
        size = dot_max * (0.55 + 0.45 * fade)
        value = int(255 * fade)

        draw.ellipse([x - size, y - size, x + size, y + size],
                     fill=(value, value, value))

    return img


def main():
    settings = load_settings()

    try:
        width, height, bpp, stride = read_fb_info()
    except Exception as e:
        print(f"Could not read framebuffer info: {e}", file=sys.stderr)
        return 1

    bytes_per_pixel = bpp // 8

    diameter = max(32, int(height * (settings["spinner_height_pct"] / 100.0)))
    diameter = min(diameter, width, height)

    origin_x = (width - diameter) // 2
    origin_y = (height - diameter) // 2

    fps = max(1.0, min(30.0, settings["spinner_fps"]))

    frames = []
    for i in range(DOT_COUNT):
        frames.append(to_fb_bytes(make_frame(i, diameter), bpp).tobytes())

    row_bytes = diameter * bytes_per_pixel

    try:
        fb = open(FB_DEV, "r+b", buffering=0)
    except Exception as e:
        print(f"Could not open {FB_DEV}: {e}", file=sys.stderr)
        return 1

    try:
        boot_image = render_boot_image(
            width, height,
            settings["boot_image_height_pct"],
            settings["boot_image_rotation"],
        )
        if boot_image is not None:
            write_full_screen(fb, boot_image, bpp, stride, width, height)
            time.sleep(max(0.0, settings["boot_image_seconds"]))

        blank_row = b"\x00" * stride
        fb.seek(0)
        for _ in range(height):
            fb.write(blank_row)

        frame_index = 0
        delay = 1.0 / fps

        while True:
            data = frames[frame_index % DOT_COUNT]

            for row in range(diameter):
                fb.seek((origin_y + row) * stride + origin_x * bytes_per_pixel)
                fb.write(data[row * row_bytes:(row + 1) * row_bytes])

            frame_index += 1
            time.sleep(delay)

    except KeyboardInterrupt:
        pass
    finally:
        fb.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
