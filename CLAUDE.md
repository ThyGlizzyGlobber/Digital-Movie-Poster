# Poster Frame

A wall-mounted digital movie poster display running on a Raspberry Pi.
Posters rotate on a schedule, are pulled automatically from TMDb/JustWatch,
and everything is configured through a self-hosted web UI.

**The web UI is the source of truth.** Nothing should require SSH to configure.
If you add a feature, add its controls to the UI.

---

## Hardware

- **Raspberry Pi 4** is the recommended/production target. A **Pi Zero W**
  (single-core ARMv6, 512MB) still works — the whole pipeline was originally
  built and tuned against one, so nothing here assumes a Pi 4 — but it's slow
  (e.g. multi-second-per-poster decode+resize, 90s boot) and no longer
  recommended for a production install.
- Raspberry Pi OS **Lite** (32-bit), Bookworm-era, **headless, no desktop**
- Display over HDMI. Currently a test monitor; the eventual target is a ~40–43"
  TV in **portrait** (≈900mm tall)
- Everything renders to the bare Linux framebuffer. There is no X, no Wayland,
  and no compositor.

---

## Layout

```
~/posterframe/
├── app.py               # Flask web UI + all config/state mutation
├── slideshow.py         # display loop: composites posters, drives fbi
├── spinner.py           # boot splash + loading spinner, writes to /dev/fb0
├── fetch_posters.py     # TMDb sync (run by timer or "Sync now")
├── plex_monitor.py      # Plex "Now Playing" poller - see Pipeline below
├── install.sh           # one-time SSH bootstrap - system packages, units,
│                        #   sudoers, boot cmdline. Rare to re-run.
├── update.sh            # git pull + service restart - what "Update now"
│                        #   in the web UI runs. See Web UI gotchas below.
├── templates/index.html # entire UI, single file, tabbed
├── config.json          # ALL state. Single source of truth.
├── .env                 # TMDB_API_KEY, PLEX_SERVER_TOKEN (chmod 600, not in git)
├── static/posters/      # processed posters — what actually gets displayed
├── originals/           # untouched full-res source images
├── prepared/            # display-ready composites (regenerated, disposable)
├── fonts_cache/         # downloaded Google Font TTFs
├── slideshow.log        # slideshow output (NOT journald — see gotcha below)
├── tmdb_sync.log        # manual sync output
├── plex_monitor.log     # plex_monitor.py output
└── update.log           # output of the last "Update now" run
```

### systemd units
| Unit | Purpose |
|---|---|
| `posterframe-web` | Flask app, port 5000, runs as `pi` |
| `posterframe-slideshow` | display loop, runs as **root**, has a drop-in override |
| `posterframe-spinner` | boot spinner, root, starts early via `sysinit.target` |
| `posterframe-plex` | Plex now-playing poller, runs as `pi`, `Restart=always` |
| `posterframe-fetch.timer` | daily TMDb sync at 04:00 |

`/etc/systemd/system/posterframe-slideshow.service.d/override.conf` sets
`StandardInput/Output=tty` and `TTYPath=/dev/tty1` — required or fbi runs blind.

`/etc/sudoers.d/posterframe` grants `pi` six things, NOPASSWD:
`systemctl poweroff`, `systemctl reboot`, `systemctl restart posterframe-slideshow`,
`systemctl restart posterframe-web`, `systemctl restart posterframe-plex` (what
`update.sh` needs to apply a pulled code update), and running `install.sh`
itself (any args) as root - see "Web UI" gotchas below for what that last one
means and why it's scoped to the whole script rather than install.sh's
individual apt/tee/systemctl calls.

---

## How the pipeline works

**Upload/sync → store → composite → display**

1. `fetch_posters.py` downloads from TMDb, POSTs to `/upload`, then POSTs
   release date + title to `/poster-meta/<filename>`.
2. `/upload` saves the untouched image to `originals/`, then runs
   `prepare_poster()` (resizes to the configured working width) into
   `static/posters/`.
3. `slideshow.py` polls `config.json` every 3s. When its signature changes it
   composites each poster onto a full-screen canvas (poster scaled to width,
   leftover space becomes text bands) into `prepared/`, then hands the list to
   a single long-lived `fbi` process.

Filename prefixes carry meaning and are load-bearing:
- `tmdb_<media>_<id>.jpg` — auto-synced; the sync will remove these
- `tmdbpin_<media>_<id>.jpg` — pinned by URL; sync **never** removes these
- `plex_nowplaying.jpg` — the Plex now-playing override, see below; not part
  of the normal rotation, hidden from the Posters tab
- anything else — manual upload; never touched automatically

