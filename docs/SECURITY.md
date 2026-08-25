# Security

## Secrets

`.env`, `relay.db`, uploaded media, backups, stream keys, SRT URLs, invitation URLs, OAuth codes/tokens, and `AGENTS.local.md` must never be committed. Treat an SRT URL as a password because it embeds publishing credentials.

`FERNET_KEY` encrypts destination URLs, publishing passwords, and Twitch access/refresh tokens. Back it up securely with the database. Rotate `SESSION_SECRET` to invalidate web sessions. Rotate `BOOTSTRAP_TOKEN` after first-run setup. Twitch client secrets must be stored only in `.env` and rotated immediately if disclosed.

## Network exposure

Only HTTPS and the SRT UDP port should be public. Keep the MediaMTX API, RTSP, HLS origin, router port, and SQLite volume private. The reverse proxy adds HSTS, content-type protection, same-origin framing, a strict referrer policy, and a restrictive permissions policy.

## Application controls

- Argon2 hashes account passwords.
- Session cookies are signed, HTTPS-only in production, and SameSite Lax.
- Mutating routes require CSRF tokens.
- MediaMTX delegates publish/read authorization to the router.
- User-owned queries are scoped by the signed-in user and stream ID.
- Recoverable secrets are encrypted before SQLite persistence.
- Destination errors redact output URLs before persistence.
- Upload size and duration are bounded and files are probed/converted by FFmpeg before activation.

## Public-release checklist

Before every push to a public remote:

```bash
git status --short
git ls-files
git grep -n -I -E '(client_secret|access_token|refresh_token|stream[_-]?key|password=|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY)'
```

Review matches; variable names and test fixtures are expected, real values are not. Also inspect untracked files and confirm `.gitignore` excludes runtime artifacts. Consider running gitleaks or GitHub secret scanning when available.

If a secret reaches Git history, revoke/rotate it first. Rewriting Git history does not make an already-disclosed secret safe.
