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

import fetch_posters  # reuse upload_poster / fetch_credits / update_poster_meta / remove_poster / fetch_now_playing_ids / log

log = fetch_posters.log  # same timestamped format, no need for a second copy

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


NUXT_DATA_RE = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>')


def _resolve_nuxt_payload(raw_array):
    """Nuxt 3's __NUXT_DATA__ payload is one flat JSON array: every unique
    value (string, number, dict, list - even True/False) gets its own slot,
    and every dict/list *value* elsewhere in the array is an integer index
    into this same array rather than an inline literal (confirmed live -
    e.g. a field genuinely valued 12 is stored as its own slot and
    referenced by index, not written inline). This walks that reference
    graph back into an ordinary nested Python structure, starting from
    slot 0 (the root), memoizing since the whole point of the format is
    heavy value sharing."""
    memo = {}

    def deref(i):
        if not isinstance(i, int) or not (0 <= i < len(raw_array)):
            return i
        if i in memo:
            return memo[i]
        val = raw_array[i]
        if isinstance(val, list):
            out = []
            memo[i] = out
            out.extend(deref(item) for item in val)
            return out
        if isinstance(val, dict):
            out = {}
            memo[i] = out
            out.update((k, deref(v)) for k, v in val.items())
            return out
        memo[i] = val
        return val

    return deref(0)


def _unwrap_shallow_reactive(value):
    """Nuxt wraps several payload sections as ["ShallowReactive", <value>] -
    a Vue reactivity hint that means nothing once we're just reading data."""
    if isinstance(value, list) and len(value) == 2 and value[0] == "ShallowReactive":
        return value[1]
    return value


