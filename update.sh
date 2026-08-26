#!/usr/bin/env bash
#
# Pulls the latest code from git and restarts the services. This is the
# mechanism behind the web UI's "Update now" button (see /update and
# /update/check in app.py) - the point is that a normal code update never
# needs SSH.
#
# Two modes:
#   ./update.sh --check   Reports what's available upstream. No changes.
#   ./update.sh           Fast-forwards to the remote branch and restarts.
#
# This only updates *code*. Changes to systemd/ or install.sh itself -
# anything install.sh applies at the system level (sudoers, boot cmdline,
# getty) - still need install.sh re-run over SSH; that's a separate,
# intentionally rare step. --check reports SYSTEM_CHANGES=1 when the
# pending update touches those paths, so the web UI can warn before the
# user updates.
#
# Runs detached from posterframe-web.service's own cgroup (launched via
# Popen from a request handler, never moved to its own cgroup). Its last
# step restarts posterframe-web, which tears down that cgroup - killing
# this script as collateral right after. Everything meaningful (the pull,
# the restarts) has already happened and been logged by then, so that's
# expected and harmless, not a bug.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Never let git block on a credential/host-key prompt with no TTY attached -
# fail fast instead of hanging. This can run fully detached with no one
# watching, so a hang would just sit there eating memory on a Zero W.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10"

LOCK_FILE="$(pwd)/.update.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "An update is already in progress." >&2
    exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"

if [[ -z "$upstream" ]]; then
    echo "Branch '$branch' has no upstream configured - can't check for updates." >&2
    exit 1
fi

timeout 30 git fetch --quiet origin "$branch"

current_sha="$(git rev-parse --short HEAD)"
current_msg="$(git log -1 --pretty=%s HEAD)"
remote_sha="$(git rev-parse --short "$upstream")"
remote_msg="$(git log -1 --pretty=%s "$upstream")"
behind="$(git rev-list --count HEAD.."$upstream")"
ahead="$(git rev-list --count "$upstream"..HEAD)"
system_changes="$(git diff --name-only HEAD "$upstream" -- systemd install.sh)"

if [[ "${1:-}" == "--check" ]]; then
    echo "CURRENT_SHA=$current_sha"
    echo "CURRENT_MSG=$current_msg"
    echo "REMOTE_SHA=$remote_sha"
    echo "REMOTE_MSG=$remote_msg"
    echo "BEHIND=$behind"
    echo "AHEAD=$ahead"
    if [[ -n "$system_changes" ]]; then
        echo "SYSTEM_CHANGES=1"
    else
        echo "SYSTEM_CHANGES=0"
    fi
    exit 0
fi

echo "=== update started $(date -Iseconds) ==="
echo "Currently at $current_sha ($current_msg)"

if [[ "$behind" == "0" ]]; then
    echo "Already up to date."
    exit 0
fi

if [[ "$ahead" != "0" ]]; then
    echo "Local branch has $ahead commit(s) not on $upstream - diverged, not auto-merging." >&2
    echo "Resolve this by hand over SSH (e.g. git status / git log)." >&2
    exit 1
fi

echo "Pulling $behind commit(s) from $upstream"
before_reqs="$(git rev-parse HEAD:requirements.txt 2>/dev/null || true)"
git merge --ff-only "$upstream"
after_reqs="$(git rev-parse HEAD:requirements.txt 2>/dev/null || true)"

if [[ -n "$system_changes" ]]; then
    echo "NOTE: this update touches system-level files:"
    echo "$system_changes" | sed 's/^/  /'
    echo "The pulled code is running now, but service definitions and"
    echo "system config are unchanged until install.sh is re-run over SSH."
fi

if [[ "$before_reqs" != "$after_reqs" ]]; then
    echo "requirements.txt changed - reinstalling dependencies"
    venv/bin/pip install -r requirements.txt -q
fi

echo "Now at $(git rev-parse --short HEAD)"

echo "Restarting slideshow"
sudo -n /usr/bin/systemctl restart posterframe-slideshow

echo "Restarting web app (this connection will drop)"
sudo -n /usr/bin/systemctl restart posterframe-web
