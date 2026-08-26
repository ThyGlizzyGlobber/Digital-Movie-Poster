import json
import os
import subprocess
import time
from datetime import date, datetime, time as dtime

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POSTER_DIR = os.path.join(BASE_DIR, "static", "posters")
PREPARED_DIR = os.path.join(BASE_DIR, "prepared")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "slideshow.log")

DEFAULT_INTERVAL = 900  # 15 minutes
POLL_SECONDS = 3
DEFAULT_ACCENT_RGB = (91, 140, 255)

ROTATION_MAP = {
    90: Image.ROTATE_90,
    180: Image.ROTATE_180,
    270: Image.ROTATE_270,
}

FONT_PATHS_BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
FONT_PATHS_MONO = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]

os.makedirs(PREPARED_DIR, exist_ok=True)


def log(message):
    with open(LOG_PATH, "a") as f:
        f.write(message + "\n")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "order": [], "interval_seconds": DEFAULT_INTERVAL, "rotation_degrees": 0,
            "brightness": 1.0, "contrast": 1.0, "saturation": 1.0,
            "poster_meta": {}, "display_width": 1080, "display_height": 1920,
            "text_color": "#5b8cff", "accent_color": "#5b8cff", "poster_position": "center",
            "top_band_content": "status", "bottom_band_content": "date",
            "top_custom_text": "", "bottom_custom_text": "", "text_size_pct": 100,
            "display_font": {}, "band_background_color": "#0a0a0b",
        }


def hex_to_rgb(hex_color):
    hex_color = (hex_color or "#5b8cff").lstrip("#")
    if len(hex_color) != 6:
        return DEFAULT_ACCENT_RGB
    try:
        return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return DEFAULT_ACCENT_RGB


def load_font(paths, size):
    for p in paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def get_font(font_info, size, bold):
    if font_info and font_info.get("path") and os.path.exists(font_info["path"]):
        try:
            if font_info.get("is_variable"):
                font = ImageFont.truetype(font_info["path"], size)
                axes = font.get_variation_axes()
                if axes:
                    min_w, max_w = axes[0]["minimum"], axes[0]["maximum"]
                    target = max(min_w, min(max_w, 700 if bold else 400))
                    font.set_variation_by_axes([target])
                return font

            bold_path = font_info.get("bold_path")
            path = bold_path if (bold and bold_path and os.path.exists(bold_path)) else font_info["path"]
            return ImageFont.truetype(path, size)
        except Exception:
            pass

    return load_font(FONT_PATHS_BOLD if bold else FONT_PATHS_MONO, size)


def clear_prepared_dir():
    for name in os.listdir(PREPARED_DIR):
        path = os.path.join(PREPARED_DIR, name)
        if os.path.isfile(path):
            os.remove(path)


