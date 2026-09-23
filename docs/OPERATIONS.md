# Operations

## Routine checks

```bash
docker compose ps
docker compose logs --tail=100 router
docker compose logs --tail=100 mediamtx
docker compose logs --tail=100 site
curl -fsS https://relay.example.com/health
```

`router` health confirms the API process is responding. Media availability, OBS online state, worker state, and last forwarding errors appear in the dashboard.

## Safe updates

Dashboard-only changes do not require restarting media services:

```bash
docker compose build site
docker compose up -d --no-deps site
```

Router changes restart destination workers and briefly interrupt forwarding:

```bash
docker compose build router
docker compose up -d --no-deps router
```

A router restart also stops every stream's ingest→program copy, so the backup screen is on air for the restart window and OBS returns on the next keyframe once the router is back; OBS itself stays connected.

MediaMTX changes interrupt ingest and monitoring. Schedule them while nobody is live:

```bash
docker compose up -d --no-deps mediamtx
```

Before a router or MediaMTX restart, check the dashboard and logs for active publishers/outputs. Never run `docker compose down -v` in production; `-v` deletes the database, encrypted credentials, and uploaded screens.

## Backup

The `relay_data` volume contains `relay.db` and `media/`. Back up the volume together with the current `.env`; both are required because `FERNET_KEY` decrypts stored SRT credentials, destination keys, and OAuth tokens.

One simple offline backup pattern is:

```bash
docker compose stop router mediamtx
docker run --rm -v relay_relay_data:/source:ro -v "$PWD/backups:/backup" alpine \
  tar czf /backup/relay-data-$(date +%F-%H%M).tgz -C /source .
cp .env backups/env-$(date +%F-%H%M)
chmod 600 backups/env-*
docker compose start router mediamtx
```

Keep backups outside the public repository. Test restores on a separate Docker volume.

## Restore

1. Stop `router` and `mediamtx`.
2. Restore the volume archive into an empty `relay_data` volume.
3. Restore the matching `.env` containing the original `FERNET_KEY`.
4. Start `router`, then `mediamtx`, then verify `/health`, login, screens, and destinations.

## Common symptoms

- OBS green but monitor blank: inspect MediaMTX path state (`<slug>` is OBS, `<slug>/program` is what viewers get) and HLS requests; confirm the user is signed in and audio/video tracks were detected.
- OBS connected but the backup screen stays on air with Live input selected: the dashboard notice names the cause. Almost always a track-layout mismatch — OBS is sending one or three audio tracks and the screens carry two — which the media server refuses at the program path. Fix the OBS output; nothing on the server needs restarting.
- Forwarder repeatedly reconnects: inspect the destination’s redacted last error and router logs; verify the platform key is current.
- BRB unavailable for a new user: upload a ready BRB for the owner first so `_default/brb.mp4` can seed invited users.
- Twitch outage ads unavailable: verify OAuth configuration, reconnect Twitch, and confirm both required scopes were granted.
- Music reaching YouTube or X: check how many audio tracks that stream is publishing before anything else. While both tracks arrive, those platforms are sent OBS track 2 and there is no setting to change, so music means track 2 in OBS is not the clean/game mix. While only one track arrives, they fall back to track 1 — the full mix — by default, and the fix is enabling track 2 in that user's OBS streaming output. Setting `music_fallback = 0` on the destination mutes it in that state instead, but does not repair the publisher.
