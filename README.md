# Poster Frame

A wall-mounted digital movie poster display. Posters rotate on a schedule,
discovered automatically via [JustWatch](https://www.justwatch.com/) and
fetched from [TMDb](https://www.themoviedb.org/), and everything is
configured through a self-hosted web UI — no SSH needed once it's set up.

Runs on a Raspberry Pi (a Pi 4 is recommended; a Pi Zero W still works, just
slower) running Raspberry Pi OS **Lite** (headless, no desktop), driving a
display over HDMI straight to the Linux framebuffer.

## Setup

1. Flash Raspberry Pi OS Lite (32-bit) to an SD card, enable SSH and Wi-Fi
   in the imager's advanced options, and boot the Pi.
2. Clone this repo onto the Pi as the user you'll run it as (e.g. `pi`):

   ```bash
   git clone <this-repo-url> ~/posterframe
   cd ~/posterframe
   ```

3. Run the installer:

   ```bash
   ./install.sh
   ```

   This installs system packages (Python, `fbi`, fonts), creates a
   virtualenv, sets up the systemd services, adds a narrowly-scoped sudoers
   rule for the web UI's power and update controls, frees `tty1` for the
   display, and silences the boot console so log text doesn't draw over the
   slideshow. It's safe to re-run. It'll offer to reboot at the end — say yes.

4. After reboot, open `http://<pi-ip-address>:5000` from another device on
   the network and configure everything there: display size/rotation, TMDb
   API key, band text, fonts, schedule, and power controls. Get a free TMDb
   API key (v3 auth) at <https://www.themoviedb.org/settings/api> — it's
   needed even though JustWatch drives discovery, since TMDb is what
   actually supplies each poster's image and details.

You can also upload your own posters directly from the web UI without ever
touching TMDb.

## Day to day

Everything is driven from the web UI — adding/removing/reordering posters,
changing the discovery settings, adjusting the display, and rebooting/
shutting down the Pi. You shouldn't need to SSH in again after initial
setup.

If you do need to check on it:

```bash
sudo systemctl status posterframe-web posterframe-slideshow posterframe-plex
tail -f ~/posterframe/slideshow.log      # display loop's own log
tail -f ~/posterframe/justwatch_sync.log # last manual/scheduled discovery sync
sudo systemctl restart posterframe-slideshow
```

The scheduled discovery sync runs daily at 04:00 by default
(`posterframe-fetch.timer`); use "Sync now" in the web UI to trigger one
immediately.

## Plex "Now Playing"

Turn on the **Plex tab → Connect to Plex** and sign in via the Plex page
that opens. Once linked, enabling **Show Now Playing overrides** makes the
frame temporarily swap to whatever movie or episode your account is playing
on that server, with a "NOW PLAYING" label — and switch back to normal
rotation automatically when playback stops. Pick which band (top/bottom/none)
carries the label; the other band keeps showing whatever it's normally set
to, so a custom top band and a Plex-driven bottom band can coexist.

Expect roughly 15-30 seconds between pressing play and the poster changing —
it's checking on an interval (default 15s, adjustable), and posters go
through the same processing every other poster does. Not a bug, just how
long the pipeline takes on a Zero W.

## Updating

When you commit and push a change to this repo, update the frame from the
web UI — **System tab → Software update**. "Check for updates" shows what's
new; "Update now" pulls it, reinstalls dependencies if `requirements.txt`
changed, and restarts the services. No SSH needed.

Start a commit's summary title with a version tag — `[v1.4.0] Fix schedule
wake bug` — and the web UI picks it up: the System tab shows "Running
v1.4.0" instead of a raw commit hash, and "Check for updates" reports
"v1.5.0: Add trailer support" instead of just a commit count. Untagged
commits still work, they just show the commit hash and message instead.

`config.json` self-heals — new settings added by an update fill in with
defaults the next time it's read, so existing config is preserved.

If a change also touches `systemd/*` or `install.sh` itself, the UI says so
before you click ("this update also changes system files") and, on "Update
now", re-runs `install.sh` automatically as part of the update — apt
packages, sudoers, systemd units, boot console settings, all reapplied with
no SSH. It won't reboot on its own even if the boot console settings
changed; if you want that to take effect immediately rather than at the
next natural reboot, do it from the Power tab afterward.

Worth knowing: this means anyone who can push to this repo can run
arbitrary code as root on the Pi, unattended, via a commit. Fine for a
single-operator frame like this one; don't add other collaborators or a
public write-access remote without reconsidering that.

The first update after upgrading to this feature is a one-time exception —
the *old* sudoers file doesn't grant `install.sh` permission to run as root
yet, so that step fails harmlessly (the code update still lands) and you'll
need to run `install.sh` over SSH once by hand to pick up its own new
permission. Every update after that is unattended:

```bash
cd ~/posterframe
git pull
./install.sh
```

That's also the fallback if the frame is ever unreachable enough that the
web UI itself can't be used to update.

## Troubleshooting

- **Nothing on screen at boot** — check `sudo systemctl status
  posterframe-spinner posterframe-slideshow`; check `slideshow.log`.
- **Power/restart/update buttons in the UI don't work** — the sudoers rule
  at `/etc/sudoers.d/posterframe` failed to install; re-run `install.sh` and
  check its output.
- **"Update now" fails or says diverged** — check `update.log`. "Diverged"
  means the Pi's local commit doesn't match GitHub — either a tracked file
  was hand-edited on the Pi outside of git (resolve that one over SSH:
  `git status`/`git diff` in `~/posterframe`), or you rewrote history with
  an `amend`/force-push from your dev machine, in which case **Force
  update** (next to "Update now" once a divergence is detected) resets the
  Pi to match GitHub exactly, no SSH needed.
- **Update applied the code but not sudoers/systemd/boot settings** — check
  `update.log` for "install.sh failed or isn't permitted yet". Expected on
  the very first update after this feature was added (see Updating above);
  otherwise, run `install.sh` over SSH and check its output for the real error.
- **Discovery sync isn't picking anything up** — check `justwatch_sync.log`,
  and confirm the API key is set (web UI → Discovery tab) and JustWatch
  automation is enabled with a sensible title count.
- **Plex "Now Playing" isn't triggering** — check `plex_monitor.log` and
  `sudo systemctl status posterframe-plex`. Confirm **Show Now Playing
  overrides** is on (Plex tab), and that you're playing from the *same*
  Plex account and server you connected with — the frame ignores playback
  from other accounts on a shared server.
- **Console text flashing over the display** — `install.sh` edits
  `/boot/firmware/cmdline.txt` to silence it; a `.orig` backup is kept
  alongside it if you need to compare or revert.

See [CLAUDE.md](CLAUDE.md) for the full architecture, pipeline details, and
a list of hard-won gotchas if you're changing the code.
