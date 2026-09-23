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

Screen uploads show two gauges in turn. The upload itself goes through `XMLHttpRequest`, because `fetch` cannot report a request body leaving the browser, so only the uploading tab sees it. Once the router has the file, the screen's entry in `/api/state` carries `progress` — the stage, the fraction FFmpeg has written, and a time-left estimate — for as long as that conversion is actually running. The router reads it from the encode's own `-progress` output and keeps it in memory only.

### FFmpeg workers

Each enabled destination has an independent worker. A worker reads the user’s MediaMTX path over internal RTSP and reconnects after failures.

Video and audio are always stream copies. Relay never mixes and never re-encodes audio, so OBS produces every mix: track 1 is the full live mix (music, game, voice) and track 2 is the clean mix (game and voice, no music). OBS may publish up to six tracks; the rest are carried through the SRT feed and simply never mapped to a destination. The mapping is derived from the platform and there is no user-facing audio setting:

| Platform | Audio sent |
| --- | --- |
| Twitch | Track 1 as the live audio and track 2 as the separate VOD track |
| YouTube | Track 2 only |
| X | Track 2 only |
| RPLAY | Track 1 only |
| Custom RTMP | Track 1 only |

Twitch is the only two-track output, and its Enhanced RTMP multitrack encapsulation is why the FFmpeg release is pinned in `router/Dockerfile`. Because referencing an audio stream the publisher is not sending fails the whole command, a publisher sending fewer than two audio tracks degrades to track 1 everywhere: Twitch, RPLAY, and custom take track 1 anyway, and YouTube and X take it too by default. Dropping the audio mapping for those two would emit FLV with no audio track at all, and neither ingest has been verified to accept that — a stream rejected on connect, or archived silent for a whole broadcast, is the worse and likelier failure. The consequence is accepted rather than hidden: track 1 is the music mix, Content ID scans the YouTube archive and X auto-publishes the replay, so the dashboard surfaces the degraded routing per destination and per stream, and a one-track publish is treated as an OBS misconfiguration for the broadcaster to repair. The per-destination `music_fallback` column is the opt-out — clearing it mutes that one YouTube or X destination instead of sending track 1. It defaults to on for those two platforms and is not offered anywhere else. A video-only publisher is forwarded with no audio mapping anywhere, instead of parking every destination on that stream in `retrying`. YouTube uses FFmpeg’s tee muxer for primary and backup ingests. The worker redacts destination URLs and embedded credentials from stored error messages.

Twitch, RPLAY, and custom destinations therefore depend on track 1 already being the finished live mix. A music-only stem on track 1 is not a supported OBS layout.

Each worker also reports `-progress` on stdout. A destination is only marked `forwarding` once FFmpeg reports it is producing output — bytes where the muxer counts them, frames and PTS where it does not, as with the tee muxer YouTube uses, and a worker whose progress stops while its process is still alive is killed and restarted.

## Program source states

Each stream has two MediaMTX paths. OBS publishes to the ingest path (`<slug>`), and it is admitted whatever is on air. Destinations and the monitor read the program path (`<slug>/program`), whose always-available file is `active.mp4`. The router's `ProgramSwitch` runs one `-c copy` FFmpeg per stream that reads the ingest over RTSP and publishes it to the program path, only while program mode is `live` and OBS is publishing.

- `live`: the copy runs, so OBS is on air as soon as it publishes.
- `brb`: Relay copies the BRB file over `active.mp4` and stops the copy; the program path drops to the file, the forwarders stay attached, and OBS stays connected, standing by.
- `starting_soon`: the same with the Starting Soon file.

Returning to live restarts the copy, which MediaMTX splices in on a keyframe. Connection-loss failover is separate from manual takeover: while program mode is `live`, OBS disappearing closes the ingest path and with it the copy's input, so the copy exits and the program path's always-available file takes over; the copy returns when OBS does. Only suspending a user disconnects OBS outright.

MediaMTX only admits a publisher onto an always-available path whose track layout matches the file's. Every screen carries two AAC tracks, so OBS must send exactly tracks 1 and 2; otherwise the copy's RTSP publish is refused while OBS itself looks connected, and the router reports the mismatch on the dashboard.

### Handoff latency

MediaMTX offsets the first slate frame's timestamp by however long the publisher was gone, so detection time is re-presented to viewers as a freeze of the same length. Two settings control it:

- `readTimeout` in `mediamtx.yml` is the SRT peer-idle timeout, i.e. how long a vanished publisher stays "connected".
- Optional per-stream **fast handoff** drops a publisher that has stopped delivering bytes for `FAST_FAILOVER_STALL_SECONDS`, rather than waiting out that timeout. It is off by default because the threshold must stay above the negotiated SRT latency plus retransmission bursts.

The screen encoder (`slate_encode_args`) reproduces the bitstream parameters of the stream each screen replaces, because a parameter-set change at the splice forces a decoder reconfiguration downstream. Those parameters are not fixed: `probe_contribution()` runs ffprobe against the live feed when a stream comes online and records resolution, frame rate, profile, level, reference frames, B-frames, pixel aspect, colour signalling, and the measured keyframe cadence for that stream. Until a stream has been seen, `DEFAULT_CONTRIBUTION` supplies platform-standard values including the two-second keyframe interval Twitch and YouTube both specify. B-frames are always disabled in the screen regardless of the feed, because they make PTS != DTS and the FLV muxer rejects that. MediaMTX reads the always-available file's parameter sets once, when the path is created, and the router then swaps the file underneath it, so every screen must come from the same recipe. Changing that recipe means bumping `SLATE_ENCODER_VERSION` and re-converting stored screens.

## Trust boundaries

- UDP 8890 is public for SRT contribution.
- HTTPS is public through Caddy.
- MediaMTX API, RTSP, HLS origin, router, and SQLite remain on internal Docker networks or loopback.
- The dashboard monitor is authenticated by the router before MediaMTX HLS is fetched with internal credentials.
- The internal media credentials may read any path but publish only to `*/program`; OBS's per-stream credentials publish only to that stream's bare slug.
