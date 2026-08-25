# Deployment

## Prerequisites

- Linux VPS with Docker Engine and Compose v2.
- DNS A/AAAA record for the dashboard hostname.
- TCP 80/443 reachable by the reverse proxy.
- UDP 8890 (or the configured `SRT_PORT`) open to OBS publishers.
- A Caddy container attached to a shared external Docker network. The default network name is `caddy`; set `PROXY_NETWORK` if yours differs.

The cheapest VPS tiers are sufficient for ordinary video-copy forwarding. Audio-mix modes consume some CPU. Video cropping/scaling/transcoding is intentionally not part of the initial horizontal forwarding path.

## Install

```bash
git clone https://github.com/kittynXR/restreamer.git
cd restreamer
chmod +x generate-env.sh
PUBLIC_HOST=relay.example.com PROXY_NETWORK=caddy ./generate-env.sh
docker network create caddy 2>/dev/null || true
docker compose build
docker compose up -d
```

`generate-env.sh` prints the one-time bootstrap token. Store it temporarily in a password manager and use it at the first-run dashboard. It is not needed after the owner exists, but rotating/removing it later is still recommended.

## Reverse proxy

Copy the `Caddyfile` block into the existing Caddy configuration. Ensure Caddy is attached to the network named by `PROXY_NETWORK`, replace the example hostname, and reload Caddy.

The proxy sends `/api/*`, `/media/*`, and `/health` to `relay-router:8787`; everything else goes to `relay-site:3000`.

## Twitch integration

Create a Twitch developer application with this OAuth redirect URL:

```text
https://relay.example.com/api/twitch/callback
```

Set `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, and (optionally) `TWITCH_REDIRECT_URI` in `.env`, then recreate only the router:

```bash
docker compose up -d --build --no-deps router
```

The integration requests `channel:read:ads` and `channel:edit:commercial`. Users connect their own broadcaster accounts from the dashboard.

## First-run checklist

1. Open the HTTPS dashboard and create the owner with the bootstrap token.
2. Upload a BRB video. Relay converts it to the failover format and makes it the default for future invited users.
3. Optionally upload Starting Soon.
4. Open Quick start, copy the generated SRT URL, and configure OBS.
5. Add destination keys, leave every destination off, and test them one at a time.
6. Connect Twitch and arm outage ads only if the streamer wants automatic commercials.

## Firewall

Expose only TCP 22 (restricted if possible), TCP 80/443, and the configured SRT UDP port. Do not expose ports 8787, 8554, 8888, or 9997 publicly.
