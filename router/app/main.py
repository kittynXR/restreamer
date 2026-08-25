from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import ssl
import statistics
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlencode, urlparse

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware


DB_PATH = Path(os.getenv("DB_PATH", "/data/relay.db"))
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "relay.example.com")
SRT_PORT = int(os.getenv("SRT_PORT", "8890"))
SESSION_SECRET = os.environ["SESSION_SECRET"]
BOOTSTRAP_TOKEN = os.environ["BOOTSTRAP_TOKEN"]
FERNET = Fernet(os.environ["FERNET_KEY"].encode())
MEDIA_INTERNAL_USER = os.environ["MEDIA_INTERNAL_USER"]
MEDIA_INTERNAL_PASS = os.environ["MEDIA_INTERNAL_PASS"]
MEDIAMTX_API = os.getenv("MEDIAMTX_API", "http://mediamtx:9997")
MEDIAMTX_HLS = os.getenv("MEDIAMTX_HLS", "http://mediamtx:8888")
MEDIAMTX_RTSP = os.getenv("MEDIAMTX_RTSP", "mediamtx:8554")
PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")
TWITCH_INGESTS_URL = "https://ingest.twitch.tv/ingests"
TWITCH_REFRESH_SECONDS = 6 * 60 * 60
RPLAY_BASE_URL = "rtmp://livestream-push.rplay.live/live"
X_BASE_URL = "rtmp://ca.pscp.tv:80/x"
YOUTUBE_PRIMARY_BASE_URL = "rtmp://a.rtmp.youtube.com/live2"
YOUTUBE_BACKUP_BASE_URL = "rtmp://b.rtmp.youtube.com/live2"
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
TWITCH_REDIRECT_URI = os.getenv(
    "TWITCH_REDIRECT_URI",
    f"https://{PUBLIC_HOST}/api/twitch/callback",
)
TWITCH_SCOPES = ("channel:read:ads", "channel:edit:commercial")
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
TWITCH_API = "https://api.twitch.tv/helix"
FAILOVER_GRACE_SECONDS = 60
MEDIA_MAX_BYTES = 2 * 1024 * 1024 * 1024
MEDIA_MAX_DURATION_SECONDS = 30 * 60
INVITE_DEFAULT_DAYS = 7
INVITE_MAX_DAYS = 30
TWITCH_FALLBACK = {
    "name": "US West (Oregon)",
    "url_template_secure": "rtmps://usw20.contribute.live-video.net/app/{stream_key}",
    "latency_ms": None,
    "checked_at": None,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'streamer',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS streams (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                slug TEXT NOT NULL UNIQUE,
                publish_user TEXT NOT NULL UNIQUE,
                publish_password_enc TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS destinations (
                id INTEGER PRIMARY KEY,
                stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                platform TEXT NOT NULL,
                output_url_enc TEXT NOT NULL,
                audio_track INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'off',
                last_error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_destinations_stream_id ON destinations(stream_id);
            CREATE INDEX IF NOT EXISTS idx_destinations_enabled ON destinations(enabled) WHERE enabled = 1;
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS twitch_connections (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                broadcaster_id TEXT NOT NULL,
                login TEXT NOT NULL,
                access_token_enc TEXT NOT NULL,
                refresh_token_enc TEXT NOT NULL,
                scopes TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                validated_at TEXT NOT NULL,
                connected_at TEXT NOT NULL,
                failover_ads_enabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS failover_ad_events (
                id INTEGER PRIMARY KEY,
                stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                failover_started_at TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                preroll_before INTEGER,
                requested_length INTEGER,
                status TEXT NOT NULL,
                message TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_failover_ad_events_stream_id
                ON failover_ad_events(stream_id, id DESC);
            CREATE TABLE IF NOT EXISTS media_assets (
                stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('brb', 'starting_soon')),
                status TEXT NOT NULL,
                original_name TEXT,
                message TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(stream_id, kind)
            );
            CREATE TABLE IF NOT EXISTS team_invites (
                id INTEGER PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_team_invites_pending
                ON team_invites(expires_at) WHERE used_at IS NULL AND revoked_at IS NULL;
            PRAGMA optimize;
            """
        )
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "enabled" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "UPDATE media_assets SET status = 'error', message = 'Conversion was interrupted; upload the file again.' WHERE status = 'converting'"
        )
    ensure_default_media_template()


def encrypt(value: str) -> str:
    return FERNET.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return FERNET.decrypt(value.encode()).decode()


def invite_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def valid_invite(conn: sqlite3.Connection, token: str) -> sqlite3.Row:
    row = conn.execute(
        """SELECT i.*, u.display_name AS invited_by
           FROM team_invites i JOIN users u ON u.id = i.created_by
           WHERE i.token_hash = ?""",
        (invite_digest(token),),
    ).fetchone()
    unavailable = (
        row is None
        or row["used_at"] is not None
        or row["revoked_at"] is not None
        or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)
    )
    if unavailable:
        raise HTTPException(status_code=410, detail="This invitation is invalid or no longer available")
    return row


def current_user(request: Request) -> sqlite3.Row | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ? AND enabled = 1", (user_id,)).fetchone()


def require_user(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def require_owner(request: Request) -> sqlite3.Row:
    user = require_user(request)
    if user["role"] != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")
    return user


def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if not expected or not secrets.compare_digest(expected, supplied or ""):
        raise HTTPException(status_code=403, detail="Invalid request token")


def stream_for_user(user_id: int) -> sqlite3.Row:
    with connect() as conn:
        row = conn.execute("SELECT * FROM streams WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Stream profile not found")
    return row


class SetupBody(BaseModel):
    token: str
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=12, max_length=200)


class LoginBody(BaseModel):
    username: str
    password: str


class InviteLookupBody(BaseModel):
    token: str = Field(min_length=32, max_length=200)


class InviteAcceptBody(InviteLookupBody):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=12, max_length=200)


class InviteCreateBody(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    expires_in_days: int = Field(default=INVITE_DEFAULT_DAYS, ge=1, le=INVITE_MAX_DAYS)


class TeamMemberStatusBody(BaseModel):
    enabled: bool


class DestinationBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    platform: str = Field(pattern=r"^(twitch|youtube|rplay|x|custom)$")
    output_url: str = Field(min_length=12, max_length=2000)
    audio_track: int = Field(ge=1, le=4)


class ToggleBody(BaseModel):
    enabled: bool


class FailoverAdsBody(BaseModel):
    enabled: bool


class ScreenModeBody(BaseModel):
    mode: str = Field(pattern=r"^(live|brb|starting_soon)$")


class TwitchIngestManager:
    def __init__(self) -> None:
        self.selection: dict[str, Any] = dict(TWITCH_FALLBACK)
        self.task: asyncio.Task | None = None
        self.ssl_context = ssl.create_default_context()

    def load_saved(self) -> None:
        with connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'twitch_ingest'").fetchone()
        if row:
            try:
                saved = json.loads(row["value"])
                if self._valid_ingest(saved):
                    self.selection = saved
            except (json.JSONDecodeError, TypeError):
                pass

    @staticmethod
    def _valid_ingest(ingest: dict[str, Any]) -> bool:
        template = ingest.get("url_template_secure", "")
        parsed = urlparse(template)
        return (
            parsed.scheme == "rtmps"
            and parsed.hostname is not None
            and parsed.hostname.endswith(".live-video.net")
            and "{stream_key}" in template
        )

    async def _connect_time(self, host: str) -> float | None:
        started = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host,
                    443,
                    ssl=self.ssl_context,
                    server_hostname=host,
                ),
                timeout=4,
            )
            elapsed = (time.perf_counter() - started) * 1000
            writer.close()
            await writer.wait_closed()
            return elapsed
        except Exception:
            return None

    async def _probe(self, ingest: dict[str, Any]) -> dict[str, Any] | None:
        if not self._valid_ingest(ingest):
            return None
        host = urlparse(ingest["url_template_secure"]).hostname
        assert host is not None
        samples = [await self._connect_time(host) for _ in range(3)]
        successful = [sample for sample in samples if sample is not None]
        if not successful:
            return None
        return {
            "name": str(ingest.get("name") or host),
            "url_template_secure": ingest["url_template_secure"],
            "latency_ms": round(statistics.median(successful), 1),
            "checked_at": now(),
        }

    async def refresh(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(TWITCH_INGESTS_URL)
            response.raise_for_status()
            ingests = response.json().get("ingests", [])
        results = await asyncio.gather(*(self._probe(ingest) for ingest in ingests))
        candidates = [result for result in results if result is not None]
        if not candidates:
            return
        selection = min(candidates, key=lambda item: item["latency_ms"])
        self.selection = selection
        with connect() as conn:
            conn.execute(
                """INSERT INTO app_settings(key, value, updated_at) VALUES('twitch_ingest', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (json.dumps(selection), now()),
            )

    async def start(self) -> None:
        self.load_saved()
        try:
            await self.refresh()
        except Exception:
            pass
        self.task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(TWITCH_REFRESH_SECONDS)
            try:
                await self.refresh()
            except Exception:
                pass

    async def shutdown(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def output_url(self, stream_key: str) -> str:
        return self.selection["url_template_secure"].replace("{stream_key}", stream_key)


twitch_ingests = TwitchIngestManager()


def twitch_configured() -> bool:
    return bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET)


def twitch_connection(user_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM twitch_connections WHERE user_id = ?",
            (user_id,),
        ).fetchone()


async def validate_twitch_token(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(
            TWITCH_VALIDATE_URL,
            headers={"Authorization": f"OAuth {access_token}"},
        )
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Twitch authorization expired")
    response.raise_for_status()
    return response.json()


async def refresh_twitch_token(user_id: int, refresh_token: str) -> str:
    if not twitch_configured():
        raise HTTPException(status_code=503, detail="Twitch integration is not configured")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            TWITCH_TOKEN_URL,
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    if response.status_code >= 400:
        with connect() as conn:
            conn.execute(
                "UPDATE twitch_connections SET failover_ads_enabled = 0 WHERE user_id = ?",
                (user_id,),
            )
        raise HTTPException(status_code=401, detail="Reconnect Twitch to use automatic outage ads")
    payload = response.json()
    access_token = payload["access_token"]
    next_refresh = payload.get("refresh_token") or refresh_token
    validated = await validate_twitch_token(access_token)
    scopes = validated.get("scopes", payload.get("scope", []))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(validated.get("expires_in", 0)))
    with connect() as conn:
        conn.execute(
            """UPDATE twitch_connections
               SET access_token_enc = ?, refresh_token_enc = ?, scopes = ?,
                   expires_at = ?, validated_at = ?, login = ?, broadcaster_id = ?
               WHERE user_id = ?""",
            (
                encrypt(access_token),
                encrypt(next_refresh),
                json.dumps(scopes),
                expires_at.isoformat(),
                now(),
                validated.get("login", ""),
                validated.get("user_id", ""),
                user_id,
            ),
        )
    return access_token


async def twitch_access_token(user_id: int, force_validate: bool = False) -> tuple[str, sqlite3.Row]:
    row = twitch_connection(user_id)
    if row is None:
        raise HTTPException(status_code=409, detail="Connect Twitch first")
    access_token = decrypt(row["access_token_enc"])
    expires_at = datetime.fromisoformat(row["expires_at"])
    validated_at = datetime.fromisoformat(row["validated_at"])
    current = datetime.now(timezone.utc)
    needs_refresh = expires_at <= current + timedelta(minutes=2)
    needs_validate = force_validate or validated_at <= current - timedelta(hours=1)
    if needs_refresh:
        access_token = await refresh_twitch_token(user_id, decrypt(row["refresh_token_enc"]))
    elif needs_validate:
        try:
            validated = await validate_twitch_token(access_token)
        except HTTPException:
            access_token = await refresh_twitch_token(user_id, decrypt(row["refresh_token_enc"]))
        else:
            scopes = validated.get("scopes", [])
            expires_at = current + timedelta(seconds=int(validated.get("expires_in", 0)))
            with connect() as conn:
                conn.execute(
                    """UPDATE twitch_connections
                       SET scopes = ?, expires_at = ?, validated_at = ?, login = ?, broadcaster_id = ?
                       WHERE user_id = ?""",
                    (
                        json.dumps(scopes),
                        expires_at.isoformat(),
                        now(),
                        validated.get("login", row["login"]),
                        validated.get("user_id", row["broadcaster_id"]),
                        user_id,
                    ),
                )
    refreshed = twitch_connection(user_id)
    assert refreshed is not None
    return access_token, refreshed


def twitch_state(user_id: int, stream_id: int) -> dict[str, Any]:
    row = twitch_connection(user_id)
    with connect() as conn:
        last_event = conn.execute(
            """SELECT failover_started_at, checked_at, preroll_before,
                      requested_length, status, message
               FROM failover_ad_events WHERE stream_id = ?
               ORDER BY id DESC LIMIT 1""",
            (stream_id,),
        ).fetchone()
    return {
        "available": twitch_configured(),
        "connected": row is not None,
        "login": row["login"] if row else None,
        "failover_ads_enabled": bool(row["failover_ads_enabled"]) if row else False,
        "grace_seconds": FAILOVER_GRACE_SECONDS,
        "last_event": dict(last_event) if last_event else None,
    }


def screen_mode_key(stream_id: int) -> str:
    return f"screen_mode:{stream_id}"


def current_screen_mode(stream_id: int) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (screen_mode_key(stream_id),),
        ).fetchone()
    return row["value"] if row and row["value"] in {"brb", "starting_soon"} else "brb"


def set_screen_mode(stream_id: int, mode: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (screen_mode_key(stream_id), mode, now()),
        )


def program_mode_key(stream_id: int) -> str:
    return f"program_mode:{stream_id}"


def current_program_mode(stream_id: int) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (program_mode_key(stream_id),),
        ).fetchone()
    return row["value"] if row and row["value"] in {"live", "brb", "starting_soon"} else "live"


def set_program_mode(stream_id: int, mode: str) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (program_mode_key(stream_id), mode, now()),
        )

def stream_media_dir(slug: str) -> Path:
    path = DB_PATH.parent / "media" / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_asset_path(slug: str, kind: str) -> Path:
    return stream_media_dir(slug) / f"{kind}.mp4"


def active_media_path(slug: str) -> Path:
    return stream_media_dir(slug) / "active.mp4"


def ensure_default_media_template() -> Path | None:
    media_root = DB_PATH.parent / "media"
    template_dir = media_root / "_default"
    template_dir.mkdir(parents=True, exist_ok=True)
    template = template_dir / "brb.mp4"
    if template.exists():
        return template
    if not DB_PATH.exists():
        return None
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.slug FROM streams s JOIN users u ON u.id = s.user_id
               ORDER BY CASE WHEN u.role = 'owner' THEN 0 ELSE 1 END, s.id"""
        ).fetchall()
    for row in rows:
        candidate = media_root / row["slug"] / "brb.mp4"
        if candidate.exists():
            shutil.copyfile(candidate, template)
            return template
    return None


def seed_stream_media(slug: str) -> bool:
    template = ensure_default_media_template()
    if template is None:
        return False
    brb = media_asset_path(slug, "brb")
    active = active_media_path(slug)
    if not brb.exists():
        shutil.copyfile(template, brb)
    if not active.exists():
        shutil.copyfile(brb, active)
    return True


def fallback_path_config(slug: str) -> dict[str, Any]:
    return {
        "source": "publisher",
        "overridePublisher": True,
        "alwaysAvailable": True,
        "alwaysAvailableFile": f"/relay-data/media/{slug}/active.mp4",
    }


async def ensure_fallback_path(slug: str, force_reload: bool = False) -> bool:
    if not seed_stream_media(slug):
        return False
    encoded = quote(slug, safe="")
    payload = fallback_path_config(slug)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{MEDIAMTX_API}/v3/config/paths/get/{encoded}")
        if response.status_code == 404:
            response = await client.post(
                f"{MEDIAMTX_API}/v3/config/paths/add/{encoded}",
                json=payload,
            )
            response.raise_for_status()
            return True
        response.raise_for_status()
        current = response.json()
        changed = any(current.get(key) != value for key, value in payload.items())
        if force_reload or changed:
            response = await client.patch(
                f"{MEDIAMTX_API}/v3/config/paths/patch/{encoded}",
                json=payload,
            )
            response.raise_for_status()
    return True


async def reload_fallback_path(slug: str) -> None:
    await ensure_fallback_path(slug, force_reload=True)


async def kick_stream_publishers(slug: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{MEDIAMTX_API}/v3/srtconns/list",
                params={"itemsPerPage": 100},
            )
            response.raise_for_status()
            for item in response.json().get("items", []):
                if item.get("path") == slug and item.get("state") == "publish":
                    kick = await client.post(
                        f"{MEDIAMTX_API}/v3/srtconns/kick/{quote(item['id'], safe='')}"
                    )
                    kick.raise_for_status()
    except Exception:
        pass


async def remove_fallback_path(slug: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.delete(
                f"{MEDIAMTX_API}/v3/config/paths/delete/{quote(slug, safe='')}"
            )
            if response.status_code != 404:
                response.raise_for_status()
    except Exception:
        pass


class PathReconciler:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def reconcile(self) -> None:
        with connect() as conn:
            slugs = [
                row["slug"]
                for row in conn.execute(
                    """SELECT s.slug FROM streams s JOIN users u ON u.id = s.user_id
                       WHERE u.enabled = 1 ORDER BY s.id"""
                )
            ]
        for slug in slugs:
            try:
                await ensure_fallback_path(slug)
            except Exception:
                pass

    async def _loop(self) -> None:
        while True:
            await self.reconcile()
            await asyncio.sleep(10)


path_reconciler = PathReconciler()


async def activate_screen_file(stream: sqlite3.Row, kind: str, reload_path: bool) -> None:
    source = media_asset_path(stream["slug"], kind)
    if not source.exists():
        raise HTTPException(status_code=409, detail=f"Upload the {kind.replace('_', ' ')} screen first")
    active = active_media_path(stream["slug"])
    pending = active.with_suffix(".pending.mp4")
    shutil.copyfile(source, pending)
    os.replace(pending, active)
    set_screen_mode(stream["id"], kind)
    if reload_path:
        await reload_fallback_path(stream["slug"])


def media_assets_state(stream: sqlite3.Row) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT kind, status, original_name, message, updated_at FROM media_assets WHERE stream_id = ?",
            (stream["id"],),
        ).fetchall()
    assets = {row["kind"]: dict(row) for row in rows}
    for kind in ("brb", "starting_soon"):
        if kind not in assets and media_asset_path(stream["slug"], kind).exists():
            assets[kind] = {
                "kind": kind,
                "status": "ready",
                "original_name": "Current relay screen",
                "message": None,
                "updated_at": None,
            }
        elif kind not in assets:
            assets[kind] = {
                "kind": kind,
                "status": "missing",
                "original_name": None,
                "message": None,
                "updated_at": None,
            }
    return {
        "mode": current_screen_mode(stream["id"]),
        "program_mode": current_program_mode(stream["id"]),
        "brb": assets["brb"],
        "starting_soon": assets["starting_soon"],
    }


class MediaConversionManager:
    def __init__(self) -> None:
        self.tasks: dict[tuple[int, str], asyncio.Task] = {}

    def running(self, stream_id: int, kind: str) -> bool:
        task = self.tasks.get((stream_id, kind))
        return bool(task and not task.done())

    def start(self, stream: sqlite3.Row, kind: str, source: Path, original_name: str) -> None:
        key = (stream["id"], kind)
        if self.running(*key):
            raise HTTPException(status_code=409, detail="That screen is already being converted")
        self.tasks[key] = asyncio.create_task(
            self._convert(dict(stream), kind, source, original_name)
        )

    async def shutdown(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def _set_status(
        self,
        stream_id: int,
        kind: str,
        status: str,
        original_name: str,
        message: str | None = None,
    ) -> None:
        with connect() as conn:
            conn.execute(
                """INSERT INTO media_assets(stream_id, kind, status, original_name, message, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(stream_id, kind) DO UPDATE SET
                       status = excluded.status,
                       original_name = excluded.original_name,
                       message = excluded.message,
                       updated_at = excluded.updated_at""",
                (stream_id, kind, status, original_name, message, now()),
            )

    async def _probe(self, source: Path) -> tuple[float, bool]:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            detail = stderr.decode(errors="replace").strip()
            raise ValueError(detail or "The uploaded file is not a readable video")
        payload = json.loads(stdout)
        duration = float(payload.get("format", {}).get("duration") or 0)
        has_audio = any(item.get("codec_type") == "audio" for item in payload.get("streams", []))
        if duration < 2:
            raise ValueError("Screen videos must be at least 2 seconds long")
        if duration > MEDIA_MAX_DURATION_SECONDS:
            raise ValueError("Screen videos can be up to 30 minutes long")
        return duration, has_audio

    async def _convert(
        self,
        stream: dict[str, Any],
        kind: str,
        source: Path,
        original_name: str,
    ) -> None:
        output = media_asset_path(stream["slug"], kind).with_suffix(".converting.mp4")
        process: asyncio.subprocess.Process | None = None
        try:
            duration, has_audio = await self._probe(source)
            command = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-i",
                str(source),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
            ]
            video_filter = (
                "[0:v:0]scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
                "fps=48,format=yuv420p[v]"
            )
            if has_audio:
                command.extend(
                    [
                        "-filter_complex",
                        video_filter + ";[0:a:0]aresample=48000,apad[a1]",
                        "-map",
                        "[v]",
                        "-map",
                        "[a1]",
                        "-map",
                        "1:a:0",
                    ]
                )
            else:
                command.extend(
                    [
                        "-filter_complex",
                        video_filter,
                        "-map",
                        "[v]",
                        "-map",
                        "1:a:0",
                        "-map",
                        "1:a:0",
                    ]
                )
            command.extend(
                [
                    "-t",
                    f"{duration:.3f}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-profile:v",
                    "high",
                    "-level:v",
                    "4.2",
                    "-b:v",
                    "8000k",
                    "-maxrate",
                    "8000k",
                    "-bufsize",
                    "16000k",
                    "-g",
                    "96",
                    "-keyint_min",
                    "96",
                    "-sc_threshold",
                    "0",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "2",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-map_metadata",
                    "-1",
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode:
                detail = stderr.decode(errors="replace").strip().splitlines()
                raise RuntimeError(detail[-1] if detail else "FFmpeg could not convert this video")
            final = media_asset_path(stream["slug"], kind)
            os.replace(output, final)
            self._set_status(stream["id"], kind, "ready", original_name)
            mode = current_screen_mode(stream["id"])
            status = await media_status(stream["slug"])
            if kind == mode:
                await activate_screen_file(stream, kind, reload_path=not status["online"])
            elif kind == "brb" and status["online"]:
                await activate_screen_file(stream, "brb", reload_path=False)
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except Exception as exc:
            self._set_status(stream["id"], kind, "error", original_name, str(exc)[:500])
        finally:
            source.unlink(missing_ok=True)
            output.unlink(missing_ok=True)


media_conversions = MediaConversionManager()


class WorkerManager:
    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task] = {}
        self.stopping = False

    async def start_enabled(self) -> None:
        with connect() as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    """SELECT d.id FROM destinations d
                       JOIN streams s ON s.id = d.stream_id
                       JOIN users u ON u.id = s.user_id
                       WHERE d.enabled = 1 AND u.enabled = 1"""
                )
            ]
        for destination_id in ids:
            self.start(destination_id)

    def start(self, destination_id: int) -> None:
        task = self.tasks.get(destination_id)
        if task and not task.done():
            return
        self.tasks[destination_id] = asyncio.create_task(self._run(destination_id))

    async def stop(self, destination_id: int) -> None:
        task = self.tasks.pop(destination_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._set_state(destination_id, "off", None)

    async def shutdown(self) -> None:
        self.stopping = True
        for destination_id in list(self.tasks):
            await self.stop(destination_id)

    def _set_state(self, destination_id: int, state: str, error: str | None) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE destinations SET state = ?, last_error = ? WHERE id = ?",
                (state, error[:500] if error else None, destination_id),
            )

    async def _run(self, destination_id: int) -> None:
        process: asyncio.subprocess.Process | None = None
        pinned_output_url: str | None = None
        pinned_backup_url: str | None = None
        try:
            while not self.stopping:
                with connect() as conn:
                    row = conn.execute(
                        """SELECT d.*, s.slug, u.enabled AS user_enabled FROM destinations d
                           JOIN streams s ON s.id = d.stream_id
                           JOIN users u ON u.id = s.user_id
                           WHERE d.id = ?""",
                        (destination_id,),
                    ).fetchone()
                if row is None or not row["enabled"] or not row["user_enabled"]:
                    return

                if pinned_output_url is None:
                    destination_secret = decrypt(row["output_url_enc"])
                    if row["platform"] == "twitch" and not destination_secret.startswith(("rtmp://", "rtmps://")):
                        pinned_output_url = twitch_ingests.output_url(destination_secret)
                    elif row["platform"] == "rplay" and not destination_secret.startswith(("rtmp://", "rtmps://")):
                        pinned_output_url = f"{RPLAY_BASE_URL}/{destination_secret}"
                    elif row["platform"] == "x" and not destination_secret.startswith(("rtmp://", "rtmps://")):
                        pinned_output_url = f"{X_BASE_URL}/{destination_secret}"
                    elif row["platform"] == "youtube" and not destination_secret.startswith(("rtmp://", "rtmps://")):
                        pinned_output_url = f"{YOUTUBE_PRIMARY_BASE_URL}/{destination_secret}"
                        pinned_backup_url = f"{YOUTUBE_BACKUP_BASE_URL}/{destination_secret}?backup=1"
                    else:
                        pinned_output_url = destination_secret
                output_url = pinned_output_url
                source = (
                    f"rtsp://{quote(MEDIA_INTERNAL_USER, safe='')}:{quote(MEDIA_INTERNAL_PASS, safe='')}"
                    f"@{MEDIAMTX_RTSP}/{row['slug']}"
                )
                command = [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
                    "-rtsp_transport", "tcp", "-i", source,
                ]
                if row["audio_track"] in {3, 4}:
                    command.extend([
                        "-filter_complex",
                        "[0:a:0][0:a:1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.95[live]",
                        "-map", "0:v:0", "-map", "[live]",
                        "-c:v", "copy", "-c:a:0", "aac", "-b:a:0", "160k",
                    ])
                    if row["audio_track"] == 3:
                        command.extend(["-map", "0:a:1", "-c:a:1", "copy"])
                else:
                    command.extend([
                        "-map", "0:v:0", "-map", f"0:a:{row['audio_track'] - 1}",
                        "-c", "copy",
                    ])
                if pinned_backup_url:
                    tee_output = (
                        f"[f=flv:onfail=ignore:flvflags=no_duration_filesize]{output_url}"
                        f"|[f=flv:onfail=ignore:flvflags=no_duration_filesize]{pinned_backup_url}"
                    )
                    command.extend([
                        "-use_fifo", "1",
                        "-fifo_options", "attempt_recovery=1:recover_any_error=1:recovery_wait_time=1",
                        "-f", "tee", tee_output,
                    ])
                else:
                    command.extend([
                        "-flvflags", "no_duration_filesize",
                        "-f", "flv", output_url,
                    ])
                self._set_state(destination_id, "connecting", None)
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.sleep(2)
                if process.returncode is None:
                    self._set_state(destination_id, "forwarding", None)

                last_line = ""
                assert process.stderr is not None
                async for raw_line in process.stderr:
                    line = raw_line.decode(errors="replace").strip()
                    if line:
                        last_line = line.replace(output_url, "[destination]")
                        if pinned_backup_url:
                            last_line = last_line.replace(pinned_backup_url, "[backup destination]")
                code = await process.wait()
                process = None
                if self.stopping:
                    return
                self._set_state(destination_id, "retrying", last_line or f"Forwarder exited with code {code}")
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=4)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        except Exception as exc:
            self._set_state(destination_id, "error", str(exc))


workers = WorkerManager()


class FailoverAdManager:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.states: dict[int, dict[str, Any]] = {}
        self.last_token_sweep = 0.0

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def _record(
        self,
        stream_id: int,
        started_at: str,
        status: str,
        message: str,
        preroll_before: int | None = None,
        requested_length: int | None = None,
    ) -> None:
        with connect() as conn:
            conn.execute(
                """INSERT INTO failover_ad_events(
                       stream_id, failover_started_at, checked_at, preroll_before,
                       requested_length, status, message
                   ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    stream_id,
                    started_at,
                    now(),
                    preroll_before,
                    requested_length,
                    status,
                    message[:500],
                ),
            )

    async def _run_outage_check(self, stream: sqlite3.Row, started_at: str) -> None:
        with connect() as conn:
            twitch = conn.execute(
                "SELECT * FROM twitch_connections WHERE user_id = ?",
                (stream["user_id"],),
            ).fetchone()
            destination = conn.execute(
                """SELECT id, enabled, state FROM destinations
                   WHERE stream_id = ? AND platform = 'twitch'
                   ORDER BY enabled DESC, id LIMIT 1""",
                (stream["id"],),
            ).fetchone()
        if twitch is None or not twitch["failover_ads_enabled"]:
            self._record(stream["id"], started_at, "disabled", "Automatic outage ads were disabled")
            return
        if destination is None or not destination["enabled"]:
            self._record(stream["id"], started_at, "skipped", "Twitch forwarding was not enabled")
            return
        if destination["state"] != "forwarding":
            self._record(stream["id"], started_at, "skipped", "Twitch forwarding was not connected")
            return

        try:
            access_token, twitch = await twitch_access_token(stream["user_id"], force_validate=True)
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": TWITCH_CLIENT_ID,
            }
            async with httpx.AsyncClient(timeout=15) as client:
                schedule_response = await client.get(
                    f"{TWITCH_API}/channels/ads",
                    params={"broadcaster_id": twitch["broadcaster_id"]},
                    headers=headers,
                )
                schedule_response.raise_for_status()
                schedule = schedule_response.json().get("data", [])
                if not schedule:
                    raise RuntimeError("Twitch returned no ad schedule")
                preroll = int(schedule[0].get("preroll_free_time", 0))
                if preroll > 1800:
                    self._record(
                        stream["id"],
                        started_at,
                        "not_needed",
                        f"{math.ceil(preroll / 60)} minutes of preroll-free time remained",
                        preroll_before=preroll,
                    )
                    return
                length = min(180, max(90, math.ceil((3600 - preroll) / 600) * 30))
                commercial_response = await client.post(
                    f"{TWITCH_API}/channels/commercial",
                    headers=headers,
                    json={
                        "broadcaster_id": twitch["broadcaster_id"],
                        "length": length,
                    },
                )
                if commercial_response.status_code >= 400:
                    detail = commercial_response.json().get("message", "Twitch rejected the commercial")
                    self._record(
                        stream["id"],
                        started_at,
                        "failed",
                        detail,
                        preroll_before=preroll,
                        requested_length=length,
                    )
                    return
                actual = commercial_response.json().get("data", [{}])[0].get("length", length)
                self._record(
                    stream["id"],
                    started_at,
                    "started",
                    f"Twitch started a {actual}-second outage commercial",
                    preroll_before=preroll,
                    requested_length=length,
                )
        except Exception as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            self._record(stream["id"], started_at, "failed", detail)

    async def _validate_tokens(self) -> None:
        with connect() as conn:
            user_ids = [
                row["user_id"]
                for row in conn.execute(
                    """SELECT t.user_id FROM twitch_connections t
                       JOIN users u ON u.id = t.user_id WHERE u.enabled = 1"""
                )
            ]
        for user_id in user_ids:
            try:
                await twitch_access_token(user_id)
            except Exception:
                pass

    async def _prepare_brb(self, stream: sqlite3.Row) -> None:
        if not media_asset_path(stream["slug"], "brb").exists():
            return
        try:
            await activate_screen_file(stream, "brb", reload_path=False)
        except Exception:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                if time.monotonic() - self.last_token_sweep >= 60 * 60:
                    await self._validate_tokens()
                    self.last_token_sweep = time.monotonic()
                with connect() as conn:
                    streams = conn.execute(
                        """SELECT s.* FROM streams s JOIN users u ON u.id = s.user_id
                           WHERE u.enabled = 1 ORDER BY s.id"""
                    ).fetchall()
                present = {stream["id"] for stream in streams}
                for stale in set(self.states) - present:
                    self.states.pop(stale, None)
                for stream in streams:
                    media = await media_status(stream["slug"])
                    online = bool(media["online"])
                    state = self.states.setdefault(
                        stream["id"],
                        {
                            "last_online": None,
                            "outage_started": None,
                            "outage_started_at": None,
                            "attempted": False,
                            "manual_override": False,
                        },
                    )
                    manual_override = current_program_mode(stream["id"]) != "live"
                    if manual_override:
                        state["last_online"] = online
                        state["outage_started"] = None
                        state["outage_started_at"] = None
                        state["attempted"] = False
                        state["manual_override"] = True
                        continue
                    if state["manual_override"]:
                        state["last_online"] = True
                        state["manual_override"] = False
                    if state["last_online"] is None:
                        state["last_online"] = online
                        if online:
                            await self._prepare_brb(stream)
                        continue
                    if online:
                        if not state["last_online"]:
                            await self._prepare_brb(stream)
                        state["outage_started"] = None
                        state["outage_started_at"] = None
                        state["attempted"] = False
                    elif state["last_online"]:
                        state["outage_started"] = time.monotonic()
                        state["outage_started_at"] = now()
                        state["attempted"] = False
                    elif (
                        state["outage_started"] is not None
                        and not state["attempted"]
                        and time.monotonic() - state["outage_started"] >= FAILOVER_GRACE_SECONDS
                    ):
                        state["attempted"] = True
                        await self._run_outage_check(stream, state["outage_started_at"])
                    state["last_online"] = online
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(3)


failover_ads = FailoverAdManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_db()
    await path_reconciler.start()
    await twitch_ingests.start()
    await workers.start_enabled()
    await failover_ads.start()
    yield
    await failover_ads.shutdown()
    await path_reconciler.shutdown()
    await media_conversions.shutdown()
    await workers.shutdown()
    await twitch_ingests.shutdown()


app = FastAPI(title="Relay Control API", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=True,
    same_site="lax",
    max_age=60 * 60 * 24 * 14,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/session")
async def session_state(request: Request) -> dict[str, Any]:
    with connect() as conn:
        setup_required = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    user = current_user(request)
    if user and "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return {
        "setup_required": setup_required,
        "authenticated": user is not None,
        "user": {"username": user["username"], "display_name": user["display_name"], "role": user["role"]} if user else None,
        "csrf": request.session.get("csrf") if user else None,
    }


@app.post("/api/setup", status_code=201)
async def setup(body: SetupBody, request: Request) -> dict[str, str]:
    if not secrets.compare_digest(body.token, BOOTSTRAP_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid setup code")
    username = body.username.lower().strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=422, detail="Use 3–32 lowercase letters, numbers, dashes, or underscores")
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise HTTPException(status_code=409, detail="Setup is already complete")
        cursor = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, role, created_at) VALUES(?,?,?,?,?)",
            (username, body.display_name.strip(), PASSWORDS.hash(body.password), "owner", now()),
        )
        user_id = cursor.lastrowid
        publish_user = f"pub_{secrets.token_hex(6)}"
        publish_password = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO streams(user_id, slug, publish_user, publish_password_enc, created_at) VALUES(?,?,?,?,?)",
            (user_id, "studio", publish_user, encrypt(publish_password), now()),
        )
    seed_stream_media("studio")
    try:
        await ensure_fallback_path("studio")
    except Exception:
        pass
    request.session.clear()
    request.session["user_id"] = user_id
    request.session["csrf"] = secrets.token_urlsafe(24)
    return {"status": "ready"}


