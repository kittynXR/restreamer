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

Relay never mixes and never re-encodes audio. OBS makes every mix and Relay only
forwards it, so what each platform receives is exactly what OBS sent. Which
track goes where is decided by the destination’s platform, not by you — the only
setting in the dashboard is the fallback described under **If Track 2 is
missing**, and it only does anything when a track is missing.

Set up two tracks in **Settings → Output → Audio**, then enable **both** of them
in the streaming output:

- **Track 1 — your full live mix.** Everything a live viewer should hear: music,
  game, and voice.
- **Track 2 — your clean mix.** The same mix with the copyrighted music removed,
  so game and voice only.

Your microphone belongs on **both** tracks.

| Destination | What it receives | Why |
| --- | --- | --- |
| Twitch | Track 1 as the live audio, Track 2 as the VOD track | Live viewers hear the music; the archive does not |
| YouTube | Track 2 only | Content ID scans the archive |
| X | Track 2 only | X publishes the replay automatically |
| RPLAY | Track 1 only | The stream is not published afterwards, so music is safe |
| Custom RTMP | Track 1 only | The full live experience is the least surprising default |

### Track 1 must be your finished live mix

Relay used to accept a Track 1 that carried the copyrighted music **alone**, as a
stem, and rebuilt the live mix on the server. That layout is no longer
supported. If your Track 1 is a music-only stem, reconfigure OBS so Track 1 is
the finished live mix — music, game, and voice together — before your next
broadcast. Left as a stem, Twitch, RPLAY, and custom destinations would carry
music with no voice or game audio at all.

### If Track 2 is missing

Everything above assumes both tracks are enabled in the streaming output. If OBS
publishes only one audio track, there is no Track 2 for Relay to send. Asking
for a track the stream does not carry fails that destination outright, so Relay
degrades instead:

| Destination | What it receives | What that means |
| --- | --- | --- |
| Twitch | Track 1 | No separate VOD track, so the archive carries your music |
| YouTube | **Track 1** — unless you set that destination to mute | Your music mix goes into the archive Content ID scans |
| X | **Track 1** — unless you set that destination to mute | Your music mix goes into the replay X publishes automatically |
| RPLAY | Track 1 | Unchanged |
| Custom RTMP | Track 1 | Unchanged |

YouTube and X receive Track 1 in this state because the alternative is sending
them a stream with no audio track at all, and it is not established that either
ingest accepts that. A destination that gets rejected outright, or that goes out
silent for an entire broadcast, is the bigger and more likely problem.

Be clear about what that costs you. Track 1 is the mix with your music in it,
YouTube scans the archive with Content ID, and X publishes the replay
automatically — so broadcasting on one track can leave copyrighted music sitting
on both, permanently and publicly.

**The fix is Track 2, not this setting.** Open **Settings → Output → Audio**,
enable Track 2 in the streaming output, and put your clean mix on it. Relay
shows the degraded routing in the dashboard for as long as one track is
arriving, and each destination says which track it is actually carrying. Treat
that as a broken OBS setup to repair before your next broadcast, not a mode to
stream in.

**If Track 2 is unavailable** is the per-destination override, and it starts on
**Send Track 1 (may contain music)**. The other choice is **Mute audio**: that
destination stays connected and sends video with no audio at all whenever Track
2 is missing. Choose it if you would rather a broadcast go out silent than carry
audio you cannot publish — for example when Track 1 contains licensed music that
must never reach an archive. Both choices leave a one-track broadcast broken in
some way; only enabling Track 2 in OBS fixes it. The setting changes nothing
while Track 2 is present, and it is not offered on Twitch, RPLAY, or Custom
RTMP, which receive Track 1 either way.

Changing it does not restart a destination that is already forwarding. Audio is
decided when a destination starts, so set it before you go live, or stop and
start that destination.

If OBS publishes no audio at all, every destination is forwarded as video only.
That keeps the rest of the stream up instead of failing every destination.

### Extra tracks

OBS can publish up to six audio tracks. Relay carries every track that arrives,
but only Tracks 1 and 2 are ever sent to a destination, so extra tracks enabled
for a local recording are harmless.

Use the authenticated browser monitor to switch between detected input audio tracks when testing. Verify with a private/unlisted destination before a production broadcast.

## Connection behavior

If OBS disconnects while Relay is in Live Input mode, the active BRB file keeps enabled destinations alive. OBS may reconnect automatically. Manual BRB or Starting Soon takeover intentionally rejects OBS until **Return to live input** is selected.
