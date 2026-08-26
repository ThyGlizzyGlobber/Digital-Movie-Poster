import os
import re
import json
import colorsys
import subprocess
from datetime import date, datetime
import numpy as np
import requests
from flask import Flask, request, redirect, url_for, render_template, jsonify
from werkzeug.utils import secure_filename
from PIL import Image, ImageFilter, ImageEnhance

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POSTER_DIR = os.path.join(BASE_DIR, "static", "posters")
ORIGINAL_DIR = os.path.join(BASE_DIR, "originals")
FONT_CACHE_DIR = os.path.join(BASE_DIR, "fonts_cache")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")
TMDB_SYNC_LOG = os.path.join(BASE_DIR, "tmdb_sync.log")
UPDATE_SCRIPT = os.path.join(BASE_DIR, "update.sh")
UPDATE_LOG = os.path.join(BASE_DIR, "update.log")
BOOT_IMAGE_PATH = os.path.join(BASE_DIR, "static", "boot_image.png")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg"}
DEFAULT_INTERVAL_SECONDS = 900  # 15 minutes
DEFAULT_ACCENT = "#5b8cff"
DEFAULT_BAND_BG = "#0a0a0b"
DEFAULT_GRAIN_INTENSITY = 0.08
DEFAULT_POSTER_MAX_WIDTH = 1600
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Commit title convention: "[v1.4.0] Fix schedule wake bug". update.sh only
# ever hands back the raw subject line (CURRENT_MSG/REMOTE_MSG) - parsing
# happens here so there's one implementation of the convention, not two.
VERSION_TAG_RE = re.compile(r"^\[v(\d+\.\d+\.\d+)\]\s*(.*)$", re.IGNORECASE)

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
        "poster_position": "center",
        "top_band_content": "status",
        "bottom_band_content": "date",
        "top_custom_text": "",
        "bottom_custom_text": "",
        "text_size_pct": 100,
        "display_font": {"family": "", "status": "", "path": None, "bold_path": None, "is_variable": False},
        "grain_intensity": DEFAULT_GRAIN_INTENSITY,
        "tmdb_enabled": True,
        "tmdb_schedule_enabled": True,
        "band_background_color": DEFAULT_BAND_BG,
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
    }

    changed = False
    for key, value in defaults.items():
        if key not in config:
            config[key] = value
            changed = True

    if "text_color" not in config:
        config["text_color"] = config.get("accent_color", DEFAULT_ACCENT)
        changed = True

    if changed:
        save_config(config)

    return config


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


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


def describe_poster(filename, meta):
    """Classifies a poster for the web UI list: where it came from, and
    whether it's out yet. Status uses the same release-date logic the
    physical display uses for its NOW SHOWING / UPCOMING band."""
    if filename.startswith("tmdbpin_"):
        kind = "pinned"
    elif filename.startswith("tmdb_"):
        kind = "tmdb"
    else:
        kind = "manual"

    info = meta.get(filename) or {}
    raw_date = info.get("release_date")
    status = "none"

    if raw_date:
        try:
            released = datetime.strptime(raw_date, "%Y-%m-%d").date()
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


def prepare_poster(image, intensity, max_width):
    """Downscale to the working width, then apply grain.

    Order matters: graining a full-resolution image and shrinking it
    afterwards both wastes time and blurs the grain into mush."""
    img = image.convert("RGB")

    if max_width and img.width > max_width:
        ratio = max_width / img.width
        img = img.resize(
            (max_width, max(1, round(img.height * ratio))), Image.LANCZOS
        )

    return apply_film_grain(img, intensity=intensity)