@app.post("/api/login")
async def login(body: LoginBody, request: Request) -> dict[str, str]:
    with connect() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND enabled = 1",
            (body.username.lower().strip(),),
        ).fetchone()
    if user is None:
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    try:
        PASSWORDS.verify(user["password_hash"], body.password)
    except VerifyMismatchError:
        raise HTTPException(status_code=401, detail="Incorrect username or password") from None
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["csrf"] = secrets.token_urlsafe(24)
    return {"status": "signed-in"}


@app.post("/api/logout")
async def logout(request: Request) -> dict[str, str]:
    require_user(request)
    require_csrf(request)
    request.session.clear()
    return {"status": "signed-out"}


@app.post("/api/invite")
async def inspect_invite(body: InviteLookupBody) -> dict[str, Any]:
    with connect() as conn:
        invite = valid_invite(conn, body.token)
    return {
        "label": invite["label"],
        "invited_by": invite["invited_by"],
        "expires_at": invite["expires_at"],
    }


@app.post("/api/invite/accept", status_code=201)
async def accept_invite(body: InviteAcceptBody, request: Request) -> dict[str, str]:
    username = body.username.lower().strip()
    display_name = body.display_name.strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(
            status_code=422,
            detail="Use 3–32 lowercase letters, numbers, dashes, or underscores",
        )
    if not display_name:
        raise HTTPException(status_code=422, detail="Display name is required")

    password_hash = PASSWORDS.hash(body.password)
    publish_user = f"pub_{secrets.token_hex(6)}"
    publish_password = secrets.token_urlsafe(24)
    created_at = now()
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            invite = valid_invite(conn, body.token)
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise HTTPException(status_code=409, detail="That username is already taken")
            cursor = conn.execute(
                """INSERT INTO users(
                       username, display_name, password_hash, role, enabled, created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (username, display_name, password_hash, "streamer", 1, created_at),
            )
            user_id = cursor.lastrowid
            while True:
                slug = f"stream-{secrets.token_hex(6)}"
                if not conn.execute("SELECT 1 FROM streams WHERE slug = ?", (slug,)).fetchone():
                    break
            conn.execute(
                """INSERT INTO streams(
                       user_id, slug, publish_user, publish_password_enc, created_at
                   ) VALUES(?,?,?,?,?)""",
                (user_id, slug, publish_user, encrypt(publish_password), created_at),
            )
            updated = conn.execute(
                """UPDATE team_invites SET used_at = ?, used_by = ?
                   WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL""",
                (created_at, user_id, invite["id"]),
            )
            if updated.rowcount != 1:
                raise HTTPException(status_code=410, detail="This invitation is no longer available")
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="That username is already taken") from exc

    seed_stream_media(slug)
    try:
        await ensure_fallback_path(slug)
    except Exception:
        pass
    request.session.clear()
    request.session["user_id"] = user_id
    request.session["csrf"] = secrets.token_urlsafe(24)
    return {"status": "account-created"}


@app.get("/api/team")
async def team_state(request: Request) -> dict[str, Any]:
    require_owner(request)
    timestamp = now()
    with connect() as conn:
        members = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.role, u.enabled, u.created_at,
                      s.slug,
                      (SELECT COUNT(*) FROM destinations d WHERE d.stream_id = s.id)
                          AS destination_count,
                      (SELECT COUNT(*) FROM destinations d
                       WHERE d.stream_id = s.id AND d.enabled = 1)
                          AS enabled_destination_count,
                      EXISTS(SELECT 1 FROM twitch_connections t WHERE t.user_id = u.id)
                          AS twitch_connected
               FROM users u JOIN streams s ON s.user_id = u.id
               ORDER BY CASE WHEN u.role = 'owner' THEN 0 ELSE 1 END,
                        lower(u.display_name), u.id"""
        ).fetchall()
        invitations = conn.execute(
            """SELECT id, label, created_at, expires_at
               FROM team_invites
               WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > ?
               ORDER BY id DESC""",
            (timestamp,),
        ).fetchall()
    return {
        "members": [
            {
                **dict(member),
                "enabled": bool(member["enabled"]),
                "twitch_connected": bool(member["twitch_connected"]),
            }
            for member in members
        ],
        "invitations": [dict(invitation) for invitation in invitations],
    }


