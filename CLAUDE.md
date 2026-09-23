# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Relay is a self-hosted multi-user restreaming control plane. OBS publishes one SRT feed to a VPS; per-destination FFmpeg workers fan it out to Twitch/YouTube/RPLAY/X/custom RTMP. Read `AGENTS.md` (invariants and handoff rules) and the relevant file under `docs/` before changing code; `AGENTS.local.md` holds operator-specific production details and is deliberately untracked.

## Commands

Dashboard (`site/`, Node >= 22.13):

```bash
cd site && npm ci && npm run lint && npm run build
```

`npm run dev` starts the dev server, `npm run typecheck` runs `tsc --noEmit`. The dashboard expects the API on the same origin, so a bare `npm run dev` has no backend — run the containers behind the reverse proxy for full integration.

Router (`router/`, Python 3.13):

```bash
cd router && python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && python -m unittest discover -s tests -v
```

On Windows use `.venv\Scripts\Activate.ps1`. A single test:

```bash
python -m unittest tests.test_team.TeamInvitationFlowTest.test_invite_accept_isolation_suspend_restore_and_revoke -v
```

`router/tests/test_team.py` sets every required env var at import time and points `DB_PATH` at a temp dir, so no `.env` is needed. It deliberately creates a pre-migration `users` table (no `enabled` column) before importing `app.main` so `initialize_db()`'s migration path is exercised, and it replaces every background manager with `AsyncMock`. New tests that touch background work must do the same.

Deployment changes:

```bash
docker compose config && docker compose build
```

## Architecture

### Media path

MediaMTX (UDP 8890 SRT in) → per-user **ingest** path named by the stream slug → `ProgramSwitch` (one `-c copy` ffmpeg per stream, RTSP in, RTSP out) → per-user **program** path `<slug>/program` → internal RTSP for FFmpeg workers and low-latency HLS for the browser monitor. Only the program path is always-available (`active.mp4`); the ingest is a plain publisher path, so it goes away when OBS does. The copy runs only while program mode is `live` and OBS is publishing, and stopping it is what a takeover does — the program path drops to the screen, the forwarders stay attached, and OBS stays connected behind it. `program_path()`, `internal_rtsp_url()`, `program_status()` (program view) and `media_status()` (ingest view, `online` = OBS connected) are the helpers; `MEDIAMTX_MEDIA_ROOT` is where MediaMTX sees the media volume. MediaMTX has `authMethod: http` and delegates *every* publish/read decision to `POST /internal/mediamtx-auth` in the router: the internal media credentials may read anything and publish only to a `*/program` path; a `publish:<slug>:<publish_user>:<password>` match may publish to the bare slug whatever is on air. Only UDP 8890 and HTTPS are public; the MediaMTX API (9997), RTSP (8554), HLS (8888), and the router (8787) stay on internal Docker networks.

### Router — one file, five background managers

`router/app/main.py` (~2k lines) is the whole control plane: schema, auth, routes, and managers. `lifespan()` starts them in order and shuts them down in reverse:

- `PathReconciler` — every 10s, PATCHes a MediaMTX path config per enabled user via the MediaMTX API so `alwaysAvailableFile` points at `/relay-data/media/<slug>/active.mp4`.
- `TwitchIngestManager` — probes `ingest.twitch.tv/ingests` TCP-connect latency every 6h, stores the winner in `app_settings`, and validates that any candidate template is `rtmps://…live-video.net` with `{stream_key}`.
- `WorkerManager` — one `asyncio.Task` per enabled destination, each running an FFmpeg retry loop.
- `MediaConversionManager` — ffprobe-validates then re-encodes uploaded BRB/Starting Soon screens to match that stream's own probed contribution format (see `slate_encode_args`), always with two audio tracks. It follows the encode's `-progress` output (through the same `progress_samples` parser the workers use) so `/api/state` can show a conversion gauge; that progress is in memory only and reported only while the task is really running.
- `FailoverAdManager` — 3s poll of every stream's MediaMTX online state; also the token-validation sweep, the BRB pre-staging, the optional fast-handoff watchdog, and the contribution-format probe on the offline→online edge.
- `SignalMetrics` — 1s poll of MediaMTX paths/srtconns/rtspsessions into per-stream ring buffers; the single shared snapshot every other caller reads.

### FFmpeg workers

`WorkerManager._run` builds the destination URL **inside the worker**, after decrypting, and pins it for the worker's lifetime (`pinned_output_url`) — Twitch ingest selection must not change mid-broadcast. Platform base URLs are constants; a stored value already starting with `rtmp(s)://` is used verbatim. Every worker is a straight `-c copy` forward: the relay never mixes and never re-encodes, because OBS owns every mix. OBS track 1 is the full live mix (music + game + voice), track 2 is the clean mix (game + voice, no music), and tracks 3-6 ride along in the SRT feed but are never mapped.

