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

# The Framed appearance's billing block always uses this bundled file - not
# the user-customizable Google Font mechanism display_font/top_font use -
# since matching the real industry-standard billing block typeface is the
# entire point of that block.
BILLING_FONT_PATH = os.path.join(BASE_DIR, "fonts", "univers_39_thin_ultra_condensed.otf")
_billing_font_cache = {}

os.makedirs(PREPARED_DIR, exist_ok=True)


def log(message):
    with open(LOG_PATH, "a") as f:
        f.write(message + "\n")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "order": [], "interval_seconds": DEFAULT_INTERVAL, "rotation_degrees": 0,
            "brightness": 1.0, "contrast": 1.0, "saturation": 1.0,
            "poster_meta": {}, "display_width": 1080, "display_height": 1920,
            "text_color": "#5b8cff", "accent_color": "#5b8cff", "poster_position": "center",
            "top_band_content": "status", "bottom_band_content": "date",
            "top_custom_text": "", "bottom_custom_text": "", "text_size_pct": 100,
            "display_font": {}, "band_background_color": "#0a0a0b",
        }
    except json.JSONDecodeError as e:
        # Falling back silently here once meant a corrupt config.json looked
        # identical to "no posters configured yet" - nothing in the log to
        # tell them apart. Log it, so a future config.json problem is
        # diagnosable instead of just an inexplicably stuck spinner.
        log(f"config.json is invalid JSON, falling back to defaults: {e}")
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