@app.post("/api/team/invites", status_code=201)
async def create_invite(body: InviteCreateBody, request: Request) -> dict[str, Any]:
    owner = require_owner(request)
    require_csrf(request)
    label = body.label.strip()
    if not label:
        raise HTTPException(status_code=422, detail="Teammate name is required")
    token = secrets.token_urlsafe(32)
    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(days=body.expires_in_days)
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO team_invites(
                   token_hash, label, created_by, created_at, expires_at
               ) VALUES(?,?,?,?,?)""",
            (
                invite_digest(token),
                label,
                owner["id"],
                created_at.isoformat(),
                expires_at.isoformat(),
            ),
        )
    return {
        "id": cursor.lastrowid,
        "invite_url": f"https://{PUBLIC_HOST}/#invite={token}",
        "expires_at": expires_at.isoformat(),
    }


@app.delete("/api/team/invites/{invite_id}", status_code=204)
async def revoke_invite(invite_id: int, request: Request) -> Response:
    require_owner(request)
    require_csrf(request)
    with connect() as conn:
        updated = conn.execute(
            """UPDATE team_invites SET revoked_at = ?
               WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL""",
            (now(), invite_id),
        )
    if updated.rowcount != 1:
        raise HTTPException(status_code=404, detail="Pending invitation not found")
    return Response(status_code=204)


@app.patch("/api/team/users/{user_id}")
async def set_team_member_status(
    user_id: int,
    body: TeamMemberStatusBody,
    request: Request,
) -> dict[str, str]:
    require_owner(request)
    require_csrf(request)
    with connect() as conn:
        member = conn.execute(
            """SELECT u.id, u.role, u.enabled, s.id AS stream_id, s.slug
               FROM users u JOIN streams s ON s.user_id = u.id WHERE u.id = ?""",
            (user_id,),
        ).fetchone()
        if member is None:
            raise HTTPException(status_code=404, detail="Team member not found")
        if member["role"] == "owner":
            raise HTTPException(status_code=409, detail="The owner account cannot be suspended")
        destination_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM destinations WHERE stream_id = ?",
                (member["stream_id"],),
            )
        ]
        conn.execute("UPDATE users SET enabled = ? WHERE id = ?", (int(body.enabled), user_id))
        if not body.enabled:
            conn.execute(
                """UPDATE destinations SET enabled = 0, state = 'off', last_error = NULL
                   WHERE stream_id = ?""",
                (member["stream_id"],),
            )
            conn.execute(
                "UPDATE twitch_connections SET failover_ads_enabled = 0 WHERE user_id = ?",
                (user_id,),
            )

    if body.enabled:
        seed_stream_media(member["slug"])
        try:
            await ensure_fallback_path(member["slug"])
        except Exception:
            pass
        return {"status": "enabled"}

    for destination_id in destination_ids:
        await workers.stop(destination_id)
    await kick_stream_publishers(member["slug"])
    await remove_fallback_path(member["slug"])
    return {"status": "suspended"}


@app.get("/api/twitch/connect")
async def twitch_connect(request: Request) -> Response:
    user = require_user(request)
    if not twitch_configured():
        raise HTTPException(status_code=503, detail="Twitch integration is not configured")
    oauth_state = secrets.token_urlsafe(32)
    request.session["twitch_oauth_state"] = oauth_state
    request.session["twitch_oauth_user"] = user["id"]
    query = urlencode(
        {
            "client_id": TWITCH_CLIENT_ID,
            "redirect_uri": TWITCH_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(TWITCH_SCOPES),
            "state": oauth_state,
            "force_verify": "true",
        }
    )
    return RedirectResponse(f"https://id.twitch.tv/oauth2/authorize?{query}", status_code=302)


@app.get("/api/twitch/callback")
async def twitch_callback(request: Request) -> Response:
    user = require_user(request)
    expected_state = request.session.pop("twitch_oauth_state", None)
    expected_user = request.session.pop("twitch_oauth_user", None)
    supplied_state = request.query_params.get("state")
    code = request.query_params.get("code")
    if (
        not expected_state
        or not supplied_state
        or not secrets.compare_digest(expected_state, supplied_state)
        or expected_user != user["id"]
    ):
        raise HTTPException(status_code=400, detail="Twitch sign-in could not be verified")
    if not code:
        raise HTTPException(status_code=400, detail="Twitch sign-in was cancelled")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            TWITCH_TOKEN_URL,
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": TWITCH_REDIRECT_URI,
            },
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=400, detail="Twitch did not accept the sign-in")
    payload = response.json()
    access_token = payload["access_token"]
    refresh_token = payload["refresh_token"]
    validated = await validate_twitch_token(access_token)
    scopes = validated.get("scopes", payload.get("scope", []))
    missing = sorted(set(TWITCH_SCOPES) - set(scopes))
    if missing:
        raise HTTPException(status_code=400, detail="Twitch did not grant the required ad permissions")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(validated.get("expires_in", 0)))
    with connect() as conn:
        conn.execute(
            """INSERT INTO twitch_connections(
                   user_id, broadcaster_id, login, access_token_enc, refresh_token_enc,
                   scopes, expires_at, validated_at, connected_at, failover_ads_enabled
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(user_id) DO UPDATE SET
                   broadcaster_id = excluded.broadcaster_id,
                   login = excluded.login,
                   access_token_enc = excluded.access_token_enc,
                   refresh_token_enc = excluded.refresh_token_enc,
                   scopes = excluded.scopes,
                   expires_at = excluded.expires_at,
                   validated_at = excluded.validated_at,
                   connected_at = excluded.connected_at,
                   failover_ads_enabled = 0""",
            (
                user["id"],
                validated["user_id"],
                validated.get("login", ""),
                encrypt(access_token),
                encrypt(refresh_token),
                json.dumps(scopes),
                expires_at.isoformat(),
                now(),
                now(),
            ),
        )
    return RedirectResponse("/?twitch=connected", status_code=302)


@app.patch("/api/twitch/failover-ads")
async def set_failover_ads(body: FailoverAdsBody, request: Request) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM twitch_connections WHERE user_id = ?",
            (user["id"],),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="Connect Twitch first")
        conn.execute(
            "UPDATE twitch_connections SET failover_ads_enabled = ? WHERE user_id = ?",
            (int(body.enabled), user["id"]),
        )
    return {"status": "armed" if body.enabled else "disabled"}


@app.post("/api/twitch/disconnect")
async def twitch_disconnect(request: Request) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    with connect() as conn:
        conn.execute("DELETE FROM twitch_connections WHERE user_id = ?", (user["id"],))
    return {"status": "disconnected"}


@app.post("/api/screens/{kind}", status_code=202)
async def upload_screen(
    kind: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    if kind not in {"brb", "starting_soon"}:
        raise HTTPException(status_code=404, detail="Unknown screen type")
    stream = stream_for_user(user["id"])
    if media_conversions.running(stream["id"], kind):
        raise HTTPException(status_code=409, detail="That screen is already being converted")
    original_name = (file.filename or "screen video")[:200]
    source = stream_media_dir(stream["slug"]) / f".{kind}-{secrets.token_hex(8)}.upload"
    total = 0
    try:
        with source.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MEDIA_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Screen videos can be up to 2 GB")
                destination.write(chunk)
    except Exception:
        source.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    if total == 0:
        source.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="The uploaded file was empty")
    media_conversions._set_status(stream["id"], kind, "converting", original_name)
    media_conversions.start(stream, kind, source, original_name)
    return {"status": "converting"}


@app.patch("/api/screens/mode")
async def change_screen_mode(body: ScreenModeBody, request: Request) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    stream = stream_for_user(user["id"])
    status = await media_status(stream["slug"])
    if body.mode == "live":
        await activate_screen_file(stream, "brb", reload_path=not status["online"])
        set_program_mode(stream["id"], "live")
        return {"status": "live"}

    await activate_screen_file(stream, body.mode, reload_path=not status["online"])
    set_program_mode(stream["id"], body.mode)
    if status["online"]:
        await kick_stream_publishers(stream["slug"])
    return {"status": body.mode}

async def media_status(slug: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{MEDIAMTX_API}/v3/paths/list")
            response.raise_for_status()
            items = response.json().get("items", [])
        path = next((item for item in items if item.get("name") == slug), None)
        if not path:
            return {"available": False, "online": False, "tracks": []}
        return {
            "available": True,
            "online": bool(path.get("online")),
            "tracks": path.get("tracks", []),
            "bytes_received": path.get("bytesReceived", 0),
        }
    except Exception:
        return {"available": False, "online": False, "tracks": []}


@app.get("/api/state")
async def state(request: Request) -> dict[str, Any]:
    user = require_user(request)
    stream = stream_for_user(user["id"])
    with connect() as conn:
        destinations = conn.execute(
            "SELECT id, name, platform, audio_track, enabled, state, last_error FROM destinations WHERE stream_id = ? ORDER BY id",
            (stream["id"],),
        ).fetchall()
    publish_password = decrypt(stream["publish_password_enc"])
    stream_id = f"publish:{stream['slug']}:{stream['publish_user']}:{publish_password}"
    obs_url = f"srt://{PUBLIC_HOST}:{SRT_PORT}?streamid={stream_id}&pkt_size=1316&latency=500000"
    return {
        "user": {"username": user["username"], "display_name": user["display_name"], "role": user["role"]},
        "csrf": request.session["csrf"],
        "stream": {"slug": stream["slug"], "obs_url": obs_url, "media": await media_status(stream["slug"])},
        "twitch_ingest": twitch_ingests.selection,
        "twitch": twitch_state(user["id"], stream["id"]),
        "screens": media_assets_state(stream),
        "destinations": [dict(row) for row in destinations],
    }


def validate_output_url(value: str) -> None:
    if not value.startswith(("rtmp://", "rtmps://")):
        raise HTTPException(status_code=422, detail="Destination must use an RTMP or RTMPS address")


def validate_stream_key(value: str, platform: str) -> None:
    if value.startswith(("rtmp://", "rtmps://")) or any(character.isspace() for character in value):
        raise HTTPException(status_code=422, detail=f"Paste only the {platform} stream key, not an RTMP address")
    if len(value) < 12:
        raise HTTPException(status_code=422, detail=f"That {platform} stream key looks incomplete")
    if platform == "YouTube" and not re.fullmatch(r"[A-Za-z0-9-]+", value):
        raise HTTPException(status_code=422, detail="That YouTube stream key contains unexpected characters")


@app.post("/api/destinations", status_code=201)
async def add_destination(body: DestinationBody, request: Request) -> dict[str, int]:
    user = require_user(request)
    require_csrf(request)
    if body.platform in {"twitch", "youtube", "rplay", "x"}:
        validate_stream_key(body.output_url.strip(), body.platform.capitalize())
    else:
        validate_output_url(body.output_url.strip())
    if body.audio_track == 3 and body.platform != "twitch":
        raise HTTPException(status_code=422, detail="The separate Twitch VOD track is only available for Twitch")
    stream = stream_for_user(user["id"])
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO destinations(stream_id, name, platform, output_url_enc, audio_track, created_at) VALUES(?,?,?,?,?,?)",
            (stream["id"], body.name.strip(), body.platform, encrypt(body.output_url.strip()), body.audio_track, now()),
        )
    return {"id": cursor.lastrowid}


@app.patch("/api/destinations/{destination_id}")
async def toggle_destination(destination_id: int, body: ToggleBody, request: Request) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    stream = stream_for_user(user["id"])
    with connect() as conn:
        row = conn.execute("SELECT id FROM destinations WHERE id = ? AND stream_id = ?", (destination_id, stream["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Destination not found")
        conn.execute("UPDATE destinations SET enabled = ?, state = ? WHERE id = ?", (int(body.enabled), "connecting" if body.enabled else "off", destination_id))
    if body.enabled:
        workers.start(destination_id)
    else:
        await workers.stop(destination_id)
    return {"status": "enabled" if body.enabled else "disabled"}


@app.delete("/api/destinations/{destination_id}", status_code=204)
async def delete_destination(destination_id: int, request: Request) -> Response:
    user = require_user(request)
    require_csrf(request)
    stream = stream_for_user(user["id"])
    await workers.stop(destination_id)
    with connect() as conn:
        conn.execute("DELETE FROM destinations WHERE id = ? AND stream_id = ?", (destination_id, stream["id"]))
    return Response(status_code=204)


@app.post("/internal/mediamtx-auth")
async def mediamtx_auth(request: Request) -> Response:
    payload = await request.json()
    action = payload.get("action", "")
    path = payload.get("path", "")
    user = payload.get("user", "")
    password = payload.get("password", "")
    if action == "api":
        return Response(status_code=204)
    if action in {"read", "playback"} and secrets.compare_digest(user, MEDIA_INTERNAL_USER) and secrets.compare_digest(password, MEDIA_INTERNAL_PASS):
        return Response(status_code=204)
    if action == "publish":
        with connect() as conn:
            stream = conn.execute(
                """SELECT s.* FROM streams s JOIN users u ON u.id = s.user_id
                   WHERE s.slug = ? AND s.publish_user = ? AND u.enabled = 1""",
                (path, user),
            ).fetchone()
        if (
            stream
            and current_program_mode(stream["id"]) == "live"
            and secrets.compare_digest(password, decrypt(stream["publish_password_enc"]))
        ):
            return Response(status_code=204)
    return JSONResponse({"detail": "Forbidden"}, status_code=403)


@app.get("/media/{media_path:path}")
async def media_proxy(media_path: str, request: Request) -> Response:
    user = require_user(request)
    stream = stream_for_user(user["id"])
    if not (media_path == stream["slug"] or media_path.startswith(stream["slug"] + "/")):
        raise HTTPException(status_code=403, detail="This monitor belongs to another stream")
    if media_path == stream["slug"]:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/media/{stream['slug']}/{query}", status_code=307)
    basic = base64.b64encode(f"{MEDIA_INTERNAL_USER}:{MEDIA_INTERNAL_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}"}
    query = f"?{request.url.query}" if request.url.query else ""
    media_cookies = {
        name: request.cookies[name]
        for name in ("cookieCheck", "hlsSession")
        if name in request.cookies
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        upstream = await client.get(
            f"{MEDIAMTX_HLS}/{media_path}{query}",
            headers=headers,
            cookies=media_cookies,
        )
    excluded = {"content-length", "connection", "transfer-encoding", "content-encoding"}
    response_headers = {key: value for key, value in upstream.headers.items() if key.lower() not in excluded}
    return Response(upstream.content, status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get("content-type"))
