# Development

## Dashboard

Requirements: Node.js 22.13 or newer.

```bash
cd site
npm ci
npm run dev
```

The dashboard expects the API on the same origin. For full local integration, run the containers and place the dashboard behind the provided reverse-proxy routes. Before committing:

```bash
npm run lint
npm run build
```

The dashboard is intentionally a compact single-page control surface. Reuse existing components and OLED design tokens. Keep destructive/stream-affecting actions behind confirmation dialogs and preserve accessible names on icon-only controls.

## Router

Requirements: Python 3.13, FFmpeg/ffprobe 8.1-compatible behavior, and the environment variables in `.env.example`.

```bash
cd router
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The test suite creates a temporary SQLite database and mocks MediaMTX/background managers. Extend it whenever changing authentication, user isolation, program modes, invitations, or destination construction.

## Database evolution

Schema creation and lightweight migrations currently live in `init_db()` in `router/app/main.py`. Migrations must be idempotent and preserve existing production rows. Never change encryption formats without a migration and rollback plan.

## Adding a destination platform

1. Add the platform identifier and key/full-URL behavior to the dashboard form.
2. Validate the input in the router.
3. Construct the output URL only inside the worker, after decrypting the stored secret.
4. Keep the final URL and key out of logs/errors.
5. Add a recognizable local logo with accessible labeling.
6. Add tests for validation, persistence, audio restrictions, and worker URL construction.
7. Document platform-specific ingest and audio behavior.

## Release checklist

1. Run dashboard lint/build and router tests.
2. Run `docker compose config` and build changed images.
3. Scan tracked content for secrets and generated state.
4. Review the Git diff, including lockfile changes.
5. Deploy only changed services.
6. Verify `/health`, login, monitor, one disabled destination toggle flow, and service logs.
