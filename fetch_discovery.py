#!/usr/bin/env python3
"""
Fired every 5 minutes by posterframe-fetch.timer - most invocations are a
no-op. It only actually runs a sync once the current time has reached the
user-configured discovery_sync_time (config.json, default 04:00) AND it
hasn't already run today, then runs whichever discovery source (TMDb or
JustWatch) is currently selected - only one script actually does any work,
the other is never even started.

Using ">= target time, not yet run today" rather than an exact time match
means a missed check (Pi off exactly at the target time, or the timer
jittering) self-heals on the very next tick instead of silently waiting
until tomorrow - no dependency on systemd's own Persistent= catch-up
semantics for this.

Manual "Sync now" clicks bypass this file entirely and call
fetch_posters.py / fetch_justwatch.py directly, since each already checks
config["discovery_source"] itself and no-ops if it isn't the active one.
"""
import json
import os
import subprocess
import sys
from datetime import date, datetime, time as dtime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STAMP_PATH = os.path.join(BASE_DIR, ".last_scheduled_sync")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def active_source(config):
    # "tmdb" is still a recognized value here (the underlying fetch_posters.py
    # sync still exists and works if ever hand-selected in config.json), but
    # the web UI no longer offers a way to choose it - JustWatch is the only
    # discovery source selectable there, and app.py's load_config() migrates
    # any existing "tmdb" value over to "justwatch" on load.
    source = config.get("discovery_source", "justwatch")
    return source if source in ("tmdb", "justwatch") else "justwatch"


def source_enabled(config, source):
    # Matches each script's own internal check (fetch_posters.py/
    # fetch_justwatch.py no-op immediately if their _enabled flag is off) -
    # duplicated here so this file can decide *before* stamping today as
    # done, not just before running the subprocess.
    if source == "justwatch":
        return config.get("justwatch_enabled", False)
    return config.get("tmdb_enabled", True)


def parse_target_time(value):
    try:
        hour, minute = (int(p) for p in str(value).split(":", 1))
        return dtime(hour, minute)
    except (ValueError, AttributeError):
        return dtime(4, 0)


def already_ran_today():
    try:
        with open(STAMP_PATH) as f:
            return f.read().strip() == date.today().isoformat()
    except FileNotFoundError:
        return False


def mark_ran_today():
    try:
        with open(STAMP_PATH, "w") as f:
            f.write(date.today().isoformat())
    except OSError:
        pass


def main():
    config = load_config()
    target = parse_target_time(config.get("discovery_sync_time", "04:00"))

    if datetime.now().time() < target:
        return 0
    if already_ran_today():
        return 0

    source = active_source(config)
    if not source_enabled(config, source):
        # The active source is currently switched off - the child script
        # would just no-op immediately, so there's no slow work to guard
        # against overlapping and nothing to gain by stamping today as done.
        # Leaving the stamp unwritten means flipping it back on later today
        # still gets a same-day sync instead of waiting until tomorrow.
        return 0

    # Marked before running, not after: a sync can take a while (image
    # downloads, TMDb lookups), and this file is checked again every 5
    # minutes - without this, a slow run risks a second overlapping
    # invocation starting before the first one finishes.
    mark_ran_today()

    script = "fetch_justwatch.py" if source == "justwatch" else "fetch_posters.py"
    log_path = os.path.join(BASE_DIR, f"{source}_sync.log")

    # Same log file "Sync now" already writes (app.py truncates it per run
    # too) - without this, a scheduled run's entire output went to whatever
    # this process's own stdout happened to be (systemd's journal, with a
    # short/volatile retention on a Pi Zero W), which meant the Logs tab's
    # TMDb/JustWatch sources only ever showed the last *manual* sync and
    # scheduled runs - the ones actually being asked about - were invisible.
    with open(log_path, "w") as log_file:
        return subprocess.call(
            [sys.executable, os.path.join(BASE_DIR, script)],
            stdout=log_file, stderr=subprocess.STDOUT,
        )


if __name__ == "__main__":
    sys.exit(main())
