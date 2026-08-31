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
    source = config.get("discovery_source", "tmdb")
    return source if source in ("tmdb", "justwatch") else "tmdb"


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

    # Marked before running, not after: a sync can take a while (image
    # downloads, TMDb lookups), and this file is checked again every 5
    # minutes - without this, a slow run risks a second overlapping
    # invocation starting before the first one finishes.
    mark_ran_today()

    script = "fetch_justwatch.py" if active_source(config) == "justwatch" else "fetch_posters.py"
    return subprocess.call([sys.executable, os.path.join(BASE_DIR, script)])


if __name__ == "__main__":
    sys.exit(main())