@app.route("/")
def index():
    poster_files = get_ordered_posters()
    config = load_config()
    poster_meta_map = config.get("poster_meta", {})
    posters = [describe_poster(f, poster_meta_map) for f in poster_files]

    interval_minutes = round(config.get("interval_seconds", DEFAULT_INTERVAL_SECONDS) / 60, 2)
    rotation_degrees = config.get("rotation_degrees", 0)

    accent_color = config.get("accent_color", DEFAULT_ACCENT)
    if not HEX_RE.match(accent_color):
        accent_color = DEFAULT_ACCENT

    text_color = config.get("text_color", accent_color)
    if not HEX_RE.match(text_color):
        text_color = accent_color

    band_bg_color = config.get("band_background_color", DEFAULT_BAND_BG)
    if not HEX_RE.match(band_bg_color):
        band_bg_color = DEFAULT_BAND_BG

    brightness_pct = round(config.get("brightness", 1.0) * 100)
    contrast_pct = round(config.get("contrast", 1.0) * 100)
    saturation_pct = round(config.get("saturation", 1.0) * 100)
    display_width = config.get("display_width", 1080)
    display_height = config.get("display_height", 1920)
    poster_position = config.get("poster_position", "center")
    top_band_content = config.get("top_band_content", "status")
    bottom_band_content = config.get("bottom_band_content", "date")
    top_custom_text = config.get("top_custom_text", "")
    bottom_custom_text = config.get("bottom_custom_text", "")
    text_size_pct = config.get("text_size_pct", 100)
    display_font = config.get("display_font", {})
    grain_intensity = config.get("grain_intensity", DEFAULT_GRAIN_INTENSITY)
    tmdb_enabled = config.get("tmdb_enabled", True)
    tmdb_schedule_enabled = config.get("tmdb_schedule_enabled", True)
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
    git_sha, git_message, git_version = get_git_info()

    return render_template(
        "index.html",
        posters=posters,
        interval_minutes=interval_minutes,
        rotation_degrees=rotation_degrees,
        accent_color=accent_color,
        accent_dim=adjust_lightness(accent_color, 0.6),
        accent_hover=adjust_lightness(accent_color, 1.2),
        accent_text=contrasting_text_color(accent_color),
        text_color=text_color,
        band_bg_color=band_bg_color,
        brightness_pct=brightness_pct,
        contrast_pct=contrast_pct,
        saturation_pct=saturation_pct,
        display_width=display_width,
        display_height=display_height,
        poster_position=poster_position,
        top_band_content=top_band_content,
        bottom_band_content=bottom_band_content,
        top_custom_text=top_custom_text,
        bottom_custom_text=bottom_custom_text,
        text_size_pct=text_size_pct,
        display_font_family=display_font.get("family", ""),
        display_font_status=display_font.get("status", ""),
        grain_intensity=grain_intensity,
        tmdb_enabled=tmdb_enabled,
        tmdb_schedule_enabled=tmdb_schedule_enabled,
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
    )


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("poster")
    if not file or not file.filename or not allowed_file(file.filename):
        return redirect(url_for("index"))

    config = load_config()
    intensity = config.get("grain_intensity", DEFAULT_GRAIN_INTENSITY)
    max_width = config.get("poster_max_width", DEFAULT_POSTER_MAX_WIDTH)

    filename = secure_filename(file.filename)

    image = Image.open(file.stream)
    image.load()

    # Originals are kept at full resolution so raising the working width
    # later just needs a re-grain, not a re-download.
    image.convert("RGB").save(os.path.join(ORIGINAL_DIR, filename))

    prepare_poster(image, intensity, max_width).save(os.path.join(POSTER_DIR, filename))

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

    config = load_config()
    config["grain_intensity"] = intensity
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
                           config.get("poster_max_width", DEFAULT_POSTER_MAX_WIDTH)
                           ).save(os.path.join(POSTER_DIR, filename))
            reprocessed += 1
        except Exception as e:
            print(f"Failed to regrain {filename}: {e}")

    print(f"Reprocessed {reprocessed} poster(s) at grain intensity {intensity}")

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
    config["poster_meta"][safe_name] = {
        "release_date": data.get("release_date"),
        "title": data.get("title"),
    }
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


TMDB_URL_RE = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)")


@app.route("/tmdb-add-link", methods=["POST"])
def tmdb_add_link():
    """Pin a specific title by TMDb URL.

    These are saved with a 'tmdbpin_' prefix rather than 'tmdb_', which
    keeps them outside the auto-sync's bookkeeping - so a pinned title
    survives the next sync instead of being cleaned up as 'no longer
    trending'."""
    raw = request.form.get("tmdb_url", "").strip()
    match = TMDB_URL_RE.search(raw)

    if not match:
        alt = re.match(r"^(movie|tv)[\s/:]+(\d+)$", raw, re.IGNORECASE)
        if not alt:
            return redirect(url_for("index"))
        media, item_id = alt.group(1).lower(), alt.group(2)
    else:
        media, item_id = match.group(1), match.group(2)

    api_key = load_env_file(ENV_PATH).get("TMDB_API_KEY", "")
    if not api_key:
        return redirect(url_for("index"))

    try:
        detail = requests.get(
            f"https://api.themoviedb.org/3/{media}/{item_id}",
            params={"api_key": api_key, "language": "en-US"},
            timeout=15,
        )
        detail.raise_for_status()
        data = detail.json()

        poster_path = data.get("poster_path")
        if not poster_path:
            return redirect(url_for("index"))

        title = data.get("title") or data.get("name") or "Untitled"
        released = data.get("release_date") or data.get("first_air_date") or ""

        image = requests.get(f"https://image.tmdb.org/t/p/original{poster_path}", timeout=15)
        image.raise_for_status()
    except requests.RequestException as e:
        print(f"TMDb link add failed: {e}")
        return redirect(url_for("index"))

    filename = f"tmdbpin_{media}_{item_id}.jpg"
    config = load_config()
    intensity = config.get("grain_intensity", DEFAULT_GRAIN_INTENSITY)

    from io import BytesIO
    source = Image.open(BytesIO(image.content))
    source.load()

    source.convert("RGB").save(os.path.join(ORIGINAL_DIR, filename))
    prepare_poster(source, intensity,
        config.get("poster_max_width", DEFAULT_POSTER_MAX_WIDTH)).save(os.path.join(POSTER_DIR, filename))

    if filename not in config["order"]:
        config["order"].append(filename)
    config.setdefault("poster_meta", {})[filename] = {
        "release_date": released, "title": title,
    }
    save_config(config)
    print(f"Pinned: {title}")

    return redirect(url_for("index"))


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