**Plex now-playing** is a parallel, temporary override on top of the pipeline
above, not a replacement for it: `plex_monitor.py` polls the configured Plex
server every `plex_poll_seconds` and, when the signed-in account is playing a
movie/episode, drives the exact same `/upload` → `/poster-meta/<filename>`
pipeline (always as the single fixed file `plex_nowplaying.jpg`), then calls
`/plex/now-playing` to flip `config["plex_now_playing"]["active"]`. `slideshow.py`
checks that flag first, on every poll; when active it shows only that one
poster and forces whichever band `plex_band` names to say "NOW PLAYING",
leaving the other band's normal content untouched. When playback stops,
`plex_monitor.py` calls `/delete/plex_nowplaying.jpg` and clears the flag,
and normal rotation resumes (from the start of `order`, same as any other
settings change causes).

---

## Gotchas (all learned the hard way — do not re-litigate)

### Display / framebuffer
- **`fbi -t` parses as whole seconds.** Any sub-second value becomes 0, which
  fbi treats as "no slideshow" — it shows frame one and stops. This is why an
  fbi-based animated spinner is impossible; `spinner.py` writes to `/dev/fb0`
  directly instead.
- **`slideshow.py` must not `print()`.** The service has `StandardOutput=tty`
  so fbi's child inherits a real console — which means stdout lands as raw text
  on the same screen fbi is drawing to. Use the `log()` helper (writes to
  `slideshow.log`). Same for any subprocess: pass `stdout=DEVNULL`.
- **Never call `systemctl start` from `ExecStartPre`.** It blocks waiting for
  its own job while systemd is still starting the outer unit → deadlock →
  `activating (start-pre)` until timeout. Use `--no-block`, or better, launch
  as a subprocess from within the running Python.
- **Keep the old fbi process alive during the slow work.** Composite first,
  *then* terminate old and start new. Killing first leaves a visible gap.
- The rebuild signature includes poster file **mtimes**, not just filenames —
  otherwise reprocessing a poster in place (e.g. a working-width change)
  never triggers a redraw.
- Console text is silenced via `console=tty3` in `/boot/firmware/cmdline.txt`
  plus `quiet loglevel=3 logo.nologo systemd.show_status=0`, and `getty@tty1`
  is disabled. Without these, boot logs draw over the spinner.
- **The blinking text-mode cursor is a separate thing from console text.**
  None of the flags above touch it - it's an fbcon overlay that stays
  visible at whatever the console's last cursor position was, independent
  of whether anything was ever printed there. `vt.global_cursor_default=0`
  in `cmdline.txt` is what actually disables it. On a rotated (portrait)
  panel it can show up in a corner other than the top-left the viewer sees,
  since the kernel's own cursor position isn't rotation-aware.

### Display power
- **`vcgencmd display_power` does nothing under KMS/DRM** and still returns
  success, so its exit code is worthless. The overnight schedule therefore only
  paints the framebuffer black — it does not save backlight hours.
- **Fixed:** the schedule's wake path used to leave a stuck spinner — if the
  Pi booted (or the service restarted) straight into an off-window, the boot
  spinner service was never stopped, so it kept redrawing over the blanked
  framebuffer for the whole window. `slideshow.py` now stops the spinner as
  soon as it decides the display should be off (not just after a successful
  on-period rebuild), and wake sets an explicit `force_rebuild` flag instead
  of relying only on the signature diff. Re-enable `schedule_enabled` in the
  UI to use it again.

### TMDb
- **Only `w780` and `original` exist** for posters. There is no `w1280` or
  `w1600` — those return 400.
- **`/movie/upcoming` has fixed date bounds** a few weeks out that cannot be
  widened; `.gte`/`.lte` are ignored. Use `/discover/movie` instead.
- With `region` set, filter on **`release_date`**, not `primary_release_date` —
  TMDb switches to regional dates when a region is given.
- **Popularity is not comparable across released/unreleased.** It tracks current
  attention, so unreleased blockbusters score in the low tens (Avengers Doomsday
  ≈80, Dune 3 ≈16) while mid-tier films in cinemas score higher. Hence separate
  thresholds: `tmdb_min_popularity` (~30–50) and
  `tmdb_min_popularity_upcoming` (~8–15).
- TV uses `name`/`first_air_date`; movies use `title`/`release_date`.
  `region` is meaningless for TV.
- Movie ID 550 and TV ID 550 are different titles — media type **must** be in
  the filename.
- Uploads need a long client timeout (180s). Full-res decode + resize on a
  Zero W regularly exceeds 30s, and timing out mid-upload strands orphaned
  posters.

### Web UI
- All settings inputs live in one `<form id="settingsForm">` via the HTML5
  `form="settingsForm"` attribute (avoids illegal nested forms). One floating
  save button submits every tab at once. **Action** forms (upload, sync, purge,
  power) stay separate.
- Hidden marker inputs `_schedule_form` / `_tmdb_form` distinguish "checkbox
  unchecked" from "different form entirely". Keep them.
