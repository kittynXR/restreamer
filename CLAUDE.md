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

MediaMTX (UDP 8890 SRT in) → per-user path named by the stream slug → internal RTSP for FFmpeg workers and low-latency HLS for the browser monitor. MediaMTX has `authMethod: http` and delegates *every* publish/read decision to `POST /internal/mediamtx-auth` in the router, which accepts either the internal media credentials (read) or a `publish:<slug>:<publish_user>:<password>` match with program mode `live` (publish). Only UDP 8890 and HTTPS are public; the MediaMTX API (9997), RTSP (8554), HLS (8888), and the router (8787) stay on internal Docker networks.

### Router — one file, five background managers

`router/app/main.py` (~2k lines) is the whole control plane: schema, auth, routes, and managers. `lifespan()` starts them in order and shuts them down in reverse:

- `PathReconciler` — every 10s, PATCHes a MediaMTX path config per enabled user via the MediaMTX API so `alwaysAvailableFile` points at `/relay-data/media/<slug>/active.mp4`.
- `TwitchIngestManager` — probes `ingest.twitch.tv/ingests` TCP-connect latency every 6h, stores the winner in `app_settings`, and validates that any candidate template is `rtmps://…live-video.net` with `{stream_key}`.
- `WorkerManager` — one `asyncio.Task` per enabled destination, each running an FFmpeg retry loop.
- `MediaConversionManager` — ffprobe-validates then re-encodes uploaded BRB/Starting Soon screens to the fixed failover format (1920x1080, fps 48, yuv420p, always with an audio track).
- `FailoverAdManager` — 3s poll of every stream's MediaMTX online state; also the token-validation sweep and the BRB pre-staging.

### FFmpeg workers

`WorkerManager._run` builds the destination URL **inside the worker**, after decrypting, and pins it for the worker's lifetime (`pinned_output_url`) — Twitch ingest selection must not change mid-broadcast. Platform base URLs are constants; a stored value already starting with `rtmp(s)://` is used verbatim. Video is always `-c:v copy`. Audio track 1 = music, 2 = clean/game:

| `audio_track` | Behavior |
| --- | --- |
| 1 / 2 | Full stream copy of that track |
| 3 | Twitch only: `amix` tracks 1+2 → AAC live, plus track 2 copied as the VOD track |
| 4 | `amix` tracks 1+2 → single AAC live track |

YouTube uses the `tee` muxer with `-use_fifo` to hit primary and backup ingests. Every stderr line is stored as `last_error` only after replacing the output/backup URLs with `[destination]` — never let a real URL or key reach the DB, logs, or an API response.

### Program mode vs. connection-loss failover

Two distinct mechanisms, both fed by `active.mp4`:

- **Failover** is automatic and passive: while program mode is `live`, MediaMTX's `alwaysAvailable` file covers a dropped SRT publisher and yields when OBS reconnects. `FailoverAdManager._prepare_brb` copies BRB over `active.mp4` while OBS is online so the file is staged before a drop.
- **Manual takeover** (`PATCH /api/screens/mode` → `brb`/`starting_soon`) copies that screen over `active.mp4`, sets program mode, and calls `kick_stream_publishers`. Because `mediamtx_auth` requires program mode `live` to authorize a publish, OBS stays locked out until the user returns to live input.

`active.mp4` is always swapped by writing `.pending.mp4` and `os.replace` so MediaMTX never reads a torn file. New users are seeded from `media/_default/brb.mp4`, itself copied from the first available (owner-first) uploaded BRB.

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
