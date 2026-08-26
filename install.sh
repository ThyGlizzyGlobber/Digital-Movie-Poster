#!/usr/bin/env bash
#
# One-time setup for the poster frame on a Raspberry Pi Zero W running
# Raspberry Pi OS Lite (Bookworm-era, headless, no desktop).
#
# Run this ON THE PI, as the regular user that owns this checkout (the
# systemd units bake that username in as the unprivileged service user).
# Needs passwordless sudo for the user running it - apt installs, systemd
# unit installation, the sudoers drop-in, and the boot cmdline edit all
# need root.
#
#   cd ~/posterframe && ./install.sh
#
# Safe to re-run: every step is idempotent.

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(id -un)"
ASSUME_YES=0

for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

log()  { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }

confirm() {
    # confirm "question" - returns 0 (yes) unless the user types something
    # other than y/Y, or we're non-interactive and not --yes.
    local prompt="$1"
    if [[ "$ASSUME_YES" == "1" ]]; then
        return 0
    fi
    if [[ ! -t 0 ]]; then
        return 1
    fi
    read -r -p "$prompt [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

if [[ "$RUN_USER" == "root" ]]; then
    warn "Run this as the regular user (e.g. 'pi'), not root."
    warn "The web app and TMDb sync services run as whoever runs this script;"
    warn "running it as root would make the systemd units run everything as root."
    exit 1
fi

if [[ "$(uname -s)" != "Linux" ]] || [[ ! -e /proc/device-tree/model ]]; then
    warn "This doesn't look like a Raspberry Pi. install.sh edits system files"
    warn "(sudoers, systemd units, /boot/firmware/cmdline.txt) that only make"
    warn "sense there - aborting rather than guess."
    exit 1
fi

if ! sudo -n true 2>/dev/null; then
    warn "This script needs passwordless sudo for: apt, systemd, sudoers, and"
    warn "the boot cmdline. Run 'sudo -v' first, or enter your password when prompted below."
fi

log "Installing for user '$RUN_USER' in $BASE_DIR"

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
log "Installing system packages (apt)"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3-venv python3-pip python3-dev build-essential \
    libjpeg-dev zlib1g-dev libopenjp2-7 libtiff6 \
    fonts-dejavu-core \
    fbi git

# ---------------------------------------------------------------------------
# Python virtualenv
# ---------------------------------------------------------------------------
if [[ ! -d "$BASE_DIR/venv" ]]; then
    log "Creating virtualenv"
    python3 -m venv "$BASE_DIR/venv"
fi

log "Installing Python dependencies (this can take a while on a Zero W)"
"$BASE_DIR/venv/bin/pip" install --upgrade pip -q
"$BASE_DIR/venv/bin/pip" install -r "$BASE_DIR/requirements.txt" -q

# ---------------------------------------------------------------------------
# Runtime directories (the apps also create these themselves, but doing it
# here up front avoids any first-run ambiguity about ownership)
# ---------------------------------------------------------------------------
log "Creating runtime directories"
mkdir -p "$BASE_DIR/static/posters" "$BASE_DIR/originals" \
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
    chmod 600 "$BASE_DIR/.env"
fi
chmod 600 "$BASE_DIR/.env"

# ---------------------------------------------------------------------------
# systemd units - copy the checked-in templates, substituting placeholders
# ---------------------------------------------------------------------------
log "Installing systemd units"
for unit in "$BASE_DIR"/systemd/*; do
    name="$(basename "$unit")"
    sed -e "s|__DIR__|$BASE_DIR|g" -e "s|__USER__|$RUN_USER|g" \
        "$unit" | sudo tee "/etc/systemd/system/$name" > /dev/null
done

sudo systemctl daemon-reload

log "Enabling services"
sudo systemctl enable posterframe-web.service
sudo systemctl enable posterframe-slideshow.service
sudo systemctl enable posterframe-spinner.service
sudo systemctl enable posterframe-fetch.timer

# ---------------------------------------------------------------------------
# sudoers - grant exactly the systemctl calls the web UI needs (power
# controls, plus the two service restarts update.sh issues after a git
# pull), nothing else. Validated with visudo before it's installed; a bad
# sudoers file can lock out sudo entirely.
# ---------------------------------------------------------------------------
log "Installing sudoers rule"
SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot, /usr/bin/systemctl restart posterframe-slideshow, /usr/bin/systemctl restart posterframe-web
EOF

if sudo visudo -c -f "$SUDOERS_TMP" > /dev/null; then
    sudo install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/posterframe
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
sudo systemctl disable --now getty@tty1.service 2>/dev/null || true

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
    [[ -f "$CMDLINE.orig" ]] || sudo cp "$CMDLINE" "$CMDLINE.orig"

    line="$(cat "$CMDLINE")"
    # Redirect kernel console output to tty3 (off-screen) instead of tty1.
    line="$(echo "$line" | sed -E 's/console=tty[0-9]+/console=tty3/')"
    for flag in quiet loglevel=3 logo.nologo systemd.show_status=0; do
        if [[ "$line" != *"$flag"* ]]; then
            line="$line $flag"
        fi
    done
    echo "$line" | sudo tee "$CMDLINE" > /dev/null
fi

# ---------------------------------------------------------------------------
log "Done."
echo
echo "  Web UI:    http://$(hostname -I 2>/dev/null | awk '{print $1}'):5000"
echo "  Config:    $BASE_DIR/config.json (created on first run)"
echo "  TMDb key:  $BASE_DIR/.env"
echo
echo "A reboot is needed to pick up the console changes and start the"
echo "slideshow/spinner against a clean tty1."

if confirm "Reboot now?"; then
    sudo reboot
else
    echo "Reboot later with: sudo reboot"
fi
