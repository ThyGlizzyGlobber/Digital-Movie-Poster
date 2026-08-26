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
# When the pull touches systemd/ or install.sh itself, this also re-runs
# install.sh (as root, non-interactively - see the sudoers note in
# install.sh's own header) so the system-level bits apply too: new unit
# files, sudoers, getty, boot cmdline. It never reboots on its own even if
# the boot cmdline changed - see install.sh's --auto-reboot note. --check
# reports SYSTEM_CHANGES=1 when the pending update touches those paths, so
# the web UI can say so before the user updates.
#
# First run after adding this feature is a chicken-and-egg case: the old
# sudoers file won't grant install.sh yet, so the auto-run fails with a
# permission error. That's expected and non-fatal - the code update still
# lands and services still restart, just once you'll see the "couldn't
# apply system changes" note and need install.sh over SSH one last time to
# pick up its own new sudoers rule. Every update after that is unattended.
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
    echo "This update touches system-level files:"
    echo "$system_changes" | sed 's/^/  /'
    echo "Applying them with install.sh (as root, no reboot)"
    if sudo -n "$(pwd)/install.sh" -y; then
        echo "install.sh applied cleanly."
    else
        echo "install.sh failed or isn't permitted yet (see above) - system-" >&2
        echo "level config is unchanged. The code update below still applies;" >&2
        echo "re-run install.sh over SSH to pick up the rest by hand." >&2
    fi
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
