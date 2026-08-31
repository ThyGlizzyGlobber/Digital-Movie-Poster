#!/usr/bin/env python3
"""
Syncs the poster frame's rotation with JustWatch's "Popular" movies list -
an alternative discovery source to TMDb, since JustWatch's popularity
ranking is often more accurate for what's actually trending than TMDb's own
metric. Filtered to the current release year only, recomputed on every run
(date.today().year, never hardcoded) so it advances on its own when the
year flips over - no year-end maintenance needed.

JustWatch has no public API for this. The movies page is server-rendered
though: the ranked list ships embedded as a `window.__APOLLO_STATE__` JSON
blob in the plain HTML response, so a plain requests.get() sees it - no
headless browser, which matters on a Pi Zero W. Confirmed by inspecting a
live response before building this: sortBy defaults to "POPULAR" already,
and title order in the embedded state matched the rendered page exactly.

JustWatch supplies discovery only (title, release year, popularity rank),
never poster art: its own internal id isn't the TMDb id (checked directly -
JustWatch's id for a title and that same title's real TMDb id are unrelated
numbers), so every title is resolved to a real TMDb id via a title+year
search, then handed to fetch_posters.py's existing upload/credits/metadata
helpers. That keeps this file thin, and means a JustWatch-sourced poster is,
underneath, still a TMDb poster - same image quality, same credits pipeline,
just discovered differently.

Auto-fetched posters are named justwatch_movie_<tmdb_id>.jpg, entirely
separate from tmdb_movie_<id>.jpg, so switching the active discovery source
never collides the two - each keeps its own independent tracking file.

Only one discovery source runs at a time (config["discovery_source"]), by
design - both this script and fetch_posters.py check it and skip
themselves out if they're not the selected one, so nothing has to
coordinate to avoid the two fighting over the rotation.
"""
import json
import os
import re
from datetime import date, datetime, timedelta

import requests

import fetch_posters  # reuse upload_poster / fetch_credits / update_poster_meta / remove_poster / fetch_now_playing_ids

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"
JUSTWATCH_URL = "https://www.justwatch.com/us/movies"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKING_FILE = os.path.join(BASE_DIR, "justwatch_tracked.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

POSTER_FILE_RE = re.compile(r"^justwatch_movie_(\d+)\.jpg$")

# JustWatch's SSR payload isn't paginated beyond the first batch without a
# client-side GraphQL call (no plain "page 2" URL) - the first batch is
# consistently plenty once filtered to this year (every sampled title in it
# was already this year's release), so pagination is left for later if it
# ever turns out not to be.
MAX_RAW_TITLES = 200


def load_web_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_tracked():
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE) as f:
            return json.load(f)
    return {}


def save_tracked(tracked):
    with open(TRACKING_FILE, "w") as f:
        json.dump(tracked, f, indent=2)


def _extract_json_object(html, start_index):
    """Bracket-counts from the first '{' at/after start_index to its
    matching close, string-aware so braces inside quoted values don't
    throw off the depth count. Used instead of a regex terminator, which
    isn't reliable against a blob this large and this variable."""
    i = start_index
    while html[i] != "{":
        i += 1
    obj_start = i
    depth = 0
    in_str = False
    esc = False
    while i < len(html):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    return html[obj_start:i]
        i += 1
    raise ValueError("Unbalanced JSON object in page")


def fetch_popular_titles():
    """Returns [{title, year}, ...] in JustWatch's own popularity order."""
    resp = requests.get(JUSTWATCH_URL, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (compatible; PosterFrame/1.0; personal use)",
    })
    resp.raise_for_status()
    html = resp.text

    marker = "window.__APOLLO_STATE__="
    start = html.find(marker)
    if start == -1:
        raise ValueError("JustWatch page did not contain the expected data - their site markup may have changed.")

    state = json.loads(_extract_json_object(html, start + len(marker))).get("defaultClient", {})
    movie_keys = [k for k in state if k.startswith("Movie:")]

    titles = []
    for mk in movie_keys:
        movie = state[mk]
        content_key = next((k for k in movie if k.startswith("content(")), None)
        if not content_key:
            continue
        content_ref = movie[content_key]
        content = state.get((content_ref or {}).get("id"))
        if not content or not content.get("title"):
            continue
        titles.append({"title": content["title"], "year": content.get("originalReleaseYear")})
        if len(titles) >= MAX_RAW_TITLES:
            break

    return titles


