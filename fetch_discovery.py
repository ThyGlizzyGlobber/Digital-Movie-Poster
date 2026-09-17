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

A run that *fails* self-heals the same way: only a successful sync marks
the day as done, so a transient DNS/network blip at exactly the scheduled
minute gets retried later the same day (throttled by RETRY_AFTER) instead
of costing a full day's worth of poster updates. On a flaky connection
that was the difference between syncing daily and not syncing for a week.

Manual "Sync now" clicks bypass this file entirely and call
fetch_posters.py / fetch_justwatch.py directly, since each already checks
config["discovery_source"] itself and no-ops if it isn't the active one.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STAMP_PATH = os.path.join(BASE_DIR, ".last_scheduled_sync")

# How long to leave a failed (or interrupted) run alone before re-attempting
# it. The timer itself ticks every 5 minutes, which is far too eager to keep
# hammering someone else's site with if the failure turns out to be
# persistent rather than a passing blip.
RETRY_AFTER = timedelta(minutes=30)


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


def read_stamp():
    """Returns (when, succeeded) for the last scheduled run, or (None, False)
    if there's no readable stamp.

    Format is "<ISO timestamp> <ok|pending>". Installs predating the
    pending/ok distinction wrote a bare "YYYY-MM-DD" when *starting* a run -
    datetime.fromisoformat still parses that (as midnight) and the absent
    status reads as success, so updating mid-day doesn't kick off a
    surprise second sync on the day the change lands."""
    try:
        with open(STAMP_PATH) as f:
            when, _, status = f.read().strip().partition(" ")
        return datetime.fromisoformat(when), status.strip() != "pending"
    except (FileNotFoundError, ValueError):
        return None, False


def write_stamp(succeeded):
    try:
        with open(STAMP_PATH, "w") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} "
                    f"{'ok' if succeeded else 'pending'}")
    except OSError:
        pass


def due_for_sync(now):
    when, succeeded = read_stamp()
    if when is None:
        return True
    if succeeded:
        return when.date() < now.date()
    # "pending" means the last attempt either failed outright or never got
    # to report back (killed mid-run, power cut). Both are retryable, just
    # not instantly - and since the stamp is written before the child
    # starts, this doubles as the guard against two runs overlapping.
    return now - when >= RETRY_AFTER


def main():
    config = load_config()
    target = parse_target_time(config.get("discovery_sync_time", "04:00"))

    now = datetime.now()
    if now.time() < target:
        return 0
    if not due_for_sync(now):
        return 0

    source = active_source(config)
    if not source_enabled(config, source):
        # The active source is currently switched off - the child script
        # would just no-op immediately, so there's no slow work to guard
        # against overlapping and nothing to gain by stamping today as done.
        # Leaving the stamp unwritten means flipping it back on later today
        # still gets a same-day sync instead of waiting until tomorrow.
        return 0

    # Stamped before running, not after: a sync can take a while (image
    # downloads, TMDb lookups), and this file is checked again every 5
    # minutes - without this, a slow run risks a second overlapping
    # invocation starting before the first one finishes. It's only promoted
    # to "ok" once the child reports success, so a failed run leaves
    # "pending" behind for RETRY_AFTER to pick up again later the same day.
    write_stamp(False)

    script = "fetch_justwatch.py" if source == "justwatch" else "fetch_posters.py"
    log_path = os.path.join(BASE_DIR, f"{source}_sync.log")

    # Same log file "Sync now" already writes (app.py truncates it per run
    # too) - without this, a scheduled run's entire output went to whatever
    # this process's own stdout happened to be (systemd's journal, with a
    # short/volatile retention on a Pi Zero W), which meant the Logs tab's
    # TMDb/JustWatch sources only ever showed the last *manual* sync and
    # scheduled runs - the ones actually being asked about - were invisible.
    with open(log_path, "w") as log_file:
        status = subprocess.call(
            [sys.executable, os.path.join(BASE_DIR, script)],
            stdout=log_file, stderr=subprocess.STDOUT,
        )

    write_stamp(status == 0)
    return status


if __name__ == "__main__":
    sys.exit(main())
