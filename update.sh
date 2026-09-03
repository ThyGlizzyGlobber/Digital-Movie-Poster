#!/usr/bin/env bash
#
# Pulls the latest code from git and restarts the services. This is the
# mechanism behind the web UI's "Update now" button (see /update and
# /update/check in app.py) - the point is that a normal code update never
# needs SSH.
#
# Modes:
#   ./update.sh --check   Reports what's available upstream. No changes.
#   ./update.sh           Fast-forwards to the remote branch and restarts.
#                          Refuses if the local branch has diverged (extra
#                          local commits) or the working tree is dirty.
#   ./update.sh --force   Like the above, but resets --hard to the remote
#                          branch instead of refusing on either of those -
#                          for the "I rewrote history with an amend/force-
#                          push and the Pi's clone is now just stale, not
#                          holding anything worth keeping" case. Discards
#                          local commits and uncommitted changes alike; see
#                          /update/force's own confirm dialog in the web UI.
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

CHECK_ONLY=0
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --force) FORCE=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# Timestamped (matches the other log files' format) so a step's actual
# wall-clock duration is readable directly off update.log instead of
# guessed at. Not used for --check's output below - that's machine-parsed
# KEY=VALUE by parse_update_check_output() in app.py, and the progress
# bar's own marker matching (UPDATE_STAGES in templates/index.html) is a
# substring search, so a timestamp prefix here doesn't break it.
log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

# Never let git block on a credential/host-key prompt with no TTY attached -
# fail fast instead of hanging. This can run fully detached with no one
# watching, so a hang would just sit there eating memory on a Zero W.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10"

LOCK_FILE="$(pwd)/.update.lock"
if [[ -n "${POSTERFRAME_LOCK_FD:-}" ]]; then
    # app.py already opened and flocked this file before spawning us, and
    # kept that exact fd open across exec (pass_fds) specifically so there
    # is no gap between it letting go and this script taking its own lock -
    # Popen() returning is not proof this script has reached this line yet,
    # and that narrow gap used to be enough for a double-click "Update now"
    # to spawn a second one of these, which then raced this same file for
    # real: the loser landed here, logged the message below, and that
    # became the only thing in update.log because ITS OWN request handler
    # had just freshly truncated it - burying the winner's actual output.
    # Re-flock the SAME inherited fd (not a fresh open of the file) rather
    # than the file path below - trivially succeeds since we already hold
    # it via the parent, which keeps the "someone else has it" check
    # below in one place rather than a separate branch.
    LOCK_FD="$POSTERFRAME_LOCK_FD"
else
    exec 200>"$LOCK_FILE"
    LOCK_FD=200
fi
if ! flock -n "$LOCK_FD"; then
    log "An update is already in progress." >&2
    exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"

if [[ -z "$upstream" ]]; then
    log "Branch '$branch' has no upstream configured - can't check for updates." >&2
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

if [[ "$CHECK_ONLY" == "1" ]]; then
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

log "=== update started ==="
log "Currently at $current_sha ($current_msg)"

# Computed once here rather than inline below - both branches need it, and
# git diff --quiet HEAD -- is itself a mutation-free check.
dirty=0
git diff --quiet HEAD -- || dirty=1

if [[ "$FORCE" != "1" ]]; then
    if [[ "$behind" == "0" ]]; then
        log "Already up to date."
        exit 0
    fi

    if [[ "$ahead" != "0" ]]; then
        log "Local branch has $ahead commit(s) not on $upstream - diverged, not auto-merging." >&2
        log "Resolve this by hand over SSH (e.g. git status / git log), or use Force" >&2
        log "update in the web UI if you know this is an intentional rewrite." >&2
        exit 1
    fi

    # ahead=0 only means "no local commits" - a tracked file hand-edited over
    # SSH but never committed wouldn't show up there, and git merge --ff-only
    # doesn't refuse a dirty tree on its own unless the incoming pull actually
    # conflicts with the uncommitted lines. Left unchecked, a non-conflicting
    # pull would silently fold the uncommitted edit into the merge result,
    # contradicting the "never silently discards work" guarantee documented
    # above.
    if [[ "$dirty" == "1" ]]; then
        log "Local working tree has uncommitted changes - not auto-merging." >&2
        log "Resolve this by hand over SSH (e.g. git status / git stash / git commit)." >&2
        exit 1
    fi
elif [[ "$behind" == "0" && "$ahead" == "0" && "$dirty" == "0" ]]; then
    # Force mode still shouldn't do a no-op reset when there's genuinely
    # nothing to reconcile - same "already up to date" result either way.
    log "Already up to date."
    exit 0
elif [[ "$ahead" != "0" || "$dirty" == "1" ]]; then
    log "Force update: local commit(s)/changes don't match $upstream - resetting to match it exactly." >&2
fi

log "Pulling $behind commit(s) from $upstream"
before_reqs="$(git rev-parse HEAD:requirements.txt 2>/dev/null || true)"
if [[ "$FORCE" == "1" ]]; then
    git reset --hard "$upstream"
else
    git merge --ff-only "$upstream"
fi
after_reqs="$(git rev-parse HEAD:requirements.txt 2>/dev/null || true)"

if [[ -n "$system_changes" ]]; then
    log "This update touches system-level files:"
    echo "$system_changes" | sed 's/^/  /'
    log "Applying them with install.sh (as root, no reboot)"
    if sudo -n "$(pwd)/install.sh" -y; then
        log "install.sh applied cleanly."
    else
        log "install.sh failed or isn't permitted yet (see above) - system-" >&2
        log "level config is unchanged. The code update below still applies;" >&2
        log "re-run install.sh over SSH to pick up the rest by hand." >&2
    fi
fi

if [[ "$before_reqs" != "$after_reqs" ]]; then
    log "requirements.txt changed - reinstalling dependencies"
    venv/bin/pip install -r requirements.txt -q
fi

log "Now at $(git rev-parse --short HEAD)"

log "Restarting slideshow"
sudo -n /usr/bin/systemctl restart posterframe-slideshow

# Non-fatal: on the very first update after posterframe-plex.service was
# added, the sudoers grant for it only exists if the install.sh step above
# already ran and succeeded. Don't let this block the web app restart
# below, which matters far more and must always run.
log "Restarting Plex monitor"
sudo -n /usr/bin/systemctl restart posterframe-plex || log "Could not restart posterframe-plex (see above) - not fatal" >&2

log "Restarting web app (this connection will drop)"
sudo -n /usr/bin/systemctl restart posterframe-web
