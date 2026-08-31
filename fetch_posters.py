#!/usr/bin/env python3
"""
Syncs the poster frame's rotation with TMDb, across whichever categories
are enabled in the web UI.

Auto-fetched posters are named tmdb_<media>_<id>.jpg so this script can
tell them apart from anything uploaded manually - it only ever adds or
removes its own files. The media type is part of the name because a
movie ID and a TV ID can collide (movie 550 and TV 550 are different).
"""
import json
import os
import re
import time
from datetime import date, datetime, timedelta

import requests

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/original"

APP_BASE = "http://127.0.0.1:5000"
REGION = "AU"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKING_FILE = os.path.join(BASE_DIR, "tmdb_tracked.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

OLD_NAME_RE = re.compile(r"^\d+$")


def log(message):
    # Timestamped (matches the other log files' format) so a step's actual
    # wall-clock duration is readable directly off tmdb_sync.log. app.py
    # captures this process's stdout straight into that file, so this only
    # needs to print - no file handling here.
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


# region only affects movie endpoints - TMDb has no concept of regional
# popularity for TV, so passing it there is simply ignored.
# "discover" sources build their own query instead of using a preset
# endpoint. /movie/upcoming is a discover call with FIXED date bounds a
# few weeks out that can't be widened, which is why big releases many
# months away never appeared in it.
SOURCES = {
    "movie_trending":    {"path": "/trending/movie/week", "media": "movie"},
    "movie_popular":     {"path": "/movie/popular",       "media": "movie"},
    "movie_now_playing": {"path": "/movie/now_playing",   "media": "movie"},
    "movie_upcoming":    {"path": "/discover/movie",      "media": "movie",
                          "discover": "upcoming"},
    "tv_trending":       {"path": "/trending/tv/week",    "media": "tv"},
    "tv_popular":        {"path": "/tv/popular",          "media": "tv"},
    "tv_on_the_air":     {"path": "/tv/on_the_air",       "media": "tv"},
}

DEFAULT_SOURCES = {
    "movie_now_playing": True,
    "movie_upcoming": True,
}


POSTER_FILE_RE = re.compile(r"^tmdb(pin)?_(movie|tv)_(\d+)\.jpg$")


def fetch_now_playing_ids(api_key):
    """TMDb has no 'still in cinemas' flag, so its own now_playing list is
    the closest thing to an authoritative answer. Used as a reprieve: a
    title past its age limit but still in cinemas is kept."""
    if not api_key:
        return set()
    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/now_playing",
            params={"api_key": api_key, "language": "en-US",
                    "page": 1, "region": REGION},
            timeout=15,
        )
        resp.raise_for_status()
        return {int(m["id"]) for m in resp.json().get("results", [])}
    except (requests.RequestException, ValueError, KeyError):
        return set()


def expire_old_posters(config, api_key):
    """Removes TMDb posters whose release date is older than the limit.

    Returns the set of keys removed, so the sync in the same run can skip
    re-adding them - otherwise a still-popular old title would be deleted
    and immediately restored, churning every day."""
    if not config.get("poster_expiry_enabled", False):
        return set()

    days = int(config.get("poster_expiry_days", 90))
    include_pinned = config.get("poster_expiry_include_pinned", False)
    cutoff = date.today() - timedelta(days=days)

    still_playing = fetch_now_playing_ids(api_key)
    expired = set()

    for filename, info in list(config.get("poster_meta", {}).items()):
        match = POSTER_FILE_RE.match(filename)
        if not match:
            continue  # a manual upload - never auto-removed

        is_pinned = bool(match.group(1))
        media, item_id = match.group(2), int(match.group(3))

        if is_pinned and not include_pinned:
            continue

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

        if media == "movie" and item_id in still_playing:
            log(f"Keeping {title} - past the age limit but still in cinemas")
            continue

        try:
            remove_poster(filename, f"{title} (released {raw_date})")
            expired.add(f"{media}:{item_id}")
        except requests.RequestException as e:
            log(f"Could not expire {title}: {e}")

    return expired


def load_web_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def normalise(item, media):
    """TV and movie payloads use different field names for the same
    things, so flatten them into one shape."""
    if media == "tv":
        title = item.get("name")
        released = item.get("first_air_date", "")
    else:
        title = item.get("title")
        released = item.get("release_date", "")

    return {
        "key": f"{media}:{item['id']}",
        "filename": f"tmdb_{media}_{item['id']}.jpg",
        "title": title or "Untitled",
        "release_date": released or "",
        "poster_path": item.get("poster_path"),
    }


