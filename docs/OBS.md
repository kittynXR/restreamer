# OBS setup

Each Relay user gets a unique SRT address. Sign in, open **Quick start** or **OBS setup**, and copy the address shown there. Do not copy another user’s address.

## Stream settings

1. Open **Settings → Stream**.
2. Choose **Custom**.
3. Paste the full Relay SRT address into **Server**.
4. Leave **Stream Key** blank unless your OBS build requires a placeholder.

The URL already contains the path, username, publishing password, packet size, and 500 ms SRT latency.

## Recommended video baseline

- H.264/x264 or hardware H.264.
- Constant bitrate.
- Two-second keyframe interval.
- 48 or 60 fps as the connection and encoder allow.
- A bitrate accepted by every enabled destination.

Relay normally copies the encoded video, so downstream destinations receive the same resolution, frame rate, keyframe cadence, and bitrate that OBS sends.

## Audio routing

The intended OBS routing is:

- Track 1: copyrighted music contribution.
- Track 2: clean/game contribution without copyrighted music.

Enable both tracks in the stream output. Relay can then create:

- Twitch mode: tracks 1+2 mixed for the live audience, with clean track 2 retained as the Twitch VOD track.
- YouTube clean mode: track 2 only.
- Combined mode: tracks 1+2 mixed into one live track for RPLAY, X, or custom destinations.

Use the authenticated browser monitor to switch between detected input audio tracks when testing. Verify with a private/unlisted destination before a production broadcast.

## Connection behavior

If OBS disconnects while Relay is in Live Input mode, the active BRB file keeps enabled destinations alive. OBS may reconnect automatically. Manual BRB or Starting Soon takeover intentionally rejects OBS until **Return to live input** is selected.