- Hidden tab panels still submit their values — that's deliberate, it's how the
  Movies/TV toggle preserves both sides' settings.
- Reordering is disabled while a filter is active: a filtered drag would submit
  only visible rows and silently drop the rest from the rotation.
- **Commit title convention: `[v1.4.0] Fix schedule wake bug`.** The System
  tab parses this (`VERSION_TAG_RE` / `parse_version_title()` in app.py) to
  show "Running v1.4.0 — Fix schedule wake bug" instead of a raw sha, and so
  "Check for updates" can say "v1.5.0: Add trailer support" instead of just a
  commit count. Only app.py parses it — `update.sh` always hands back the raw
  subject line, so there's one implementation of the convention, not two.
  Commits that don't follow it degrade gracefully to sha + full message, so
  it's fine if not every commit is tagged - only tag the ones you'd want to
  see as "the version" in the UI.
- **"Update now" (System tab) runs `update.sh`**: fast-forward-only `git pull`,
  then (if `systemd/`/`install.sh` changed) re-run `install.sh -y`, then
  reinstall deps if `requirements.txt` changed, restart `posterframe-slideshow`,
  then restart `posterframe-web` last. It refuses (rather than force-merging)
  if the local branch has diverged — e.g. someone hand-edited a tracked file
  on the Pi — so it never silently discards work.
- **The updater's own process gets killed by its last step, on purpose.**
  `/update` launches `update.sh` detached via `Popen(start_new_session=True)`,
  but `start_new_session` only escapes the OS session/process group — it does
  **not** move the process to a different systemd cgroup. `update.sh` is still
  in `posterframe-web.service`'s cgroup, so when its final line runs
  `systemctl restart posterframe-web`, systemd's default `KillMode=control-group`
  SIGTERMs everything in that cgroup — including `update.sh` itself — as part
  of tearing the old instance down. That's fine: by that point the pull,
  dependency install, and slideshow restart have already happened and been
  logged to `update.log`; the restart job is already handed to systemd (PID 1)
  before the client dies, so it completes regardless. Don't "fix" this by
  reordering the restarts or adding cleanup after the `restart posterframe-web`
  line — there is no "after" to reach.
- **A pulled `systemd/*`/`install.sh` change is applied automatically, not
  just flagged.** `update.sh --check` reports `SYSTEM_CHANGES=1` when the
  pending pull touches those paths so the UI can say so *before* the click,
  but "Update now" itself runs `sudo -n install.sh -y` as part of the update
  — apt packages, unit files, sudoers, getty, boot cmdline all get reapplied
  unattended. It deliberately does **not** pass `--auto-reboot`, so if the
  boot cmdline changed, a reboot (Power tab, whenever convenient) is still
  the one manual step left. This is a real trust-model consequence, not a
  detail: **whoever can push to this repo can now run arbitrary code as
  root on the Pi, unattended, via a plain commit.** That's an acceptable
  trade for a single-operator appliance (which is what this is) but would
  not be if this repo ever gained other collaborators or a public remote
  with write access.