def resolve_tmdb_id(title, year):
    """Title+year search against TMDb - the only bridge between JustWatch's
    id space and TMDb's. Prefers an exact (case-insensitive) title match in
    the given year; falls back to TMDb's own top search result, since
    search is already relevance-ranked, rather than giving up."""
    if not TMDB_API_KEY:
        return None
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}
    if year:
        params["year"] = year
    try:
        resp = requests.get(f"{TMDB_BASE}/search/movie", params=params, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException:
        return None
    if not results:
        return None

    lowered = title.strip().lower()
    for r in results:
        if (r.get("title") or "").strip().lower() == lowered:
            return r
    return results[0]


def expire_old_posters(config):
    """Mirrors fetch_posters.py's expiry against the same shared
    poster_expiry_* settings - one cleanup policy for whichever source is
    active, rather than a second set of controls to keep in sync."""
    if not config.get("poster_expiry_enabled", False):
        return set()

    days = int(config.get("poster_expiry_days", 90))
    cutoff = date.today() - timedelta(days=days)
    still_playing = fetch_posters.fetch_now_playing_ids(TMDB_API_KEY)

    expired = set()
    for filename, info in list(config.get("poster_meta", {}).items()):
        match = POSTER_FILE_RE.match(filename)
        if not match:
            continue  # not a JustWatch-sourced file

        item_id = int(match.group(1))
        raw_date = (info or {}).get("release_date")
        if not raw_date:
            continue
        try:
            released = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if released > cutoff:
            continue

        title = (info or {}).get("title", filename)
        if item_id in still_playing:
            print(f"Keeping {title} - past the age limit but still in cinemas", flush=True)
            continue

        try:
            fetch_posters.remove_poster(filename, f"{title} (released {raw_date})")
            expired.add(str(item_id))
        except requests.RequestException as e:
            print(f"Could not expire {title}: {e}", flush=True)

    return expired


def main():
    config = load_web_config()

    if not config.get("justwatch_enabled", False):
        print("JustWatch integration is disabled in the web UI - skipping.", flush=True)
        return

    if config.get("discovery_source", "tmdb") != "justwatch":
        print("JustWatch is not the active discovery source - skipping.", flush=True)
        return

    if os.environ.get("POSTERFRAME_TRIGGER") == "schedule":
        if not config.get("justwatch_schedule_enabled", True):
            print("Scheduled sync is disabled in the web UI - skipping.", flush=True)
            return

    if not TMDB_API_KEY:
        # Discovery comes from JustWatch, but poster art and credits still
        # come from TMDb, so its key is required either way.
        print("TMDB_API_KEY environment variable not set - aborting.", flush=True)
        return

    expired = expire_old_posters(config)

    max_titles = max(1, min(40, int(config.get("justwatch_max_titles", 10))))
    this_year = date.today().year

    try:
        candidates = fetch_popular_titles()
    except (requests.RequestException, ValueError) as e:
        print(f"Failed to fetch JustWatch's popular list: {e}", flush=True)
        return

    this_year_titles = [c for c in candidates if c.get("year") == this_year]
    print(f"JustWatch: {len(candidates)} popular title(s) fetched, "
          f"{len(this_year_titles)} released in {this_year}.", flush=True)

    tracked = load_tracked()
    wanted = {}

    for entry in this_year_titles:
        if len(wanted) >= max_titles:
            break

        result = resolve_tmdb_id(entry["title"], entry["year"])
        if not result or not result.get("poster_path"):
            print(f"Could not match on TMDb: {entry['title']} ({entry['year']})", flush=True)
            continue

        key = str(result["id"])
        if key in expired:
            continue

        wanted[key] = {
            "key": key,
            "filename": f"justwatch_movie_{key}.jpg",
            "title": result.get("title") or entry["title"],
            "release_date": result.get("release_date") or "",
            "poster_path": result["poster_path"],
        }

    if not wanted:
        print("No results resolved - leaving the current rotation alone.", flush=True)
        save_tracked(tracked)
        return

    for key, item in wanted.items():
        if key not in tracked:
            try:
                fetch_posters.upload_poster(item)
                tracked[key] = item["title"]
            except requests.RequestException as e:
                print(f"Failed to add {item['title']}: {e}", flush=True)
                continue

        try:
            cast_count = config.get("appearances", {}).get("framed", {}).get("cast_count", 4)
            credits = fetch_posters.fetch_credits("movie", key, cast_count)
            fetch_posters.update_poster_meta(item, credits)
        except requests.RequestException as e:
            print(f"Failed to update metadata for {item['title']}: {e}", flush=True)

    for key in list(tracked.keys()):
        if key in expired:
            del tracked[key]
            continue
        if key not in wanted:
            try:
                fetch_posters.remove_poster(f"justwatch_movie_{key}.jpg", tracked[key])
            except requests.RequestException as e:
                print(f"Failed to remove {tracked[key]}: {e}", flush=True)
            del tracked[key]

    save_tracked(tracked)
    print(f"Done. Tracking {len(tracked)} JustWatch-sourced poster(s).", flush=True)


if __name__ == "__main__":
    main()