`build_audio_args(platform, audio_tracks, music_fallback=True)` derives the mapping from the platform alone — there is no `audio_track` request field and nothing reads the `audio_track` column (kept only because migrations preserve production rows):

| platform | maps | why |
| --- | --- | --- |
| twitch | `0:a:0` + `0:a:1` | Enhanced RTMP multitrack: track 1 is the live audio, track 2 the separate VOD track |
| youtube / x | `0:a:1` | the archive/replay is public, so no music |
| rplay / custom | `0:a:0` | unpublished, so the full live mix is safe |

Twitch's dual-track encapsulation needs the FFmpeg 8.x build pinned by `FFMPEG_RELEASE` in `router/Dockerfile`. `audio_tracks` comes from `audio_track_count()` and is `None` until the publisher's layout is known — a worker usually starts before OBS connects — which means "assume the documented two-track layout".

A known count below 2 is the one place a user choice exists, and it is a safety choice, not a routing one. Referencing `0:a:1` against a publisher that is not sending it fails the whole command and parks every destination on that stream in `retrying`, so the mapping degrades to `0:a:0` on **every** platform, youtube and x included — unless that destination’s `music_fallback` is cleared, which drops the audio map entirely. Twitch/rplay/custom map track 1 anyway, so nothing is inverted there. For the clean-track platforms the degrade is a deliberate, informed trade: muting them means emitting FLV with no audio track at all, and that shape has never been verified against either ingest, so the alternative risks a stream the service rejects outright or archives silent — a worse and far likelier failure than one carrying the wrong mix. The cost is real and stated, not hidden: track 1 is the music mix, YouTube runs Content ID over the archive and X auto-publishes the replay, so a one-track publish puts music on two scanned destinations. A publisher sending one track is misconfigured whatever the flag says and has to be fixed either way, so the compensating control is the dashboard — the degraded routing is shown per destination and per stream, prominently, and weakening that display is what breaks this design. Do not restore fail-to-silence as the default.

`destinations.music_fallback` (`INTEGER NOT NULL DEFAULT 1`) is now the mute switch rather than the music switch: it is on by default, and **clearing it is the explicit opt-out**. It is accepted on the create form and on `PATCH /api/destinations/{id}/music-fallback`; set to 0, that youtube or x destination stays connected and silent instead of taking the music mix. `validate_music_fallback` still rejects `True` on any platform other than youtube/x rather than storing a control that does nothing, but only a value the caller actually sent reaches it: `DestinationBody.music_fallback` is tri-state (`bool | None = None`) and `resolve_music_fallback` turns an omitted flag into the platform's own default — on for youtube/x, off elsewhere — so a twitch create that never mentioned the field does not 422 on the model default. The migration's `ADD COLUMN … DEFAULT 1` stamps 1 onto every pre-existing row, so an idempotent `UPDATE … WHERE music_fallback <> 0 AND platform NOT IN ('youtube','x')` runs right after the sweep; a stored 1 therefore only ever sits on a youtube or x row, never as a leftover on a destination that cannot act on it. The flag changes nothing when both tracks are present, and a video-only publisher (`0`) still gets no audio map anywhere. That route deliberately does not restart the worker — the map is fixed when FFmpeg starts, and a settings toggle must not drop a live output.

Workers report `-progress` on stdout; stdout and stderr must be drained concurrently or FFmpeg deadlocks at 64 KiB. `forwarding` means FFmpeg is producing output, not that the process is alive — bytes where the muxer counts them, frames or PTS where it does not, since `total_size` is N/A for the tee muxer YouTube uses.

YouTube uses the `tee` muxer with `-use_fifo` to hit primary and backup ingests. Every stderr line is stored as `last_error` only after replacing the output/backup URLs with `[destination]` — never let a real URL or key reach the DB, logs, or an API response.

### Program mode vs. connection-loss failover

Two distinct mechanisms, both fed by `active.mp4`:

- **Failover** is automatic and passive: while program mode is `live`, OBS dropping closes the ingest path, MediaMTX closes the copy's read session, the copy exits, and the program path's `alwaysAvailable` file takes over; the copy comes back (on a keyframe) when OBS does. `FailoverAdManager._prepare_brb` copies BRB over `active.mp4` while OBS is online so the file is staged before a drop. Fast/ultra failover still kick OBS's SRT connection on the ingest path.
- **Manual takeover** (`PATCH /api/screens/mode` → `brb`/`starting_soon`) sets program mode, copies that screen over `active.mp4`, then `await program_switch.stop(slug)` — in that order, because MediaMTX opens the file by name when the copy's publish session ends. OBS is never kicked and `mediamtx_auth` keeps admitting it: it stands by, connected and off air. Returning to live sets the mode, stages BRB, and `program_switch.wake(slug)` puts OBS back on air at once. The switch's `desired` flag is in memory and route-owned; the loop must never re-read the mode from the DB.
- **Track layout**: MediaMTX refuses a publisher whose tracks differ from the always-available file's, so with a seeded screen OBS must send exactly two AAC tracks. A mismatch surfaces as the copy's RTSP publish being refused while OBS looks connected; `ProgramSwitch._explain` turns that into a plain-language `last_error` the dashboard shows.