@app.route("/update", methods=["POST"])
def update_now():
    """Pulls latest and restarts services - the actual "Update now" action.

    Runs update.sh detached and returns a standby page immediately, since
    the last thing update.sh does is restart posterframe-web itself. See
    update.sh's own comments for why that's safe despite killing this
    request's own process tree."""
    if not os.path.exists(UPDATE_SCRIPT):
        return redirect(url_for("index"))

    already_running = subprocess.run(
        ["pgrep", "-f", UPDATE_SCRIPT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0

    if not already_running:
        with open(UPDATE_LOG, "w") as log_file:
            subprocess.Popen(
                # 900s not 600s: a system-file update also runs install.sh
                # (apt + pip), which needs more room than a code-only pull.
                ["timeout", "900", "bash", UPDATE_SCRIPT],
                cwd=BASE_DIR,
                stdout=log_file, stderr=subprocess.STDOUT,
                start_new_session=True,
            )

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
    running = subprocess.run(
        ["pgrep", "-f", UPDATE_SCRIPT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0

    content = ""
    if os.path.exists(UPDATE_LOG):
        with open(UPDATE_LOG, errors="replace") as f:
            content = f.read()[-4000:]

    return jsonify({"running": running, "content": content})


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


@app.route("/settings", methods=["POST"])
def settings():
    config = load_config()

    minutes = request.form.get("interval_minutes", type=float)
    if minutes is not None and minutes > 0:
        config["interval_seconds"] = max(5.0, round(minutes * 60, 2))

    rotation = request.form.get("rotation_degrees", type=int)
    if rotation in (0, 90, 180, 270):
        config["rotation_degrees"] = rotation

    accent = request.form.get("accent_color", "").strip()
    if HEX_RE.match(accent):
        config["accent_color"] = accent

    text_color = request.form.get("text_color", "").strip()
    if HEX_RE.match(text_color):
        config["text_color"] = text_color

    band_bg = request.form.get("band_background_color", "").strip()
    if HEX_RE.match(band_bg):
        config["band_background_color"] = band_bg

    max_w = request.form.get("poster_max_width", type=int)
    if max_w is not None:
        config["poster_max_width"] = max(400, min(3000, max_w))

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

    position = request.form.get("poster_position")
    if position in ("top", "center", "bottom"):
        config["poster_position"] = position

    top_content = request.form.get("top_band_content")
    if top_content in ("none", "status", "date", "custom"):
        config["top_band_content"] = top_content

    bottom_content = request.form.get("bottom_band_content")
    if bottom_content in ("none", "status", "date", "custom"):
        config["bottom_band_content"] = bottom_content

    config["top_custom_text"] = request.form.get("top_custom_text", "").strip()[:60]
    config["bottom_custom_text"] = request.form.get("bottom_custom_text", "").strip()[:60]

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

    text_size_pct = request.form.get("text_size_pct", type=float)
    if text_size_pct is not None:
        config["text_size_pct"] = max(50, min(200, text_size_pct))

    font_family = request.form.get("display_font_family", "").strip()
    current_font = config.get("display_font", {})
    if font_family != current_font.get("family", ""):
        if not font_family:
            config["display_font"] = {"family": "", "status": "", "path": None, "bold_path": None, "is_variable": False}
        else:
            font_info, status = fetch_and_cache_font(font_family)
            if font_info:
                config["display_font"] = {"family": font_family, "status": status, **font_info}
            else:
                config["display_font"] = {**current_font, "family": font_family, "status": status}

    save_config(config)

    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