- **Why root is granted `install.sh` by *name*, not its individual
  apt/tee/systemctl calls.** Sudoers wildcard-matches command *arguments*
  as literal text, not resolved paths — `sudo tee /etc/systemd/system/*`
  would also match `tee /etc/systemd/system/../shadow` textually, which
  `tee` then happily resolves to `/etc/shadow`. Granting one script by exact
  path sidesteps that whole class of mistake. Inside install.sh, this shows
  up as the `AS_ROOT` dual-mode: run directly by a human (as `pi`, over
  SSH) it still does per-line `sudo <cmd>` as before; run via
  `sudo install.sh` (root euid) it skips the redundant `sudo` and instead
  drops *down* to the checkout's owner (via `sudo -u`, which needs no
  password since real root already has it) for the steps that must stay
  user-owned — venv, `.env`, runtime dirs. Don't reintroduce a `RUN_USER ==
  root` guard "for safety" — that's exactly the mode update.sh depends on.
- **First auto-update after this feature ships won't fully apply itself.**
  The *old* sudoers file (from before `install.sh` added itself to the
  NOPASSWD list) doesn't grant the new `install.sh` line yet, so
  `sudo -n install.sh -y` fails with a permission error on that one run.
  `update.sh` treats this as non-fatal — the code still lands and services
  still restart — but system-level config is unchanged until `install.sh`
  is run over SSH once by hand. Every update after that bootstraps itself.
- **The progress bar's "restarting" state is inferred, not reported.**
  There is no route that tells the browser "the service is about to
  restart" — `posterframe-web` just stops answering while systemd swaps it
  out. The System tab's JS polls `GET /update/log` (tails `update.log`,
  reports whether `update.sh` is still running) roughly every second; a
  *failed* poll (connection refused, mid-restart) is what flips the UI into
  "reconnecting", and the first poll that *succeeds again afterward* is
  what's treated as "restart finished". The 5-second reload countdown only
  starts at that point — deliberately not right after "download complete",
  since restart duration varies (a plain code update restarts in under a
  second; one that also ran `install.sh` can take minutes) and a fixed
  countdown from an earlier point could fire before the new instance is
  actually back up. If you touch this, keep that ordering: countdown after
  confirmed reconnect, not instead of it.
- **`UPDATE_STAGES` in the template and update.sh's `echo` lines are
  coupled by string matching, not a real protocol.** The JS scans the log
  tail for the *last* matching marker (e.g. `'Restarting slideshow'`) to
  decide which stage to show. Changing what update.sh prints without
  updating `UPDATE_STAGES` in `templates/index.html` (or vice versa) just
  makes the progress bar stall on a stale label — it still degrades
  harmlessly (worst case: bar stops advancing until the restart/reconnect
  fires anyway), but keep the two in sync when editing either.

### Plex
- **Sign-in is a PIN flow, not a pasted token.** "Connect to Plex" (Plex tab)
  POSTs `/plex/connect`, which creates a pin via `plex.tv/api/v2/pins` and
  hands back an `app.plex.tv/auth` URL; the JS opens that in a new tab (opened
  *synchronously* on the click, then re-pointed once the fetch resolves - see
  next gotcha) and polls `/plex/connect/status` until `authToken` shows up.
  Both ends need the *same* `X-Plex-Client-Identifier` - it's generated once
  (`plex_client_id` in config.json, not a secret so it doesn't belong in
  `.env`) and reused for every Plex API call, including `plex_monitor.py`'s.
- **`window.open()` is called before the fetch, not after.** Calling it
  inside the `/plex/connect` response handler would happen async, after the
  click's own call stack has already unwound - most browsers' popup blockers
  treat that as an unsolicited popup and kill it. Opening a blank tab
  synchronously in the click handler, then setting `.location` on it once the
  real `auth_url` arrives, keeps it inside the trusted user-gesture window.
- **Only one secret is persisted: `PLEX_SERVER_TOKEN`.** The account-level
  token from the PIN flow is used in-memory just long enough to list the
  account's servers (`plex.tv/api/v2/resources`) and pick one (first match,
  preferring a `local: true` connection) - what actually gets saved is that
  *resource's own* `accessToken`, used against the local Plex Media Server's
  `/status/sessions`. No account token is ever written to disk.
- **`plex_monitor.py` never touches `config.json` or `static/posters/`
  directly** - same rule as `fetch_posters.py`. It drives the frame purely
  through `app.py`'s existing `/upload` and `/poster-meta/<filename>` routes
  plus `/delete/<filename>`, and one new internal route, `/plex/now-playing`,
  that only it calls.
- **A network hiccup doesn't flip the override off.** If `/status/sessions`
  fails, `plex_monitor.py` logs it and skips that poll *without* touching
  `plex_now_playing.active` - only an explicit empty/no-match session list
  counts as "stopped". Otherwise a blip would flicker the display back to
  normal rotation and immediately back.
- **No staleness check.** If `posterframe-plex.service` dies outright (not
  just crashes-and-restarts, which `Restart=always` already handles),
  `plex_now_playing.active` can stay stuck `true` with nothing to correct it.
  Known v1 limitation, not solved yet.
- **Expect real latency from "press play" to "poster changes."** Up to
  `plex_poll_seconds` to notice, plus the same decode+resize pipeline
  every other poster goes through, plus slideshow.py's normal 3s poll.
  15-30s end to end is normal, not a bug.

---

## Conventions

- Python 3, stdlib + Flask/Pillow/numpy/requests. Venv at `./venv`.
- No database, no ORM, no frontend framework. `config.json` and plain files.
- `load_config()` self-heals: add new keys to its `defaults` dict and existing
  installs pick them up on next read.
- Comments explain **why**, especially where something looks wrong but isn't.
- Verify changes actually landed. Patches have silently failed here more than
  once: `grep` for the new code after editing.

---

## State of play

Working: poster upload, drag reorder, JustWatch discovery/sync (the only
selectable discovery source - TMDb's own category-based discovery still
exists in fetch_posters.py but isn't reachable from the UI), age-based
expiry with a "still in cinemas" reprieve, pin-by-URL (TMDb link or a
Discovery-grid click, purged separately from JustWatch-synced posters),
boot logo + animated spinner, display calibration, custom Google Fonts,
band text with release-aware status, tabbed UI with unified save, power
controls, in-UI code updates ("Update now" and "Force update" for a
diverged/rewritten git history) with a live progress bar through the
restart/reconnect cycle, Plex "Now Playing" overrides (PIN sign-in,
dedicated band independent of the normal top/bottom content settings).
No film-grain step - posters are stored resized but otherwise untouched.