def get_billing_font(size):
    if size not in _billing_font_cache:
        if os.path.exists(BILLING_FONT_PATH):
            _billing_font_cache[size] = ImageFont.truetype(BILLING_FONT_PATH, size)
        else:
            _billing_font_cache[size] = load_font(FONT_PATHS_MONO, size)
    return _billing_font_cache[size]


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
    # Sized from canvas_h, not from the band itself - the band is a side
    # effect of poster aspect ratio and position, not a proxy for how big
    # the user wants the text. Driving size off the band meant a normal
    # band's "natural" size already sat at fit_text_font's width ceiling by
    # ~120% on the slider, leaving 150-200% nowhere to go. Clamping to the
    # band is still needed so text can't overflow a genuinely tiny one.
    top_text = resolve_band_text(top_content, meta, top_custom_text)
    if top_text and top_band > 12:
        bold = is_bold_content(top_content)
        target_size = int(canvas_h * 0.045 * scale_factor)
        base_size = max(14, min(target_size, int(top_band * 0.9)))
        font = fit_text_font(draw, canvas_w, top_text, font_info, base_size, bold)
        draw_centered_text(draw, canvas_w, top_band // 2, top_text, font, text_rgb)

    bottom_text = resolve_band_text(bottom_content, meta, bottom_custom_text)
    if bottom_text and bottom_band > 12:
        bold = is_bold_content(bottom_content)
        target_size = int(canvas_h * 0.035 * scale_factor)
        base_size = max(12, min(target_size, int(bottom_band * 0.9)))
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


def build_billing_lines(credits, cast_count):
    """Role/value pairs for the Framed billing block, ordered the same way
    classic theatrical one-sheets stack them (cast first, director closest
    to the bottom edge). Any role the source data doesn't have is simply
    left out - this is the graceful-degradation the full billing block
    needs, since neither TMDb nor Plex reliably populates every field for
    every title."""
    credits = credits or {}
    lines = []
    cast = credits.get("cast") or []
    if cast:
        lines.append(("STARRING", ", ".join(cast[:cast_count]).upper()))
    if credits.get("composer"):
        lines.append(("MUSIC BY", credits["composer"].upper()))
    if credits.get("producers"):
        lines.append(("PRODUCED BY", " & ".join(credits["producers"]).upper()))
    if credits.get("writers"):
        lines.append(("WRITTEN BY", " & ".join(credits["writers"]).upper()))
    if credits.get("director"):
        lines.append(("DIRECTED BY", credits["director"].upper()))
    return lines


def billing_fonts(scale):
    role_font = get_billing_font(max(10, int(14 * scale)))
    name_font = get_billing_font(max(12, int(19 * scale)))
    line_gap = max(2, int(4 * scale))
    block_gap = max(6, int(10 * scale))
    return role_font, name_font, line_gap, block_gap


def draw_centered_line(draw, canvas_w, y, text, font, fill):
    """Like draw_centered_text, but for stacking lines top-to-bottom rather
    than centering one line in a fixed box - returns the line's ink height
    so the caller can advance y for the next line."""
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text(((canvas_w - w) // 2, y - bbox[1]), text, font=font, fill=fill)
    return bbox[3] - bbox[1]


def billing_block_height(draw, lines, scale):
    role_font, name_font, line_gap, block_gap = billing_fonts(scale)
    total = 0
    for role, value in lines:
        role_bbox = draw.textbbox((0, 0), role, font=role_font)
        value_bbox = draw.textbbox((0, 0), value, font=name_font)
        total += (role_bbox[3] - role_bbox[1]) + line_gap
        total += (value_bbox[3] - value_bbox[1]) + block_gap
    return total


def draw_billing_block(draw, canvas_w, top_y, lines, text_rgb, scale):
    role_font, name_font, line_gap, block_gap = billing_fonts(scale)
    y = top_y
    for role, value in lines:
        y += draw_centered_line(draw, canvas_w, y, role, role_font, text_rgb) + line_gap
        y += draw_centered_line(draw, canvas_w, y, value, name_font, text_rgb) + block_gap
    return y


def build_framed_poster(source_path, meta, canvas_w, canvas_h, text_rgb, bg_rgb, position,
                         poster_scale_pct, top_content, top_custom_text, top_font_info, cast_count):
    """The Framed appearance: poster scaled down and inset (not full-bleed),
    a small status line above it, and a full cast/crew billing block below
    it - modeled on classic theatrical one-sheets, unlike build_composited_
    poster's full-width poster + leftover-space bands."""
    poster = Image.open(source_path).convert("RGB")
    poster_w, poster_h = poster.size

    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_rgb)
    draw = ImageDraw.Draw(canvas)
    # Billing-block point sizes below were tuned by eye against a 1920px-tall
    # canvas - scale keeps them proportionally correct at other resolutions.
    scale = canvas_h / 1920

    credits = (meta or {}).get("credits") if meta else None
    billing_lines = build_billing_lines(credits, cast_count)
    billing_margin = int(24 * scale)
    billing_height = (
        billing_block_height(draw, billing_lines, scale) + billing_margin * 2
        if billing_lines else 0
    )

    top_text = resolve_band_text(top_content, meta, top_custom_text)
    top_zone_height = int(canvas_h * 0.07) if top_text else int(canvas_h * 0.03)

    # Poster is scaled to a fraction of canvas width, then capped further if
    # that would overflow the vertical space left after the two text zones.
    available_h = max(1, canvas_h - top_zone_height - billing_height)
    target_w = max(1, int(canvas_w * poster_scale_pct / 100))
    poster_scale = target_w / poster_w
    scaled_h = round(poster_h * poster_scale)
    if scaled_h > available_h:
        poster_scale = available_h / poster_h
        scaled_h = available_h
        target_w = round(poster_w * poster_scale)
    poster_resized = poster.resize((max(1, target_w), max(1, scaled_h)), Image.LANCZOS)

    leftover = available_h - scaled_h
    if position == "top":
        poster_y = top_zone_height
    elif position == "bottom":
        poster_y = top_zone_height + leftover
    else:
        poster_y = top_zone_height + leftover // 2
    poster_x = (canvas_w - poster_resized.width) // 2
    canvas.paste(poster_resized, (poster_x, poster_y))

    if top_text:
        bold = is_bold_content(top_content)
        base_size = max(14, int(top_zone_height * 0.45))
        font = fit_text_font(draw, canvas_w, top_text, top_font_info, base_size, bold)
        draw_centered_text(draw, canvas_w, top_zone_height // 2, top_text, font, text_rgb)

    if billing_lines:
        draw_billing_block(draw, canvas_w, canvas_h - billing_height + billing_margin,
                            billing_lines, text_rgb, scale)

    return canvas


def build_framed_display_list(order, poster_meta, rotation_degrees, brightness, contrast,
                               saturation, display_w, display_h, text_rgb, bg_rgb, position,
                               poster_scale_pct, top_content, top_custom_text, top_font_info,
                               cast_count):
    clear_prepared_dir()
    paths = []

    for filename in order:
        source_path = os.path.join(POSTER_DIR, filename)
        if not os.path.exists(source_path):
            continue

        meta = poster_meta.get(filename)
        canvas = build_framed_poster(
            source_path, meta, display_w, display_h, text_rgb, bg_rgb, position,
            poster_scale_pct, top_content, top_custom_text, top_font_info, cast_count,
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

            active_appearance = config.get("active_appearance", "classic")
            appearances = config.get("appearances", {})
            classic = appearances.get("classic", {})
            framed = appearances.get("framed", {})

            # Plex "now playing" override: plex_monitor.py drives this via
            # config.json, same as everything else here - this script just
            # renders whatever it finds. Swap in the single override poster
            # and force the appearance's status text to "NOW PLAYING".
            plex_state = config.get("plex_now_playing", {})
            plex_filename = plex_state.get("filename")
            # Trust the flag only as far as the file it points at actually
            # existing. plex_monitor.py's deactivate() clears the flag and
            # deletes the file via two separate HTTP calls - if the process
            # (or posterframe-web) gets interrupted between them, "active"
            # can be stuck true with nothing left to show. Falling back to
            # normal rotation here means a stale flag never blanks the
            # display outright; it just quietly stops overriding anything
            # until plex_monitor.py or a reconnect sorts the flag out.
            plex_active = bool(
                plex_state.get("active") and plex_filename
                and os.path.exists(os.path.join(POSTER_DIR, plex_filename))
            )
            if plex_active:
                order = [plex_filename]

            if active_appearance == "framed":
                text_rgb = hex_to_rgb(framed.get("text_color") or "#ffffff")
                band_bg_rgb = hex_to_rgb(framed.get("background_color") or "#000000")
                position = framed.get("poster_position", "center")
                poster_scale_pct = framed.get("poster_scale_pct", 78)
                top_content = "now_playing" if plex_active else framed.get("top_content", "status")
                top_custom_text = framed.get("top_custom_text", "")
                top_font_info = framed.get("top_font", {})
                cast_count = framed.get("cast_count", 4)
            else:
                text_rgb = hex_to_rgb(classic.get("text_color") or config.get("accent_color") or "#5b8cff")
                band_bg_rgb = hex_to_rgb(classic.get("band_background_color") or "#0a0a0b")
                position = classic.get("poster_position", "center")
                top_content = classic.get("top_band_content", "status")
                bottom_content = classic.get("bottom_band_content", "date")
                top_custom_text = classic.get("top_custom_text", "")
                bottom_custom_text = classic.get("bottom_custom_text", "")
                text_size_pct = classic.get("text_size_pct", 100)
                font_info = classic.get("display_font", {})
                if plex_active:
                    # Leaves the other band's normal content untouched so e.g.
                    # a custom top band and a Plex-driven bottom band coexist.
                    plex_band = classic.get("plex_band", "bottom")
                    if plex_band == "top":
                        top_content = "now_playing"
                    elif plex_band == "bottom":
                        bottom_content = "now_playing"

            order = [f for f in order if os.path.exists(os.path.join(POSTER_DIR, f))]

            poster_fingerprint = tuple(
                (f, os.path.getmtime(os.path.join(POSTER_DIR, f))) for f in order
            )
            # Includes title/credits, not just release_date, so a TMDb/Plex
            # metadata refresh (e.g. credits arriving after the poster itself)
            # triggers a rebuild too, not just a poster file changing.
            meta_fingerprint = tuple(sorted(
                (k, v.get("release_date"), v.get("title"), json.dumps(v.get("credits"), sort_keys=True))
                for k, v in poster_meta.items()
            ))

            if active_appearance == "framed":
                signature = (
                    "framed", poster_fingerprint, interval, rotation, brightness, contrast,
                    saturation, display_w, display_h, text_rgb, band_bg_rgb, position,
                    poster_scale_pct, top_content, top_custom_text,
                    top_font_info.get("path"), top_font_info.get("is_variable"),
                    cast_count, meta_fingerprint,
                )
            else:
                signature = (
                    "classic", poster_fingerprint, interval, rotation, brightness, contrast,
                    saturation, display_w, display_h, text_rgb, band_bg_rgb, position,
                    top_content, bottom_content, top_custom_text, bottom_custom_text,
                    text_size_pct, font_info.get("path"), font_info.get("is_variable"),
                    meta_fingerprint,
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

                if active_appearance == "framed":
                    paths = build_framed_display_list(
                        order, poster_meta, rotation, brightness, contrast, saturation,
                        display_w, display_h, text_rgb, band_bg_rgb, position,
                        poster_scale_pct, top_content, top_custom_text, top_font_info, cast_count,
                    )
                else:
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