def fetch_source(source_key, max_items, min_popularity=0.0, upcoming_months=12):
    """Fetches one category.

    min_popularity filters out obscure titles. TMDb's popularity score is
    a relative daily metric - blockbusters sit well above 200, mid-tier
    around 20-80, and obscure titles under 10.
    """
    source = SOURCES[source_key]
    base_params = {"api_key": TMDB_API_KEY, "language": "en-US"}
    if source["media"] == "movie":
        base_params["region"] = REGION

    if source.get("discover") == "upcoming":
        today = date.today()
        horizon = today + timedelta(days=int(upcoming_months * 30.44))
        # release_date (not primary_release_date) is the right filter when
        # region is set - TMDb then uses the regional release date, so this
        # returns films actually scheduled for AU cinemas.
        base_params.update({
            "sort_by": "popularity.desc",
            "release_date.gte": today.isoformat(),
            "release_date.lte": horizon.isoformat(),
            "with_release_type": "2|3",   # theatrical, limited + wide
            "include_adult": "false",
        })

    items = []
    # A popularity floor can wipe out most of a page, so walk a few pages
    # to still end up with a useful number of results.
    max_pages = 3 if min_popularity > 0 else 1

    for page in range(1, max_pages + 1):
        params = dict(base_params, page=page)
        resp = requests.get(f"{TMDB_BASE}{source['path']}", params=params, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            break

        for raw in results:
            if float(raw.get("popularity") or 0) < min_popularity:
                continue
            item = normalise(raw, source["media"])
            if item["poster_path"]:
                items.append(item)
            if len(items) >= max_items:
                return items

    return items


def load_tracked():
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE) as f:
            return json.load(f)
    return {}


def save_tracked(tracked):
    with open(TRACKING_FILE, "w") as f:
        json.dump(tracked, f, indent=2)


def upload_poster(item):
    image_resp = requests.get(IMAGE_BASE + item["poster_path"], timeout=15)
    image_resp.raise_for_status()

    files = {"poster": (item["filename"], image_resp.content, "image/jpeg")}
    # Generous: the Pi has to decode a full-resolution poster, resize it,
    # and apply grain before responding. On a Zero W that can take well
    # over 30s, and timing out here is what strands half-added posters.
    resp = requests.post(f"{APP_BASE}/upload", files=files, timeout=180)
    resp.raise_for_status()
    log(f"Added: {item['title']}")


