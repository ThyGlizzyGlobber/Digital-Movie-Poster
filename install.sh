#!/usr/bin/env bash
#
# One-time setup for the poster frame on a Raspberry Pi Zero W running
# Raspberry Pi OS Lite (Bookworm-era, headless, no desktop).
#
# Two ways this runs:
#   1. Interactively, as the regular user that owns this checkout (e.g.
#      'pi'), over SSH:
#        cd ~/posterframe && ./install.sh
#      Needs passwordless (or session-cached) sudo - apt, systemd, the
#      sudoers drop-in, and the boot cmdline edit all need root.
#   2. As root via sudo, unattended, invoked by update.sh when a pulled
#      update touches systemd/ or install.sh itself. Root is granted this
#      one script by name (see the sudoers section below) rather than a
#      pile of individual apt/tee/systemctl commands - deliberately: sudo's
#      wildcard argument matching on file-writing commands like `tee` is a
#      known path-traversal footgun, and one whitelisted script is easier
#      to reason about than a dozen narrow ones. In this mode the pi-owned
#      steps (venv, .env, runtime dirs) drop back down to the checkout's
#      owner instead of leaving root-owned files behind - see AS_ROOT below.
#
# Safe to re-run: every step is idempotent.
#
# Flags:
#   -y, --yes       Skip prompts (currently just the TMDb key prompt on a
#                    fresh install). Does NOT imply --auto-reboot.
#   --auto-reboot   Reboot at the end without asking. Off by default even
#                    with -y - rebooting a wall-mounted display is a bigger
#                    deal than skipping a text prompt, so it needs its own
#                    opt-in. update.sh calls install.sh with -y but not
#                    this, so an auto-triggered update never reboots the
#                    Pi out from under whoever's looking at it.

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSUME_YES=0
AUTO_REBOOT=0

for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        --auto-reboot) AUTO_REBOOT=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

log()  { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }

