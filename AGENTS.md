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

1. OBS publishes over SRT; MediaMTX provides RTSP to FFmpeg and HLS to the authenticated monitor.
2. Copy H.264 video for normal horizontal outputs. Decode/re-encode only for an explicitly added transformed output.
3. Audio track 1 is music and track 2 is clean/game. Mode 3 is Twitch live mix plus clean VOD track 2; mode 4 is a combined live mix.
4. Destination secrets, SRT publishing passwords, and Twitch tokens remain encrypted with Fernet in SQLite.
5. Every state-changing browser request requires the session CSRF token.
6. Each user can access only their stream, monitor, destinations, screens, and Twitch connection. Only the owner can manage the team.
7. Starting Soon/BRB manual takeover must stop the active OBS publisher; returning to live permits OBS to reconnect.
8. YouTube sends matching copies to primary and backup ingests. Twitch ingest selection is pinned for the lifetime of a forwarding worker.
9. Automatic outage ads are opt-in, wait 60 seconds, make one attempt per outage, and request the minimum supported commercial needed for 60 minutes of preroll-free time.

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
