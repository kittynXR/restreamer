# Relay Restreamer

Relay is a self-hosted, multi-user restreaming control plane for a VPS. OBS sends one SRT feed to the VPS; Relay forwards it to Twitch, YouTube, RPLAY, X, or a custom RTMP destination. Video is copied whenever possible, so ordinary forwarding is inexpensive. Audio is selected or mixed per destination.

The project includes an OLED dashboard, per-user OBS credentials, destination toggles with confirmation, browser monitoring, BRB/Starting Soon uploads, connection-loss failover, Twitch VOD-track handling, team invitations, automatic Twitch ingest selection, and optional Twitch outage ads.

## Data flow

```text
OBS --SRT/UDP--> MediaMTX --RTSP--> per-destination FFmpeg --> Twitch / YouTube / RPLAY / X
                         |                    |
                         |                    +-- video copy; audio copy or mix
                         +-- HLS monitor

Browser --HTTPS--> Caddy --> dashboard + FastAPI router --> SQLite + encrypted secrets
```

## Repository layout

- `router/`: FastAPI control plane, SQLite schema, FFmpeg workers, failover logic, and tests.
- `site/`: React/Vinext dashboard.
- `mediamtx.yml`: SRT ingest, RTSP fan-out, HLS monitoring, and HTTP auth.
- `docker-compose.yml`: MediaMTX, router, and dashboard services.
- `Caddyfile`: reverse-proxy example for a Caddy instance on the shared proxy network.
- `docs/`: architecture, deployment, operations, development, OBS, and security handoff.
- `AGENTS.md`: invariants and working rules for future coding agents.

## Quick start

Requirements: a Linux VPS, Docker Engine with Compose v2, a DNS name, UDP port 8890 open, and an existing Caddy-compatible Docker network.

```bash
chmod +x generate-env.sh
PUBLIC_HOST=relay.example.com PROXY_NETWORK=caddy ./generate-env.sh
# Add Twitch credentials to .env only if using the Twitch integration.
docker network create caddy 2>/dev/null || true
docker compose build
docker compose up -d
```

Add `Caddyfile` to the Caddy instance, replace `relay.example.com`, and reload Caddy. Visit the HTTPS dashboard and use the bootstrap token printed by `generate-env.sh` to create the owner account. Then upload a BRB video and use the dashboard’s OBS walkthrough to copy the per-user SRT address.

Read [Deployment](docs/DEPLOYMENT.md) before a production install and [Operations](docs/OPERATIONS.md) before changing a running relay.

## Development checks

```bash
cd site
npm ci
npm run lint
npm run build

cd ../router
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## Security

Never commit `.env`, the SQLite database, uploaded media, OAuth tokens, stream keys, or backups. Destination keys and Twitch tokens are encrypted at rest with `FERNET_KEY`; losing that key makes them unrecoverable. See [Security](docs/SECURITY.md).
