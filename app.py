import os
import re
import json
import colorsys
import fcntl
import glob
import subprocess
import tempfile
import threading
import uuid
from datetime import date, datetime, timedelta
from io import BytesIO
from urllib.parse import urlencode
import numpy as np
import requests
from flask import Flask, request, redirect, url_for, render_template, jsonify
from werkzeug.utils import secure_filename
from PIL import Image, ImageFilter, ImageEnhance

# fetch_posters.py/fetch_justwatch.py normally only ever talk to app.py over
# HTTP (as detached subprocesses) - never the other way around, since only
# this process can safely write config.json (save_config's flock). This is
# the one exception: importing their pure category-fetch/scrape/resolve
# helpers (read-only, no config writes) for the Discovery tab's live preview
# grid, rather than duplicating that logic a third time. Both modules have no
# import-time side effects beyond reading an env var, so this is safe.
import fetch_posters
import fetch_justwatch

app = Flask(__name__)


@app.template_filter("numfmt")
def numfmt(value):
    """Trim a numeric display value down to its shortest exact form: 5.0 -> 5,
    5.50 -> 5.5, but a genuine 5.5 is left alone. Several settings are stored
    as floats (old form submits, JS defaults, config.json edited by hand) even
    when the user only ever entered a whole number, which made the number
    inputs across tabs inconsistent - some showing "5", others "5.0" for the
    same kind of value."""
    if value is None or value == "":
        return value
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    if f == int(f):
        return str(int(f))
    return repr(f)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POSTER_DIR = os.path.join(BASE_DIR, "static", "posters")
ORIGINAL_DIR = os.path.join(BASE_DIR, "originals")
FONT_CACHE_DIR = os.path.join(BASE_DIR, "fonts_cache")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")
TMDB_SYNC_LOG = os.path.join(BASE_DIR, "tmdb_sync.log")
JUSTWATCH_SYNC_LOG = os.path.join(BASE_DIR, "justwatch_sync.log")
UPDATE_SCRIPT = os.path.join(BASE_DIR, "update.sh")
UPDATE_LOG = os.path.join(BASE_DIR, "update.log")
UPDATE_LOCK_FILE = os.path.join(BASE_DIR, ".update.lock")
SLIDESHOW_LOG = os.path.join(BASE_DIR, "slideshow.log")
PLEX_MONITOR_LOG = os.path.join(BASE_DIR, "plex_monitor.log")

# Whitelisted by name, not by path from the request - the Logs tab passes
# one of these keys, never a filename, so there's no path-traversal surface.
LOG_SOURCES = {
    "slideshow": ("Slideshow", SLIDESHOW_LOG),
    "plex": ("Plex monitor", PLEX_MONITOR_LOG),
    "tmdb": ("TMDb sync", TMDB_SYNC_LOG),
    "justwatch": ("JustWatch sync", JUSTWATCH_SYNC_LOG),
    "update": ("Update", UPDATE_LOG),
}
LOG_TAIL_BYTES = 20000
BOOT_IMAGE_PATH = os.path.join(BASE_DIR, "static", "boot_image.png")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}
PLEX_NOWPLAYING_FILENAME = "plex_nowplaying.jpg"
PLEX_PRODUCT = "Poster Frame"
PLEX_VERSION = "1.0"
PLEX_TV = "https://plex.tv"
DEFAULT_INTERVAL_SECONDS = 900  # 15 minutes
DEFAULT_ACCENT = "#5b8cff"
DEFAULT_BAND_BG = "#0a0a0b"
DEFAULT_GRAIN_INTENSITY = 0.08
DEFAULT_POSTER_MAX_WIDTH = 1600
EMPTY_FONT = {"family": "", "status": "", "path": None, "bold_path": None, "is_variable": False}

# Per-appearance settings, keyed by appearance name. Each appearance keeps its
# own independent copy of these so switching "active_appearance" in the UI
# never loses the other appearance's configuration. Fields NOT listed here
# (display_width, rotation_degrees, brightness/contrast/saturation, etc.)
# describe the physical screen/processing pipeline and stay global instead.
CLASSIC_DEFAULTS = {
    "poster_position": "center",
    "top_band_content": "status",
    "bottom_band_content": "date",
    "top_custom_text": "",
    "bottom_custom_text": "",
    "text_size_pct": 100,
    "band_background_color": DEFAULT_BAND_BG,
    "text_color": DEFAULT_ACCENT,
    "display_font": dict(EMPTY_FONT),
    "plex_band": "bottom",
}
FRAMED_DEFAULTS = {
    "poster_position": "center",
    "poster_scale_pct": 78,
    "top_content": "status",
    "top_custom_text": "",
    "top_font": dict(EMPTY_FONT),
    "background_color": "#000000",
    "text_color": "#ffffff",
    "cast_count": 4,
}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Commit title convention: "[v1.4.0] Fix schedule wake bug". update.sh only
# ever hands back the raw subject line (CURRENT_MSG/REMOTE_MSG) - parsing
# happens here so there's one implementation of the convention, not two.
# \d+(?:\.\d+)+ (not a fixed \d+\.\d+\.\d+) because the version scheme has
# grown a 4th segment over time (v1.0.6.5) - a fixed 3-part pattern stopped
# matching current tags entirely, silently falling back to the raw commit
# sha everywhere this is displayed.
VERSION_TAG_RE = re.compile(r"^\[v(\d+(?:\.\d+)+)\]\s*(.*)$", re.IGNORECASE)

# Must stay in step with SOURCES in fetch_posters.py
TMDB_SOURCE_KEYS = [
    "movie_trending", "movie_popular", "movie_now_playing", "movie_upcoming",
    "tv_trending", "tv_popular", "tv_on_the_air",
]

GITHUB_API = "https://api.github.com/repos/google/fonts/contents"
GITHUB_RAW = "https://raw.githubusercontent.com/google/fonts/main"
LICENSE_DIRS = ["ofl", "apache", "ufl"]

os.makedirs(POSTER_DIR, exist_ok=True)
os.makedirs(ORIGINAL_DIR, exist_ok=True)


def load_env_file(path):
    """Tiny .env parser - avoids adding python-dotenv as a dependency for
    the one file we need to read."""
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def save_env_file(path, env):
    """Full-file rewrite, same simplicity level as save_config() - callers
    load_env_file() first, mutate the dict, then pass it here."""
    with open(path, "w") as f:
        for key, value in env.items():
            f.write(f"{key}={value}\n")
    os.chmod(path, 0o600)


def get_plex_client_id(config):
    """A stable per-install identifier Plex needs on every request - not a
    secret, so it lives in config.json rather than .env."""
    client_id = config.get("plex_client_id")
    if not client_id:
        client_id = str(uuid.uuid4())
        config["plex_client_id"] = client_id
        save_config(config)
    return client_id


