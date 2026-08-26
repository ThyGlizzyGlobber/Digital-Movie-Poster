# Poster Frame

A wall-mounted digital movie poster display. Posters rotate on a schedule,
are pulled automatically from [TMDb](https://www.themoviedb.org/), and
everything is configured through a self-hosted web UI — no SSH needed once
it's set up. A film-grain filter is baked into each poster so it reads as a
printed sheet rather than a screen.

Built for a Raspberry Pi Zero W running Raspberry Pi OS **Lite** (headless,
no desktop), driving a display over HDMI straight to the Linux framebuffer.

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
   the network and configure everything there: display size/rotation,
   TMDb API key and sync filters, band text, fonts, schedule, and power
   controls. Get a free TMDb API key (v3 auth) at
   <https://www.themoviedb.org/settings/api>.

You can also upload your own posters directly from the web UI without ever
touching TMDb.

## Day to day

Everything is driven from the web UI — adding/removing/reordering posters,
changing the sync filters, adjusting the display, and rebooting/shutting
down the Pi. You shouldn't need to SSH in again after initial setup.

If you do need to check on it:

```bash
sudo systemctl status posterframe-web posterframe-slideshow
tail -f ~/posterframe/slideshow.log      # display loop's own log
tail -f ~/posterframe/tmdb_sync.log      # last manual/scheduled TMDb sync
sudo systemctl restart posterframe-slideshow
```

The scheduled TMDb sync runs daily at 04:00 (`posterframe-fetch.timer`); use
"Sync now" in the web UI to trigger one immediately.

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

This only updates code. If a change also touches `systemd/*` or `install.sh`
itself, the UI will say so ("this update also changes system files") — those
need `install.sh` re-run over SSH, since they configure things outside the
app (sudoers, boot config, systemd units):

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
  means a tracked file was hand-edited on the Pi outside of git; resolve it
  over SSH (`git status`/`git diff` in `~/posterframe`) rather than force it.
- **TMDb sync isn't picking anything up** — check `tmdb_sync.log`, and
  confirm the API key is set (web UI → TMDb tab) and at least one source
  category is enabled with a low enough popularity threshold.
- **Console text flashing over the display** — `install.sh` edits
  `/boot/firmware/cmdline.txt` to silence it; a `.orig` backup is kept
  alongside it if you need to compare or revert.

See [CLAUDE.md](CLAUDE.md) for the full architecture, pipeline details, and
a list of hard-won gotchas if you're changing the code.
