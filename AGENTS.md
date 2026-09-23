# Agent handoff

This repository is the canonical source for Relay Restreamer. Read this file and the relevant document under `docs/` before changing code.

## Product intent

Relay lets low-bandwidth streamers send one resilient SRT contribution feed to a VPS and independently toggle multiple RTMP/RTMPS destinations. It is intentionally approachable for non-technical streamers. Preserve the OLED UI, plain-language copy, confirmation before starting/stopping an output, and per-user isolation.

## Canonical files

- Backend/control plane: `router/app/main.py`
- Backend tests: `router/tests/test_team.py`
- Dashboard: `site/app/page.tsx`
- Dashboard styles: `site/app/globals.css`
- Container topology: `docker-compose.yml`
- Media server: `mediamtx.yml`
- Edge proxy example: `Caddyfile`

Do not introduce a second deployment copy of these files. Runtime state belongs in the `relay_data` Docker volume, never in Git.

## Invariants

1. OBS publishes over SRT to its stream's ingest path (`<slug>`). Destinations and the authenticated monitor read a separate program path (`<slug>/program`), which the router feeds from the ingest with an internal `-c copy` RTSP republisher (`ProgramSwitch`) while live input is on air and which falls back to `active.mp4` whenever that copy is stopped. MediaMTX provides RTSP to FFmpeg and HLS to the monitor from the program path.
2. Copy H.264 video for normal horizontal outputs. Decode/re-encode only for an explicitly added transformed output.
3. Relay never mixes and never re-encodes audio. OBS produces every mix and workers are always `-c copy`. Track 1 is the full live mix (music, game, voice) and track 2 is the clean mix (game and voice, no music). MediaMTX only admits a publisher onto an always-available path whose track layout matches the file's, and every screen carries exactly two AAC tracks, so a seeded stream must send exactly tracks 1 and 2: a third track, or a lone track, is refused at the program path and the dashboard says so (`ProgramSwitch._explain`). Routing is derived from the platform, never from user input: Twitch takes tracks 1 and 2 as live audio plus the Enhanced RTMP VOD track (which requires the pinned FFmpeg 8.x build), YouTube and X take track 2 because their archives are public, RPLAY and custom take track 1. A publisher sending fewer than two tracks degrades rather than failing every worker on that stream, and the degrade falls back to track 1 on every platform, YouTube and X included. That is a deliberate, informed choice and must not be reverted: muting those two means emitting FLV with no audio track at all, which is unverified against both ingests, so the alternative risks a stream the service rejects outright or archives silent — a worse and likelier failure than one carrying the wrong mix, and a publisher sending one track is misconfigured whatever this flag says. The cost is accepted with full knowledge of it: track 1 carries the music, Content ID scans the YouTube archive and X auto-publishes the replay, so a one-track OBS misconfiguration can put copyrighted audio on both. The compensating control is the dashboard, and it is part of this invariant: the degraded routing must be surfaced prominently, per destination and per stream, so the broadcaster fixes OBS. Weakening that display, or restoring fail-to-silence as the default, breaks this invariant. A video-only publisher is forwarded with no audio mapping on every platform. `destinations.music_fallback` is the one user-settable bit and it picks no track: it only decides whether a YouTube or X destination takes track 1 when track 2 is absent. It defaults to 1, so clearing it is the explicit opt-out — 0 means that destination stays connected and silent instead. An explicit true is still rejected on any other platform instead of being stored and ignored, but only a value the caller actually sent is judged: the request model is tri-state and `resolve_music_fallback` resolves an omitted flag to the platform's own default, on for YouTube and X and off everywhere else. The migration that adds the column stamps 1 onto every existing row, so a normalising `UPDATE` immediately clears it wherever it cannot act; a stored 1 only ever sits on a youtube or x row, though it means the platform default as often as an explicit choice. The flag changes nothing while both tracks are present and never restarts a running worker. The `audio_track` column stays in the schema because migrations preserve production rows; nothing reads it.
4. Destination secrets, SRT publishing passwords, and Twitch tokens remain encrypted with Fernet in SQLite.
5. Every state-changing browser request requires the session CSRF token.
6. Each user can access only their stream, monitor, destinations, screens, and Twitch connection. Only the owner can manage the team.
7. A Starting Soon/BRB takeover puts that screen on the program path and holds OBS off air by stopping the ingest→program copy; it never disconnects OBS, which may stay connected and stand by behind the screen. Returning to live puts OBS back on air at once if it is publishing. `mediamtx_auth` admits OBS on the ingest path whatever is on air; only suspension kicks it. The order inside a takeover is fixed: write the mode, swap `active.mp4`, then stop the copy — MediaMTX opens the file by name at that stop, and the copy's "wanted" flag is in-memory and route-owned so the loop can never act between those steps.
8. YouTube sends matching copies to primary and backup ingests. Twitch ingest selection is pinned for the lifetime of a forwarding worker.
9. Automatic outage ads are opt-in, wait 60 seconds, make one attempt per outage, and request the minimum supported commercial needed for 60 minutes of preroll-free time. A tick where MediaMTX could not be reached is not an outage: `media_status` reports `known: false` and the state machine must hold, not advance.
10. Every failover screen comes from `slate_encode_args(profile)`, built from that stream's own probed contribution format. Seeded slates additionally snap to one of the six geometries in `SLATE_VARIANTS` (720p/1080p at 30/48/60) and are cached one file per geometry under `media/_default/`; seeding must never copy another tenant's screen, which both leaked one operator's card onto another's channel and served a single 720p48 file to 1080p feeds for days. Relay is multi-tenant: never hard-code a resolution, frame rate, keyframe interval, or bitstream parameter to one operator's encoder. Platform-standard values (a two-second keyframe interval) belong in `DEFAULT_CONTRIBUTION` as a fallback until a stream has been observed. Changing the recipe means bumping `SLATE_ENCODER_VERSION`; a stream whose feed changes shape is caught automatically by `contribution_fingerprint()`.
11. Destination routes resolve ownership before acting. `WorkerManager.stop()` takes a bare destination id with no stream predicate, so the scoped query must come first. The same goes for `ProgramSwitch.stop()` and `kick_stream_publishers()`, which take a bare slug.

## Required checks

For dashboard changes:

```bash
cd site && npm ci && npm run lint && npm run build
```

For router changes:

```bash
cd router
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

For deployment changes:

```bash
docker compose config
docker compose build
```

Before any public push, scan tracked files for credentials and verify `.env`, databases, media, backups, caches, and `AGENTS.local.md` are ignored. Never paste secret values into tests, documentation, commands, commits, issues, or pull requests.

## Production discipline

Check `AGENTS.local.md` when present for operator-specific connection details; it is deliberately ignored. Preserve unrelated live state. Rebuild/restart only changed services when practical. Before touching `router` or `mediamtx`, determine whether a stream is live because those restarts interrupt ingest or forwarding. Dashboard-only restarts do not restart the media router.