`active.mp4` is always swapped by writing `.pending.mp4` and `os.replace` so MediaMTX never reads a torn file; the swap runs off the event loop and prefers `os.link` over a copy. Seeding never copies another tenant’s screen. `SLATE_VARIANTS` enumerates the six supported geometries — 720p and 1080p at 30/48/60 — and `ensure_slate_variants()` renders one cached slate per geometry into `media/_default/slate-<W>x<H>p<F>v<SLATE_ENCODER_VERSION>.mp4` — the version is part of the filename because only missing files are rendered, so a recipe bump must change the name to take effect. `snap_slate_variant()` maps a stream’s probed contribution profile to the nearest, height first and ties resolving upward, and `seed_stream_media()` hands the stream that file. When a probe shows the feed changed shape, `refresh_stream_slate()` re-seeds — but only a seeded screen, never an uploaded one, which `has_uploaded_screen()` distinguishes via `media_assets` — and then attempts `reload_fallback_path()` so the new file reaches the wire without waiting for a MediaMTX restart (the reload declines unless the path is idle). MediaMTX opens `alwaysAvailableFile` at config load, so a file must exist before the media server starts; seeding therefore falls back to whatever is already on disk if the variant has not been rendered yet.

MediaMTX reads the always-available file's SPS/PPS and AudioSpecificConfig once, when the path is created, and then plays samples from whatever file is at that path. Every screen must therefore be produced by `slate_encode_args(profile)` where `profile` is that stream's own observed contribution format — resolution, frame rate, profile/level, refs, B-frames, pixel aspect, colour signalling, and measured keyframe cadence. `probe_contribution()` reads those from the live feed with ffprobe on the offline→online edge and stores them per stream; `contribution_fingerprint()` marks screens stale when the feed changes shape. Nothing in this path may be hard-coded to one operator's encoder — this is a multi-tenant deployment. Replacing `active.mp4` does not reach viewers until the path is recreated — `reload_fallback_path` does that, but only while the path is idle, otherwise the change is staged for the next reconnect.

### Dashboard

`site/` is a Vinext (Vite + Next-compatible) app, not Next.js proper — `npm run dev/build/start` go through `vinext`, while `next.config.ts` and the `@next/eslint-plugin-next` rules are still in play. It is one client-rendered page (`site/app/page.tsx`) that polls `GET /api/state` and sends CSRF-protected mutations; `site/app/globals.css` is a light-theme base followed by an `/* OLED theme */` block that overrides the same tokens. Keep the OLED look, plain-language copy, accessible names on icon-only controls, and confirmation dialogs in front of anything that starts or stops an output.

### Data and secrets

SQLite at `/data/relay.db` in the `relay_data` volume, alongside `media/<slug>/`. Schema plus idempotent migrations live in `initialize_db()`; migrations must preserve production rows. Fernet (`FERNET_KEY`) encrypts recoverable secrets — destination URLs/keys, SRT publish passwords, Twitch access and refresh tokens. Argon2 hashes passwords; invite tokens are stored as SHA-256 digests. Losing `FERNET_KEY` makes every stored secret unrecoverable, so back it up with the volume.

Per-request rules that every new route must follow: resolve the user with `require_user`/`require_owner`, scope queries through `stream_for_user` (never trust a client-supplied stream or destination id without the `stream_id` predicate), and call `require_csrf` on anything mutating. The state endpoint returns the generated OBS SRT URL — treat it as a password.

## Working rules

- These files are canonical and must have exactly one copy: `router/app/main.py`, `router/tests/test_team.py`, `site/app/page.tsx`, `site/app/globals.css`, `docker-compose.yml`, `mediamtx.yml`, `Caddyfile`. Do not add a parallel "deployment" copy.
- Runtime state (DB, uploaded media, `.env`) belongs in the `relay_data` volume and on the VPS only — never in Git. Before a public push, run the checks in `docs/SECURITY.md`.
- Adding a destination platform touches the dashboard form, router validation (`validate_stream_key`/`validate_output_url`), worker URL construction, a local logo, and tests — see `docs/DEVELOPMENT.md`.
- Restart blast radius differs per service: `site` is safe any time (`docker compose build site && docker compose up -d --no-deps site`), `router` interrupts forwarding, `mediamtx` interrupts ingest and monitoring. Check whether anyone is live first. Never `docker compose down -v` in production.