def plex_headers(client_id, token=None):
    headers = {
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Version": PLEX_VERSION,
        "Accept": "application/json",
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def plex_connection_reachable(uri, headers, timeout=4):
    """Plex's 'local' flag on a connection just means it detected the
    address on one of the server machine's own interfaces - it says nothing
    about whether *this* client can route to it. A server behind Docker/
    Kubernetes networking (e.g. a TrueNAS SCALE app without host networking)
    happily reports its internal pod/bridge IP as 'local'. Probing each
    candidate is the only way to know it actually works."""
    try:
        resp = requests.get(f"{uri}/identity", headers=headers, timeout=timeout)
        return resp.status_code < 500
    except requests.RequestException:
        return False


def parse_version_title(raw_message):
    """Splits a "[v1.4.0] Fix schedule wake bug" commit subject into
    (version, title). Falls back to (None, raw_message) for commits that
    don't follow the convention - old history, WIP commits, etc."""
    match = VERSION_TAG_RE.match(raw_message or "")
    if match:
        return match.group(1), match.group(2)
    return None, raw_message


def get_git_info():
    """Currently-running commit, for the System tab. Local only (no
    fetch/network) so page loads stay instant - the network-dependent
    "is an update available" check is a separate call the UI makes on
    demand, hitting update.sh --check."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=5,
        )
        message = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=5,
        )
        if sha.returncode != 0:
            return None, None, None
        version, title = parse_version_title(message.stdout.strip())
        return sha.stdout.strip(), title, version
    except Exception:
        return None, None, None


def parse_update_check_output(text):
    info = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            info[key.strip()] = value.strip()
    return info


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# --- Color helpers, used to derive a full palette from one user-picked accent ---

def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c))) for c in rgb))


def adjust_lightness(hex_color, factor):
    r, g, b = (c / 255 for c in hex_to_rgb(hex_color))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l * factor))
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return rgb_to_hex((r2 * 255, g2 * 255, b2 * 255))


def contrasting_text_color(hex_color):
    r, g, b = hex_to_rgb(hex_color)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#14100e" if luminance > 140 else "#f5f3f0"


def next_daily_sync(time_str):
    """Next occurrence of the user-configured discovery_sync_time
    (HH:MM). config.json is the actual source of truth for this now -
    fetch_discovery.py reads the same field to decide when to actually
    run - this just mirrors the same math for display in the UI."""
    try:
        hour, minute = (int(p) for p in time_str.split(":", 1))
    except (ValueError, AttributeError):
        hour, minute = 4, 0

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# --- Google Font fetching, used for the physical display's custom fonts ---

def slugify_font_name(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_font_files(family_name):
    slug = slugify_font_name(family_name)
    if not slug:
        return None, "not_found"

    saw_rate_limit = False

    for license_dir in LICENSE_DIRS:
        try:
            resp = requests.get(f"{GITHUB_API}/{license_dir}/{slug}", timeout=10)
        except requests.RequestException:
            return None, "error"

        if resp.status_code == 403:
            saw_rate_limit = True
            continue
        if resp.status_code != 200:
            continue

        try:
            items = resp.json()
        except ValueError:
            continue
        if not isinstance(items, list):
            continue

        result = {"variable": None, "regular": None, "bold": None,
                   "license_dir": license_dir, "slug": slug}

        for item in items:
            fname = item.get("name", "")
            if not fname.lower().endswith(".ttf"):
                continue
            lower = fname.lower()
            if "[wght]" in lower or "[opsz" in lower:
                result["variable"] = fname
            elif lower.endswith("-regular.ttf"):
                result["regular"] = fname
            elif lower.endswith("-bold.ttf"):
                result["bold"] = fname

        if not result["variable"] and not result["regular"]:
            try:
                static_resp = requests.get(f"{GITHUB_API}/{license_dir}/{slug}/static", timeout=10)
                if static_resp.status_code == 200:
                    for item in static_resp.json():
                        fname = item.get("name", "")
                        lower = fname.lower()
                        if lower.endswith("-regular.ttf"):
                            result["regular"] = f"static/{fname}"
                        elif lower.endswith("-bold.ttf"):
                            result["bold"] = f"static/{fname}"
            except requests.RequestException:
                pass

        if result["variable"] or result["regular"]:
            return result, "ok"

    if saw_rate_limit:
        return None, "rate_limited"

    return None, "not_found"


def download_font_file(license_dir, slug, filename, dest_path):
    url = f"{GITHUB_RAW}/{license_dir}/{slug}/{filename}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)


def fetch_and_cache_font(family_name):
    files, status = find_font_files(family_name)
    if not files:
        return None, status

    slug = files["slug"]
    license_dir = files["license_dir"]
    os.makedirs(FONT_CACHE_DIR, exist_ok=True)

    try:
        if files["variable"]:
            dest = os.path.join(FONT_CACHE_DIR, f"{slug}-variable.ttf")
            if not os.path.exists(dest):
                download_font_file(license_dir, slug, files["variable"], dest)
            return {"path": dest, "bold_path": None, "is_variable": True}, "ok"

        dest = os.path.join(FONT_CACHE_DIR, f"{slug}-regular.ttf")
        if not os.path.exists(dest):
            download_font_file(license_dir, slug, files["regular"], dest)

        bold_dest = None
        if files["bold"]:
            bold_dest = os.path.join(FONT_CACHE_DIR, f"{slug}-bold.ttf")
            if not os.path.exists(bold_dest):
                download_font_file(license_dir, slug, files["bold"], bold_dest)

        return {"path": dest, "bold_path": bold_dest, "is_variable": False}, "ok"
    except requests.RequestException:
        return None, "error"


# --- Config ---

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    else:
        config = {}

    defaults = {
        "order": [],
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "rotation_degrees": 0,
        "accent_color": DEFAULT_ACCENT,
        "brightness": 1.0,
        "contrast": 1.0,
        "saturation": 1.0,
        "poster_meta": {},
        "display_width": 1080,
        "display_height": 1920,
        "grain_intensity": DEFAULT_GRAIN_INTENSITY,
        "grain_enabled": True,
        "tmdb_enabled": True,
        "tmdb_schedule_enabled": True,
        "boot_image_seconds": 3.0,
        "boot_image_height_pct": 25,
        "boot_image_rotation": 0,
        "spinner_height_pct": 17,
        "spinner_fps": 8,
        "schedule_enabled": False,
        "display_off_time": "23:00",
        "display_on_time": "07:00",
        "reboot_enabled": False,
        "reboot_time": "04:30",
        "tmdb_sources": {
            "movie_now_playing": True,
            "movie_upcoming": True,
        },
        "tmdb_max_per_source": 6,
        "tmdb_source_limits": {},
        "poster_max_width": DEFAULT_POSTER_MAX_WIDTH,
        "tmdb_media_mode": "movie",
        "poster_expiry_enabled": False,
        "poster_expiry_days": 90,
        "poster_expiry_include_pinned": False,
        "tmdb_min_popularity": 0,
        "tmdb_min_popularity_upcoming": 10,
        "tmdb_upcoming_months": 12,
        "discovery_source": "tmdb",
        "discovery_sync_time": "04:00",
        "justwatch_enabled": False,
        "justwatch_schedule_enabled": True,
        "justwatch_max_titles": 10,
        "randomize_after_sync": False,
        "plex_client_id": "",
        "plex_enabled": False,
        "plex_poll_seconds": 15.0,
        "plex_stop_delay_seconds": 30,
        "plex_username": "",
        "plex_server_name": "",
        "plex_server_url": "",
        "plex_home_users": [],
        "plex_now_playing": {"active": False},
    }

    changed = False
    for key, value in defaults.items():
        if key not in config:
            config[key] = value
            changed = True

    # hdmi_width/height (the resolution install.sh forces the physical HDMI
    # signal to - always the panel's own native/unrotated orientation, since
    # that's what the receiver actually expects regardless of how it's
    # mounted) used to just be display_width/height, which instead follows
    # the opposite convention (pre-rotation render canvas, swapped relative
    # to the panel's native orientation when rotation_degrees is 90/270 -
    # confirmed against the original working setup: display_width/height
    # 1080x1920 with rotation 270 forced a working 1920x1080 signal, i.e.
    # the SWAPPED value was what the panel actually needed). Migrating the
    # old value straight across without swapping it back would silently
    # force the wrong (often unsupported) orientation for any existing
    # install with rotation_degrees set to 90/270.
    if "hdmi_width" not in config or "hdmi_height" not in config:
        display_width = config.get("display_width", 1080)
        display_height = config.get("display_height", 1920)
        if config.get("rotation_degrees") in (90, 270):
            display_width, display_height = display_height, display_width
        config.setdefault("hdmi_width", display_width)
        config.setdefault("hdmi_height", display_height)
        changed = True

    if "appearances" not in config:
        # One-time migration: these used to be flat top-level keys. Pull
        # whatever the install already had into "classic" (falling back to
        # the pre-migration text_color source) so existing settings survive
        # untouched, then seed "framed" fresh. Going forward both appearances
        # keep their own independent copies of these fields.
        if "text_color" not in config:
            config["text_color"] = config.get("accent_color", DEFAULT_ACCENT)
        migrated_classic = dict(CLASSIC_DEFAULTS)
        for key in CLASSIC_DEFAULTS:
            if key in config:
                migrated_classic[key] = config.pop(key)
        config["appearances"] = {"classic": migrated_classic, "framed": dict(FRAMED_DEFAULTS)}
        config["active_appearance"] = "classic"
        changed = True
    else:
        for appearance_key, appearance_defaults in (("classic", CLASSIC_DEFAULTS), ("framed", FRAMED_DEFAULTS)):
            appearance_config = config["appearances"].setdefault(appearance_key, {})
            for key, value in appearance_defaults.items():
                if key not in appearance_config:
                    appearance_config[key] = value
                    changed = True
        if "active_appearance" not in config:
            config["active_appearance"] = "classic"
            changed = True

    if changed:
        save_config(config)

    return config


_CONFIG_LOCK = threading.Lock()


def save_config(config):
    # Write-then-rename, not write-in-place: an in-place write (open(path,
    # "w")) truncates before writing, so any overlap between two writers -
    # e.g. a browser save landing while the Plex connect flow's status poll
    # is also saving plex_home_users - interleaves their bytes into a torn,
    # unparseable file. os.replace() is atomic (POSIX rename), and each
    # writer gets its own temp file, so the worst case with this is a clean
    # last-writer-wins instead of corruption. The lock only prevents a
    # lost-update race between threads in this same process (plex_monitor.py
    # and fetch_posters.py write via HTTP, not this function directly).
    with _CONFIG_LOCK:
        fd, tmp_path = tempfile.mkstemp(prefix=".config.json.", dir=BASE_DIR)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp_path, CONFIG_PATH)
        except BaseException:
            os.unlink(tmp_path)
            raise


def get_ordered_posters():
    config = load_config()
    order = config.get("order", [])
    on_disk = set(os.listdir(POSTER_DIR))

    order = [f for f in order if f in on_disk]

    known = set(order)
    untracked = sorted(f for f in on_disk if f not in known)
    order.extend(untracked)

    if order != config.get("order", []):
        config["order"] = order
        save_config(config)

    return order


def apply_film_grain(image, intensity=0.08, grain_size=1.0):
    img = image.convert("RGB")
    width, height = img.size
    arr = np.array(img).astype(np.float32)

    raw_noise = np.random.normal(loc=0, scale=255 * intensity, size=(height, width))
    noise_uint8 = np.clip(raw_noise + 128, 0, 255).astype(np.uint8)
    noise_img = Image.fromarray(noise_uint8, mode="L")
    if grain_size > 0:
        noise_img = noise_img.filter(ImageFilter.GaussianBlur(radius=grain_size))

    noise = (np.array(noise_img).astype(np.float32) - 128)[:, :, np.newaxis]

    grainy = np.clip(arr + noise, 0, 255).astype(np.uint8)
    result = Image.fromarray(grainy)

    result = ImageEnhance.Contrast(result).enhance(0.96)
    result = ImageEnhance.Color(result).enhance(0.92)

    return result


def poster_media_type(filename):
    """movie or tv, from the filename prefix - tmdb_<media>_<id>.jpg /
    tmdbpin_<media>_<id>.jpg encode it directly; justwatch_movie_<id>.jpg
    and manual uploads are always effectively movie for this purpose (no
    digital-release concept applies to either the same way). Mirrors
    slideshow.py's identical helper - kept in sync by hand since the two
    processes don't share code."""
    return "tv" if "_tv_" in filename else "movie"


def describe_poster(filename, meta):
    """Classifies a poster for the web UI list: where it came from, and
    whether it's out yet. Status uses the same logic the physical display
    uses for its NOW SHOWING / COMING SOON band."""
    if filename.startswith("tmdbpin_"):
        kind = "pinned"
    elif filename.startswith("tmdb_"):
        kind = "tmdb"
    else:
        kind = "manual"

    info = meta.get(filename) or {}
    raw_date = info.get("release_date")
    status = "none"

    # For movies, "showing" means digitally available, not just released in
    # cinemas - a movie can sit in theatres for months before it's watchable
    # at home. So a movie with no known digital_release_date is "upcoming"
    # even if its theatrical release_date has already passed - confirmed
    # digitally available is the bar, not "we haven't checked yet" or "TMDb
    # genuinely has nothing recorded" (brand new theatrical releases
    # routinely have no digital date on TMDb for months). TV has no
    # digital-release concept on TMDb, so it keeps using release_date/
    # air-date directly.
    if poster_media_type(filename) == "tv":
        effective_date = raw_date
    else:
        effective_date = info.get("digital_release_date")
        if not effective_date and raw_date:
            status = "upcoming"

    if effective_date:
        try:
            released = datetime.strptime(effective_date, "%Y-%m-%d").date()
            status = "showing" if released <= date.today() else "upcoming"
        except (ValueError, TypeError):
            status = "none"

    return {
        "filename": filename,
        "kind": kind,
        "status": status,
        "title": info.get("title") or "",
        "release_date": raw_date or "",
    }


def prepare_poster(image, intensity, max_width, grain_enabled=True):
    """Downscale to the working width, then apply grain unless disabled.

    Order matters: graining a full-resolution image and shrinking it
    afterwards both wastes time and blurs the grain into mush.

    grain_enabled=False skips apply_film_grain entirely rather than calling
    it with intensity=0 - the numpy noise array, Gaussian blur, and contrast/
    color passes all run regardless of intensity, so zero-strength grain
    costs the same as full-strength grain. On weak hardware (a Pi Zero W)
    that cost is real; skipping the call is what actually saves it."""
    img = image.convert("RGB")

    if max_width and img.width > max_width:
        ratio = max_width / img.width
        img = img.resize(
            (max_width, max(1, round(img.height * ratio))), Image.LANCZOS
        )

    if not grain_enabled:
        return img

    return apply_film_grain(img, intensity=intensity)


@app.route("/")
def index():
    # The Plex now-playing override is system-managed (plex_monitor.py),
    # not something to show in the normal draggable rotation list.
    poster_files = [f for f in get_ordered_posters() if f != PLEX_NOWPLAYING_FILENAME]
    config = load_config()
    poster_meta_map = config.get("poster_meta", {})
    posters = [describe_poster(f, poster_meta_map) for f in poster_files]

    interval_minutes = round(config.get("interval_seconds", DEFAULT_INTERVAL_SECONDS) / 60, 2)
    rotation_degrees = config.get("rotation_degrees", 0)

    accent_color = config.get("accent_color", DEFAULT_ACCENT)
    if not HEX_RE.match(accent_color):
        accent_color = DEFAULT_ACCENT

    active_appearance = config.get("active_appearance", "classic")
    classic = config["appearances"]["classic"]
    framed = config["appearances"]["framed"]

    classic_text_color = classic.get("text_color", accent_color)
    if not HEX_RE.match(classic_text_color):
        classic_text_color = accent_color

    classic_band_bg_color = classic.get("band_background_color", DEFAULT_BAND_BG)
    if not HEX_RE.match(classic_band_bg_color):
        classic_band_bg_color = DEFAULT_BAND_BG

    framed_text_color = framed.get("text_color", "#ffffff")
    if not HEX_RE.match(framed_text_color):
        framed_text_color = "#ffffff"

    framed_background_color = framed.get("background_color", "#000000")
    if not HEX_RE.match(framed_background_color):
        framed_background_color = "#000000"

    brightness_pct = round(config.get("brightness", 1.0) * 100)
    contrast_pct = round(config.get("contrast", 1.0) * 100)
    saturation_pct = round(config.get("saturation", 1.0) * 100)
    display_width = config.get("display_width", 1080)
    display_height = config.get("display_height", 1920)
    hdmi_width = config.get("hdmi_width", display_width)
    hdmi_height = config.get("hdmi_height", display_height)
    classic_display_font = classic.get("display_font", {})
    framed_top_font = framed.get("top_font", {})
    grain_intensity = config.get("grain_intensity", DEFAULT_GRAIN_INTENSITY)
    grain_enabled = config.get("grain_enabled", True)
    tmdb_enabled = config.get("tmdb_enabled", True)
    tmdb_schedule_enabled = config.get("tmdb_schedule_enabled", True)
    discovery_source = config.get("discovery_source", "tmdb")
    discovery_sync_time = config.get("discovery_sync_time", "04:00")
    justwatch_enabled = config.get("justwatch_enabled", False)
    justwatch_schedule_enabled = config.get("justwatch_schedule_enabled", True)
    justwatch_max_titles = config.get("justwatch_max_titles", 10)
    randomize_after_sync = config.get("randomize_after_sync", False)
    boot_image_seconds = config.get("boot_image_seconds", 3.0)
    boot_image_height_pct = config.get("boot_image_height_pct", 25)
    boot_image_rotation = config.get("boot_image_rotation", 0)
    spinner_height_pct = config.get("spinner_height_pct", 17)
    spinner_fps = config.get("spinner_fps", 8)
    boot_image_exists = os.path.exists(BOOT_IMAGE_PATH)
    schedule_enabled = config.get("schedule_enabled", False)
    display_off_time = config.get("display_off_time", "23:00")
    display_on_time = config.get("display_on_time", "07:00")
    reboot_enabled = config.get("reboot_enabled", False)
    reboot_time = config.get("reboot_time", "04:30")
    tmdb_sources = config.get("tmdb_sources", {})
    tmdb_max_per_source = config.get("tmdb_max_per_source", 6)
    tmdb_source_limits = config.get("tmdb_source_limits", {})
    poster_max_width = config.get("poster_max_width", DEFAULT_POSTER_MAX_WIDTH)
    tmdb_media_mode = config.get("tmdb_media_mode", "movie")
    poster_expiry_enabled = config.get("poster_expiry_enabled", False)
    poster_expiry_days = config.get("poster_expiry_days", 90)
    poster_expiry_include_pinned = config.get("poster_expiry_include_pinned", False)
    tmdb_min_popularity = config.get("tmdb_min_popularity", 0)
    tmdb_min_popularity_upcoming = config.get("tmdb_min_popularity_upcoming", 10)
    tmdb_upcoming_months = config.get("tmdb_upcoming_months", 12)
    plex_enabled = config.get("plex_enabled", False)
    plex_poll_seconds = config.get("plex_poll_seconds", 15.0)
    plex_stop_delay_seconds = config.get("plex_stop_delay_seconds", 30)
    plex_username = config.get("plex_username", "")
    plex_server_name = config.get("plex_server_name", "")
    plex_home_users = config.get("plex_home_users", [])
    classic_plex_band = classic.get("plex_band", "bottom")
    plex_connected = bool(plex_username and load_env_file(ENV_PATH).get("PLEX_SERVER_TOKEN"))
    git_sha, git_message, git_version = get_git_info()
    log_sources = {key: label for key, (label, _) in LOG_SOURCES.items()}

    next_sync = next_daily_sync(discovery_sync_time) - datetime.now()
    next_sync_hours, remainder = divmod(int(next_sync.total_seconds()), 3600)
    next_sync_minutes = remainder // 60

    return render_template(
        "index.html",
        posters=posters,
        log_sources=log_sources,
        interval_minutes=interval_minutes,
        rotation_degrees=rotation_degrees,
        accent_color=accent_color,
        accent_dim=adjust_lightness(accent_color, 0.6),
        accent_hover=adjust_lightness(accent_color, 1.2),
        accent_text=contrasting_text_color(accent_color),
        next_sync_hours=next_sync_hours,
        next_sync_minutes=next_sync_minutes,
        active_appearance=active_appearance,
        classic_text_color=classic_text_color,
        classic_band_bg_color=classic_band_bg_color,
        classic_poster_position=classic.get("poster_position", "center"),
        classic_top_band_content=classic.get("top_band_content", "status"),
        classic_bottom_band_content=classic.get("bottom_band_content", "date"),
        classic_top_custom_text=classic.get("top_custom_text", ""),
        classic_bottom_custom_text=classic.get("bottom_custom_text", ""),
        classic_text_size_pct=classic.get("text_size_pct", 100),
        classic_display_font_family=classic_display_font.get("family", ""),
        classic_display_font_status=classic_display_font.get("status", ""),
        framed_text_color=framed_text_color,
        framed_background_color=framed_background_color,
        framed_poster_position=framed.get("poster_position", "center"),
        framed_poster_scale_pct=framed.get("poster_scale_pct", 78),
        framed_top_content=framed.get("top_content", "status"),
        framed_top_custom_text=framed.get("top_custom_text", ""),
        framed_top_font_family=framed_top_font.get("family", ""),
        framed_top_font_status=framed_top_font.get("status", ""),
        framed_cast_count=framed.get("cast_count", 4),
        brightness_pct=brightness_pct,
        contrast_pct=contrast_pct,
        saturation_pct=saturation_pct,
        display_width=display_width,
        display_height=display_height,
        hdmi_width=hdmi_width,
        hdmi_height=hdmi_height,
        grain_intensity=grain_intensity,
        grain_enabled=grain_enabled,
        tmdb_enabled=tmdb_enabled,
        tmdb_schedule_enabled=tmdb_schedule_enabled,
        discovery_source=discovery_source,
        discovery_sync_time=discovery_sync_time,
        justwatch_enabled=justwatch_enabled,
        justwatch_schedule_enabled=justwatch_schedule_enabled,
        justwatch_max_titles=justwatch_max_titles,
        randomize_after_sync=randomize_after_sync,
        boot_image_seconds=boot_image_seconds,
        boot_image_height_pct=boot_image_height_pct,
        boot_image_rotation=boot_image_rotation,
        spinner_height_pct=spinner_height_pct,
        spinner_fps=spinner_fps,
        boot_image_exists=boot_image_exists,
        schedule_enabled=schedule_enabled,
        display_off_time=display_off_time,
        display_on_time=display_on_time,
        reboot_enabled=reboot_enabled,
        reboot_time=reboot_time,
        tmdb_sources=tmdb_sources,
        tmdb_max_per_source=tmdb_max_per_source,
        tmdb_source_limits=tmdb_source_limits,
        poster_max_width=poster_max_width,
        tmdb_media_mode=tmdb_media_mode,
        poster_expiry_enabled=poster_expiry_enabled,
        poster_expiry_days=poster_expiry_days,
        poster_expiry_include_pinned=poster_expiry_include_pinned,
        tmdb_min_popularity=tmdb_min_popularity,
        tmdb_min_popularity_upcoming=tmdb_min_popularity_upcoming,
        tmdb_upcoming_months=tmdb_upcoming_months,
        git_sha=git_sha,
        git_message=git_message,
        git_version=git_version,
        plex_enabled=plex_enabled,
        plex_poll_seconds=plex_poll_seconds,
        plex_stop_delay_seconds=plex_stop_delay_seconds,
        plex_username=plex_username,
        plex_server_name=plex_server_name,
        plex_home_users=plex_home_users,
        classic_plex_band=classic_plex_band,
        plex_connected=plex_connected,
    )


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("poster")
    if not file or not file.filename or not allowed_file(file.filename):
        return redirect(url_for("index"))

    config = load_config()
    intensity = config.get("grain_intensity", DEFAULT_GRAIN_INTENSITY)
    grain_enabled = config.get("grain_enabled", True)
    max_width = config.get("poster_max_width", DEFAULT_POSTER_MAX_WIDTH)

    filename = secure_filename(file.filename)

    image = Image.open(file.stream)
    image.load()

    # Originals are kept at full resolution so raising the working width
    # later just needs a re-grain, not a re-download.
    image.convert("RGB").save(os.path.join(ORIGINAL_DIR, filename))

    prepare_poster(image, intensity, max_width, grain_enabled).save(os.path.join(POSTER_DIR, filename))

    if filename not in config["order"]:
        config["order"].append(filename)
    save_config(config)

    return redirect(url_for("index"))


@app.route("/regrain", methods=["POST"])
def regrain():
    intensity = request.form.get("grain_intensity", type=float)
    if intensity is None:
        return redirect(url_for("index"))
    intensity = max(0.0, min(0.3, intensity))
    grain_enabled = "grain_enabled" in request.form

    config = load_config()
    config["grain_intensity"] = intensity
    config["grain_enabled"] = grain_enabled
    save_config(config)

    reprocessed = 0
    for filename in os.listdir(ORIGINAL_DIR):
        original_path = os.path.join(ORIGINAL_DIR, filename)
        if not os.path.isfile(original_path):
            continue
        try:
            image = Image.open(original_path)
            image.load()
            prepare_poster(image, intensity,
                           config.get("poster_max_width", DEFAULT_POSTER_MAX_WIDTH),
                           grain_enabled,
                           ).save(os.path.join(POSTER_DIR, filename))
            reprocessed += 1
        except Exception as e:
            print(f"Failed to regrain {filename}: {e}")

    print(f"Reprocessed {reprocessed} poster(s), grain {'on' if grain_enabled else 'off'} (intensity {intensity})")

    return redirect(url_for("index"))


@app.route("/upload-boot-image", methods=["POST"])
def upload_boot_image():
    file = request.files.get("boot_image")
    if not file or not file.filename or not allowed_file(file.filename):
        return redirect(url_for("index"))

    try:
        image = Image.open(file.stream)
        image.load()
    except Exception as e:
        print(f"Could not read uploaded boot image: {e}")
        return redirect(url_for("index"))

    # Always store as PNG so the spinner has one predictable path, and
    # keep any alpha channel so transparent logos stay transparent.
    if image.mode not in ("RGBA", "RGB"):
        image = image.convert("RGBA")

    os.makedirs(os.path.dirname(BOOT_IMAGE_PATH), exist_ok=True)
    image.save(BOOT_IMAGE_PATH, "PNG")

    return redirect(url_for("index"))


@app.route("/delete-boot-image", methods=["POST"])
def delete_boot_image():
    if os.path.exists(BOOT_IMAGE_PATH):
        os.remove(BOOT_IMAGE_PATH)
    return redirect(url_for("index"))


@app.route("/delete/<path:filename>", methods=["POST"])
def delete(filename):
    safe_name = secure_filename(filename)
    if safe_name != filename:
        return "Invalid filename", 400

    poster_path = os.path.join(POSTER_DIR, safe_name)
    original_path = os.path.join(ORIGINAL_DIR, safe_name)

    if os.path.exists(poster_path):
        os.remove(poster_path)
    if os.path.exists(original_path):
        os.remove(original_path)

    config = load_config()
    config["order"] = [f for f in config["order"] if f != safe_name]
    config.get("poster_meta", {}).pop(safe_name, None)
    save_config(config)

    return redirect(url_for("index"))


@app.route("/poster-meta/<path:filename>", methods=["POST"])
def poster_meta(filename):
    safe_name = secure_filename(filename)
    if safe_name != filename:
        return jsonify({"error": "invalid filename"}), 400

    data = request.get_json(silent=True) or {}

    config = load_config()
    config.setdefault("poster_meta", {})
    # Merge rather than replace: fetch_posters.py and plex_monitor.py can each
    # post their own subset of fields (e.g. credits arriving separately from
    # title/release_date) without one call wiping out what the other set.
    existing = config["poster_meta"].get(safe_name, {})
    existing["release_date"] = data.get("release_date", existing.get("release_date"))
    existing["title"] = data.get("title", existing.get("title"))
    if "credits" in data:
        existing["credits"] = data["credits"]
    if "digital_release_date" in data:
        existing["digital_release_date"] = data["digital_release_date"]
    # Stamped once, on whichever call is the first to ever set metadata for
    # this filename, and never touched again - this is what age-based expiry
    # (fetch_posters.py/fetch_justwatch.py's expire_old_posters) actually
    # measures from. Deliberately not release_date: a sync category can
    # surface a film that came out months ago, and expiring by release date
    # meant a poster like that could get pulled again within a day of being
    # added - the age limit is meant to bound time in *your* rotation, not
    # the movie's real age.
    existing.setdefault("added_date", date.today().isoformat())
    config["poster_meta"][safe_name] = existing
    save_config(config)

    return jsonify({"status": "ok"})


@app.route("/tmdb-sync-now", methods=["POST"])
def tmdb_sync_now():
    config = load_config()
    if not config.get("tmdb_enabled", True):
        return redirect(url_for("index"))

    api_key = load_env_file(ENV_PATH).get("TMDB_API_KEY", "")
    if not api_key:
        return redirect(url_for("index"))

    fetch_script = os.path.join(BASE_DIR, "fetch_posters.py")
    venv_python = os.path.join(BASE_DIR, "venv", "bin", "python3")

    subprocess_env = os.environ.copy()
    subprocess_env["TMDB_API_KEY"] = api_key

    with open(TMDB_SYNC_LOG, "w") as log_file:
        subprocess.Popen(
            [venv_python, fetch_script],
            env=subprocess_env,
            cwd=BASE_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    return redirect(url_for("index"))


@app.route("/justwatch-sync-now", methods=["POST"])
def justwatch_sync_now():
    config = load_config()
    if not config.get("justwatch_enabled", False):
        return redirect(url_for("index"))
    # Only the currently-selected source can pull, so a stale JustWatch tab
    # left open after switching back to TMDb can't kick off a sync that
    # would just fight the TMDb one over the rotation.
    if config.get("discovery_source", "tmdb") != "justwatch":
        return redirect(url_for("index"))

    api_key = load_env_file(ENV_PATH).get("TMDB_API_KEY", "")
    if not api_key:
        return redirect(url_for("index"))

    fetch_script = os.path.join(BASE_DIR, "fetch_justwatch.py")
    venv_python = os.path.join(BASE_DIR, "venv", "bin", "python3")

    subprocess_env = os.environ.copy()
    subprocess_env["TMDB_API_KEY"] = api_key

    with open(JUSTWATCH_SYNC_LOG, "w") as log_file:
        subprocess.Popen(
            [venv_python, fetch_script],
            env=subprocess_env,
            cwd=BASE_DIR,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    return redirect(url_for("index"))


# How many candidates the JustWatch preview grid resolves against TMDb per
# request - independent of justwatch_max_titles (which caps the *auto-sync's*
# target rotation size, not how much this Discovery tab lets you browse).
# Each candidate is one sequential TMDb search call, so this is also the main
# knob on how long a preview fetch takes (~15-20s at 40 on a Pi Zero W) - fine
# for an explicit, on-demand, loading-state-shown action.
JUSTWATCH_PREVIEW_CAP = 40

# TMDb's own page size - how many items fetch_source() pulls per category for
# the preview grid, deliberately not capped down to tmdb_source_limits (which
# governs what the *auto-sync* actually keeps) so the grid shows more than
# just what would get synced.
TMDB_PREVIEW_PER_CATEGORY = 20


@app.route("/discovery-preview/tmdb", methods=["GET"])
def discovery_preview_tmdb():
    config = load_config()
    api_key = load_env_file(ENV_PATH).get("TMDB_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "TMDb API key is not configured"})

    mode = config.get("tmdb_media_mode", "movie")
    if mode not in ("movie", "tv"):
        mode = "movie"
    sources = config.get("tmdb_sources") or fetch_posters.DEFAULT_SOURCES
    enabled = [
        k for k, src in fetch_posters.SOURCES.items()
        if src["media"] == mode and sources.get(k)
    ]
    if not enabled:
        return jsonify({"ok": True, "items": []})

    min_popularity = float(config.get("tmdb_min_popularity", 0))
    min_pop_upcoming = float(config.get("tmdb_min_popularity_upcoming", 10))
    upcoming_months = float(config.get("tmdb_upcoming_months", 12))

    wanted = {}
    for source_key in enabled:
        is_upcoming = fetch_posters.SOURCES[source_key].get("discover") == "upcoming"
        threshold = min_pop_upcoming if is_upcoming else min_popularity
        try:
            for item in fetch_posters.fetch_source(
                source_key, TMDB_PREVIEW_PER_CATEGORY, threshold, upcoming_months, api_key=api_key,
            ):
                wanted.setdefault(item["key"], item)
        except requests.RequestException as e:
            print(f"Discovery preview: failed to fetch {source_key}: {e}")

    items = []
    for item in wanted.values():
        media, _, item_id = item["key"].partition(":")
        items.append({
            "media": media,
            "id": int(item_id),
            "title": item["title"],
            "release_date": item["release_date"],
            "thumb_url": f"https://image.tmdb.org/t/p/w342{item['poster_path']}",
            "already_added": poster_already_in_rotation(config, media, item_id) is not None,
        })

    return jsonify({"ok": True, "items": items})


@app.route("/discovery-preview/justwatch", methods=["GET"])
def discovery_preview_justwatch():
    config = load_config()
    api_key = load_env_file(ENV_PATH).get("TMDB_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "error": "TMDb API key is not configured"})

    try:
        candidates = fetch_justwatch.fetch_popular_titles()
    except (requests.RequestException, ValueError) as e:
        return jsonify({"ok": False, "error": f"Could not reach JustWatch: {e}"})

    this_year = date.today().year
    this_year_titles = [c for c in candidates if c.get("year") == this_year]

    wanted = {}
    for entry in this_year_titles:
        if len(wanted) >= JUSTWATCH_PREVIEW_CAP:
            break
        result = fetch_justwatch.resolve_tmdb_id(entry["title"], entry["year"], api_key=api_key)
        if not result or not result.get("poster_path"):
            continue
        item_id = str(result["id"])
        wanted.setdefault(item_id, {
            "id": item_id,
            "title": result.get("title") or entry["title"],
            "release_date": result.get("release_date") or "",
            "poster_path": result["poster_path"],
        })

    items = [
        {
            "media": "movie",
            "id": int(item["id"]),
            "title": item["title"],
            "release_date": item["release_date"],
            "thumb_url": f"https://image.tmdb.org/t/p/w342{item['poster_path']}",
            "already_added": poster_already_in_rotation(config, "movie", item["id"]) is not None,
        }
        for item in wanted.values()
    ]

    return jsonify({"ok": True, "items": items})


TMDB_URL_RE = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)")


def poster_already_in_rotation(config, media, item_id):
    """The same TMDb title can end up in the rotation under three different
    filenames depending on how it got there (auto-synced, manually pinned,
    or JustWatch-sourced) - checked here so pinning, and the Discovery
    preview grid's "already in rotation" state, agree on one definition
    rather than each guessing independently."""
    candidates = [f"tmdb_{media}_{item_id}.jpg", f"tmdbpin_{media}_{item_id}.jpg"]
    if media == "movie":
        candidates.append(f"justwatch_movie_{item_id}.jpg")
    order = config.get("order", [])
    return next((f for f in candidates if f in order), None)


def pin_tmdb_title(media, item_id):
    """Core of pinning a specific TMDb title into the rotation - shared by
    /tmdb-add-link (paste a URL) and /discovery-pin (click a poster in the
    Discovery preview grid).

    Saved with a 'tmdbpin_' prefix rather than 'tmdb_', which keeps it
    outside the auto-sync's bookkeeping - so a pinned title survives the
    next sync instead of being cleaned up as 'no longer trending'.

    Returns (filename, title). Raises ValueError for a problem worth
    showing the user directly (no API key configured, or TMDb has no poster
    for this id) or requests.RequestException for a network failure -
    callers decide how to surface each."""
    config = load_config()
    existing = poster_already_in_rotation(config, media, item_id)
    if existing:
        meta = config.get("poster_meta", {}).get(existing) or {}
        return existing, meta.get("title", "Untitled")

    api_key = load_env_file(ENV_PATH).get("TMDB_API_KEY", "")
    if not api_key:
        raise ValueError("TMDb API key is not configured")

    detail = requests.get(
        f"https://api.themoviedb.org/3/{media}/{item_id}",
        params={"api_key": api_key, "language": "en-US"},
        timeout=15,
    )
    detail.raise_for_status()
    data = detail.json()

    poster_path = data.get("poster_path")
    if not poster_path:
        raise ValueError("This title has no poster art on TMDb")

    title = data.get("title") or data.get("name") or "Untitled"
    released = data.get("release_date") or data.get("first_air_date") or ""

    image = requests.get(f"https://image.tmdb.org/t/p/original{poster_path}", timeout=15)
    image.raise_for_status()

    filename = f"tmdbpin_{media}_{item_id}.jpg"
    intensity = config.get("grain_intensity", DEFAULT_GRAIN_INTENSITY)
    grain_enabled = config.get("grain_enabled", True)

    source = Image.open(BytesIO(image.content))
    source.load()

    source.convert("RGB").save(os.path.join(ORIGINAL_DIR, filename))
    prepare_poster(source, intensity,
        config.get("poster_max_width", DEFAULT_POSTER_MAX_WIDTH), grain_enabled).save(os.path.join(POSTER_DIR, filename))

    # Unlike the old /tmdb-add-link (which only saved title/release_date),
    # this fetches credits too - the sync scripts already do this, so a
    # manually pinned title shouldn't be the one path that skips the
    # Framed appearance's cast/crew billing block.
    cast_count = config.get("appearances", {}).get("framed", {}).get("cast_count", 4)
    credits = fetch_posters.fetch_credits(media, item_id, cast_count, api_key=api_key)
    # TV has no release_dates equivalent on TMDb - only movies.
    digital_release_date = (
        fetch_posters.fetch_digital_release_date(item_id, api_key=api_key) if media == "movie" else None
    )

    if filename not in config["order"]:
        config["order"].append(filename)
    meta = {"release_date": released, "title": title, "added_date": date.today().isoformat()}
    if credits:
        meta["credits"] = credits
    if digital_release_date:
        meta["digital_release_date"] = digital_release_date
    config.setdefault("poster_meta", {})[filename] = meta
    save_config(config)
    print(f"Pinned: {title}")

    return filename, title


@app.route("/tmdb-add-link", methods=["POST"])
def tmdb_add_link():
    raw = request.form.get("tmdb_url", "").strip()
    match = TMDB_URL_RE.search(raw)

    if not match:
        alt = re.match(r"^(movie|tv)[\s/:]+(\d+)$", raw, re.IGNORECASE)
        if not alt:
            return redirect(url_for("index"))
        media, item_id = alt.group(1).lower(), alt.group(2)
    else:
        media, item_id = match.group(1), match.group(2)

    try:
        pin_tmdb_title(media, item_id)
    except (ValueError, requests.RequestException) as e:
        print(f"TMDb link add failed: {e}")

    return redirect(url_for("index"))


@app.route("/discovery-pin", methods=["POST"])
def discovery_pin():
    """Click-to-pin from the Discovery tab's live preview grid - same
    underlying pin as /tmdb-add-link, just fed a media/id pair the frontend
    already has from /discovery-preview/* instead of a pasted URL, and
    answering with JSON so the grid tile can update itself in place instead
    of a full page reload."""
    data = request.get_json(silent=True) or {}
    media = data.get("media")
    item_id = data.get("id")
    if media not in ("movie", "tv") or not item_id:
        return jsonify({"ok": False, "error": "Invalid title"}), 400

    try:
        filename, title = pin_tmdb_title(media, str(item_id))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Could not reach TMDb: {e}"}), 502

    return jsonify({"ok": True, "filename": filename, "title": title})


@app.route("/purge-tmdb", methods=["POST"])
def purge_tmdb():
    """Remove every TMDb-sourced poster in one go - both auto-synced and
    pinned. Manually uploaded posters are left alone."""
    config = load_config()
    removed = 0

    for directory in (POSTER_DIR, ORIGINAL_DIR):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if name.startswith("tmdb_") or name.startswith("tmdbpin_"):
                try:
                    os.remove(os.path.join(directory, name))
                    if directory == POSTER_DIR:
                        removed += 1
                except OSError:
                    pass

    config["order"] = [
        f for f in config.get("order", [])
        if not (f.startswith("tmdb_") or f.startswith("tmdbpin_"))
    ]
    config["poster_meta"] = {
        k: v for k, v in config.get("poster_meta", {}).items()
        if not (k.startswith("tmdb_") or k.startswith("tmdbpin_"))
    }
    save_config(config)

    tracking = os.path.join(BASE_DIR, "tmdb_tracked.json")
    if os.path.exists(tracking):
        os.remove(tracking)

    print(f"Purged {removed} TMDb poster(s)")
    return redirect(url_for("index"))


@app.route("/purge-justwatch", methods=["POST"])
def purge_justwatch():
    """Remove every JustWatch-sourced poster. Available regardless of which
    source is currently active, so switching away from JustWatch still
    leaves a way to clear out what it already added."""
    config = load_config()
    removed = 0

    for directory in (POSTER_DIR, ORIGINAL_DIR):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if name.startswith("justwatch_"):
                try:
                    os.remove(os.path.join(directory, name))
                    if directory == POSTER_DIR:
                        removed += 1
                except OSError:
                    pass

    config["order"] = [f for f in config.get("order", []) if not f.startswith("justwatch_")]
    config["poster_meta"] = {
        k: v for k, v in config.get("poster_meta", {}).items()
        if not k.startswith("justwatch_")
    }
    save_config(config)

    tracking = os.path.join(BASE_DIR, "justwatch_tracked.json")
    if os.path.exists(tracking):
        os.remove(tracking)

    print(f"Purged {removed} JustWatch poster(s)")
    return redirect(url_for("index"))


@app.route("/power/<action>", methods=["POST"])
def power(action):
    """Clean shutdown / reboot from the UI.

    Flask runs unprivileged, so this relies on a narrowly-scoped sudoers
    rule permitting exactly these two systemctl calls and nothing else.
    Returns a static page rather than redirecting, since the server is
    about to stop answering."""
    if action not in ("poweroff", "reboot", "restart-display"):
        return redirect(url_for("index"))

    # Restarting just the display service doesn't take the web app down,
    # so it redirects back instead of showing a standby page.
    if action == "restart-display":
        try:
            subprocess.Popen(
                ["sudo", "-n", "/usr/bin/systemctl", "restart", "posterframe-slideshow"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"Could not restart display service: {e}")
        return redirect(url_for("index"))

    verb = "Shutting down" if action == "poweroff" else "Rebooting"
    detail = (
        "Wait for the green activity LED on the Pi to stop flickering "
        "(about 10 seconds), then it is safe to cut power."
        if action == "poweroff"
        else "The frame will come back on its own in a minute or two."
    )

    try:
        subprocess.Popen(
            ["sudo", "-n", "/usr/bin/systemctl", action],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Power command failed</title>
<style>
  body {{ background:#0a0a0b; color:#f2f2f0; font-family:-apple-system,sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  div {{ text-align:center; max-width:460px; padding:0 24px; }}
  h1 {{ font-size:1.3rem; font-weight:600; margin:0 0 12px; color:#e5484d; }}
  p {{ color:#88888e; font-size:0.9rem; line-height:1.5; margin:0 0 10px; }}
  code {{ font-family:monospace; font-size:0.82rem; color:#f2f2f0; }}
  a {{ color:#5b8cff; }}
</style></head>
<body><div>
<h1>Couldn't run that command</h1>
<p>The poster frame isn't permitted to power the Pi down. This usually means
the sudoers rule hasn't been set up yet.</p>
<p><code>{e}</code></p>
<p>You can still shut down safely over SSH with <code>sudo shutdown -h now</code>.</p>
<p><a href="/">Back</a></p>
</div></body></html>""", 500

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{verb}</title>
<style>
  body {{ background:#0a0a0b; color:#f2f2f0; font-family:-apple-system,sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  div {{ text-align:center; max-width:420px; padding:0 24px; }}
  h1 {{ font-size:1.3rem; font-weight:600; margin:0 0 12px; }}
  p {{ color:#88888e; font-size:0.9rem; line-height:1.5; margin:0; }}
</style></head>
<body><div><h1>{verb}&hellip;</h1><p>{detail}</p></div></body></html>"""


@app.route("/update/check")
def update_check():
    """Whether a newer commit exists upstream. Read-only - runs `git
    fetch` but never merges anything. Network-bound (unlike get_git_info,
    used for the always-on "currently running" line), so this is only
    called when the user clicks "Check for updates", not on page load."""
    if not os.path.exists(UPDATE_SCRIPT):
        return jsonify({"ok": False, "error": "update.sh is missing"}), 500

    try:
        result = subprocess.run(
            ["bash", UPDATE_SCRIPT, "--check"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=45,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timed out reaching the git remote"}), 504

    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "git check failed").strip().splitlines()
        return jsonify({"ok": False, "error": lines[-1] if lines else "git check failed"}), 500

    info = parse_update_check_output(result.stdout)
    behind = int(info.get("BEHIND") or 0)
    ahead = int(info.get("AHEAD") or 0)
    current_version, current_title = parse_version_title(info.get("CURRENT_MSG"))
    remote_version, remote_title = parse_version_title(info.get("REMOTE_MSG"))

    return jsonify({
        "ok": True,
        "current_sha": info.get("CURRENT_SHA"),
        "current_message": info.get("CURRENT_MSG"),
        "current_version": current_version,
        "current_title": current_title,
        "remote_sha": info.get("REMOTE_SHA"),
        "remote_message": info.get("REMOTE_MSG"),
        "remote_version": remote_version,
        "remote_title": remote_title,
        "behind": behind,
        "ahead": ahead,
        "up_to_date": behind == 0,
        "diverged": ahead > 0,
        "system_changes": info.get("SYSTEM_CHANGES") == "1",
    })


def acquire_update_lock():
    """Opens and non-blockingly flocks .update.lock - the same lock file
    update.sh itself takes. Returns the open fd (caller must os.close() it
    to release) if acquired, or None if someone else already holds it -
    either a genuinely running update.sh, or another /update request that's
    still mid-spawn. The caller is expected to hold this across its whole
    check-then-truncate-then-spawn sequence, not just probe and immediately
    release: two /update requests racing close together used to both see
    pgrep report nothing running (Popen() returning doesn't guarantee the
    new process is visible to a separate pgrep call yet), both truncate
    update.log, and both spawn their own update.sh - each stomping the
    other's log output, which is exactly what made a genuinely-running
    update look stuck with nothing but "already in progress" logged.
    Holding one real lock for the whole critical section closes that race
    outright, rather than just narrowing it."""
    try:
        fd = os.open(UPDATE_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def update_already_running():
    """Read-only status probe (e.g. for the progress bar's polling) - not
    holding anything, just asking "is someone currently holding this lock"."""
    fd = acquire_update_lock()
    if fd is None:
        return True
    os.close(fd)
    return False


@app.route("/update", methods=["POST"])
def update_now():
    """Pulls latest and restarts services - the actual "Update now" action.

    Runs update.sh detached and returns a standby page immediately, since
    the last thing update.sh does is restart posterframe-web itself. See
    update.sh's own comments for why that's safe despite killing this
    request's own process tree."""
    if not os.path.exists(UPDATE_SCRIPT):
        return redirect(url_for("index"))

    lock_fd = acquire_update_lock()
    already_running = lock_fd is None

    if lock_fd is not None:
        try:
            with open(UPDATE_LOG, "w") as log_file:
                subprocess_env = os.environ.copy()
                # Hands the already-held lock straight to update.sh via the
                # inherited fd (pass_fds) instead of letting go of it here -
                # see update.sh's own use of POSTERFRAME_LOCK_FD for why.
                # Closing lock_fd below still happens immediately either
                # way; it's safe precisely because the child now holds its
                # own reference to the same open file description, so the
                # underlying flock stays held until update.sh itself exits.
                subprocess_env["POSTERFRAME_LOCK_FD"] = str(lock_fd)
                subprocess.Popen(
                    # 900s not 600s: a system-file update also runs install.sh
                    # (apt + pip), which needs more room than a code-only pull.
                    ["timeout", "900", "bash", UPDATE_SCRIPT],
                    cwd=BASE_DIR,
                    stdout=log_file, stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(lock_fd,),
                    env=subprocess_env,
                )
        finally:
            # This closes only *our* reference. It used to be the moment
            # the lock was actually released - and Popen() returning is no
            # guarantee update.sh has reached its own locking code yet, so
            # a second /update request landing in that gap would sail
            # through this same acquire_update_lock() call, spawn its own
            # update.sh, and the two would only then race for the real OS
            # lock - the loser logging "An update is already in progress."
            # into update.log right after its own request handler had just
            # freshly truncated that file, burying the winner's actual
            # output. pass_fds above keeps a second reference to the same
            # open file description alive in the child the whole time, so
            # closing ours here no longer releases anything - the lock
            # stays held continuously with no gap at all.
            os.close(lock_fd)

    # Best-effort peek at whether this pull also touches system files, just
    # to set expectations - the UI already warned about this before the
    # button was clicked, so a failure here isn't worth blocking on.
    system_changes = False
    try:
        precheck = subprocess.run(
            ["bash", UPDATE_SCRIPT, "--check"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=20,
        )
        system_changes = parse_update_check_output(precheck.stdout).get("SYSTEM_CHANGES") == "1"
    except Exception:
        pass

    if system_changes:
        reload_ms, wait_copy = 90000, (
            "This one also applies system-level changes (install.sh runs "
            "automatically), so it can take a few minutes - longer than a "
            "plain code update."
        )
    else:
        reload_ms, wait_copy = 25000, "This page will check back on its own in about 25 seconds."

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Updating</title>
<style>
  body {{ background:#0a0a0b; color:#f2f2f0; font-family:-apple-system,sans-serif;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  div {{ text-align:center; max-width:420px; padding:0 24px; }}
  h1 {{ font-size:1.3rem; font-weight:600; margin:0 0 12px; }}
  p {{ color:#88888e; font-size:0.9rem; line-height:1.5; margin:0; }}
</style>
<script>setTimeout(function(){{ window.location.href = '/'; }}, {reload_ms});</script>
</head>
<body><div><h1>Updating&hellip;</h1>
<p>Pulling the latest code and restarting. {wait_copy} If the frame doesn't
come back by then, give it a bit longer and refresh.</p></div></body></html>"""


@app.route("/update/log")
def update_log():
    """Polled by the System tab's progress bar while an update runs. Tails
    update.log and reports whether update.sh is still alive - the frontend
    infers "the service is restarting" from this request itself failing
    (posterframe-web goes down as update.sh's last act), not from anything
    in this response, so this route doesn't need to predict that."""
    running = update_already_running()

    content = ""
    if os.path.exists(UPDATE_LOG):
        with open(UPDATE_LOG, errors="replace") as f:
            content = f.read()[-4000:]

    return jsonify({"running": running, "content": content})


@app.route("/logs/<name>")
def tail_log(name):
    """Polled by the Logs tab. Seeks to the last LOG_TAIL_BYTES instead of
    reading the whole file - plex_monitor.log in particular grows large over
    time, and this gets polled every couple seconds on a Pi Zero W."""
    source = LOG_SOURCES.get(name)
    if source is None:
        return jsonify({"ok": False, "error": "Unknown log"}), 404
    _, path = source

    content = ""
    if os.path.exists(path):
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            content = f.read().decode("utf-8", errors="replace")

    return jsonify({"ok": True, "content": content})


# Holds the pin id from the most recent /plex/connect call, so
# /plex/connect/status knows what to poll. A single pending pin is enough -
# this is a single-process app and only one browser tab would ever be
# driving the connect flow at a time.
_plex_pending_pin = None


def fetch_plex_home_users(client_id, token):
    """Best-effort - lets the user pick which Plex Home profile's playback
    to track instead of always assuming it's whoever ran the PIN sign-in,
    which matters when multiple people share one Plex Home and only one of
    them should drive Now Playing. Never raises: the connect flow succeeds
    either way, this just doesn't offer a picker if it comes back empty.
    Unverified against a real multi-profile Plex Home account - the
    /status/sessions User.title matching this project already relies on for
    the single-account case is proven live; this listing endpoint and its
    response shape aren't, so this parses somewhat defensively and simply
    returns nothing usable rather than guessing wrong."""
    try:
        resp = requests.get(
            f"{PLEX_TV}/api/v2/home/users",
            headers=plex_headers(client_id, token),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []

    if isinstance(data, dict):
        for key in ("users", "Users", "entries", "Entries"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return []
    if not isinstance(data, list):
        return []

    users = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title") or entry.get("username") or entry.get("friendlyName")
        if not title:
            continue
        users.append({"id": entry.get("id"), "title": title})
    return users


@app.route("/plex/connect", methods=["POST"])
def plex_connect():
    """Starts the Plex PIN sign-in flow. The frontend opens the returned
    auth_url in a new tab, then polls /plex/connect/status."""
    global _plex_pending_pin

    config = load_config()
    client_id = get_plex_client_id(config)

    try:
        resp = requests.post(
            f"{PLEX_TV}/api/v2/pins",
            headers=plex_headers(client_id),
            params={"strong": "true"},
            timeout=15,
        )
        resp.raise_for_status()
        pin = resp.json()
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Could not reach plex.tv: {e}"}), 502

    _plex_pending_pin = {"id": pin["id"]}

    auth_url = "https://app.plex.tv/auth#?" + urlencode({
        "clientID": client_id,
        "code": pin["code"],
        "context[device][product]": PLEX_PRODUCT,
    })

    return jsonify({"ok": True, "auth_url": auth_url})


@app.route("/plex/connect/status")
def plex_connect_status():
    """Polled by the frontend after opening the auth_url. Once the user
    approves the pin on plex.tv, this finishes setup in one shot: capture
    the username, find a server, and store what plex_monitor.py needs."""
    global _plex_pending_pin

    if not _plex_pending_pin:
        return jsonify({"ok": False, "error": "No connection in progress"}), 400

    config = load_config()
    client_id = get_plex_client_id(config)

    try:
        resp = requests.get(
            f"{PLEX_TV}/api/v2/pins/{_plex_pending_pin['id']}",
            headers=plex_headers(client_id),
            timeout=15,
        )
        resp.raise_for_status()
        pin = resp.json()
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Could not reach plex.tv: {e}"}), 502

    token = pin.get("authToken")
    if not token:
        return jsonify({"ok": True, "status": "pending"})

    _plex_pending_pin = None

    try:
        user_resp = requests.get(
            f"{PLEX_TV}/api/v2/user",
            headers=plex_headers(client_id, token),
            timeout=15,
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()
        username = user_data.get("username") or user_data.get("title") or user_data.get("email") or "Plex user"

        # Best-effort - the token this needs only exists in-memory for this
        # request (see the module-level note on why only PLEX_SERVER_TOKEN
        # ever gets persisted), so this is the only point this can ever be
        # fetched from - a later "switch profile" action just picks from
        # whatever got stored here, it can't re-fetch a fresh list itself.
        home_users = fetch_plex_home_users(client_id, token)

        resources_resp = requests.get(
            f"{PLEX_TV}/api/v2/resources",
            headers=plex_headers(client_id, token),
            params={"includeHttps": 1, "includeRelay": 1, "includeIPv6": 1},
            timeout=15,
        )
        resources_resp.raise_for_status()
        resources = resources_resp.json()
        if isinstance(resources, dict):
            resources = resources.get("resources") or resources.get("Device") or []
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"Signed in, but couldn't finish setup: {e}"}), 502

    server = None
    fallback = None
    for resource in resources:
        if "server" not in (resource.get("provides") or ""):
            continue
        connections = resource.get("connections") or []
        # Try local-flagged connections first, but the flag only reflects
        # what the server *thinks* its own address is - see
        # plex_connection_reachable. Fall back to non-local (including
        # relay) candidates if none of the local ones actually respond.
        ordered = sorted(connections, key=lambda c: not c.get("local"))
        access_token = resource.get("accessToken") or token
        headers = plex_headers(client_id, access_token)

        if ordered and not fallback and ordered[0].get("uri"):
            fallback = {
                "name": resource.get("name") or "Plex server",
                "url": ordered[0]["uri"],
                "token": access_token,
            }

        reachable = next(
            (c for c in ordered if c.get("uri") and plex_connection_reachable(c["uri"], headers)),
            None,
        )
        if reachable:
            server = {
                "name": resource.get("name") or "Plex server",
                "url": reachable["uri"],
                "token": access_token,
            }
            break

    warning = None
    if not server:
        if not fallback:
            return jsonify({"ok": False, "error": "Signed in, but no Plex server was found on this account."}), 502
        server = fallback
        warning = (
            "Couldn't verify any of this server's addresses are reachable from the frame - "
            "using its best guess. Now Playing may not work until that's resolved."
        )

    config["plex_username"] = username
    config["plex_server_name"] = server["name"]
    config["plex_server_url"] = server["url"]
    if home_users:
        config["plex_home_users"] = home_users
    save_config(config)

    env = load_env_file(ENV_PATH)
    env["PLEX_SERVER_TOKEN"] = server["token"]
    save_env_file(ENV_PATH, env)

    result = {"ok": True, "status": "linked", "username": username, "server_name": server["name"]}
    if home_users:
        result["home_users"] = home_users
    if warning:
        result["warning"] = warning
    return jsonify(result)


@app.route("/plex/select-user", methods=["POST"])
def plex_select_user():
    """Switches which Plex Home profile's sessions Now Playing tracks -
    just changes plex_username, the same field the connect flow already
    populates and plex_monitor.py already matches session User.title
    against, so no other code needs to know this exists. Doesn't touch
    Plex at all: the choice is only ever among names already stored in
    plex_home_users from the last connect, since that's the only point an
    account token (needed to ask Plex who's in the Home) is ever available -
    see fetch_plex_home_users."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "No profile given"}), 400

    config = load_config()
    config["plex_username"] = title
    save_config(config)
    return jsonify({"ok": True, "username": title})


@app.route("/plex/disconnect", methods=["POST"])
def plex_disconnect():
    config = load_config()
    config["plex_username"] = ""
    config["plex_server_name"] = ""
    config["plex_server_url"] = ""
    config["plex_home_users"] = []
    config["plex_enabled"] = False
    config["plex_now_playing"] = {"active": False}
    config["order"] = [f for f in config.get("order", []) if f != PLEX_NOWPLAYING_FILENAME]
    config.get("poster_meta", {}).pop(PLEX_NOWPLAYING_FILENAME, None)
    save_config(config)

    for directory in (POSTER_DIR, ORIGINAL_DIR):
        path = os.path.join(directory, PLEX_NOWPLAYING_FILENAME)
        if os.path.exists(path):
            os.remove(path)

    env = load_env_file(ENV_PATH)
    if env.pop("PLEX_SERVER_TOKEN", None) is not None:
        save_env_file(ENV_PATH, env)

    return redirect(url_for("index"))


@app.route("/plex/now-playing", methods=["POST"])
def plex_now_playing():
    """Internal - called only by plex_monitor.py to report what it found,
    never by the browser. No auth beyond what every other route here has
    (none): this is a LAN-only home appliance, an existing trust model,
    not something this feature changes."""
    data = request.get_json(silent=True) or {}
    config = load_config()

    if data.get("active"):
        config["plex_now_playing"] = {
            "active": True,
            "filename": data.get("filename"),
            "title": data.get("title"),
        }
    else:
        config["plex_now_playing"] = {"active": False}

    save_config(config)
    return jsonify({"status": "ok"})


@app.route("/reorder", methods=["POST"])
def reorder():
    data = request.get_json(silent=True)
    if not data or "order" not in data:
        return jsonify({"error": "missing order"}), 400

    on_disk = set(os.listdir(POSTER_DIR))
    new_order = [secure_filename(f) for f in data["order"] if secure_filename(f) in on_disk]

    config = load_config()
    config["order"] = new_order
    save_config(config)

    return jsonify({"status": "ok"})


def find_connected_hdmi_edid():
    """The DRM driver keeps reading a connected display's real EDID for
    capability purposes independently of whatever mode install.sh's video=
    kernel parameter is currently forcing the output to - so this reflects
    the display's actual native resolution, not just an echo of what we
    told it to output. Connector naming varies by board (a Pi 4's two
    micro-HDMI ports show up as separate HDMI-A-1/HDMI-A-2, for instance),
    so this looks for whichever one currently reports itself connected
    rather than assuming a fixed path."""
    for status_path in sorted(glob.glob("/sys/class/drm/card*-HDMI-*/status")):
        try:
            with open(status_path) as f:
                if f.read().strip() == "connected":
                    return status_path.replace("/status", "/edid")
        except OSError:
            continue
    return None


def parse_edid_preferred_resolution(edid_bytes):
    """The first Detailed Timing Descriptor (bytes 54-71 of the base EDID
    block) encodes the display's preferred/native timing - stable across
    EDID 1.3/1.4, true for virtually every real display. A zero pixel clock
    there means those bytes hold a monitor descriptor instead (rare, but
    possible on some odd panels), not a timing - nothing usable to return."""
    if len(edid_bytes) < 72:
        return None
    dtd = edid_bytes[54:72]
    pixel_clock = dtd[0] | (dtd[1] << 8)
    if pixel_clock == 0:
        return None
    h_active = dtd[2] | ((dtd[4] >> 4) << 8)
    v_active = dtd[5] | ((dtd[7] >> 4) << 8)
    if h_active <= 0 or v_active <= 0:
        return None
    return h_active, v_active


def detect_pi_model():
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().strip("\x00").strip()
    except OSError:
        return None


def safe_render_long_edge(model):
    """A render resolution ceiling (longest edge, px) for hardware too weak
    to composite/grain a full-size image in reasonable time - see
    apply_film_grain's unconditional numpy/blur/enhance cost. Zero W is the
    only board actually measured (CLAUDE.md's ~13s/poster baseline is at
    today's ~1920px long edge), so it's the only one capped; everything else
    (Pi 4/5, unrecognized future boards) is trusted uncapped rather than
    guessing at tiers with no measurements behind them."""
    if model and "Zero" in model:
        return 1920
    return None


@app.route("/detect-display")
def detect_display():
    """On-demand only - never called automatically at boot. This project
    forces its HDMI mode from config.json specifically because this class
    of EDID read can fail on a slow-to-wake TV at boot time (see install.sh);
    running this same read unattended on every boot would silently reproduce
    that exact bug. Safe here because a user triggers it long after boot,
    once the display is definitely awake, and only ever pre-fills the
    resolution fields for review - saving is still a separate, explicit step.
    Also suggests a capped render resolution when running on weak hardware
    (see safe_render_long_edge) - the raw detected size still goes to
    hdmi_width/height (the physical signal should match the display's true
    native resolution regardless of what the Pi can comfortably render;
    fbi's own autoscale bridges the gap - see install.sh).

    Known limitation, confirmed on a Pi Zero W running vc4-fkms-v3d with
    disable_fw_kms_setup=1 (this project's actual target hardware): once
    install.sh has forced a video= mode, this always returns no EDID data,
    not just early after boot - tested writing "detect" to the connector's
    status file (the standard DRM reprobe trick) and vcgencmd's HDMI
    commands, neither revives it. Not a bug to chase further here; the
    error message below tells the user what to do instead."""
    edid_path = find_connected_hdmi_edid()
    if not edid_path:
        return jsonify({"ok": False, "error": "No connected HDMI display detected."}), 404

    try:
        with open(edid_path, "rb") as f:
            edid_bytes = f.read()
    except OSError as e:
        return jsonify({"ok": False, "error": f"Couldn't read EDID: {e}"}), 500

    if not edid_bytes:
        return jsonify({
            "ok": False,
            "error": (
                "The display returned no EDID data. If a Display resolution is already forced "
                "(install.sh has run and written a video= line to cmdline.txt), this is expected - "
                "on this Pi's driver, forcing a mode stops it from reading EDID at all, live "
                "retries included. Read the resolution off the TV's own input-signal display and "
                "enter it by hand, or temporarily remove the video= line from cmdline.txt, reboot, "
                "and try again while watching for it to come up."
            ),
        }), 502

    result = parse_edid_preferred_resolution(edid_bytes)
    if not result:
        return jsonify({"ok": False, "error": "Couldn't find a usable preferred resolution in the display's EDID."}), 502

    width, height = result

    pi_model = detect_pi_model()
    cap = safe_render_long_edge(pi_model)
    render_width, render_height = width, height
    if cap and max(width, height) > cap:
        scale = cap / max(width, height)
        render_width, render_height = round(width * scale), round(height * scale)
    capped = (render_width, render_height) != (width, height)

    # display_width/height (the render canvas) follows the OPPOSITE
    # convention from hdmi_width/height: it's pre-rotation, swapped relative
    # to the panel's native orientation when rotation_degrees is 90/270 -
    # see the migration in load_config() for why (verified against the
    # original working setup). width/height above are the panel's raw
    # native EDID reading and go to hdmi_width/height unswapped; only this
    # response's render_width/render_height (bound for display_width/height)
    # need the swap applied here.
    rotation_degrees = load_config().get("rotation_degrees")
    if rotation_degrees in (90, 270):
        render_width, render_height = render_height, render_width

    return jsonify({
        "ok": True,
        "width": width,
        "height": height,
        "render_width": render_width,
        "render_height": render_height,
        "pi_model": pi_model,
        "capped": capped,
    })


@app.route("/settings", methods=["POST"])
def settings():
    config = load_config()
    classic = config["appearances"]["classic"]
    framed = config["appearances"]["framed"]

    active_appearance = request.form.get("active_appearance")
    if active_appearance in ("classic", "framed"):
        config["active_appearance"] = active_appearance

    minutes = request.form.get("interval_minutes", type=float)
    if minutes is not None and minutes > 0:
        config["interval_seconds"] = max(5.0, round(minutes * 60, 2))

    rotation = request.form.get("rotation_degrees", type=int)
    if rotation in (0, 90, 180, 270):
        config["rotation_degrees"] = rotation

    accent = request.form.get("accent_color", "").strip()
    if HEX_RE.match(accent):
        config["accent_color"] = accent

    max_w = request.form.get("poster_max_width", type=int)
    if max_w is not None:
        config["poster_max_width"] = max(400, min(3000, max_w))

    config["randomize_after_sync"] = "randomize_after_sync" in request.form

    boot_seconds = request.form.get("boot_image_seconds", type=float)
    if boot_seconds is not None:
        config["boot_image_seconds"] = max(0.0, min(30.0, boot_seconds))

    boot_height = request.form.get("boot_image_height_pct", type=float)
    if boot_height is not None:
        config["boot_image_height_pct"] = max(5, min(90, boot_height))

    boot_rotation = request.form.get("boot_image_rotation", type=int)
    if boot_rotation in (0, 90, 180, 270):
        config["boot_image_rotation"] = boot_rotation

    spinner_height = request.form.get("spinner_height_pct", type=float)
    if spinner_height is not None:
        config["spinner_height_pct"] = max(5, min(60, spinner_height))

    spinner_fps = request.form.get("spinner_fps", type=float)
    if spinner_fps is not None:
        config["spinner_fps"] = max(1, min(30, spinner_fps))

    brightness_pct = request.form.get("brightness_pct", type=float)
    if brightness_pct is not None:
        config["brightness"] = max(0.5, min(1.5, brightness_pct / 100))

    contrast_pct = request.form.get("contrast_pct", type=float)
    if contrast_pct is not None:
        config["contrast"] = max(0.5, min(1.5, contrast_pct / 100))

    saturation_pct = request.form.get("saturation_pct", type=float)
    if saturation_pct is not None:
        config["saturation"] = max(0.0, min(2.0, saturation_pct / 100))

    width = request.form.get("display_width", type=int)
    if width is not None and 100 <= width <= 8000:
        config["display_width"] = width

    height = request.form.get("display_height", type=int)
    if height is not None and 100 <= height <= 8000:
        config["display_height"] = height

    hdmi_width = request.form.get("hdmi_width", type=int)
    if hdmi_width is not None and 100 <= hdmi_width <= 8000:
        config["hdmi_width"] = hdmi_width

    hdmi_height = request.form.get("hdmi_height", type=int)
    if hdmi_height is not None and 100 <= hdmi_height <= 8000:
        config["hdmi_height"] = hdmi_height

    # --- Classic appearance ---
    c_text_color = request.form.get("classic_text_color", "").strip()
    if HEX_RE.match(c_text_color):
        classic["text_color"] = c_text_color

    c_band_bg = request.form.get("classic_band_background_color", "").strip()
    if HEX_RE.match(c_band_bg):
        classic["band_background_color"] = c_band_bg

    c_position = request.form.get("classic_poster_position")
    if c_position in ("top", "center", "bottom"):
        classic["poster_position"] = c_position

    c_top_content = request.form.get("classic_top_band_content")
    if c_top_content in ("none", "status", "date", "custom"):
        classic["top_band_content"] = c_top_content

    c_bottom_content = request.form.get("classic_bottom_band_content")
    if c_bottom_content in ("none", "status", "date", "custom"):
        classic["bottom_band_content"] = c_bottom_content

    classic["top_custom_text"] = request.form.get("classic_top_custom_text", "").strip()[:60]
    classic["bottom_custom_text"] = request.form.get("classic_bottom_custom_text", "").strip()[:60]

    c_text_size_pct = request.form.get("classic_text_size_pct", type=float)
    if c_text_size_pct is not None:
        classic["text_size_pct"] = max(50, min(200, c_text_size_pct))

    c_font_family = request.form.get("classic_display_font_family", "").strip()
    c_current_font = classic.get("display_font", {})
    if c_font_family != c_current_font.get("family", ""):
        if not c_font_family:
            classic["display_font"] = dict(EMPTY_FONT)
        else:
            font_info, status = fetch_and_cache_font(c_font_family)
            if font_info:
                classic["display_font"] = {"family": c_font_family, "status": status, **font_info}
            else:
                classic["display_font"] = {**c_current_font, "family": c_font_family, "status": status}

    # --- Framed appearance ---
    f_text_color = request.form.get("framed_text_color", "").strip()
    if HEX_RE.match(f_text_color):
        framed["text_color"] = f_text_color

    f_bg_color = request.form.get("framed_background_color", "").strip()
    if HEX_RE.match(f_bg_color):
        framed["background_color"] = f_bg_color

    f_position = request.form.get("framed_poster_position")
    if f_position in ("top", "center", "bottom"):
        framed["poster_position"] = f_position

    f_scale = request.form.get("framed_poster_scale_pct", type=float)
    if f_scale is not None:
        framed["poster_scale_pct"] = max(50, min(95, f_scale))

    f_top_content = request.form.get("framed_top_content")
    if f_top_content in ("none", "status", "date", "custom"):
        framed["top_content"] = f_top_content

    framed["top_custom_text"] = request.form.get("framed_top_custom_text", "").strip()[:60]

    f_cast_count = request.form.get("framed_cast_count", type=int)
    if f_cast_count is not None:
        framed["cast_count"] = max(0, min(10, f_cast_count))

    f_font_family = request.form.get("framed_top_font_family", "").strip()
    f_current_font = framed.get("top_font", {})
    if f_font_family != f_current_font.get("family", ""):
        if not f_font_family:
            framed["top_font"] = dict(EMPTY_FONT)
        else:
            font_info, status = fetch_and_cache_font(f_font_family)
            if font_info:
                framed["top_font"] = {"family": f_font_family, "status": status, **font_info}
            else:
                framed["top_font"] = {**f_current_font, "family": f_font_family, "status": status}

    # Same hidden-marker trick as the TMDb form: checkbox absence alone
    # can't distinguish "unchecked" from "different form entirely".
    if request.form.get("_schedule_form") == "1":
        config["schedule_enabled"] = "schedule_enabled" in request.form
        config["reboot_enabled"] = "reboot_enabled" in request.form

        for field in ("display_off_time", "display_on_time", "reboot_time"):
            value = request.form.get(field, "").strip()
            if TIME_RE.match(value):
                config[field] = value

    if request.form.get("_tmdb_form") == "1":
        config["tmdb_enabled"] = "tmdb_enabled" in request.form
        config["tmdb_schedule_enabled"] = "tmdb_schedule_enabled" in request.form

        config["tmdb_sources"] = {
            key: (f"source_{key}" in request.form) for key in TMDB_SOURCE_KEYS
        }

        max_per = request.form.get("tmdb_max_per_source", type=int)
        if max_per is not None:
            config["tmdb_max_per_source"] = max(1, min(20, max_per))

        limits = dict(config.get("tmdb_source_limits") or {})
        for key in TMDB_SOURCE_KEYS:
            value = request.form.get(f"limit_{key}", type=int)
            if value is not None:
                limits[key] = max(1, min(20, value))
        config["tmdb_source_limits"] = limits

        mode = request.form.get("tmdb_media_mode")
        if mode in ("movie", "tv"):
            config["tmdb_media_mode"] = mode

        config["poster_expiry_enabled"] = "poster_expiry_enabled" in request.form
        config["poster_expiry_include_pinned"] = "poster_expiry_include_pinned" in request.form

        expiry_days = request.form.get("poster_expiry_days", type=int)
        if expiry_days is not None:
            config["poster_expiry_days"] = max(1, min(3650, expiry_days))

        min_pop = request.form.get("tmdb_min_popularity", type=float)
        if min_pop is not None:
            config["tmdb_min_popularity"] = max(0, min(2000, min_pop))

        min_pop_up = request.form.get("tmdb_min_popularity_upcoming", type=float)
        if min_pop_up is not None:
            config["tmdb_min_popularity_upcoming"] = max(0, min(2000, min_pop_up))

        months = request.form.get("tmdb_upcoming_months", type=float)
        if months is not None:
            config["tmdb_upcoming_months"] = max(1, min(36, months))

    if request.form.get("_justwatch_form") == "1":
        source = request.form.get("discovery_source")
        if source in ("tmdb", "justwatch"):
            config["discovery_source"] = source

        sync_time = request.form.get("discovery_sync_time", "").strip()
        if TIME_RE.match(sync_time):
            config["discovery_sync_time"] = sync_time

        config["justwatch_enabled"] = "justwatch_enabled" in request.form
        config["justwatch_schedule_enabled"] = "justwatch_schedule_enabled" in request.form

        max_titles = request.form.get("justwatch_max_titles", type=int)
        if max_titles is not None:
            config["justwatch_max_titles"] = max(1, min(40, max_titles))

    if request.form.get("_plex_form") == "1":
        config["plex_enabled"] = "plex_enabled" in request.form

        poll_seconds = request.form.get("plex_poll_seconds", type=float)
        if poll_seconds is not None:
            config["plex_poll_seconds"] = max(5.0, min(120.0, poll_seconds))

        stop_delay = request.form.get("plex_stop_delay_seconds", type=int)
        if stop_delay is not None:
            config["plex_stop_delay_seconds"] = max(0, min(600, stop_delay))

        band = request.form.get("plex_band")
        if band in ("top", "bottom", "none"):
            classic["plex_band"] = band

    save_config(config)

    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