def compute_status_text(meta):
    if not meta or not meta.get("release_date"):
        return None
    try:
        release_date = datetime.strptime(meta["release_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return "NOW SHOWING" if release_date <= date.today() else "UPCOMING"


def compute_date_text(meta):
    if not meta or not meta.get("release_date"):
        return None
    try:
        release_date = datetime.strptime(meta["release_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return release_date.strftime("%b %d").upper()


def resolve_band_text(content_type, meta, custom_text):
    if content_type == "custom":
        return custom_text.strip() if custom_text and custom_text.strip() else None
    if content_type == "status":
        return compute_status_text(meta)
    if content_type == "date":
        return compute_date_text(meta)
    if content_type == "now_playing":
        return "NOW PLAYING"
    return None


def is_bold_content(content_type):
    return content_type in ("status", "now_playing")


def draw_centered_text(draw, canvas_w, y_center, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    draw.text(((canvas_w - w) // 2, y_center - h // 2 - bbox[1]), text, font=font, fill=fill)


def fit_text_font(draw, canvas_w, text, font_info, base_size, bold, max_width_ratio=0.92):
    size = base_size
    min_size = 10
    while size > min_size:
        font = get_font(font_info, size, bold)
        bbox = draw.textbbox((0, 0), text, font=font)
        width = bbox[2] - bbox[0]
        if width <= canvas_w * max_width_ratio:
            return font
        size -= 2
    return get_font(font_info, min_size, bold)


def build_composited_poster(source_path, meta, canvas_w, canvas_h, text_rgb, band_bg_rgb,
                             position, top_content, bottom_content,
                             top_custom_text, bottom_custom_text, text_size_pct, font_info):
    poster = Image.open(source_path).convert("RGB")
    poster_w, poster_h = poster.size

    scale = canvas_w / poster_w
    scaled_h = round(poster_h * scale)

    if scaled_h > canvas_h:
        scale = canvas_h / poster_h
        scaled_w = round(poster_w * scale)
        poster_resized = poster.resize((max(1, scaled_w), canvas_h), Image.LANCZOS)
        top_band, bottom_band = 0, 0
        paste_x = (canvas_w - poster_resized.width) // 2
        paste_y = 0
    else:
        poster_resized = poster.resize((canvas_w, max(1, scaled_h)), Image.LANCZOS)
        leftover = canvas_h - scaled_h

        if position == "top":
            top_band, bottom_band = 0, leftover
        elif position == "bottom":
            top_band, bottom_band = leftover, 0
        else:
            top_band = leftover // 2
            bottom_band = leftover - top_band

        paste_x = 0
        paste_y = top_band

    canvas = Image.new("RGB", (canvas_w, canvas_h), band_bg_rgb)
    canvas.paste(poster_resized, (paste_x, paste_y))

    draw = ImageDraw.Draw(canvas)
    scale_factor = max(0.5, min(2.0, text_size_pct / 100))

    top_text = resolve_band_text(top_content, meta, top_custom_text)
    if top_text and top_band > 12:
        bold = is_bold_content(top_content)
        base_size = max(14, min(72, int(top_band * 0.5 * scale_factor)))
        font = fit_text_font(draw, canvas_w, top_text, font_info, base_size, bold)
        draw_centered_text(draw, canvas_w, top_band // 2, top_text, font, text_rgb)

    bottom_text = resolve_band_text(bottom_content, meta, bottom_custom_text)
    if bottom_text and bottom_band > 12:
        bold = is_bold_content(bottom_content)
        base_size = max(12, min(56, int(bottom_band * 0.4 * scale_factor)))
        font = fit_text_font(draw, canvas_w, bottom_text, font_info, base_size, bold)
        draw_centered_text(draw, canvas_w, canvas_h - bottom_band // 2, bottom_text, font, text_rgb)

    return canvas


def build_display_list(order, poster_meta, rotation_degrees, brightness, contrast,
                        saturation, display_w, display_h, text_rgb, band_bg_rgb, position,
                        top_content, bottom_content, top_custom_text, bottom_custom_text,
                        text_size_pct, font_info):
    clear_prepared_dir()
    paths = []

    for filename in order:
        source_path = os.path.join(POSTER_DIR, filename)
        if not os.path.exists(source_path):
            continue

        meta = poster_meta.get(filename)
        canvas = build_composited_poster(
            source_path, meta, display_w, display_h, text_rgb, band_bg_rgb,
            position, top_content, bottom_content, top_custom_text,
            bottom_custom_text, text_size_pct, font_info,
        )

        transpose_method = ROTATION_MAP.get(rotation_degrees)
        if transpose_method is not None:
            canvas = canvas.transpose(transpose_method)

        if brightness != 1.0:
            canvas = ImageEnhance.Brightness(canvas).enhance(brightness)
        if contrast != 1.0:
            canvas = ImageEnhance.Contrast(canvas).enhance(contrast)
        if saturation != 1.0:
            canvas = ImageEnhance.Color(canvas).enhance(saturation)

        out_path = os.path.join(PREPARED_DIR, filename)
        canvas.save(out_path)
        paths.append(out_path)

    return paths


def start_fbi(paths, interval_seconds):
    return subprocess.Popen([
        "fbi",
        "-a",
        "-T", "1",
        "-t", str(max(1, int(round(interval_seconds)))),
        "-noverbose",
        *paths,
    ])


def parse_hhmm(value, fallback):
    try:
        hours, minutes = str(value).split(":")
        return dtime(int(hours), int(minutes))
    except Exception:
        return fallback


def display_should_be_on(config, now_time):
    """Whether the panel should be lit right now. Handles an off-window
    that crosses midnight (the normal case, e.g. 23:00 -> 07:00)."""
    if not config.get("schedule_enabled", False):
        return True

    off_t = parse_hhmm(config.get("display_off_time"), dtime(23, 0))
    on_t = parse_hhmm(config.get("display_on_time"), dtime(7, 0))

    if off_t == on_t:
        return True

    if off_t < on_t:
        return not (off_t <= now_time < on_t)

    return on_t <= now_time < off_t


def blank_framebuffer():
    """Paint the whole framebuffer black.

    This is the only part of "display off" that's guaranteed to work. It
    also fixes a side effect: when fbi exits it restores whatever was in
    the buffer beforehand, which is why the boot spinner reappeared when
    the schedule kicked in."""
    try:
        with open("/sys/class/graphics/fb0/virtual_size") as f:
            width, height = (int(v) for v in f.read().strip().split(","))
        with open("/sys/class/graphics/fb0/bits_per_pixel") as f:
            bpp = int(f.read().strip())

        stride_path = "/sys/class/graphics/fb0/stride"
        if os.path.exists(stride_path):
            with open(stride_path) as f:
                stride = int(f.read().strip())
        else:
            stride = width * (bpp // 8)

        with open("/dev/fb0", "r+b", buffering=0) as fb:
            row = b"\x00" * stride
            for _ in range(height):
                fb.write(row)
        return True
    except Exception as e:
        log(f"Could not blank framebuffer: {e}")
        return False


def set_display_power(on):
    """Best-effort panel power control.

    Note vcgencmd reports success even when the KMS/DRM driver ignores it
    entirely, so its result is not trustworthy - we try every method and
    rely on the black framebuffer as the guaranteed floor. Whether the
    panel truly sleeps depends on the graphics driver in use (the legacy
    fkms driver honours vcgencmd; full kms does not)."""
    if not on:
        blank_framebuffer()

    try:
        subprocess.run(
            ["vcgencmd", "display_power", "1" if on else "0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
    except Exception:
        pass

    try:
        with open("/sys/class/graphics/fb0/blank", "w") as f:
            f.write("0" if on else "1")
    except Exception:
        pass

    return True


def maybe_scheduled_reboot(config, now):
    """Reboots at the configured time, at most once per day. Note this is
    general housekeeping - it does NOT preserve the panel, since the
    display stays lit through a reboot."""
    if not config.get("reboot_enabled", False):
        return

    reboot_t = parse_hhmm(config.get("reboot_time"), dtime(4, 30))
    now_t = now.time()

    if not (now_t.hour == reboot_t.hour and now_t.minute == reboot_t.minute):
        return

    stamp_path = os.path.join(BASE_DIR, ".last_reboot")
    today = now.date().isoformat()

    try:
        with open(stamp_path) as f:
            if f.read().strip() == today:
                return
    except FileNotFoundError:
        pass

    try:
        with open(stamp_path, "w") as f:
            f.write(today)
    except Exception:
        pass

    log(f"Scheduled reboot at {now_t.strftime('%H:%M')}")
    subprocess.run(["reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_spinner():
    """Stops the boot spinner service. Deliberately called only once the
    replacement image is ready to draw - the spinner's last frame stays
    on the framebuffer until fbi paints over it, so there's no black gap
    in between."""
    subprocess.run(
        ["systemctl", "stop", "posterframe-spinner.service"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main():
    current_process = None
    applied_signature = None
    spinner_stopped = False
    display_on = True
    force_rebuild = False

    try:
        while True:
            config = load_config()

            now = datetime.now()
            maybe_scheduled_reboot(config, now)

            if not display_should_be_on(config, now.time()):
                if display_on:
                    log("Scheduled off period - turning display off")
                    if current_process:
                        current_process.terminate()
                        current_process.wait()
                        current_process = None
                    # Forget what was applied so waking triggers a fresh
                    # rebuild rather than assuming the screen still holds it
                    applied_signature = None
                    # The spinner is a separate process that redraws itself
                    # in a loop; if it's still running (e.g. the Pi booted
                    # straight into an off-window, before any rebuild ever
                    # stopped it) it will keep painting over the blanked
                    # screen forever. Stop it here too, not just after a
                    # successful on-period rebuild.
                    if not spinner_stopped:
                        stop_spinner()
                        spinner_stopped = True
                    set_display_power(False)
                    display_on = False
                time.sleep(POLL_SECONDS)
                continue

            if not display_on:
                log("Scheduled on period - waking display, forcing rebuild")
                set_display_power(True)
                display_on = True
                # Don't rely solely on the signature diff below to notice
                # the wake - force the next rebuild explicitly so a stale
                # fbi/spinner frame never gets left on screen.
                force_rebuild = True

            order = config.get("order", [])
            interval = max(5, config.get("interval_seconds", DEFAULT_INTERVAL))
            rotation = config.get("rotation_degrees", 0)
            brightness = config.get("brightness", 1.0)
            contrast = config.get("contrast", 1.0)
            saturation = config.get("saturation", 1.0)
            poster_meta = config.get("poster_meta", {})
            display_w = config.get("display_width", 1080)
            display_h = config.get("display_height", 1920)
            text_rgb = hex_to_rgb(config.get("text_color") or config.get("accent_color"))
            band_bg_rgb = hex_to_rgb(config.get("band_background_color") or "#0a0a0b")
            position = config.get("poster_position", "center")
            top_content = config.get("top_band_content", "status")
            bottom_content = config.get("bottom_band_content", "date")
            top_custom_text = config.get("top_custom_text", "")
            bottom_custom_text = config.get("bottom_custom_text", "")
            text_size_pct = config.get("text_size_pct", 100)
            font_info = config.get("display_font", {})

            # Plex "now playing" override: plex_monitor.py drives this via
            # config.json, same as everything else here - this script just
            # renders whatever it finds. Swap in the single override poster
            # and force whichever band is assigned to "NOW PLAYING",
            # leaving the other band's normal content untouched so e.g. a
            # custom top band and a Plex-driven bottom band can coexist.
            plex_state = config.get("plex_now_playing", {})
            plex_filename = plex_state.get("filename")
            if plex_state.get("active") and plex_filename:
                order = [plex_filename]
                plex_band = config.get("plex_band", "bottom")
                if plex_band == "top":
                    top_content = "now_playing"
                elif plex_band == "bottom":
                    bottom_content = "now_playing"

            order = [f for f in order if os.path.exists(os.path.join(POSTER_DIR, f))]

            poster_fingerprint = tuple(
                (f, os.path.getmtime(os.path.join(POSTER_DIR, f))) for f in order
            )

            signature = (
                poster_fingerprint, interval, rotation, brightness, contrast, saturation,
                display_w, display_h, text_rgb, band_bg_rgb, position, top_content, bottom_content,
                top_custom_text, bottom_custom_text, text_size_pct,
                font_info.get("path"), font_info.get("is_variable"),
                tuple(sorted((k, v.get("release_date")) for k, v in poster_meta.items())),
            )

            process_died = current_process is not None and current_process.poll() is not None

            if not order:
                if current_process:
                    current_process.terminate()
                    current_process.wait()
                    current_process = None
                applied_signature = None
                log("No posters to show yet, waiting...")
                time.sleep(POLL_SECONDS)
                continue

            if signature != applied_signature or process_died or force_rebuild:
                log(f"Rebuilding display: {len(order)} poster(s), {interval}s each")

                paths = build_display_list(
                    order, poster_meta, rotation, brightness, contrast, saturation,
                    display_w, display_h, text_rgb, band_bg_rgb, position, top_content, bottom_content,
                    top_custom_text, bottom_custom_text, text_size_pct, font_info,
                )

                old_process = current_process
                if old_process:
                    old_process.terminate()

                current_process = start_fbi(paths, interval)

                if old_process:
                    old_process.wait()

                if not spinner_stopped:
                    stop_spinner()
                    spinner_stopped = True

                applied_signature = signature
                force_rebuild = False

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        pass
    finally:
        if current_process:
            current_process.terminate()


if __name__ == "__main__":
    main()