def fetch_popular_titles():
    """Returns [{title, year}, ...] in JustWatch's own popularity order.

    JustWatch rebuilt their frontend on Nuxt/Vue at some point after this
    was first written - window.__APOLLO_STATE__ is gone, replaced by a
    <script id="__NUXT_DATA__"> payload (see _resolve_nuxt_payload for its
    shape). The data underneath is still Apollo Client's normalized
    GraphQL cache - same Movie:<id> / ROOT_QUERY / content(...) shape as
    before, just serialized differently. Confirmed live: ROOT_QUERY carries
    a separate popularTitles(...) field per distinct query variables (the
    page fires more than one - a small carousel alongside the full grid),
    so pick the one actually sortBy=POPULAR/objectTypes=[MOVIE] with the
    highest `first` count rather than assuming there's only one."""
    resp = requests.get(JUSTWATCH_URL, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (compatible; PosterFrame/1.0; personal use)",
    })
    resp.raise_for_status()
    html = resp.text

    match = NUXT_DATA_RE.search(html)
    if not match:
        raise ValueError("JustWatch page did not contain the expected data - their site markup may have changed.")
    end = html.find("</script>", match.end())

    try:
        raw_array = json.loads(html[match.end():end])
        payload = _unwrap_shallow_reactive(_resolve_nuxt_payload(raw_array))
        apollo_cache = _unwrap_shallow_reactive(payload["data"])["apollo:default"]
        root_query = apollo_cache["ROOT_QUERY"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise ValueError(f"JustWatch page did not contain the expected data - their site markup may have changed ({e}).")

    best_key, best_first = None, -1
    for key in root_query:
        if not key.startswith("popularTitles("):
            continue
        try:
            args = json.loads(key[len("popularTitles("):-1])
        except ValueError:
            continue
        if args.get("sortBy") != "POPULAR" or args.get("filter", {}).get("objectTypes") != ["MOVIE"]:
            continue
        if args.get("first", 0) > best_first:
            best_key, best_first = key, args["first"]

    if best_key is None:
        raise ValueError("JustWatch page did not contain the expected data - their site markup may have changed.")

    titles = []
    for edge in root_query[best_key].get("edges", []):
        ref = (edge.get("node") or {}).get("__ref")
        movie = apollo_cache.get(ref) if ref else None
        if not movie:
            continue
        content_key = next((k for k in movie if k.startswith("content(")), None)
        content = movie.get(content_key) if content_key else None
        if not content or not content.get("title"):
            continue
        titles.append({"title": content["title"], "year": content.get("originalReleaseYear")})
        if len(titles) >= MAX_RAW_TITLES:
            break

    return titles


def resolve_tmdb_id(title, year, api_key=None):
    """Title+year search against TMDb - the only bridge between JustWatch's
    id space and TMDb's. Prefers an exact (case-insensitive) title match in
    the given year; falls back to TMDb's own top search result, since
    search is already relevance-ranked, rather than giving up.

    api_key: see fetch_posters.fetch_credits' docstring - same reasoning
    (app.py calling this in-process for the Discovery preview grid, not as a
    subprocess with the key injected into its environment)."""
    api_key = api_key or TMDB_API_KEY
    if not api_key:
        return None
    params = {"api_key": api_key, "query": title, "language": "en-US"}
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
    active, rather than a second set of controls to keep in sync.

    Measured from poster_meta's added_date (when the poster actually landed
    in your rotation), not release_date. JustWatch's "Popular" list is not
    "new releases" - it can surface a film that came out months ago, and
    keying off release_date meant a title like that was born already past
    the age cutoff, so the very next sync expired it again almost
    immediately. See fetch_posters.py's expire_old_posters for the full
    reasoning - same fix, same shared setting."""
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
        raw_added = (info or {}).get("added_date")
        try:
            added = datetime.strptime(raw_added, "%Y-%m-%d").date() if raw_added else date.today()
        except ValueError:
            added = date.today()
        if added > cutoff:
            continue

        title = (info or {}).get("title", filename)
        if item_id in still_playing:
            log(f"Keeping {title} - past the age limit but still in cinemas")
            continue

        try:
            fetch_posters.remove_poster(filename, f"{title} (added {raw_added or 'unknown date'})")
            expired.add(str(item_id))
        except requests.RequestException as e:
            log(f"Could not expire {title}: {e}")

    return expired


def main():
    config = load_web_config()

    if not config.get("justwatch_enabled", False):
        log("JustWatch integration is disabled in the web UI - skipping.")
        return

    if config.get("discovery_source", "justwatch") != "justwatch":
        log("JustWatch is not the active discovery source - skipping.")
        return

    if os.environ.get("POSTERFRAME_TRIGGER") == "schedule":
        if not config.get("justwatch_schedule_enabled", True):
            log("Scheduled sync is disabled in the web UI - skipping.")
            return

    if not TMDB_API_KEY:
        # Discovery comes from JustWatch, but poster art and credits still
        # come from TMDb, so its key is required either way.
        log("TMDB_API_KEY environment variable not set - aborting.")
        return

    expired = expire_old_posters(config)

    # How many candidates to resolve from JustWatch's current top-this-year
    # ranking per run. Whether this also acts as a hard cap on how many
    # JustWatch posters can exist at once depends on whether expiry is
    # enabled - see the ranking-based eviction block below for why.
    max_titles = max(1, min(40, int(config.get("justwatch_max_titles", 10))))
    this_year = date.today().year

    try:
        candidates = fetch_popular_titles()
    except (requests.RequestException, ValueError) as e:
        log(f"Failed to fetch JustWatch's popular list: {e}")
        return

    this_year_titles = [c for c in candidates if c.get("year") == this_year]
    log(f"JustWatch: {len(candidates)} popular title(s) fetched, "
          f"{len(this_year_titles)} released in {this_year}.")

    tracked = load_tracked()

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

    wanted = {}

    for entry in this_year_titles:
        if len(wanted) >= max_titles:
            break

        result = resolve_tmdb_id(entry["title"], entry["year"])
        if not result or not result.get("poster_path"):
            log(f"Could not match on TMDb: {entry['title']} ({entry['year']})")
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

    # Every currently-tracked title gets a chance to pick up a missing
    # digital_release_date, not just the ones in this run's `wanted` - a
    # title that's fallen out of JustWatch's current ranking but is still
    # kept around (age is the only removal criterion once poster_expiry is
    # on) would otherwise never have this backfilled, and would stay stuck
    # showing Now Showing/status purely off release_date forever. Always
    # movie-only here, unlike fetch_posters.py's TMDb sync.
    poster_meta = config.get("poster_meta", {})
    for key in tracked:
        fetch_posters.backfill_digital_release_date(f"justwatch_movie_{key}.jpg", key, poster_meta)

    if not wanted:
        log("No results resolved - leaving the current rotation alone.")
        save_tracked(tracked)
        fetch_posters.maybe_randomize_order(config)
        return

    for key, item in wanted.items():
        if key not in tracked:
            try:
                fetch_posters.upload_poster(item)
                tracked[key] = item["title"]
            except requests.RequestException as e:
                log(f"Failed to add {item['title']}: {e}")
                continue

        try:
            cast_count = config.get("appearances", {}).get("framed", {}).get("cast_count", 4)
            credits = fetch_posters.fetch_credits("movie", key, cast_count)
            digital_release_date = fetch_posters.fetch_digital_release_date(key)
            fetch_posters.update_poster_meta(item, credits, digital_release_date)
        except requests.RequestException as e:
            log(f"Failed to update metadata for {item['title']}: {e}")

    # Ranking-based eviction - deliberately only when expiry is off. With
    # expiry on, age is the sole removal criterion (see expire_old_posters'
    # docstring for why: this exact loop used to run unconditionally, and a
    # title could get pulled just for slipping a few spots in the ranking,
    # regardless of poster_expiry_days). With expiry off there's no such
    # conflict - nothing else ever removes anything, so without this the
    # rotation would just grow forever as new titles are discovered. This
    # restores the original "stay capped at max_titles, newest climbers
    # bump out whoever's no longer in today's top ranking" behavior, but
    # only for that one specific case.
    if not config.get("poster_expiry_enabled", False):
        for key in list(tracked.keys()):
            if key not in wanted:
                try:
                    fetch_posters.remove_poster(f"justwatch_movie_{key}.jpg", tracked[key])
                except requests.RequestException as e:
                    log(f"Failed to remove {tracked[key]}: {e}")
                del tracked[key]

    save_tracked(tracked)
    log(f"Done. Tracking {len(tracked)} JustWatch-sourced poster(s).")
    fetch_posters.maybe_randomize_order(config)


if __name__ == "__main__":
    main()
