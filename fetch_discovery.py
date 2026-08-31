#!/usr/bin/env python3
"""
Daily-timer entry point. Runs whichever discovery source (TMDb or
JustWatch) is currently selected in the web UI - only one script actually
does any work; the other is never even started. Manual "Sync now" clicks
bypass this and call fetch_posters.py / fetch_justwatch.py directly, since
each already checks config["discovery_source"] itself and no-ops if it
isn't the active one.
"""
import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def active_source():
    try:
        with open(CONFIG_PATH) as f:
            source = json.load(f).get("discovery_source", "tmdb")
    except (FileNotFoundError, json.JSONDecodeError):
        source = "tmdb"
    return source if source in ("tmdb", "justwatch") else "tmdb"


def main():
    script = "fetch_justwatch.py" if active_source() == "justwatch" else "fetch_posters.py"
    return subprocess.call([sys.executable, os.path.join(BASE_DIR, script)])


if __name__ == "__main__":
    sys.exit(main())
