#!/bin/sh
set -eu

umask 077
if [ -f .env ]; then
  exit 0
fi

session_secret="$(openssl rand -hex 32)"
bootstrap_token="$(openssl rand -hex 12)"
fernet_key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')="
media_user="internal_$(openssl rand -hex 6)"
media_pass="$(openssl rand -base64 24 | tr -d '\n')"
public_host="${PUBLIC_HOST:-relay.example.com}"
srt_port="${SRT_PORT:-8890}"
proxy_network="${PROXY_NETWORK:-caddy}"

printf '%s\n' \
  "PUBLIC_HOST=$public_host" \
  "SRT_PORT=$srt_port" \
  "PROXY_NETWORK=$proxy_network" \
  "SESSION_SECRET=$session_secret" \
  "BOOTSTRAP_TOKEN=$bootstrap_token" \
  "FERNET_KEY=$fernet_key" \
  "MEDIA_INTERNAL_USER=$media_user" \
  "MEDIA_INTERNAL_PASS=$media_pass" \
  "TWITCH_CLIENT_ID=" \
  "TWITCH_CLIENT_SECRET=" \
  "TWITCH_REDIRECT_URI=" > .env

chmod 600 .env
printf 'Bootstrap token: %s\n' "$bootstrap_token"
