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
import random
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
    """Removes TMDb posters that have been sitting in the rotation longer
    than the limit - measured from when each poster was actually added
    (poster_meta's added_date, stamped once by /poster-meta and never
    overwritten), not from the movie's real release date.

    This used to key off release_date instead, which broke badly for
    Trending/Popular: those categories can surface a film that came out
    months ago, and a poster like that would be born already past the
    cutoff - expired again on the very next sync, sometimes under 24 hours
    after being added. The age limit is meant to bound how long something
    sits on your frame, not how old the movie is.

    A poster with no added_date yet (mid-migration, or the very run it was
    first added on, before this run's later metadata-refresh step stamps
    it) is treated as added today rather than skipped or aged out from
    unknown data - it simply isn't old enough to expire yet either way.

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

        raw_added = (info or {}).get("added_date")
        try:
            added = datetime.strptime(raw_added, "%Y-%m-%d").date() if raw_added else date.today()
        except ValueError:
            added = date.today()

        if added > cutoff:
            continue

        title = (info or {}).get("title", filename)

        if media == "movie" and item_id in still_playing:
            log(f"Keeping {title} - past the age limit but still in cinemas")
            continue

        try:
            remove_poster(filename, f"{title} (added {raw_added or 'unknown date'})")
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


def fetch_source(source_key, max_items, min_popularity=0.0, upcoming_months=12, api_key=None):
    """Fetches one category.

    min_popularity filters out obscure titles. TMDb's popularity score is
    a relative daily metric - blockbusters sit well above 200, mid-tier
    around 20-80, and obscure titles under 10.

    api_key: see fetch_credits' docstring - same reason (app.py calling this
    directly for the Discovery preview grid, rather than as a subprocess with
    the key injected into its environment) needs the same override.
    """
    api_key = api_key or TMDB_API_KEY
    source = SOURCES[source_key]
    base_params = {"api_key": api_key, "language": "en-US"}
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


def fetch_credits(media, tmdb_id, cast_count=4, api_key=None):
    """Director/writer(s)/producer(s)/composer/cast, for the Framed
    appearance's billing block. Movie and TV credits payloads shape crew
    differently - a movie crew entry has one flat 'job' string, a TV
    (aggregate_credits) entry has a 'jobs' list per person - so they're
    mapped separately. Any role TMDb has nothing for is simply left out of
    the returned dict; the billing block skips lines it has no data for.

    api_key defaults to this module's own TMDB_API_KEY (read from the
    environment this process was launched with, e.g. by app.py's Popen when
    running as a sync script). app.py itself never has that env var set -
    it reads the key from .env per-request - so it calls this with an
    explicit api_key instead, letting it reuse this function directly rather
    than duplicating TMDb's credits-fetch logic a third time."""
    api_key = api_key or TMDB_API_KEY
    if not api_key:
        return {}
    try:
        if media == "movie":
            resp = requests.get(
                f"{TMDB_BASE}/movie/{tmdb_id}/credits",
                params={"api_key": api_key}, timeout=15,
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
                params={"api_key": api_key}, timeout=15,
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


def fetch_digital_release_date(tmdb_id, api_key=None):
    """Earliest US digital (release type 4) date for a movie, or None if
    TMDb has no digital release recorded yet - used so the NOW SHOWING /
    UPCOMING status reflects "can this actually be watched" rather than
    just "has it hit cinemas".

    US specifically, not the app's own AU region: checked directly against
    TMDb's real data before building this - AU-specific digital dates are
    almost never filled in (1 of 11 sampled current titles had one), while
    US coverage was complete across the same sample. US is a genuinely
    different market with its own release windowing, so the date itself
    can be off by weeks from the true AU date, but it's the only signal
    TMDb reliably has - AU-only would leave most titles reading COMING
    SOON long after they're actually available.

    TV has no equivalent on TMDb (release_dates is a movie-only endpoint) -
    callers should only invoke this for media == "movie"."""
    api_key = api_key or TMDB_API_KEY
    if not api_key:
        return None
    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/{tmdb_id}/release_dates",
            params={"api_key": api_key}, timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException:
        return None

    us = next((e for e in results if e.get("iso_3166_1") == "US"), None)
    if not us:
        return None

    # TMDb dates come back as full ISO datetimes (e.g. "2026-11-17T00:00:00.000Z") -
    # trimmed to match the plain YYYY-MM-DD this app stores release_date as
    # everywhere else. A title can have multiple digital releases (rental vs
    # buy, or a re-release) - the earliest is the one that answers "when did
    # this become watchable".
    digital_dates = [
        rd["release_date"][:10] for rd in us.get("release_dates", [])
        if rd.get("type") == 4 and rd.get("release_date")
    ]
    return min(digital_dates) if digital_dates else None


def update_poster_meta(item, credits=None, digital_release_date=None):
    payload = {"release_date": item["release_date"], "title": item["title"]}
    if credits:
        payload["credits"] = credits
    if digital_release_date:
        payload["digital_release_date"] = digital_release_date
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


RANDOMIZE_DELAY_SECONDS = 90


def maybe_randomize_order(config):
    """Shared by both discovery sources (fetch_justwatch.py calls this too).
    Runs at the very end of a sync, only once there's actually nothing left
    to do - both callers already run as a detached background process
    (app.py's Popen for "Sync now", or the scheduler timer), so blocking
    here for the delay doesn't hold up a web request or anything else.
    The delay itself exists so a sync that just added several new posters
    doesn't shuffle them in mid-composite - see CLAUDE.md's pipeline
    section: /upload's response (and so this script's own upload_poster
    call) already waits for resize+grain to finish, but slideshow.py still
    needs its own next 3s poll plus however long compositing the new
    entries takes before they're actually ready to display."""
    if not config.get("randomize_after_sync", False):
        return

    log(f"Auto-randomise is on - waiting {RANDOMIZE_DELAY_SECONDS}s before shuffling the order")
    time.sleep(RANDOMIZE_DELAY_SECONDS)

    order = list(load_web_config().get("order", []))
    if len(order) < 2:
        return
    random.shuffle(order)

    try:
        resp = requests.post(f"{APP_BASE}/reorder", json={"order": order}, timeout=15)
        resp.raise_for_status()
        log("Rotation order randomised.")
    except requests.RequestException as e:
        log(f"Could not randomise order: {e}")


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

    # expire_old_posters() already deleted the actual file for anything in
    # `expired` - this just keeps the tracking JSON in sync with that. Done
    # unconditionally, before the "nothing new resolved" early return below,
    # so a run that expires a title but finds zero new candidates doesn't
    # leave a phantom tracked entry pointing at a poster that's already gone
    # (which would then block a legitimate future re-add of that same title,
    # since the "already tracked" check further down would wrongly skip it).
    for key in list(tracked.keys()):
        if key in expired:
            del tracked[key]

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
        maybe_randomize_order(config)
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
            # TV has no release_dates equivalent on TMDb - only movies.
            digital_release_date = fetch_digital_release_date(tmdb_id) if media == "movie" else None
            update_poster_meta(item, credits, digital_release_date)
        except requests.RequestException as e:
            log(f"Failed to update metadata for {item['title']}: {e}")

    # Ranking-based eviction - deliberately only when expiry is off. With
    # expiry on, age (expire_old_posters, above) is the sole removal
    # criterion - this loop used to run unconditionally, and a title could
    # get pulled just for slipping a few spots in Trending/Popular that
    # week, regardless of poster_expiry_days. With expiry off there's no
    # such conflict - nothing else ever removes anything, so without this
    # the rotation would just grow forever as new titles are discovered.
    # This restores the original "stay capped per category, newest
    # climbers bump out whoever's no longer in today's top ranking"
    # behavior, but only for that one specific case.
    if not config.get("poster_expiry_enabled", False):
        for key in list(tracked.keys()):
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
    maybe_randomize_order(config)


if __name__ == "__main__":
    main()
