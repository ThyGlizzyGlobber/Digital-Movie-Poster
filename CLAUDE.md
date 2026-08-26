# Poster Frame

A wall-mounted digital movie poster display running on a Raspberry Pi Zero W.
Posters rotate on a schedule, are pulled automatically from TMDb, and everything
is configured through a self-hosted web UI. A film-grain filter is baked into
each poster so it reads as a printed sheet rather than a screen.

**The web UI is the source of truth.** Nothing should require SSH to configure.
If you add a feature, add its controls to the UI.

---

## Hardware

- Raspberry Pi Zero W (single-core ARMv6, 512MB) — slow; performance matters
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
├── templates/index.html # entire UI, single file, tabbed
├── config.json          # ALL state. Single source of truth.
├── .env                 # TMDB_API_KEY (chmod 600, not in git)
├── static/posters/      # processed posters — what actually gets displayed
├── originals/           # untouched full-res source images
├── prepared/            # display-ready composites (regenerated, disposable)
├── fonts_cache/         # downloaded Google Font TTFs
├── slideshow.log        # slideshow output (NOT journald — see gotcha below)
└── tmdb_sync.log        # manual sync output
```

### systemd units
| Unit | Purpose |
|---|---|
| `posterframe-web` | Flask app, port 5000, runs as `pi` |
| `posterframe-slideshow` | display loop, runs as **root**, has a drop-in override |
| `posterframe-spinner` | boot spinner, root, starts early via `sysinit.target` |
| `posterframe-fetch.timer` | daily TMDb sync at 04:00 |

`/etc/systemd/system/posterframe-slideshow.service.d/override.conf` sets
`StandardInput/Output=tty` and `TTYPath=/dev/tty1` — required or fbi runs blind.

`/etc/sudoers.d/posterframe` grants `pi` exactly three commands:
`systemctl poweroff`, `systemctl reboot`, `systemctl restart posterframe-slideshow`.

---

## How the pipeline works

**Upload/sync → store → composite → display**

1. `fetch_posters.py` downloads from TMDb, POSTs to `/upload`, then POSTs
   release date + title to `/poster-meta/<filename>`.
2. `/upload` saves the untouched image to `originals/`, then runs
   `prepare_poster()` (**resize first, then grain**) into `static/posters/`.
3. `slideshow.py` polls `config.json` every 3s. When its signature changes it
   composites each poster onto a full-screen canvas (poster scaled to width,
   leftover space becomes text bands) into `prepared/`, then hands the list to
   a single long-lived `fbi` process.

Filename prefixes carry meaning and are load-bearing:
- `tmdb_<media>_<id>.jpg` — auto-synced; the sync will remove these
- `tmdbpin_<media>_<id>.jpg` — pinned by URL; sync **never** removes these
- anything else — manual upload; never touched automatically

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
  otherwise re-graining (which overwrites in place) never triggers a redraw.
- Console text is silenced via `console=tty3` in `/boot/firmware/cmdline.txt`
  plus `quiet loglevel=3 logo.nologo systemd.show_status=0`, and `getty@tty1`
  is disabled. Without these, boot logs draw over the spinner.

### Display power
- **`vcgencmd display_power` does nothing under KMS/DRM** and still returns
  success, so its exit code is worthless. The overnight schedule therefore only
  paints the framebuffer black — it does not save backlight hours.
- **Known bug:** the schedule's wake path doesn't reliably restart fbi, leaving
  a stuck spinner. Currently the schedule should be left OFF. Fix = force a
  rebuild on wake rather than relying on the signature changing.

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
- Uploads need a long client timeout (180s). Full-res decode + grain on a Zero W
  regularly exceeds 30s, and timing out mid-upload strands orphaned posters.

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

Working: poster upload with grain, drag reorder, TMDb sync (movies + TV, per-
category limits, popularity filters, age-based expiry with a "still in cinemas"
reprieve), pin-by-URL, boot logo + animated spinner, display calibration,
custom Google Fonts, band text with release-aware status, tabbed UI with unified
save, power controls.

### Open items
1. **Schedule wake bug** (above) — schedule is off until fixed.
2. **Trailer playback.** The goal is trailer on top, poster below, no gap. Not
   feasible on a Zero W or on a bare framebuffer — needs a **Pi 5** and a
   browser kiosk, where the layout is trivial CSS. This would replace
   `slideshow.py` and `spinner.py` entirely; the web UI, TMDb sync, grain
   pipeline and config all survive unchanged. Note TMDb only supplies YouTube
   IDs, not video files, so sourcing is unsolved.
3. **Pi 5 migration.** Fixes the 90s boot, ~13s/poster grain, and sync timeouts.
   Caveat: Pi 5 has **no H.264 hardware decoder** (HEVC only), but its CPU
   software-decodes H.264 faster than the Pi 4's hardware could, without the
   1080p cap.
4. **Final panel + frame.** ~900mm portrait ≈ 40–43" 16:9. Avoid OLED — static
   posters for 15min at a time is a burn-in worst case. Set **Display
   resolution** in the UI to match, and **Working width** to the screen's short
   edge.