def dedupe(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def fetch_credits(media, tmdb_id, cast_count=4):
    """Director/writer(s)/producer(s)/composer/cast, for the Framed
    appearance's billing block. Movie and TV credits payloads shape crew
    differently - a movie crew entry has one flat 'job' string, a TV
    (aggregate_credits) entry has a 'jobs' list per person - so they're
    mapped separately. Any role TMDb has nothing for is simply left out of
    the returned dict; the billing block skips lines it has no data for."""
    if not TMDB_API_KEY:
        return {}
    try:
        if media == "movie":
            resp = requests.get(
                f"{TMDB_BASE}/movie/{tmdb_id}/credits",
                params={"api_key": TMDB_API_KEY}, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            crew = data.get("crew", [])
            director = next((c["name"] for c in crew if c.get("job") == "Director"), None)
            writers = [c["name"] for c in crew if c.get("job") in ("Screenplay", "Writer", "Story")]
            producers = [c["name"] for c in crew if c.get("job") == "Producer"]
            composer = next((c["name"] for c in crew if c.get("job") == "Original Music Composer"), None)
            cast = [c["name"] for c in sorted(data.get("cast", []), key=lambda c: c.get("order", 999))]
        else:
            # aggregate_credits covers the whole series, not just the latest
            # season - a plain /tv/{id}/credits call would silently drift to
            # whichever season TMDb currently considers "current".
            resp = requests.get(
                f"{TMDB_BASE}/tv/{tmdb_id}/aggregate_credits",
                params={"api_key": TMDB_API_KEY}, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            crew = data.get("crew", [])

            def has_job(person, *jobs):
                return any(j.get("job") in jobs for j in person.get("jobs", []))

            director = next((c["name"] for c in crew if has_job(c, "Director")), None)
            writers = [c["name"] for c in crew if has_job(c, "Writer", "Story")]
            producers = [c["name"] for c in crew if has_job(c, "Executive Producer", "Producer")]
            composer = next((c["name"] for c in crew if has_job(c, "Original Music Composer")), None)
            cast = [
                c["name"] for c in sorted(
                    data.get("cast", []), key=lambda c: -(c.get("total_episode_count") or 0)
                )
            ]
    except requests.RequestException:
        return {}

    credits = {}
    if director:
        credits["director"] = director
    if writers:
        credits["writers"] = dedupe(writers)[:3]
    if producers:
        credits["producers"] = dedupe(producers)[:3]
    if composer:
        credits["composer"] = composer
    if cast:
        credits["cast"] = dedupe(cast)[:cast_count]
    return credits


def update_poster_meta(item, credits=None):
    payload = {"release_date": item["release_date"], "title": item["title"]}
    if credits:
        payload["credits"] = credits
    resp = requests.post(
        f"{APP_BASE}/poster-meta/{item['filename']}",
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()


def remove_poster(filename, title):
    resp = requests.post(f"{APP_BASE}/delete/{filename}", timeout=15)
    resp.raise_for_status()
    log(f"Removed: {title}")


def migrate_old_entries(tracked):
    """Earlier versions named files tmdb_<id>.jpg with no media type.
    Clear those out so they don't linger untracked forever."""
    old_keys = [k for k in tracked if OLD_NAME_RE.match(str(k))]
    for key in old_keys:
        try:
            remove_poster(f"tmdb_{key}.jpg", f"{tracked[key]} (old format)")
        except requests.RequestException as e:
            log(f"Could not remove old entry {key}: {e}")
        del tracked[key]
    if old_keys:
        log(f"Migrated {len(old_keys)} poster(s) to the new naming scheme.")


def main():
    config = load_web_config()

    if not config.get("tmdb_enabled", True):
        log("TMDb integration is disabled in the web UI - skipping.")
        return

    if os.environ.get("POSTERFRAME_TRIGGER") == "schedule":
        if not config.get("tmdb_schedule_enabled", True):
            log("Scheduled sync is disabled in the web UI - skipping.")
            return

    if not TMDB_API_KEY:
        log("TMDB_API_KEY environment variable not set - aborting.")
        return

    expired = expire_old_posters(config, TMDB_API_KEY)

    sources = config.get("tmdb_sources") or DEFAULT_SOURCES
    default_limit = int(config.get("tmdb_max_per_source", 6))
    source_limits = config.get("tmdb_source_limits") or {}

    # Only one media type is active at a time. The other type's category
    # settings stay saved in config, they're just not consulted here.
    mode = config.get("tmdb_media_mode", "movie")
    if mode not in ("movie", "tv"):
        mode = "movie"

    enabled = [
        k for k, src in SOURCES.items()
        if src["media"] == mode and sources.get(k)
    ]
    if not enabled:
        label = "movie" if mode == "movie" else "TV"
        log(f"No {label} categories enabled - nothing to sync.")
        return

    limits_desc = ", ".join(
        f"{k} x{source_limits.get(k, default_limit)}" for k in enabled
    )
    log(f"Syncing {mode} categories: {limits_desc}")

    tracked = load_tracked()
    migrate_old_entries(tracked)

    # Collect across all enabled sources, de-duplicating: the same title
    # can easily appear in both trending and popular.
    wanted = {}
    min_popularity = float(config.get("tmdb_min_popularity", 0))
    min_pop_upcoming = float(config.get("tmdb_min_popularity_upcoming", 10))
    upcoming_months = float(config.get("tmdb_upcoming_months", 12))

    for source_key in enabled:
        # Unreleased films score far lower than released ones - popularity
        # reflects current attention, and a film with no audience yet has
        # little of it regardless of how big it will be. So upcoming gets
        # its own, much lower threshold.
        is_upcoming = SOURCES[source_key].get("discover") == "upcoming"
        threshold = min_pop_upcoming if is_upcoming else min_popularity

        try:
            limit = int(source_limits.get(source_key, default_limit))
        except (TypeError, ValueError):
            limit = default_limit
        limit = max(1, min(20, limit))

        try:
            for item in fetch_source(source_key, limit,
                                      threshold, upcoming_months):
                if item["key"] in expired:
                    continue  # aged out this run - don't bring it straight back
                wanted.setdefault(item["key"], item)
        except requests.RequestException as e:
            log(f"Failed to fetch {source_key}: {e}")

    if not wanted:
        log("No results returned - leaving the current rotation alone.")
        save_tracked(tracked)
        return

    for key, item in wanted.items():
        if key not in tracked:
            try:
                upload_poster(item)
                tracked[key] = item["title"]
            except requests.RequestException as e:
                log(f"Failed to add {item['title']}: {e}")
                continue

        try:
            media, _, tmdb_id = key.partition(":")
            cast_count = config.get("appearances", {}).get("framed", {}).get("cast_count", 4)
            credits = fetch_credits(media, tmdb_id, cast_count)
            update_poster_meta(item, credits)
        except requests.RequestException as e:
            log(f"Failed to update metadata for {item['title']}: {e}")

    for key in list(tracked.keys()):
        if key in expired:
            del tracked[key]
            continue
        if key not in wanted:
            media, _, item_id = key.partition(":")
            try:
                remove_poster(f"tmdb_{media}_{item_id}.jpg", tracked[key])
            except requests.RequestException as e:
                log(f"Failed to remove {tracked[key]}: {e}")
            del tracked[key]

    save_tracked(tracked)
    log(f"Done. Tracking {len(tracked)} auto-fetched poster(s) "
          f"across {len(enabled)} categor{'y' if len(enabled) == 1 else 'ies'}.")


if __name__ == "__main__":
    main()
