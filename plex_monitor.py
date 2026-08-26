#!/usr/bin/env python3
"""
Watches Plex for the configured account playing a movie or episode, and
drives the poster frame's "now playing" override accordingly.

Talks to the frame only through app.py's own HTTP routes - /upload,
/poster-meta/<filename>, /delete/<filename>, /plex/now-playing - never
touching config.json or static/posters/ directly, same relationship
fetch_posters.py already has with TMDb. No Pillow/numpy import here on
purpose: the actual resize+grain work happens server-side in app.py's
existing /upload pipeline, so this process stays a lightweight poller.
"""
import json
import os
import time

import requests

APP_BASE = "http://127.0.0.1:5000"
POSTER_FILENAME = "plex_nowplaying.jpg"
PLEX_PRODUCT = "Poster Frame"
PLEX_VERSION = "1.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")
LOG_PATH = os.path.join(BASE_DIR, "plex_monitor.log")

POLL_FLOOR = 5
ACTIVE_STATES = ("playing", "paused", "buffering")


def log(message):
    with open(LOG_PATH, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def plex_headers(token, client_id):
    return {
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Version": PLEX_VERSION,
        "Accept": "application/json",
    }


def title_for(session):
    if session.get("type") == "episode" and session.get("grandparentTitle"):
        return session["grandparentTitle"]
    return session.get("title", "")


def poster_thumb_for(session):
    """For a movie, session 'thumb' is already the movie's poster (portrait,
    same ~2:3 ratio TMDb uses). For an episode it's the episode's own
    screenshot instead - a 16:9 still frame, nothing like a poster shape.
    grandparentThumb is the *show's* poster, portrait like everything else,
    so prefer it for episodes the same way title_for prefers the show name."""
    if session.get("type") == "episode" and session.get("grandparentThumb"):
        return session["grandparentThumb"]
    return session.get("thumb")


def fetch_credits(server_url, headers, session, cast_count=4):
    """Session objects from /status/sessions carry no crew/cast at all - only
    the full metadata object (fetched by ratingKey) has Director/Writer/
    Producer/Role arrays. For an episode, use the *show's* metadata
    (grandparentRatingKey) rather than the episode's own - same reasoning as
    poster_thumb_for: since the poster shown is the show's poster, the
    credits should describe the show (creator/regular cast), not one
    episode's specific writer/director. Plex has no dedicated "composer"
    field, so that line is simply never populated here - the billing block
    just skips it, same as any other missing field."""
    rating_key = session.get("grandparentRatingKey") if session.get("type") == "episode" else session.get("ratingKey")
    if not rating_key:
        return {}

    try:
        resp = requests.get(f"{server_url}/library/metadata/{rating_key}", headers=headers, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("MediaContainer", {}).get("Metadata", [])
    except requests.RequestException:
        return {}
    if not items:
        return {}
    meta = items[0]

    def tags(field):
        return [entry["tag"] for entry in meta.get(field, []) if entry.get("tag")]

    credits = {}
    directors = tags("Director")
    if directors:
        credits["director"] = directors[0]
    writers = tags("Writer")
    if writers:
        credits["writers"] = writers[:3]
    producers = tags("Producer")
    if producers:
        credits["producers"] = producers[:3]
    cast = tags("Role")
    if cast:
        credits["cast"] = cast[:cast_count]
    return credits


def find_now_playing(server_url, headers, username):
    resp = requests.get(f"{server_url}/status/sessions", headers=headers, timeout=10)
    resp.raise_for_status()
    sessions = resp.json().get("MediaContainer", {}).get("Metadata", []) or []

    for session in sessions:
        if session.get("type") not in ("movie", "episode"):
            continue
        if (session.get("User") or {}).get("title") != username:
            continue
        if (session.get("Player") or {}).get("state") not in ACTIVE_STATES:
            continue
        return session
    return None


def activate(server_url, headers, session, cast_count=4):
    thumb = poster_thumb_for(session)
    if not thumb:
        return False

    image_resp = requests.get(f"{server_url}{thumb}", headers=headers, timeout=15)
    image_resp.raise_for_status()

    files = {"poster": (POSTER_FILENAME, image_resp.content, "image/jpeg")}
    # 180s: same reasoning as fetch_posters.py - a Pi Zero can take well
    # over 30s to resize+grain a full-resolution poster.
    requests.post(f"{APP_BASE}/upload", files=files, timeout=180).raise_for_status()

    title = title_for(session)
    credits = fetch_credits(server_url, headers, session, cast_count)
    requests.post(
        f"{APP_BASE}/poster-meta/{POSTER_FILENAME}",
        json={"title": title, "release_date": None, "credits": credits},
        timeout=15,
    ).raise_for_status()

    requests.post(
        f"{APP_BASE}/plex/now-playing",
        json={"active": True, "filename": POSTER_FILENAME, "title": title},
        timeout=15,
    ).raise_for_status()
    return True


def deactivate():
    requests.post(f"{APP_BASE}/delete/{POSTER_FILENAME}", timeout=15).raise_for_status()
    requests.post(f"{APP_BASE}/plex/now-playing", json={"active": False}, timeout=15).raise_for_status()


def main():
    current_session_key = None

    while True:
        config = load_config()
        poll_seconds = max(POLL_FLOOR, config.get("plex_poll_seconds", 15.0))

        server_url = config.get("plex_server_url")
        username = config.get("plex_username")
        client_id = config.get("plex_client_id", "")
        token = load_env().get("PLEX_SERVER_TOKEN")

        if not (config.get("plex_enabled") and server_url and username and token):
            time.sleep(poll_seconds)
            continue

        headers = plex_headers(token, client_id)

        try:
            session = find_now_playing(server_url, headers, username)
        except requests.RequestException as e:
            # Transient network hiccup or the server being asleep/off -
            # skip this cycle without touching the active state, so a
            # brief blip doesn't flicker the display back to normal
            # rotation and back.
            log(f"Could not reach Plex server: {e}")
            time.sleep(poll_seconds)
            continue

        session_key = session.get("sessionKey") if session else None

        if session_key and session_key != current_session_key:
            log(f"Now playing: {title_for(session)}")
            cast_count = config.get("appearances", {}).get("framed", {}).get("cast_count", 4)
            try:
                if activate(server_url, headers, session, cast_count):
                    current_session_key = session_key
            except requests.RequestException as e:
                log(f"Could not activate now-playing poster: {e}")

        elif not session and current_session_key:
            log("Playback stopped")
            try:
                deactivate()
                current_session_key = None
            except requests.RequestException as e:
                log(f"Could not clear now-playing poster: {e}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