confirm() {
    # confirm "question" - returns 0 (yes) only if there's a TTY to ask on
    # and the user types y/Y. Its only caller is the final reboot prompt,
    # which deliberately has its own separate gate (AUTO_REBOOT) - ASSUME_YES
    # ("-y", meant for skipping benign prompts like the TMDb key) must NEVER
    # answer this one, or an unattended `install.sh -y` (what update.sh runs
    # after every system-file change) would reboot the Pi out from under
    # whoever's looking at it. See --auto-reboot above.
    local prompt="$1"
    if [[ ! -t 0 ]]; then
        return 1
    fi
    read -r -p "$prompt [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

if [[ "$(id -u)" == "0" ]]; then
    # Running as root (via sudo). Figure out who the unprivileged service
    # user should be from who owns the checkout, rather than "root" - the
    # venv, .env and runtime dirs still need to belong to that user.
    AS_ROOT=1
    RUN_USER="$(stat -c '%U' "$BASE_DIR")"
    if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
        warn "Running as root, but $BASE_DIR is root-owned so there's no"
        warn "unprivileged user to install for. Run this as that user instead:"
        warn "  su - pi -c '$BASE_DIR/install.sh $*'"
        exit 1
    fi
else
    AS_ROOT=0
    RUN_USER="$(id -un)"
fi

# priv <cmd>: needs root. Already root -> run directly; otherwise escalate.
# as_user <cmd>: must end up owned by RUN_USER. Already RUN_USER -> run
# directly; running as root -> drop down via sudo -u (root can always do
# this without a password, so this never needs its own sudoers entry).
priv() {
    if [[ "$AS_ROOT" == "1" ]]; then "$@"; else sudo "$@"; fi
}
as_user() {
    if [[ "$AS_ROOT" == "1" ]]; then sudo -u "$RUN_USER" -H "$@"; else "$@"; fi
}

if [[ "$(uname -s)" != "Linux" ]] || [[ ! -e /proc/device-tree/model ]]; then
    warn "This doesn't look like a Raspberry Pi. install.sh edits system files"
    warn "(sudoers, systemd units, /boot/firmware/cmdline.txt) that only make"
    warn "sense there - aborting rather than guess."
    exit 1
fi

if [[ "$AS_ROOT" != "1" ]] && ! sudo -n true 2>/dev/null; then
    warn "This script needs passwordless sudo for: apt, systemd, sudoers, and"
    warn "the boot cmdline. Run 'sudo -v' first, or enter your password when prompted below."
fi

log "Installing for user '$RUN_USER' in $BASE_DIR"

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
log "Installing system packages (apt)"
priv apt-get update -qq
priv apt-get install -y --no-install-recommends \
    python3-venv python3-pip python3-dev build-essential \
    libjpeg-dev zlib1g-dev libopenjp2-7 libtiff6 \
    fonts-dejavu-core \
    fbi git

# ---------------------------------------------------------------------------
# Python virtualenv
# ---------------------------------------------------------------------------
if [[ ! -d "$BASE_DIR/venv" ]]; then
    log "Creating virtualenv"
    as_user python3 -m venv "$BASE_DIR/venv"
fi

log "Installing Python dependencies (this can take a while on a Zero W)"
as_user "$BASE_DIR/venv/bin/pip" install --upgrade pip -q
as_user "$BASE_DIR/venv/bin/pip" install -r "$BASE_DIR/requirements.txt" -q

# ---------------------------------------------------------------------------
# Runtime directories (the apps also create these themselves, but doing it
# here up front avoids any first-run ambiguity about ownership)
# ---------------------------------------------------------------------------
log "Creating runtime directories"
as_user mkdir -p "$BASE_DIR/static/posters" "$BASE_DIR/originals" \
         "$BASE_DIR/prepared" "$BASE_DIR/fonts_cache"

# ---------------------------------------------------------------------------
# .env (TMDb API key)
# ---------------------------------------------------------------------------
if [[ ! -f "$BASE_DIR/.env" ]]; then
    log "Creating .env"
    if [[ -t 0 && "$ASSUME_YES" != "1" ]]; then
        read -r -p "TMDb API key (blank to fill in later via the web UI): " tmdb_key
    else
        tmdb_key=""
    fi
    printf 'TMDB_API_KEY=%s\n' "$tmdb_key" > "$BASE_DIR/.env"
fi
priv chown "$RUN_USER:$RUN_USER" "$BASE_DIR/.env"
chmod 600 "$BASE_DIR/.env"

# ---------------------------------------------------------------------------
# systemd units - copy the checked-in templates, substituting placeholders
# ---------------------------------------------------------------------------
log "Installing systemd units"
for unit in "$BASE_DIR"/systemd/*; do
    name="$(basename "$unit")"
    sed -e "s|__DIR__|$BASE_DIR|g" -e "s|__USER__|$RUN_USER|g" \
        "$unit" | priv tee "/etc/systemd/system/$name" > /dev/null
done

priv systemctl daemon-reload

log "Enabling services"
priv systemctl enable posterframe-web.service
priv systemctl enable posterframe-slideshow.service
priv systemctl enable posterframe-spinner.service
priv systemctl enable posterframe-plex.service
priv systemctl enable posterframe-fetch.timer

# ---------------------------------------------------------------------------
# sudoers - grant exactly what the web UI needs: five specific systemctl
# calls (power controls, plus the three service restarts update.sh issues
# after a git pull), and this script by name (so update.sh can apply a
# pulled systemd/install.sh change unattended - see the top-of-file note).
# Nothing else. Validated with visudo before it's installed; a bad sudoers
# file can lock out sudo entirely.
# ---------------------------------------------------------------------------
log "Installing sudoers rule"
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot, /usr/bin/systemctl restart posterframe-slideshow, /usr/bin/systemctl restart posterframe-web, /usr/bin/systemctl restart posterframe-plex, $BASE_DIR/install.sh
EOF

if priv visudo -c -f "$SUDOERS_TMP" > /dev/null; then
    priv install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/posterframe
else
    warn "Generated sudoers rule failed validation - not installed."
    warn "The web UI's power/restart/update buttons won't work until this is fixed by hand."
fi
rm -f "$SUDOERS_TMP"

# ---------------------------------------------------------------------------
# Free tty1 for fbi/spinner - getty would otherwise fight them for the
# console, and its login prompt would flash over the display.
# ---------------------------------------------------------------------------
log "Disabling getty on tty1"
priv systemctl disable --now getty@tty1.service 2>/dev/null || true

# ---------------------------------------------------------------------------
# Silence the kernel/boot console so log text doesn't draw over the spinner
# or the slideshow. Idempotent: only touched if the flags aren't already
# there, and a one-time .orig backup is kept.
# ---------------------------------------------------------------------------
CMDLINE=""
for candidate in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [[ -f "$candidate" ]]; then
        CMDLINE="$candidate"
        break
    fi
done

if [[ -z "$CMDLINE" ]]; then
    warn "Couldn't find cmdline.txt (checked /boot/firmware and /boot) - skipping"
    warn "console silencing. Boot log text may draw over the spinner."
elif grep -q 'systemd.show_status=0' "$CMDLINE"; then
    log "Boot console already silenced ($CMDLINE)"
else
    log "Silencing boot console ($CMDLINE)"
    [[ -f "$CMDLINE.orig" ]] || priv cp "$CMDLINE" "$CMDLINE.orig"

    line="$(cat "$CMDLINE")"
    # Redirect kernel console output to tty3 (off-screen) instead of tty1.
    line="$(echo "$line" | sed -E 's/console=tty[0-9]+/console=tty3/')"
    for flag in quiet loglevel=3 logo.nologo systemd.show_status=0; do
        if [[ "$line" != *"$flag"* ]]; then
            line="$line $flag"
        fi
    done
    echo "$line" | priv tee "$CMDLINE" > /dev/null
fi

# ---------------------------------------------------------------------------
# Force a fixed HDMI output mode instead of relying on EDID auto-detection
# at boot. The Pi finishes booting well before some smart TVs' HDMI ports
# are ready to answer an EDID query - when that race is lost, the VideoCore
# firmware silently falls back to its hardcoded 720x480 NTSC safe mode and
# stays there for the rest of the boot, with no error anywhere. Everything
# downstream (fbi autoscaling composited images into that tiny framebuffer)
# then looks cropped/skewed, which looks exactly like a TV overscan problem
# but isn't - the real fix is to never depend on EDID succeeding at all.
# 1920x1080@60 (CEA mode 16) matches what rotation_degrees pre-rotates the
# composited canvas to before handing it to fbi - see slideshow.py.
# ---------------------------------------------------------------------------
CONFIG_TXT=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$candidate" ]]; then
        CONFIG_TXT="$candidate"
        break
    fi
done

if [[ -z "$CONFIG_TXT" ]]; then
    warn "Couldn't find config.txt (checked /boot/firmware and /boot) - skipping"
    warn "forced HDMI mode. A slow-to-wake TV may still fall back to 720x480."
elif grep -q '^hdmi_mode=' "$CONFIG_TXT"; then
    log "HDMI mode already forced ($CONFIG_TXT)"
else
    log "Forcing HDMI output to 1920x1080@60 ($CONFIG_TXT)"
    [[ -f "$CONFIG_TXT.orig" ]] || priv cp "$CONFIG_TXT" "$CONFIG_TXT.orig"
    {
        echo ""
        echo "# Force 1920x1080@60 instead of trusting EDID at boot - see install.sh"
        echo "hdmi_force_hotplug=1"
        echo "hdmi_group=1"
        echo "hdmi_mode=16"
    } | priv tee -a "$CONFIG_TXT" > /dev/null
fi

# ---------------------------------------------------------------------------
log "Done."
echo
echo "  Web UI:    http://$(hostname -I 2>/dev/null | awk '{print $1}'):5000"
echo "  Config:    $BASE_DIR/config.json (created on first run)"
echo "  TMDb key:  $BASE_DIR/.env"
echo
echo "A reboot is needed to pick up the console changes, any forced HDMI"
echo "mode, and start the slideshow/spinner against a clean tty1."

if [[ "$AUTO_REBOOT" == "1" ]]; then
    log "Rebooting now (--auto-reboot)"
    priv reboot
elif confirm "Reboot now?"; then
    priv reboot
else
    echo "Reboot later with: sudo reboot, or the Power tab in the web UI."
fi
