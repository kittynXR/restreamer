# Architecture

## Components

### MediaMTX

MediaMTX listens for SRT publishers on UDP 8890. HTTP authentication delegates every publish/read decision to the router. A successful publisher becomes a path named for that user’s stream slug. MediaMTX exposes that path internally as RTSP for forwarding and as low-latency HLS for browser monitoring.

Each path can use an `active.mp4` file from the shared `relay_data` volume as its always-available source. When OBS disconnects, MediaMTX can keep the path alive with this file. The router dynamically reconciles paths for enabled users.

### Router

The FastAPI router owns:

- first-run owner setup, login sessions, CSRF protection, invitations, and account suspension/revocation;
- SQLite schema and runtime settings;
- generated SRT publishing credentials and OBS URLs;
- encrypted destination keys and Twitch OAuth tokens;
- MediaMTX path authentication and reconciliation;
- per-destination FFmpeg workers with retry loops;
- screen upload, probe, conversion, and program-source switching;
- Twitch ingest checks and optional outage-ad decisions;
- authenticated HLS proxying for the signed-in user’s monitor.

The database and media files live beneath `/data` in the `relay_data` Docker volume. Fernet encrypts recoverable secrets; Argon2 hashes account passwords and invitation tokens are stored as SHA-256 digests.

### Dashboard

The React/Vinext dashboard is a client-rendered control surface. It polls `/api/state`, sends CSRF-protected mutations to the router, and never receives stored destination keys or OAuth tokens. It includes onboarding because invited streamers may not be technical.

### FFmpeg workers

Each enabled destination has an independent worker. A worker reads the user’s MediaMTX path over internal RTSP and reconnects after failures.

| Audio mode | Output behavior |
| --- | --- |
| 1 | Copy video and audio track 1 (music) |
| 2 | Copy video and audio track 2 (clean/game) |
| 3 | Twitch only: mix tracks 1+2 for live audio, retain track 2 as the VOD track |
| 4 | Mix tracks 1+2 into one live audio track |

Modes 1 and 2 are stream copies. Modes 3 and 4 copy video but decode/mix/re-encode audio to AAC. YouTube uses FFmpeg’s tee muxer for primary and backup ingests. The worker redacts destination URLs from stored error messages.

## Program source states

- `live`: OBS is allowed to publish.
- `brb`: Relay activates the BRB file and rejects/kicks the OBS publisher.
- `starting_soon`: Relay activates the Starting Soon file and rejects/kicks the OBS publisher.

Connection-loss failover is separate from manual takeover: while program mode is `live`, MediaMTX’s always-available file takes over when the SRT publisher disappears and yields when OBS reconnects.

## Trust boundaries

- UDP 8890 is public for SRT contribution.
- HTTPS is public through Caddy.
- MediaMTX API, RTSP, HLS origin, router, and SQLite remain on internal Docker networks or loopback.
- The dashboard monitor is authenticated by the router before MediaMTX HLS is fetched with internal credentials.
