from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import shutil
import sqlite3
import ssl
import statistics
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterable, AsyncIterator, Iterator, Sequence
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
# Where MediaMTX sees the relay_data volume. The router writes active.mp4 under
# DB_PATH.parent/media; this is the same directory from MediaMTX's side.
MEDIAMTX_MEDIA_ROOT = os.getenv("MEDIAMTX_MEDIA_ROOT", "/relay-data/media")
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
# Every takeover parses this file's moov atom once per track before the first
# slate frame reaches a viewer, so the cap is a handoff-latency budget, not just
# a disk limit. Shorter screens hand off faster; 30 seconds is ideal for BRB.
MEDIA_MAX_DURATION_SECONDS = 10 * 60
# A time-left estimate for a screen conversion waits this long into the encode.
# Earlier than this the run is mostly ffmpeg's start-up, and a straight-line
# extrapolation from it swings wildly between polls.
CONVERSION_ESTIMATE_AFTER_SECONDS = 3.0
INVITE_DEFAULT_DAYS = 7
INVITE_MAX_DAYS = 30

# The failover slate must reproduce the contribution encoder's bitstream
# parameters, because MediaMTX reads SPS/PPS and AudioSpecificConfig once when
# the path is created and then swaps samples underneath. Any divergence forces a
# decoder reconfiguration on the viewer at the exact moment of the handoff.
SLATE_VIDEO_BITRATE = "3000k"
SLATE_AUDIO_BITRATE = "160k"
# Bump whenever the slate recipe below changes so stored screens can be
# recognised as stale and re-converted.
SLATE_ENCODER_VERSION = 3

# A failover screen only hands off cleanly when it reproduces the bitstream
# parameters of the stream it replaces, so the recipe is derived per stream from
# that user's own feed rather than fixed to one encoder. These defaults apply
# until a stream has been observed; two seconds is the keyframe interval both
# Twitch and YouTube specify.
DEFAULT_CONTRIBUTION: dict[str, Any] = {
    "width": 1920,
    "height": 1080,
    "fps": 60.0,
    "gop_seconds": 2.0,
    "profile": "high",
    "level": 42,
    "refs": 1,
    "bframes": 0,
    "sar": "1/1",
    "color_primaries": "bt709",
    "color_trc": "bt709",
    "colorspace": "bt709",
    "color_range": "tv",
}
CONTRIBUTION_FPS_RANGE = (10.0, 120.0)
CONTRIBUTION_PROBE_SECONDS = 4

# A failover slate only splices cleanly when it matches the geometry of the feed
# it stands in for: hand a 1080p60 stream a 720p48 slate and every OBS reconnect
# changes resolution and frame rate mid-stream underneath `-c:v copy`. Rather
# than encode a bespoke file per stream, one slate is cached per supported
# geometry and each stream is handed the closest match. Anything a streamer
# actually publishes snaps to one of these.
SLATE_VARIANTS: tuple[tuple[int, int, float], ...] = (
    (1280, 720, 30.0),
    (1280, 720, 48.0),
    (1280, 720, 60.0),
    (1920, 1080, 30.0),
    (1920, 1080, 48.0),
    (1920, 1080, 60.0),
)
# ffprobe reports human-readable profile names ("High 4:2:2 Predictive") that are
# not valid libx264 profile arguments, so probed values are mapped to the set
# x264 accepts and anything unrecognised falls back to high.
X264_PROFILES = {
    "baseline": "baseline", "constrainedbaseline": "baseline",
    "main": "main", "high": "high",
    "high10": "high10", "high10intra": "high10",
    "high422": "high422", "high422intra": "high422", "high422predictive": "high422",
    "high444": "high444", "high444predictive": "high444", "high444intra": "high444",
}

# Metrics sampling. One MediaMTX snapshot per tick is shared by every stream.
METRICS_INTERVAL_SECONDS = 1.0
METRICS_SIGNAL_SAMPLES = 240
METRICS_OUTPUT_SAMPLES = 120
METRICS_SERIES_POINTS = 40
# Lookback for the derived per-destination output rate, and the shortest span
# we will still answer from while a worker is warming up. See
# derive_output_rate for why this is seconds and not one -progress block.
METRICS_OUTPUT_RATE_WINDOW_S = 4.0
METRICS_OUTPUT_RATE_MIN_SPAN_S = 2.0

# Fast failover watchdog. Opt-in per stream: when the publisher is online but has
# delivered no bytes for this long, drop it so the slate can take over instead of
# waiting out MediaMTX's peer-idle timeout. Must stay comfortably above the
# negotiated SRT latency (500 ms) plus retransmission bursts.
# How long a connected publisher may deliver no bytes before fast failover
# drops it so the slate can take over. The floor is SRT's own recovery: with a
# 500 ms tsbpd window, gaps up to ~1 s are routinely refilled by retransmission
# and never reach a viewer, so kicking inside that band trades an invisible blip
# for a forced OBS reconnect. Detection adds the metrics sampling interval on
# top of this threshold, so the kick lands roughly 1.5-2.5 s after the feed dies.
FAST_FAILOVER_STALL_SECONDS = 1.5
FAST_FAILOVER_CHECK_SECONDS = 1.0

# The stall ledger's probe. The 1 s metrics sampler cannot see gaps shorter
# than its own tick -- on a healthy feed the byte counter advances every
# sample, so its resolution floor is a whole tick -- so recovered stalls are
# measured by a dedicated 4 Hz poll of the path byte counter, honest down to
# the 0.5 s floor. Events persist so the week's counts survive redeploys.
STALL_PROBE_INTERVAL_SECONDS = 0.25
STALL_EVENT_MIN_SECONDS = 0.5
STALL_EVENT_RETENTION_DAYS = 14
STALL_REPORT_WINDOW_DAYS = 7

# Ultra-fast handoff: opt-in per stream, enforced from the 4 Hz probe because
# the 1 s sampler cannot resolve half a second. The measured band is
# post-buffer -- SRT's 500 ms tsbpd window keeps delivery moving through
# shorter network gaps, so 0.5 s of delivery silence means the network has
# already been dead for roughly a full second. The stall ledger is the
# evidence basis: enable this only on a link whose ledger stays empty.
ULTRA_FAILOVER_STALL_SECONDS = 0.5

WORKER_RETRY_MIN_SECONDS = 0.5
WORKER_RETRY_MAX_SECONDS = 30.0
WORKER_HEALTHY_SECONDS = 30.0
WORKER_STALL_SECONDS = 20.0
WORKER_ERROR_LINES = 20
# The ingest→program copy (ProgramSwitch). Its retries are shorter than a
# forwarder's because every second it is down is a second the backup screen
# is on air instead of OBS.
PROGRAM_SWITCH_RETRY_MIN_SECONDS = 0.5
PROGRAM_SWITCH_RETRY_MAX_SECONDS = 30.0
PROGRAM_SWITCH_HEALTHY_SECONDS = 10.0
PROGRAM_SWITCH_STALL_SECONDS = 10.0
# Consecutive 1 s samples with OBS gone before a still-running copy is killed.
# MediaMTX closes the copy's read session itself when the publisher leaves, so
# this only catches a copy whose input somehow outlived the publisher.
PROGRAM_SWITCH_OFFLINE_SAMPLES = 3
PROGRAM_SWITCH_ERROR_LINES = 10
TWITCH_FALLBACK = {
    "name": "US West (Oregon)",
    "url_template_secure": "rtmps://usw20.contribute.live-video.net/app/{stream_key}",
    "latency_ms": None,
    "checked_at": None,
}


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# The metrics sampler makes three MediaMTX calls a second; at INFO that is a
# quarter of a million log lines a day and it buries anything real.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("relay")

# Matches "scheme://user:password@host" so RTSP/RTMP credentials can never reach
# the database, an API response, or the container log.
CREDENTIAL_RE = re.compile(r"(?<=://)[^/@\s]+:[^/@\s]+(?=@)")


def redact(text: str, *urls: str | None) -> str:
    """Strip destination URLs and any embedded credentials from FFmpeg output."""
    cleaned = text
    for url in urls:
        if url:
            cleaned = cleaned.replace(url, "[destination]")
    return CREDENTIAL_RE.sub("[redacted]", cleaned)


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
                music_fallback INTEGER NOT NULL DEFAULT 1,
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
            CREATE TABLE IF NOT EXISTS stall_events (
                id INTEGER PRIMARY KEY,
                stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                duration_s REAL NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stall_events_stream_time
                ON stall_events(stream_id, recorded_at);
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
        # CREATE TABLE IF NOT EXISTS cannot add a column to a table that already
        # exists, so every column added after the first release must be listed
        # here. The sweep is idempotent and preserves production rows.
        added_columns = {
            "users": {"enabled": "INTEGER NOT NULL DEFAULT 1"},
            "destinations": {
                "restart_count": "INTEGER NOT NULL DEFAULT 0",
                "last_started_at": "TEXT",
                # Defaults to 1, matching the create default: a one-track
                # publish sends track 1 to YouTube and X rather than an FLV
                # stream with no audio track at all, which is unverified
                # against both ingests. The normalisation below then clears the
                # flag on the platforms that map track 1 whatever it says.
                "music_fallback": "INTEGER NOT NULL DEFAULT 1",
            },
            "media_assets": {
                "encoder_version": "INTEGER NOT NULL DEFAULT 0",  # legacy, superseded
                "encoder_fingerprint": "TEXT",
            },
        }
        for table, columns in added_columns.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    log.info("migrated %s: added column %s", table, column)
        # ADD COLUMN stamps the new default onto every existing row, including
        # the platforms that map track 1 whatever this flag says. Clearing it
        # there preserves the column's one invariant — a stored 1 is a real
        # YouTube or X opt-in, never a leftover on a destination that cannot act
        # on it — which is what validate_music_fallback enforces on the way in
        # and what the dashboard reads back. Idempotent: it only ever touches
        # rows that already violate the invariant.
        conn.execute(
            "UPDATE destinations SET music_fallback = 0 "
            "WHERE music_fallback <> 0 AND platform NOT IN ('youtube', 'x')"
        )
        conn.execute(
            "UPDATE media_assets SET status = 'error', message = 'Conversion was interrupted; upload the file again.' WHERE status = 'converting'"
        )

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
    # Tri-state on purpose. None is "the caller made no choice", which is every
    # platform whose form never shows the control; a plain `bool = True` would
    # push True into validate_music_fallback on a Twitch create and 422 a field
    # the user never set. resolve_music_fallback() turns None into the
    # platform's own default.
    music_fallback: bool | None = None


class MusicFallbackBody(BaseModel):
    enabled: bool


class ToggleBody(BaseModel):
    enabled: bool


class FastFailoverBody(BaseModel):
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

    async def start(self, probe_now: bool = True) -> None:
        self.load_saved()
        if probe_now:
            try:
                await self.refresh()
            except Exception:
                pass
        self.task = asyncio.create_task(self._refresh_loop(immediate=not probe_now))

    async def _refresh_loop(self, immediate: bool = False) -> None:
        if not immediate:
            await asyncio.sleep(TWITCH_REFRESH_SECONDS)
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                log.warning("Twitch ingest refresh failed: %s", exc)
            await asyncio.sleep(TWITCH_REFRESH_SECONDS)

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

def fast_failover_key(stream_id: int) -> str:
    return f"fast_failover:{stream_id}"


def ultra_failover_key(stream_id: int) -> str:
    return f"ultra_failover:{stream_id}"


def ultra_failover_enabled(stream_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (ultra_failover_key(stream_id),),
        ).fetchone()
    return bool(row and row["value"] == "1")


def set_ultra_failover(stream_id: int, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (ultra_failover_key(stream_id), "1" if enabled else "0", now()),
        )


def fast_failover_enabled(stream_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (fast_failover_key(stream_id),),
        ).fetchone()
    return bool(row and row["value"] == "1")


def set_fast_failover(stream_id: int, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (fast_failover_key(stream_id), "1" if enabled else "0", now()),
        )


def stream_media_dir(slug: str) -> Path:
    path = DB_PATH.parent / "media" / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_asset_path(slug: str, kind: str) -> Path:
    return stream_media_dir(slug) / f"{kind}.mp4"


def active_media_path(slug: str) -> Path:
    return stream_media_dir(slug) / "active.mp4"


def contribution_key(stream_id: int) -> str:
    return f"contribution:{stream_id}"


def contribution_profile(stream_id: int) -> dict[str, Any]:
    """The observed bitstream parameters of this stream's contribution feed."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (contribution_key(stream_id),)
        ).fetchone()
    profile = dict(DEFAULT_CONTRIBUTION)
    if row:
        try:
            stored = json.loads(row["value"])
            if isinstance(stored, dict):
                profile.update({k: v for k, v in stored.items() if k in DEFAULT_CONTRIBUTION})
        except (json.JSONDecodeError, TypeError):
            pass
    return profile


def contribution_fingerprint(profile: dict[str, Any]) -> str:
    """Identifies the recipe a screen was encoded for.

    A screen stays valid only while the feed it has to match is unchanged, so the
    fingerprint covers the profile as well as the encoder version.
    """
    payload = json.dumps(
        {k: profile.get(k) for k in sorted(DEFAULT_CONTRIBUTION)}, separators=(",", ":")
    )
    return f"{SLATE_ENCODER_VERSION}:{hashlib.sha256(payload.encode()).hexdigest()[:12]}"


def _parse_rate(value: Any) -> float | None:
    try:
        if isinstance(value, str) and "/" in value:
            num, _, den = value.partition("/")
            return float(num) / float(den) if float(den) else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


async def probe_contribution(slug: str) -> dict[str, Any] | None:
    """Read the live feed's actual encoding parameters.

    Everything here comes out of the bitstream itself, so it works for any
    encoder a streamer happens to use rather than assuming one setup.
    """
    # The ingest path: OBS's own bitstream, never the screen on the program path.
    source = internal_rtsp_url(slug)
    command = [
        "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,profile,level,refs,has_b_frames,avg_frame_rate,r_frame_rate,"
        "sample_aspect_ratio,color_primaries,color_transfer,color_space,color_range",
        "-show_entries", "frame=key_frame",
        "-read_intervals", f"%+{CONTRIBUTION_PROBE_SECONDS}",
        "-of", "json", source,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=CONTRIBUTION_PROBE_SECONDS + 15
        )
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("contribution probe failed for %s: %s", slug, exc)
        return None
    if process.returncode:
        log.warning(
            "contribution probe failed for %s: %s",
            slug,
            redact(stderr.decode(errors="replace").strip()[:200]),
        )
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    streams = payload.get("streams") or []
    if not streams:
        return None
    video = streams[0]

    profile = dict(DEFAULT_CONTRIBUTION)
    if video.get("width") and video.get("height"):
        profile["width"] = int(video["width"])
        profile["height"] = int(video["height"])
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    if fps and CONTRIBUTION_FPS_RANGE[0] <= fps <= CONTRIBUTION_FPS_RANGE[1]:
        profile["fps"] = round(fps, 3)
    named = video.get("profile")
    if isinstance(named, str):
        key = named.lower().replace(" ", "").replace(":", "")
        if key not in X264_PROFILES:
            log.info("unmapped H.264 profile %r for %s; using high", named, slug)
        profile["profile"] = X264_PROFILES.get(key, "high")
    level = video.get("level")
    if isinstance(level, int) and 10 <= level <= 62:
        profile["level"] = level
    if video.get("refs"):
        profile["refs"] = max(1, int(video["refs"]))
    if video.get("has_b_frames") is not None:
        profile["bframes"] = int(video["has_b_frames"])
    sar = video.get("sample_aspect_ratio")
    if isinstance(sar, str) and ":" in sar and sar != "0:1":
        profile["sar"] = sar.replace(":", "/")
    for probed, key in (
        ("color_primaries", "color_primaries"),
        ("color_transfer", "color_trc"),
        ("color_space", "colorspace"),
        ("color_range", "color_range"),
    ):
        value = video.get(probed)
        if isinstance(value, str) and value not in ("unknown", "reserved"):
            profile[key] = value

    # Measure the keyframe cadence rather than assuming it.
    frames = payload.get("frames") or []
    keys = [index for index, frame in enumerate(frames) if frame.get("key_frame") == 1]
    if len(keys) >= 2 and profile["fps"]:
        spacing = statistics.median(b - a for a, b in zip(keys, keys[1:]))
        seconds = spacing / profile["fps"]
        if 0.4 <= seconds <= 10:
            profile["gop_seconds"] = round(seconds, 3)
    return profile


def store_contribution_profile(stream_id: int, profile: dict[str, Any]) -> bool:
    """Persist a probed profile; returns True when it actually changed."""
    if profile == contribution_profile(stream_id):
        return False
    with connect() as conn:
        conn.execute(
            """INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (contribution_key(stream_id), json.dumps(profile), now()),
        )
    return True


def slate_video_filter(profile: dict[str, Any], source_label: str = "0:v:0") -> str:
    # Resolution and frame rate come from the stream this slate has to replace; a
    # slate at a different size or cadence forces the viewer's decoder to
    # reconfigure at the handoff. setsar matters for the same reason: x264 writes
    # aspect_ratio_info_present_flag, so an unset SAR produces a different SPS.
    width, height = int(profile["width"]), int(profile["height"])
    sar = str(profile["sar"]).replace("/", "/")
    return (
        f"[{source_label}]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar={sar},"
        f"fps={profile['fps']},format=yuv420p[v]"
    )


def slate_encode_args(profile: dict[str, Any]) -> list[str]:
    """Encoder arguments for one stream's failover screens.

    Every field that lands in the SPS or PPS is taken from that stream's own
    observed feed -- resolution, frame rate, profile, level, reference frames,
    B-frames, pixel aspect, and colour signalling -- because a screen only hands
    off cleanly when its parameter sets match the stream it replaces. The
    keyframe interval follows the measured cadence so the downstream segmenter
    does not have to re-anchor at the splice.

    Changing this recipe means bumping SLATE_ENCODER_VERSION; a stream whose feed
    changes shape is picked up automatically through contribution_fingerprint.
    """
    fps = float(profile["fps"])
    gop = max(1, round(fps * float(profile["gop_seconds"])))
    bframes = int(profile["bframes"])
    x264 = [
        "stitchable=1",
        f"ref={max(1, int(profile['refs']))}",
        # Weighted prediction sets weighted_pred_flag in the PPS. OBS defaults it
        # off; matching it avoids a parameter-set change mid-broadcast.
        "weightp=0" if bframes == 0 else "weightp=1",
        "cabac=1",
        "8x8dct=1",
        "aud=0",
        "open-gop=0",
        f"colorprim={profile['color_primaries']}",
        f"transfer={profile['color_trc']}",
        f"colormatrix={profile['colorspace']}",
        f"fullrange={'on' if profile['color_range'] == 'pc' else 'off'}",
        f"sar={profile['sar']}",
    ]
    return [
        "-r", str(fps),
        "-fps_mode", "cfr",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-profile:v", str(profile["profile"]),
        "-level:v", f"{int(profile['level']) / 10:.1f}",
        # B-frames change POC handling and make PTS != DTS, which the FLV muxer
        # rejects, so a contribution feed using them still gets a slate without.
        "-bf", "0",
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-b:v", SLATE_VIDEO_BITRATE,
        "-maxrate", SLATE_VIDEO_BITRATE,
        "-bufsize", SLATE_VIDEO_BITRATE,
        "-pix_fmt", "yuv420p",
        "-color_primaries", str(profile["color_primaries"]),
        "-color_trc", str(profile["color_trc"]),
        "-colorspace", str(profile["colorspace"]),
        "-color_range", str(profile["color_range"]),
        "-x264-params", ":".join(x264),
        "-c:a", "aac",
        "-profile:a", "aac_low",
        "-b:a", SLATE_AUDIO_BITRATE,
        "-ar", "48000",
        "-ac", "2",
        "-map_metadata", "-1",
        "-movflags", "+faststart",
    ]


def snap_slate_variant(profile: dict[str, Any]) -> tuple[int, int, float]:
    """The supported slate geometry closest to an observed contribution profile.

    Height decides first and frame rate breaks the tie: a resolution change at
    the splice is what forces a decoder reconfiguration, while a frame-rate
    change is merely a cadence the segmenter has to re-anchor on.

    An exact tie (900p sits 180 lines from both 720 and 1080) resolves upward, so
    the choice is a stated rule rather than an accident of list order. Scaling a
    larger slate down costs nothing; the level and bitrate are already sized for
    the bigger geometry either way.
    """
    def number(key: str, cast):
        try:
            value = profile.get(key)
            return cast(DEFAULT_CONTRIBUTION[key] if value is None else value)
        except (TypeError, ValueError):
            return cast(DEFAULT_CONTRIBUTION[key])

    height, fps = number("height", int), number("fps", float)
    return min(
        SLATE_VARIANTS,
        key=lambda v: (abs(v[1] - height), -v[1], abs(v[2] - fps), -v[2]),
    )


def slate_variant_path(variant: tuple[int, int, float]) -> Path:
    width, height, fps = variant
    # The recipe version is part of the filename: ensure_slate_variants only
    # renders what is missing, so without it a SLATE_ENCODER_VERSION bump would
    # keep serving files rendered by the old recipe forever.
    return (
        DB_PATH.parent / "media" / "_default"
        / f"slate-{width}x{height}p{fps:g}v{SLATE_ENCODER_VERSION}.mp4"
    )


def stream_slate_variant(slug: str) -> tuple[int, int, float]:
    with connect() as conn:
        row = conn.execute("SELECT id FROM streams WHERE slug = ?", (slug,)).fetchone()
    profile = contribution_profile(row["id"]) if row else dict(DEFAULT_CONTRIBUTION)
    return snap_slate_variant(profile)


def has_uploaded_screen(stream_id: int, kind: str) -> bool:
    """True when this screen is the user's own upload rather than a seeded slate.

    Seeded slates are ours to replace when the feed changes shape. An uploaded
    one is the user's content; MediaConversionManager re-encodes that against the
    stream's full profile instead.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM media_assets WHERE stream_id = ? AND kind = ?", (stream_id, kind)
        ).fetchone()
    return row is not None


async def generate_placeholder_slate(target: Path, profile: dict[str, Any] | None = None) -> bool:
    """Render a plain standby card so a fresh install has a usable slate.

    MediaMTX opens `alwaysAvailableFile` when it loads its configuration, so on a
    brand-new deployment — before anyone has uploaded a BRB — there must already
    be a file at that path or the media server cannot start.
    """
    pending = target.with_suffix(".bootstrap.mp4")
    # Geometry comes from the caller; everything else that lands in the SPS keeps
    # the conservative defaults, because one variant is shared by every stream
    # that snaps to it and cannot carry any single stream's refs or level.
    profile = {**DEFAULT_CONTRIBUTION, **(profile or {})}
    width, height = int(profile["width"]), int(profile["height"])
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"color=c=0x0C1319:s={width}x{height}:r={profile['fps']}:d=10",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        # drawbox only: drawtext needs a font, and the runtime image ships none,
        # so a text card would fail exactly when this placeholder is needed.
        "-filter_complex",
        f"[0:v:0]drawbox=x=(iw-300)/2:y=(ih/2)-8:w=300:h=6:color=0x35A7B8@0.9:t=fill,"
        f"drawbox=x=(iw-180)/2:y=(ih/2)+20:w=180:h=6:color=0x35A7B8@0.45:t=fill,"
        "setsar=1,format=yuv420p[v]",
        # Two audio tracks so the slate's stream layout matches a live OBS feed
        # that publishes track 1 and track 2.
        "-map", "[v]", "-map", "1:a:0", "-map", "1:a:0",
        "-t", "10",
        *slate_encode_args(profile),
        str(pending),
    ]
    try:
        # ffmpeg writes the pending file straight into this directory, and on a
        # fresh volume nothing else has created media/_default yet.
        target.parent.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        if process.returncode:
            log.error("could not render placeholder slate: %s", stderr.decode(errors="replace").strip()[:300])
            pending.unlink(missing_ok=True)
            return False
        os.replace(pending, target)
        log.info("rendered placeholder slate at %s", target)
        return True
    except Exception as exc:
        log.error("could not render placeholder slate: %s", exc)
        pending.unlink(missing_ok=True)
        return False


async def ensure_slate_variants() -> None:
    """Render any supported slate geometry a current stream needs.

    Each variant is rendered once and shared by every stream that snaps to it, so
    this is a no-op on all but the first boot after a feed changes shape.
    """
    with connect() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM streams")]
    wanted = {snap_slate_variant(contribution_profile(i)) for i in ids}
    # The generated OBS path exists before its stream row does, so the default
    # geometry always has to be on disk.
    wanted.add(snap_slate_variant(dict(DEFAULT_CONTRIBUTION)))
    for variant in sorted(wanted):
        target = slate_variant_path(variant)
        if target.exists():
            continue
        width, height, fps = variant
        if await generate_placeholder_slate(
            target, {"width": width, "height": height, "fps": fps}
        ):
            log.info("rendered slate variant %sx%sp%g", width, height, fps)


def replace_seeded_slate(source: Path, brb: Path, active: Path | None) -> None:
    """Swap a seeded slate without ever exposing a torn file.

    active.mp4 goes through .pending.mp4 and os.replace for the same reason every
    other screen swap here does: MediaMTX may be reading it at any moment. It is
    left alone when None: the BRB screen is not what is on air right now.
    """
    brb.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, brb)
    if active is None:
        return
    pending = active.with_suffix(".pending.mp4")
    shutil.copyfile(source, pending)
    os.replace(pending, active)


async def refresh_stream_slate(stream_id: int, slug: str) -> None:
    """Re-seed a stream's slate after its feed changed shape."""
    if has_uploaded_screen(stream_id, "brb"):
        return
    await ensure_slate_variants()
    variant = snap_slate_variant(contribution_profile(stream_id))
    source = slate_variant_path(variant)
    if not source.exists():
        return
    brb = media_asset_path(slug, "brb")
    # Variants differ in resolution and cadence, so they differ in size; a match
    # means this stream is already on the right one.
    if brb.exists() and brb.stat().st_size == source.stat().st_size:
        return
    # This runs on the online edge and at boot, both of which can coincide with
    # a takeover: active.mp4 then carries the user's Starting Soon screen, not
    # the slate, and rewriting it would put BRB on air with Starting Soon lit --
    # the same clobber _prepare_brb guards against. Decide under the activation
    # lock so a takeover's swap cannot slip between the check and the write.
    lock = _activation_locks.setdefault(slug, asyncio.Lock())
    async with lock:
        active = active_media_path(slug) if current_screen_mode(stream_id) == "brb" else None
        await asyncio.to_thread(replace_seeded_slate, source, brb, active)
    log.info("re-seeded %s slate at %sx%sp%g", slug, *variant)
    # MediaMTX fixed the path's parameter sets from the previous file when the
    # path was created, so the new slate reaches the wire only once the path is
    # recreated. Attempting it here is safe: reload declines unless the path is
    # genuinely idle, and a router-only deploy has no other recreation point.
    await reload_fallback_path(slug)


async def ensure_bootstrap_media() -> None:
    """Guarantee a slate exists before MediaMTX is allowed to start."""
    await ensure_slate_variants()
    with connect() as conn:
        streams = conn.execute("SELECT id, slug FROM streams").fetchall()
    # mediamtx.yml declares a static path whose alwaysAvailableFile must resolve
    # at load time even before that user exists.
    for slug in {*(row["slug"] for row in streams), "studio"}:
        try:
            seed_stream_media(slug)
        except Exception as exc:
            log.warning("could not seed media for %s: %s", slug, exc)
    # Seeding only writes when nothing is there, so a stream seeded before this
    # recipe existed still carries a slate of the wrong geometry and nothing else
    # would notice until its feed happened to change shape. Correct those here so
    # the repair applies itself on deploy rather than needing files deleted by
    # hand. Uploaded screens are left alone.
    for row in streams:
        try:
            await refresh_stream_slate(row["id"], row["slug"])
        except Exception as exc:
            log.warning("could not refresh slate for %s: %s", row["slug"], exc)


def seed_stream_media(slug: str) -> bool:
    """Give this stream a slate that matches its own feed geometry.

    This deliberately never copies another tenant's screen. The previous
    behaviour took the first uploaded BRB it could find and propagated it to
    every stream, which put one operator's card on another operator's channel and
    handed everybody whatever resolution that one file happened to be — in
    practice a single 720p48 file standing in for 1080p feeds.
    """
    brb = media_asset_path(slug, "brb")
    active = active_media_path(slug)
    source = slate_variant_path(stream_slate_variant(slug))
    if not source.exists():
        # Bootstrap ordering: variants are rendered once the DB is readable, but
        # MediaMTX opens alwaysAvailableFile at config load. Anything already on
        # disk beats no file at all.
        if not brb.exists():
            return False
        source = brb
    brb.parent.mkdir(parents=True, exist_ok=True)
    if not brb.exists():
        shutil.copyfile(source, brb)
    if not active.exists():
        shutil.copyfile(brb, active)
    return True


def program_path(slug: str) -> str:
    """The MediaMTX path the forwarders and the monitor read.

    OBS publishes to the bare slug, so its SRT address never changes. This second
    path carries whatever is actually on air: OBS, by way of the ProgramSwitch
    copy, or active.mp4 whenever that copy is stopped. Keeping the two apart is
    what lets OBS stay connected while a screen is on air — an always-available
    file only plays while nothing publishes to its path, so on a single path the
    only way to hold a screen was to refuse OBS.
    """
    return f"{slug}/program"


def internal_rtsp_url(name: str) -> str:
    """Internal RTSP address of a MediaMTX path. Carries credentials: redact it."""
    return (
        f"rtsp://{quote(MEDIA_INTERNAL_USER, safe='')}:{quote(MEDIA_INTERNAL_PASS, safe='')}"
        f"@{MEDIAMTX_RTSP}/{name}"
    )


def ingest_path_config() -> dict[str, Any]:
    # Explicit falses, not omissions: _ensure_path only PATCHes keys that differ,
    # and a path created before the split still carries the always-available
    # file. Left there, it would play the screen to the copy while OBS is away
    # and the copy would never see OBS leave.
    return {
        "source": "publisher",
        "overridePublisher": True,
        "alwaysAvailable": False,
        "alwaysAvailableFile": "",
    }


def program_path_config(slug: str, with_fallback: bool = True) -> dict[str, Any]:
    config: dict[str, Any] = {"source": "publisher", "overridePublisher": True}
    if with_fallback:
        config["alwaysAvailable"] = True
        config["alwaysAvailableFile"] = f"{MEDIAMTX_MEDIA_ROOT}/{slug}/active.mp4"
    return config


def _encode_path(name: str) -> str:
    # The program path carries a slash. MediaMTX's API reads the whole remainder
    # of the URL as the name, so the slash travels as itself.
    return quote(name, safe="/")


async def _ensure_path(client: httpx.AsyncClient, name: str, payload: dict[str, Any]) -> bool:
    """Create or correct one MediaMTX path config. True when it was created."""
    encoded = _encode_path(name)
    response = await client.get(f"{MEDIAMTX_API}/v3/config/paths/get/{encoded}")
    if response.status_code == 404:
        response = await client.post(f"{MEDIAMTX_API}/v3/config/paths/add/{encoded}", json=payload)
        response.raise_for_status()
        return True
    response.raise_for_status()
    current = response.json()
    if any(current.get(key) != value for key, value in payload.items()):
        response = await client.patch(f"{MEDIAMTX_API}/v3/config/paths/patch/{encoded}", json=payload)
        response.raise_for_status()
        log.info("updated MediaMTX path config for %s", name)
    return False


async def ensure_fallback_path(slug: str) -> bool:
    """Make sure both of a stream's paths exist and are configured.

    Path creation must not depend on a slate existing: an invited streamer with
    no uploads still needs somewhere to publish. Only the program path's
    always-available keys are conditional.
    """
    seeded = seed_stream_media(slug)
    async with httpx.AsyncClient(timeout=10) as client:
        return await _ensure_stream_paths(client, slug, seeded)


async def _ensure_stream_paths(client: Any, slug: str, seeded: bool) -> bool:
    if await _ensure_path(client, slug, ingest_path_config()):
        log.info("created MediaMTX ingest path for %s", slug)
    if await _ensure_path(client, program_path(slug), program_path_config(slug, with_fallback=seeded)):
        log.info("created MediaMTX program path for %s (fallback=%s)", slug, seeded)
    return True


async def path_is_idle(name: str) -> bool:
    """True when nothing is publishing to or reading from this path."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{MEDIAMTX_API}/v3/paths/get/{_encode_path(name)}")
            if response.status_code == 404:
                return True
            response.raise_for_status()
            path = response.json()
    except Exception:
        return False
    source = path.get("source") or {}
    # "ready" is useless here — an always-available path is always ready. Idle
    # means no publisher and nobody reading, because recreating the path drops
    # both.
    return not path.get("online", path.get("ready")) and not source and not path.get("readers")


async def reload_fallback_path(slug: str) -> bool:
    """Make MediaMTX reopen active.mp4, returning False when it had to be staged.

    MediaMTX reads the always-available file's parameter sets when the path is
    created and the offline player keeps its own descriptors open, so replacing
    active.mp4 has no effect until the path is recreated. Recreating disconnects
    whoever is attached, so it only happens when the program path is genuinely
    idle; otherwise the new screen goes on air at the next reconnect.
    """
    name = program_path(slug)
    if not await path_is_idle(name):
        log.info("staged screen change for %s; path is in use", slug)
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.delete(f"{MEDIAMTX_API}/v3/config/paths/delete/{_encode_path(name)}")
            if response.status_code != 404:
                response.raise_for_status()
        await ensure_fallback_path(slug)
        log.info("reloaded MediaMTX path for %s", slug)
        return True
    except Exception as exc:
        log.warning("could not reload path for %s: %s", slug, exc)
        return False


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
    for name in (program_path(slug), slug):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.delete(
                    f"{MEDIAMTX_API}/v3/config/paths/delete/{_encode_path(name)}"
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
            except Exception as exc:
                # Slug only — never the file path or any credential.
                log.warning("path reconcile failed for %s: %s", slug, exc)
            program_switch.ensure(slug)
        program_switch.prune(set(slugs))
        # Re-arm any destination whose worker died for good; start() no-ops when
        # a task is already running, so this is cheap and idempotent.
        try:
            await workers.start_enabled()
        except Exception as exc:
            log.warning("worker re-arm failed: %s", exc)

    async def _loop(self) -> None:
        while True:
            await self.reconcile()
            await asyncio.sleep(10)


path_reconciler = PathReconciler()


_activation_locks: dict[str, asyncio.Lock] = {}


def _swap_active_file(source: Path, active: Path) -> None:
    """Publish `source` as `active.mp4` without ever exposing a torn file.

    Screen assets are only ever replaced wholesale via os.replace, never written
    in place, so a hard link is safe and turns a multi-hundred-megabyte copy into
    a constant-time operation. Falls back to copying across filesystems.
    """
    pending = active.with_suffix(".pending.mp4")
    pending.unlink(missing_ok=True)
    try:
        os.link(source, pending)
    except OSError:
        shutil.copyfile(source, pending)
    os.replace(pending, active)


async def activate_screen_file(stream: sqlite3.Row, kind: str, reload_path: bool) -> None:
    source = media_asset_path(stream["slug"], kind)
    if not source.exists():
        raise HTTPException(status_code=409, detail=f"Upload the {kind.replace('_', ' ')} screen first")
    active = active_media_path(stream["slug"])
    lock = _activation_locks.setdefault(stream["slug"], asyncio.Lock())
    async with lock:
        try:
            if active.exists() and os.path.samefile(source, active):
                set_screen_mode(stream["id"], kind)
                if reload_path:
                    await reload_fallback_path(stream["slug"])
                return
        except OSError:
            pass
        await asyncio.to_thread(_swap_active_file, source, active)
    set_screen_mode(stream["id"], kind)
    if reload_path:
        await reload_fallback_path(stream["slug"])


def media_assets_state(stream: sqlite3.Row) -> dict[str, Any]:
    fingerprint = contribution_fingerprint(contribution_profile(stream["id"]))
    with connect() as conn:
        rows = conn.execute(
            "SELECT kind, status, original_name, message, updated_at, encoder_fingerprint FROM media_assets WHERE stream_id = ?",
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
                "encoder_fingerprint": None,
            }
        elif kind not in assets:
            assets[kind] = {
                "kind": kind,
                "status": "missing",
                "original_name": None,
                "message": None,
                "updated_at": None,
                "encoder_fingerprint": None,
            }
        # A screen encoded for a different feed shape still plays, but it will
        # not hand off cleanly, so the dashboard prompts for a re-upload.
        assets[kind]["stale"] = (
            assets[kind]["status"] == "ready"
            and assets[kind].get("encoder_fingerprint") != fingerprint
        )
        assets[kind]["progress"] = (
            media_conversions.snapshot(stream["id"], kind)
            if assets[kind]["status"] == "converting"
            else None
        )
    return {
        "mode": current_screen_mode(stream["id"]),
        "program_mode": current_program_mode(stream["id"]),
        "brb": assets["brb"],
        "starting_soon": assets["starting_soon"],
    }


class MediaConversionManager:
    def __init__(self) -> None:
        self.tasks: dict[tuple[int, str], asyncio.Task] = {}
        # How far each running conversion has got, for the dashboard's gauge.
        # Memory only, like the task it describes: a conversion does not survive
        # a router restart (initialize_db marks it failed), so neither may its
        # progress.
        self.progress: dict[tuple[int, str], dict[str, Any]] = {}

    def running(self, stream_id: int, kind: str) -> bool:
        task = self.tasks.get((stream_id, kind))
        return bool(task and not task.done())

    def snapshot(self, stream_id: int, kind: str) -> dict[str, Any] | None:
        """How far this screen's conversion has got, or None if none is running.

        `checking` is the ffprobe pass, which has nothing to measure. `encoding`
        reports how much of the video ffmpeg has written, which is honest
        because the encode is cut to exactly the probed duration. A stored
        'converting' row with no task behind it gets None rather than a bar
        that will never move.
        """
        entry = self.progress.get((stream_id, kind))
        if entry is None or not self.running(stream_id, kind):
            return None
        if entry["stage"] != "encoding":
            return {"stage": entry["stage"], "fraction": None, "eta_s": None}
        fraction = min(max(entry["done_s"] / entry["duration_s"], 0.0), 1.0)
        elapsed = time.monotonic() - entry["started_at"]
        eta_s = None
        if fraction > 0 and elapsed >= CONVERSION_ESTIMATE_AFTER_SECONDS:
            # Straight-line from the run so far: libx264 at a fixed preset moves
            # through a screen at a steady rate, and ffmpeg's start-up is in the
            # elapsed time, so an early estimate errs long rather than short.
            eta_s = round(elapsed * (1 - fraction) / fraction)
        return {"stage": "encoding", "fraction": round(fraction, 4), "eta_s": eta_s}

    def start(self, stream: sqlite3.Row, kind: str, source: Path, original_name: str) -> None:
        key = (stream["id"], kind)
        if self.running(*key):
            raise HTTPException(status_code=409, detail="That screen is already being converted")
        self.progress[key] = {"stage": "checking"}
        self.tasks[key] = asyncio.create_task(
            self._convert(dict(stream), kind, source, original_name)
        )

    def _begin_encode(self, key: tuple[int, str], duration_s: float) -> None:
        self.progress[key] = {
            "stage": "encoding",
            "duration_s": duration_s,
            "done_s": 0.0,
            "started_at": time.monotonic(),
        }

    async def _read_progress(self, stream: AsyncIterable[bytes], key: tuple[int, str]) -> None:
        """Follow the encode's -progress output into self.progress.

        Like the forwarders' reader this must drain stdout to the end while
        stderr is read alongside it, or ffmpeg blocks once a pipe fills.
        """
        async for sample in progress_samples(stream):
            entry = self.progress.get(key)
            # N/A before the first packet is written: keep the last position
            # rather than drop the gauge back to empty.
            if entry is not None and sample["out_time_s"] is not None:
                entry["done_s"] = sample["out_time_s"]

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
        encoder_fingerprint: str | None = None,
    ) -> None:
        with connect() as conn:
            conn.execute(
                """INSERT INTO media_assets(stream_id, kind, status, original_name, message, updated_at, encoder_fingerprint)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(stream_id, kind) DO UPDATE SET
                       status = excluded.status,
                       original_name = excluded.original_name,
                       message = excluded.message,
                       updated_at = excluded.updated_at,
                       encoder_fingerprint = excluded.encoder_fingerprint""",
                (stream_id, kind, status, original_name, message, now(), encoder_fingerprint),
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
            raise ValueError(
                f"Screen videos can be up to {MEDIA_MAX_DURATION_SECONDS // 60} minutes long. "
                "Shorter screens also switch on air faster."
            )
        return duration, has_audio

    async def _convert(
        self,
        stream: dict[str, Any],
        kind: str,
        source: Path,
        original_name: str,
    ) -> None:
        key = (stream["id"], kind)
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
                # The dashboard's conversion gauge, read from stdout.
                "-progress",
                "pipe:1",
                "-stats_period",
                "1",
                "-y",
                "-i",
                str(source),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
            ]
            # Match this stream's own contribution feed, not a fixed recipe.
            profile = contribution_profile(stream["id"])
            video_filter = slate_video_filter(profile)
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
                    *slate_encode_args(profile),
                    "-threads",
                    "2",
                    str(output),
                ]
            )
            self._begin_encode(key, duration)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None
            _, stderr = await asyncio.gather(
                self._read_progress(process.stdout, key),
                process.stderr.read(),
            )
            await process.wait()
            if process.returncode:
                detail = stderr.decode(errors="replace").strip().splitlines()
                raise RuntimeError(detail[-1] if detail else "FFmpeg could not convert this video")
            final = media_asset_path(stream["slug"], kind)
            os.replace(output, final)
            self._set_status(
                stream["id"], kind, "ready", original_name,
                encoder_fingerprint=contribution_fingerprint(profile),
            )
            mode = current_screen_mode(stream["id"])
            status = await program_status(stream["slug"])
            try:
                if kind == mode:
                    await activate_screen_file(stream, kind, reload_path=not status["online"])
                elif kind == "brb" and status["online"]:
                    await activate_screen_file(stream, "brb", reload_path=False)
            except Exception as exc:
                # The conversion itself succeeded; a MediaMTX hiccup while
                # activating must not report the upload as failed.
                log.warning("converted %s but could not activate it: %s", kind, exc)
        except asyncio.CancelledError:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except Exception as exc:
            self._set_status(stream["id"], kind, "error", original_name, redact(str(exc))[:500])
        finally:
            self.progress.pop(key, None)
            source.unlink(missing_ok=True)
            output.unlink(missing_ok=True)


media_conversions = MediaConversionManager()


AUDIO_CODEC_HINTS = ("audio", "aac", "opus", "ac-3", "g711", "lpcm", "mp3", "vorbis")


def audio_track_count(media: dict[str, Any]) -> int | None:
    """How many audio tracks the path is publishing, or None when unknown.

    MediaMTX's `tracks` list mixes video and audio, so it cannot be used as a
    count directly. None matters as much as a number: a worker usually starts
    before OBS connects, and assuming a single track then would permanently
    forward only track 1.
    """
    if not media.get("known", True) or not media.get("available"):
        return None
    tracks2 = media.get("tracks2") or []
    if tracks2:
        return sum(
            1
            for track in tracks2
            if (track.get("codecProps") or {}).get("sampleRate")
            or any(hint in str(track.get("codec", "")).lower() for hint in AUDIO_CODEC_HINTS)
        )
    tracks = media.get("tracks") or []
    if not tracks:
        return None
    return sum(
        1 for track in tracks if any(hint in str(track).lower() for hint in AUDIO_CODEC_HINTS)
    )


def build_audio_args(
    platform: str, audio_tracks: int | None, music_fallback: bool = True
) -> list[str]:
    """Audio mapping for one destination, derived entirely from the platform.

    The relay never mixes and never re-encodes — OBS owns every mix and this is
    always a straight `-c copy` forward. OBS track 1 is the full live mix (music
    + game + voice) and track 2 is the clean mix (game + voice, no music).
    Tracks 3-6 ride along in the SRT feed and are simply never mapped, so there
    is nothing for the user to choose.

    Twitch is the only platform that carries both. Enhanced RTMP multitrack
    sends track 1 as the live audio and track 2 as the separate VOD track, so
    the archive stays free of music without changing what live viewers hear;
    that encapsulation needs the FFmpeg 8.x build pinned by FFMPEG_RELEASE in
    router/Dockerfile. YouTube and X archive or auto-publish the replay, so they
    take the clean track only. RPLAY and custom destinations stay unpublished
    and take the full live mix.

    `audio_tracks` is None when the publisher's layout is not yet known — a
    worker usually starts before OBS connects — in which case the documented
    two-track layout is assumed.

    `music_fallback` only changes what YouTube and X do when the clean track is
    missing, and it defaults to True — track 1 — because the alternative is
    emitting FLV with no audio track at all, and that has never been verified
    against either ingest. A stream the service rejects outright, or archives
    silent, is a worse and far likelier failure than one carrying the wrong mix.
    The cost is real and known: YouTube runs Content ID over the archive and X
    auto-publishes the replay, so a one-track publish puts the music mix on two
    scanned destinations. But a publisher sending one track is misconfigured
    whatever this flag says — the setup has to be fixed either way — so the
    answer is to make the degraded state loud in the dashboard rather than to
    trade a certain outage for an uncertain one. Setting this False is the
    explicit opt-out for an operator who would rather go silent than mistaken.
    """
    args = ["-map", "0:v:0"]
    # Twitch, RPLAY and custom take track 1 in the normal case too, so degrading
    # to it inverts nothing. Only the clean-track platforms have a choice to
    # make here.
    clean_track_only = platform in {"youtube", "x"}
    if audio_tracks is not None and audio_tracks < 2:
        # Referencing 0:a:1 against a publisher that is not sending it fails the
        # whole command, so every platform degrades to whatever exists — and a
        # video-only publisher gets no audio map at all rather than parking every
        # destination on this stream in retrying forever. That same no-audio-map
        # shape is what an opted-out YouTube or X destination gets: it stays
        # connected and silent instead of taking the music mix.
        if audio_tracks and (not clean_track_only or music_fallback):
            args.extend(["-map", "0:a:0"])
    elif platform == "twitch":
        args.extend(["-map", "0:a:0", "-map", "0:a:1"])
    elif clean_track_only:
        args.extend(["-map", "0:a:1"])
    else:
        args.extend(["-map", "0:a:0"])
    args.extend(["-c", "copy", "-muxdelay", "0", "-muxpreload", "0"])
    return args


def parse_progress_block(block: dict[str, str]) -> dict[str, Any]:
    """Normalise one ffmpeg -progress block into plain numbers."""

    def number(key: str) -> float | None:
        raw = (block.get(key) or "").strip()
        if not raw or raw.startswith("N/A"):
            return None
        raw = raw.removesuffix("kbits/s").removesuffix("x").strip()
        try:
            return float(raw)
        except ValueError:
            return None

    out_time_us = number("out_time_us")
    # ffmpeg's own `bitrate=` is deliberately not carried here. It is
    # total_size * 8 / out_time, a lifetime average, and any caller that
    # reached for it would be showing a user a number that means "since this
    # process started" while claiming to mean "now". Rate is derived from
    # total_bytes deltas by derive_output_rate instead.
    return {
        "total_bytes": number("total_size"),
        "frames": number("frame"),
        "fps": number("fps"),
        "speed": number("speed"),
        "drop_frames": number("drop_frames"),
        "dup_frames": number("dup_frames"),
        "out_time_s": (out_time_us / 1_000_000) if out_time_us is not None else None,
    }


async def progress_samples(stream: AsyncIterable[bytes]) -> AsyncIterator[dict[str, Any]]:
    """One parse_progress_block() sample per ffmpeg -progress block.

    ffmpeg writes key=value lines and closes every block with a progress= line,
    so nothing is parsed until that line arrives. The stream is always read to
    its end, whatever it contains.
    """
    block: dict[str, str] = {}
    async for raw_line in stream:
        line = raw_line.decode(errors="replace").strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        block[key.strip()] = value
        if key.strip() != "progress":
            continue
        yield parse_progress_block(block)
        block = {}


def derive_output_rate(
    history: Sequence[tuple[float, float]],
    window_s: float = METRICS_OUTPUT_RATE_WINDOW_S,
) -> float | None:
    """Current output rate in kbps from an (out_time_s, total_bytes) history.

    The time base is ffmpeg's own output clock, not the wall clock we read the
    block on. Both numbers arrive in the same -progress block, so they stay
    consistent with each other however late the reader gets to them; timestamping
    on arrival instead would difference several seconds of bytes across a
    millisecond whenever a backlog drains in one wakeup, and print a rate several
    times the truth. Staleness is a separate question and is answered from the
    wall clock in `snapshot`.

    Why derive it at all: ffmpeg reports `bitrate=` as total_size * 8 /
    out_time, an average over the whole life of the process. A worker that
    spent its first minute pushing the 96 KB failover slate still reads
    hundreds of kbps low ten minutes later, and the figure only ever creeps up
    as the process ages. Nobody should ever be shown a lifetime bitrate: the
    panel says "now", so it has to mean now.

    Why the window is several seconds and not the gap between two consecutive
    blocks: workers run with -stats_period 1, and output bytes leave in
    keyframe-sized lumps. Differencing over ~1 s against a 2 s GOP aliases the
    keyframe cadence into a sawtooth that swings either side of the true rate,
    which is exactly the kind of number-that-does-not-mean-what-it-looks-like
    that cost this panel its credibility in the first place. `then` is the most
    recent sample at least `window_s` old, so the span stays bounded no matter
    how much history is retained; before a worker has that much history a
    shorter span is accepted, down to METRICS_OUTPUT_RATE_MIN_SPAN_S, so the
    panel fills in within a couple of seconds instead of sitting blank.

    Returns None rather than a guess whenever the history cannot support an
    honest answer: a single sample, too short a span, a clock that did not
    advance, or a byte counter that went backwards.
    """
    if len(history) < 2:
        return None
    now_t, now_bytes = history[-1]
    then_t, then_bytes = history[0]
    for point in history:
        if now_t - point[0] < window_s:
            break
        then_t, then_bytes = point
    span = now_t - then_t
    if span <= 0 or span < min(window_s, METRICS_OUTPUT_RATE_MIN_SPAN_S):
        return None
    delta = now_bytes - then_bytes
    if delta < 0:
        # Counter restarted underneath us; no rate beats a negative one.
        return None
    return delta * 8 / span / 1000


class WorkerManager:
    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        # Destinations whose current teardown must not send a clean RTMP
        # unpublish, because we intend to reconnect immediately.
        self.abrupt: set[int] = set()
        self.stopping = False
        # Per-destination live telemetry, never persisted at sample rate.
        self.metrics: dict[int, dict[str, Any]] = {}

    def _lock(self, destination_id: int) -> asyncio.Lock:
        return self.locks.setdefault(destination_id, asyncio.Lock())

    def snapshot(self, destination_id: int) -> dict[str, Any]:
        entry = self.metrics.get(destination_id)
        if not entry:
            return {
                "bitrate_kbps": None, "speed": None, "frames": None,
                "total_bytes": None, "uptime_s": None, "sample_age_s": None,
                "series": [],
            }
        last = entry.get("last") or {}
        started = entry.get("started_at")
        sampled = entry.get("sampled_at")
        rate = entry.get("rate_kbps")
        # A derived rate is only true while blocks keep arriving. If ffmpeg's
        # output write wedges, _read_progress stops and this value would sit on
        # screen labelled "right now" until the stall watchdog fires seconds
        # later. Expire it over the same window it was measured across, so a
        # stalled forwarder reads as unknown rather than as its last good rate.
        if sampled is None or time.monotonic() - sampled > METRICS_OUTPUT_RATE_WINDOW_S * 2:
            rate = None
        return {
            # Current rate, derived from total_size deltas over the last few
            # seconds — NOT ffmpeg's `bitrate=` field, which is a lifetime
            # average and is deliberately no longer parsed. The key keeps its
            # name because the name is now finally accurate; do not repoint it
            # at a -progress field.
            "bitrate_kbps": round(rate, 1) if rate is not None else None,
            "speed": last.get("speed"),
            "frames": last.get("frames"),
            "total_bytes": last.get("total_bytes"),
            "uptime_s": round(time.monotonic() - started, 1) if started else None,
            "sample_age_s": round(time.monotonic() - sampled, 1) if sampled else None,
            "series": list(entry["series"])[-METRICS_SERIES_POINTS:],
        }

    def _reset_metrics(self, destination_id: int) -> None:
        self.metrics[destination_id] = {
            "series": deque(maxlen=METRICS_OUTPUT_SAMPLES),
            # (out_time_s, total_bytes) pairs behind the derived rate. Cleared
            # with everything else so a restarted worker never differences its
            # fresh byte counter against the dead process's last reading.
            "rate_history": deque(maxlen=METRICS_OUTPUT_SAMPLES),
            "rate_kbps": None,
            "last": {},
            "started_at": time.monotonic(),
            "sampled_at": None,
        }

    async def _read_progress(self, stream: asyncio.StreamReader, destination_id: int) -> None:
        """Consume -progress from stdout.

        This must run concurrently with the stderr reader: an undrained pipe
        deadlocks ffmpeg once 64 KiB accumulates.
        """
        async for sample in progress_samples(stream):
            entry = self.metrics.get(destination_id)
            if entry is None:
                continue
            now = time.monotonic()
            entry["last"] = sample
            entry["sampled_at"] = now
            total_bytes = sample["total_bytes"]
            out_time = sample["out_time_s"]
            if total_bytes is None or out_time is None:
                # The tee muxer (YouTube primary+backup) reports total_size as
                # N/A, so there is nothing to difference — and without
                # out_time there is no honest time base either. No number is the
                # right answer; ffmpeg's cumulative bitrate=, the input rate and
                # speed are all wrong answers dressed up as right ones.
                entry["rate_kbps"] = None
                continue
            history = entry["rate_history"]
            history.append((out_time, total_bytes))
            # Twice the window is all the lookback the derivation can use, and
            # holding more would only keep stale bytes around across a stall.
            cutoff = out_time - METRICS_OUTPUT_RATE_WINDOW_S * 2
            while history and history[0][0] < cutoff:
                history.popleft()
            rate = derive_output_rate(history, METRICS_OUTPUT_RATE_WINDOW_S)
            entry["rate_kbps"] = rate
            if rate is not None:
                entry["series"].append(round(rate, 1))

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
        task = asyncio.create_task(self._run(destination_id))
        self.tasks[destination_id] = task
        # Reap so a long-lived process never accumulates finished tasks.
        task.add_done_callback(
            lambda finished, key=destination_id: self.tasks.pop(key, None)
            if self.tasks.get(key) is finished
            else None
        )

    async def stop(self, destination_id: int, *, abrupt: bool = False) -> None:
        """Stop a worker.

        `abrupt` decides what the platform is told. On SIGTERM, FFmpeg runs its
        normal shutdown and the RTMP muxer sends FCUnpublish and deleteStream —
        exactly what OBS sends when a streamer presses Stop Streaming — so the
        platform treats the broadcast as deliberately ended, which on Twitch can
        mean a new stream id, a split VOD, and a reset viewer count.

        That is right when the user is turning a destination off, and wrong when
        we are only restarting to apply a setting. SIGKILL drops the socket with
        no unpublish, so the platform sees a brief connection loss instead and
        the session survives the reconnect.
        """
        # Serialise against a concurrent start so a toggle storm cannot leave two
        # FFmpeg processes publishing to the same stream key.
        async with self._lock(destination_id):
            if abrupt:
                self.abrupt.add(destination_id)
            task = self.tasks.get(destination_id)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                finally:
                    if self.tasks.get(destination_id) is task:
                        self.tasks.pop(destination_id, None)
            self.abrupt.discard(destination_id)
            self.metrics.pop(destination_id, None)
            # Only claim "off" if it has not been re-enabled in the meantime.
            with connect() as conn:
                conn.execute(
                    "UPDATE destinations SET state = 'off', last_error = NULL WHERE id = ? AND enabled = 0",
                    (destination_id,),
                )

    async def shutdown(self) -> None:
        self.stopping = True
        await asyncio.gather(
            *(self.stop(destination_id) for destination_id in list(self.tasks)),
            return_exceptions=True,
        )
        self.stopping = False

    def _set_state(self, destination_id: int, state: str, error: str | None) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE destinations SET state = ?, last_error = ? WHERE id = ?",
                (state, error[:500] if error else None, destination_id),
            )

    def _destination_urls(self, row: sqlite3.Row) -> tuple[str, str | None]:
        secret = decrypt(row["output_url_enc"])
        preset = not secret.startswith(("rtmp://", "rtmps://"))
        if row["platform"] == "twitch" and preset:
            return twitch_ingests.output_url(secret), None
        if row["platform"] == "rplay" and preset:
            return f"{RPLAY_BASE_URL}/{secret}", None
        if row["platform"] == "x" and preset:
            return f"{X_BASE_URL}/{secret}", None
        if row["platform"] == "youtube" and preset:
            # ?backup=1 belongs to the RTMP *app*, not the playpath; appending it
            # after the key makes FFmpeg send it as part of the stream name.
            return (
                f"{YOUTUBE_PRIMARY_BASE_URL}/{secret}",
                f"{YOUTUBE_BACKUP_BASE_URL}?backup=1/{secret}",
            )
        return secret, None

    async def _run(self, destination_id: int) -> None:
        process: asyncio.subprocess.Process | None = None
        pinned_output_url: str | None = None
        pinned_backup_url: str | None = None
        failures = 0
        try:
            while not self.stopping:
                try:
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
                        # Pinned for the lifetime of a forwarding process so the
                        # Twitch ingest cannot change mid-broadcast, but
                        # re-derived after repeated failures so a dead ingest is
                        # not retried forever.
                        pinned_output_url, pinned_backup_url = self._destination_urls(row)
                    output_url = pinned_output_url
                    # The program path, never the ingest: a screen on air is a
                    # screen the forwarders must carry. With a seeded stream
                    # its track count is the screen's two whether OBS is on
                    # air or not, so the mapping is decided against the file's
                    # layout, which MediaMTX requires OBS to match anyway.
                    source = internal_rtsp_url(program_path(row["slug"]))
                    media = await program_status(row["slug"])
                    command = [
                        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
                        "-rtsp_transport", "tcp",
                        # Without this a stalled RTSP session blocks forever and
                        # the destination sits on "forwarding" with no retry.
                        "-timeout", "10000000",
                        "-i", source,
                        "-progress", "pipe:1", "-stats_period", "1",
                    ]
                    command.extend(
                        build_audio_args(
                            row["platform"],
                            audio_track_count(media),
                            bool(row["music_fallback"]),
                        )
                    )
                    if pinned_backup_url:
                        tee_output = (
                            f"[f=flv:onfail=ignore:flvflags=no_duration_filesize]{output_url}"
                            f"|[f=flv:onfail=ignore:flvflags=no_duration_filesize]{pinned_backup_url}"
                        )
                        command.extend([
                            "-use_fifo", "1",
                            # Without drop_pkts_on_overflow a stalled backup
                            # ingest blocks the producer and starves the healthy
                            # primary. Overflow flushes the queue, so the restart
                            # must resume on a keyframe.
                            "-fifo_options",
                            "queue_size=240:drop_pkts_on_overflow=1:restart_with_keyframe=1"
                            ":attempt_recovery=1:recover_any_error=1:recovery_wait_time=1"
                            ":max_recovery_attempts=10",
                            "-f", "tee", tee_output,
                        ])
                    else:
                        command.extend([
                            "-flvflags", "no_duration_filesize",
                            "-f", "flv", output_url,
                        ])
                    self._set_state(destination_id, "connecting", None)
                    self._reset_metrics(destination_id)
                    with connect() as conn:
                        conn.execute(
                            "UPDATE destinations SET restart_count = restart_count + 1, last_started_at = ? WHERE id = ?",
                            (now(), destination_id),
                        )
                    started = time.monotonic()
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                    errors: deque[str] = deque(maxlen=WORKER_ERROR_LINES)

                    async def drain_stderr(pipe: asyncio.StreamReader) -> None:
                        async for raw_line in pipe:
                            line = raw_line.decode(errors="replace").strip()
                            if line:
                                errors.append(redact(line, output_url, pinned_backup_url, source))

                    assert process.stdout is not None and process.stderr is not None
                    readers = [
                        asyncio.create_task(self._read_progress(process.stdout, destination_id)),
                        asyncio.create_task(drain_stderr(process.stderr)),
                    ]
                    try:
                        code = await self._supervise(destination_id, process)
                    finally:
                        for reader in readers:
                            reader.cancel()
                        await asyncio.gather(*readers, return_exceptions=True)
                    process = None
                    if self.stopping:
                        return

                    if time.monotonic() - started >= WORKER_HEALTHY_SECONDS:
                        failures = 0
                    else:
                        failures += 1
                    if failures and failures % 5 == 0:
                        pinned_output_url = None
                        pinned_backup_url = None
                    detail = " · ".join(list(errors)[-3:]) or f"Forwarder exited with code {code}"
                    self._set_state(destination_id, "retrying", detail)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Anything transient — a locked database, a subprocess spawn
                    # failure — must retry rather than kill the destination for
                    # the rest of the process's life.
                    failures += 1
                    log.warning("worker %s error: %s", destination_id, redact(str(exc)))
                    try:
                        self._set_state(destination_id, "retrying", redact(str(exc)))
                    except Exception:
                        pass
                delay = min(WORKER_RETRY_MAX_SECONDS, WORKER_RETRY_MIN_SECONDS * (2 ** min(failures, 6)))
                await asyncio.sleep(delay * random.uniform(0.7, 1.3))
        except asyncio.CancelledError:
            if process and process.returncode is None:
                if destination_id in self.abrupt:
                    # No SIGTERM: letting FFmpeg shut down cleanly would tell the
                    # platform the broadcast ended on purpose.
                    process.kill()
                    await process.wait()
                else:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            raise
        finally:
            self.metrics.pop(destination_id, None)

    async def _supervise(
        self, destination_id: int, process: asyncio.subprocess.Process
    ) -> int | None:
        """Wait for the process, promoting to `forwarding` only once bytes move.

        Also kills a wedged FFmpeg: the process can stay alive with an open
        socket while no progress block has arrived for far longer than the
        reporting period.
        """
        promoted = False
        # One waiter for the whole run: re-wrapping process.wait() each tick
        # would orphan a pending task every second.
        waiter = asyncio.ensure_future(process.wait())
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=1.0)
                if done:
                    return waiter.result()
                entry = self.metrics.get(destination_id) or {}
                sampled = entry.get("sampled_at")
                last = entry.get("last") or {}
                # total_size is N/A for the tee muxer, so YouTube would never be
                # promoted on bytes alone. frame and out_time come from PTS and
                # are reported for every muxer.
                moving = bool(last.get("total_bytes") or last.get("frames") or last.get("out_time_s"))
                if not promoted and moving:
                    promoted = True
                    self._set_state(destination_id, "forwarding", None)
                # sampled_at stays None until the first block lands, so keying
                # the watchdog on it alone leaves an ffmpeg that hangs during the
                # output handshake to sit on `connecting` forever — -timeout
                # covers the RTSP input only. Fall back to the start stamp.
                reference = sampled or entry.get("started_at")
                if reference and time.monotonic() - reference > WORKER_STALL_SECONDS:
                    log.warning("worker %s stopped reporting progress; restarting", destination_id)
                    process.kill()
                    return await waiter
        finally:
            if not waiter.done():
                waiter.cancel()


workers = WorkerManager()


async def end_process(process: asyncio.subprocess.Process) -> None:
    """SIGTERM, a two-second grace for an orderly RTSP teardown, then SIGKILL."""
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


class ProgramSwitch:
    """Puts OBS on air, and takes it off again, without touching OBS itself.

    One `-c copy` ffmpeg per stream reads the ingest path and publishes it to
    the program path. It runs only while two things hold: the operator wants
    live input on air (`desired`, written by the screen routes) and OBS is
    actually publishing. Stopping it is the takeover — the program path drops to
    active.mp4 the moment the copy's publish session ends, and the forwarders
    reading that path stay attached — while OBS's own SRT connection is never
    touched, so OBS can sit connected behind Starting Soon for as long as it
    likes and go on air the instant the operator returns to live.

    `desired` lives here, in memory, and only the routes write it. The loop must
    never re-read the program mode from the database: a takeover writes the
    mode, then swaps active.mp4, then stops the copy, and MediaMTX opens the
    file by name at that stop. A loop that stopped the copy between the first two
    steps would put the previous screen on air.
    """

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.desired: dict[str, bool] = {}
        self.wakeups: dict[str, asyncio.Event] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self.stopping = False

    def _lock(self, slug: str) -> asyncio.Lock:
        return self.locks.setdefault(slug, asyncio.Lock())

    def _wakeup(self, slug: str) -> asyncio.Event:
        return self.wakeups.setdefault(slug, asyncio.Event())

    def _set_state(self, slug: str, state: str, error: str | None = None) -> None:
        entry = self.status.get(slug)
        # The loop restates "stopped"/"waiting" every second; `since` is when the
        # state was entered, not when it was last confirmed.
        if entry and entry["state"] == state and entry["last_error"] == error:
            return
        self.status[slug] = {"state": state, "last_error": error, "since": time.monotonic()}

    def snapshot(self, slug: str) -> dict[str, Any]:
        """For /api/state: `stopped` (a screen is on air), `waiting` (live input
        wanted, OBS absent), `running` (OBS on air), `retrying` (the copy died
        and `last_error` says why, in words the dashboard can show)."""
        entry = self.status.get(slug)
        if entry is None:
            return {"state": "stopped", "last_error": None, "since_s": None}
        return {
            "state": entry["state"],
            "last_error": entry["last_error"],
            "since_s": round(time.monotonic() - entry["since"], 1),
        }

    @staticmethod
    def command(source: str, output: str) -> list[str]:
        return [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-fflags", "+nobuffer",
            "-rtsp_transport", "tcp",
            # Without this a stalled RTSP session blocks forever.
            "-timeout", "10000000",
            "-i", source,
            "-progress", "pipe:1", "-stats_period", "1",
            # Every track, in order, untouched: the forwarders map 0:a:0 and
            # 0:a:1 off the program path, and MediaMTX only lets a publisher
            # onto an always-available path whose layout matches the file's.
            "-map", "0", "-c", "copy",
            "-f", "rtsp", "-rtsp_transport", "tcp", output,
        ]

    async def start(self) -> None:
        self.stopping = False
        with connect() as conn:
            slugs = [
                row["slug"]
                for row in conn.execute(
                    """SELECT s.slug FROM streams s JOIN users u ON u.id = s.user_id
                       WHERE u.enabled = 1 ORDER BY s.id"""
                )
            ]
        for slug in slugs:
            self.ensure(slug)

    async def shutdown(self) -> None:
        self.stopping = True
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def ensure(self, slug: str) -> None:
        """Run the loop for this stream. The first call seeds `desired` from the
        stored program mode; after that only wake() and stop() change it."""
        if slug not in self.desired:
            with connect() as conn:
                row = conn.execute("SELECT id FROM streams WHERE slug = ?", (slug,)).fetchone()
            self.desired[slug] = bool(row) and current_program_mode(row["id"]) == "live"
        task = self.tasks.get(slug)
        if task is None or task.done():
            self.tasks[slug] = asyncio.create_task(self._run(slug))

    def prune(self, keep: set[str]) -> None:
        for slug in list(self.tasks):
            if slug not in keep:
                self.forget(slug)

    def forget(self, slug: str) -> None:
        task = self.tasks.pop(slug, None)
        if task and not task.done():
            task.cancel()
        self.desired.pop(slug, None)
        self.status.pop(slug, None)

    def wake(self, slug: str) -> None:
        """Live input is wanted: start the copy as soon as OBS is there."""
        self.desired[slug] = True
        self._wakeup(slug).set()

    async def stop(self, slug: str) -> None:
        """A screen is going on air. Returns only once the copy is gone, so the
        caller's reply means the program path is on the file.

        Takes a bare slug with no ownership predicate — resolve the caller's
        stream first, as with kick_stream_publishers.
        """
        self.desired[slug] = False
        async with self._lock(slug):
            process = self.processes.pop(slug, None)
            if process and process.returncode is None:
                await end_process(process)
        self._wakeup(slug).set()

    async def _ready(self, slug: str) -> bool:
        """OBS is publishing, the program path exists, and bytes are moving.

        A MediaMTX that cannot be reached answers False here — hold, never act
        on a reading that does not exist (invariant 9).
        """
        ingest = await media_status(slug)
        if not ingest.get("known", True) or not ingest["online"]:
            return False
        program = await program_status(slug)
        if not program.get("known", True) or not program["available"]:
            return False
        stalled = signal_metrics.stalled_for(slug)
        return stalled is None or stalled < 1.0

    @staticmethod
    async def _pause(wakeup: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _explain(self, slug: str, errors: list[str], code: int | None) -> str:
        """Why the copy died, in the operator's terms where that is possible.

        MediaMTX refuses a publisher whose track layout differs from the
        always-available file's, and ffmpeg only ever sees the RTSP status
        line, so the layouts are compared here to say what actually went wrong.
        """
        # ffmpeg reports a refused RTSP publish as a header write failure
        # ("Could not write header (incorrect codec parameters ?)"), and older
        # builds name the method instead.
        if any(re.search(r"method (ANNOUNCE|RECORD) failed|Could not write header", line) for line in errors):
            sending = audio_track_count(await media_status(slug))
            expected = audio_track_count(await program_status(slug))
            if sending is not None and expected is not None and sending != expected:
                return (
                    f"OBS is sending {sending} audio track{'' if sending == 1 else 's'}, but this "
                    f"stream's screens carry {expected}. Relay cannot put OBS on air until OBS "
                    "sends exactly Tracks 1 and 2."
                )
            return (
                "Relay could not attach OBS to the program feed. Check that OBS sends exactly "
                "Tracks 1 and 2 as AAC."
            )
        return " · ".join(errors[-3:]) or f"Program copy exited with code {code}"

    async def _run(self, slug: str) -> None:
        source = internal_rtsp_url(slug)
        output = internal_rtsp_url(program_path(slug))
        wakeup = self._wakeup(slug)
        failures = 0
        process: asyncio.subprocess.Process | None = None
        try:
            while True:
                # Cleared before the state is read, so a wake() landing during
                # the checks below is not lost.
                wakeup.clear()
                try:
                    if not self.desired.get(slug):
                        self._set_state(slug, "stopped")
                        await self._pause(wakeup, 1.0)
                        continue
                    if not await self._ready(slug):
                        self._set_state(slug, "waiting")
                        await self._pause(wakeup, 1.0)
                        continue
                    # Spawned under the same lock stop() takes, so a takeover
                    # either sees no process and the loop sees desired=False,
                    # or sees the process and ends it. Never a copy that
                    # outlives the decision to stop it.
                    async with self._lock(slug):
                        if not self.desired.get(slug):
                            continue
                        process = await asyncio.create_subprocess_exec(
                            *self.command(source, output),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        self.processes[slug] = process
                    started = time.monotonic()
                    self._set_state(slug, "running")
                    errors: deque[str] = deque(maxlen=PROGRAM_SWITCH_ERROR_LINES)
                    heartbeat: dict[str, float | None] = {"sampled_at": None}

                    async def read_progress(pipe: asyncio.StreamReader) -> None:
                        async for _sample in progress_samples(pipe):
                            heartbeat["sampled_at"] = time.monotonic()

                    async def drain_stderr(pipe: asyncio.StreamReader) -> None:
                        async for raw_line in pipe:
                            line = raw_line.decode(errors="replace").strip()
                            if line:
                                errors.append(redact(line, source, output))

                    assert process.stdout is not None and process.stderr is not None
                    readers = [
                        asyncio.create_task(read_progress(process.stdout)),
                        asyncio.create_task(drain_stderr(process.stderr)),
                    ]
                    try:
                        code = await self._supervise(slug, process, heartbeat, started)
                    finally:
                        for reader in readers:
                            reader.cancel()
                        await asyncio.gather(*readers, return_exceptions=True)
                    self.processes.pop(slug, None)
                    process = None
                    if self.stopping:
                        return
                    if not self.desired.get(slug):
                        # Ended by stop(): a screen went on air. Not a failure.
                        failures = 0
                        self._set_state(slug, "stopped")
                        continue
                    if time.monotonic() - started >= PROGRAM_SWITCH_HEALTHY_SECONDS:
                        failures = 0
                    else:
                        failures += 1
                    detail = await self._explain(slug, list(errors), code)
                    self._set_state(slug, "retrying", detail)
                    log.warning("program copy for %s ended: %s", slug, detail)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    self._set_state(slug, "retrying", redact(str(exc), source, output))
                    log.warning("program copy for %s error: %s", slug, redact(str(exc), source, output))
                delay = min(
                    PROGRAM_SWITCH_RETRY_MAX_SECONDS,
                    PROGRAM_SWITCH_RETRY_MIN_SECONDS * (2 ** min(failures, 6)),
                )
                await self._pause(wakeup, delay * random.uniform(0.7, 1.3))
        except asyncio.CancelledError:
            if process and process.returncode is None:
                await end_process(process)
            raise
        finally:
            self.processes.pop(slug, None)

    async def _supervise(
        self,
        slug: str,
        process: asyncio.subprocess.Process,
        heartbeat: dict[str, float | None],
        started: float,
    ) -> int | None:
        """Wait for the copy, killing one whose input is gone or wedged.

        MediaMTX closes the copy's read session when OBS leaves, so the normal
        end is an input EOF; the checks here are for the copy that somehow
        outlives its publisher, and the one that stops reporting.
        """
        waiter = asyncio.ensure_future(process.wait())
        offline = 0
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=1.0)
                if done:
                    return waiter.result()
                ingest = await media_status(slug)
                if ingest.get("known", True):
                    offline = 0 if ingest["online"] else offline + 1
                reference = heartbeat["sampled_at"] or started
                if (
                    offline >= PROGRAM_SWITCH_OFFLINE_SAMPLES
                    or time.monotonic() - reference > PROGRAM_SWITCH_STALL_SECONDS
                ):
                    log.warning("program copy for %s lost its input; restarting", slug)
                    process.kill()
                    return await waiter
        finally:
            if not waiter.done():
                waiter.cancel()


program_switch = ProgramSwitch()


class SignalMetrics:
    """Samples MediaMTX once per tick and keeps per-stream history in memory.

    Every counter MediaMTX reports is cumulative per connection, so the whole
    series is dropped whenever the SRT connection id changes — otherwise a
    reconnect draws a negative spike. `bitrate` and `rtt` are passed through
    from MediaMTX verbatim and are NOT derived here; only `bytes`/`moving_at`
    are delta-tracked, and they feed `stalled_for`, never a display.
    """

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.paths: dict[str, dict[str, Any]] = {}
        self.publishers: dict[str, dict[str, Any]] = {}
        self.readers: dict[str, int] = {}
        self.history: dict[str, dict[str, Any]] = {}
        self.sampled_at: float | None = None
        self.reachable = False

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reachable = False
                log.debug("metrics sample failed: %s", exc)
            await asyncio.sleep(METRICS_INTERVAL_SECONDS)

    @staticmethod
    async def _items(client: httpx.AsyncClient, endpoint: str) -> list[dict[str, Any]]:
        response = await client.get(f"{MEDIAMTX_API}{endpoint}", params={"itemsPerPage": 500})
        response.raise_for_status()
        return response.json().get("items", []) or []

    async def sample(self) -> None:
        async with httpx.AsyncClient(timeout=4) as client:
            paths, srtconns, rtsp = await asyncio.gather(
                self._items(client, "/v3/paths/list"),
                self._items(client, "/v3/srtconns/list"),
                self._items(client, "/v3/rtspsessions/list"),
                return_exceptions=True,
            )
        if isinstance(paths, Exception):
            raise paths
        self.reachable = True
        self.paths = {item.get("name"): item for item in paths if item.get("name")}
        self.publishers = {}
        if not isinstance(srtconns, Exception):
            for item in srtconns:
                if item.get("state") == "publish" and item.get("path"):
                    self.publishers[item["path"]] = item
        self.readers = {}
        if not isinstance(rtsp, Exception):
            for item in rtsp:
                # Readers only: the ProgramSwitch copy holds a publish session on
                # the program path, and it is not a forwarder.
                if item.get("path") and item.get("state") == "read":
                    self.readers[item["path"]] = self.readers.get(item["path"], 0) + 1
        self.sampled_at = time.monotonic()
        for slug in self.paths:
            self._record(slug)
        for stale in set(self.history) - set(self.paths):
            self.history.pop(stale, None)

    def _record(self, slug: str) -> None:
        path = self.paths.get(slug) or {}
        conn = self.publishers.get(slug)
        connection_id = conn.get("id") if conn else None
        entry = self.history.get(slug)
        if entry is None or entry["connection_id"] != connection_id:
            entry = {
                "connection_id": connection_id,
                "bitrate": deque(maxlen=METRICS_SIGNAL_SAMPLES),
                "rtt": deque(maxlen=METRICS_SIGNAL_SAMPLES),
                "bytes": None,
                "bytes_at": None,
                "moving_at": time.monotonic(),
            }
            self.history[slug] = entry

        received = path.get("inboundBytes")
        if received is None:
            received = path.get("bytesReceived")
        moment = time.monotonic()
        if isinstance(received, (int, float)):
            if entry["bytes"] is not None and received > entry["bytes"]:
                entry["moving_at"] = moment
            entry["bytes"] = received
            entry["bytes_at"] = moment

        if conn:
            rate = conn.get("mbpsReceiveRate")
            if isinstance(rate, (int, float)):
                entry["bitrate"].append(round(float(rate) * 1000, 1))
            rtt = conn.get("msRTT")
            if isinstance(rtt, (int, float)):
                entry["rtt"].append(round(float(rtt), 1))

    def stalled_for(self, slug: str) -> float | None:
        """Seconds since the publisher last delivered bytes, if one is connected.

        Gated on an actual SRT publisher rather than on the path being readable —
        an always-available path is readable whether or not anyone is publishing,
        so using that would kick a publisher that does not exist.
        """
        entry = self.history.get(slug)
        if not entry or slug not in self.publishers or entry["bytes"] is None:
            return None
        return time.monotonic() - entry["moving_at"]

    @staticmethod
    def _describe_tracks(path: dict[str, Any]) -> list[str]:
        described: list[str] = []
        for track in path.get("tracks2") or []:
            codec = track.get("codec") or "?"
            props = track.get("codecProps") or {}
            if props.get("width"):
                described.append(
                    f"{props['width']}×{props.get('height', '?')} {codec}"
                    + (f" {props['profile']}" if props.get("profile") else "")
                )
            elif props.get("sampleRate"):
                channels = props.get("channelCount")
                described.append(
                    f"{codec} {int(props['sampleRate']) // 1000} kHz"
                    + (" stereo" if channels == 2 else f" {channels}ch" if channels else "")
                )
            else:
                described.append(str(codec))
        if described:
            return described
        return [str(item) for item in (path.get("tracks") or [])]

    def snapshot(self, slug: str) -> dict[str, Any]:
        path = self.paths.get(slug) or {}
        conn = self.publishers.get(slug) or {}
        entry = self.history.get(slug) or {}
        loss_rate = conn.get("packetsReceivedLossRate")
        return {
            "known": self.reachable,
            "publishing": bool(conn),
            "rtt_ms": conn.get("msRTT"),
            "receive_mbps": conn.get("mbpsReceiveRate"),
            "link_capacity_mbps": conn.get("mbpsLinkCapacity"),
            # Already a percentage from MediaMTX; scaling it again read 100x high
            # and tripped the "Degraded" chip at a hundredth of the real loss.
            "loss_rate": round(float(loss_rate), 3) if isinstance(loss_rate, (int, float)) else None,
            "packets_lost": conn.get("packetsReceivedLoss"),
            "packets_retransmitted": conn.get("packetsReceivedRetrans"),
            "packets_dropped": conn.get("packetsReceivedDrop"),
            "packets_belated": conn.get("packetsReceivedBelated"),
            "receive_buffer_ms": conn.get("msReceiveBuf"),
            "latency_ms": conn.get("msReceiveTsbPdDelay"),
            "frames_in_error": path.get("inboundFramesInError"),
            "bytes_received": entry.get("bytes"),
            "online_since": path.get("onlineTime") or path.get("readyTime"),
            "reader_count": self.readers.get(program_path(slug), 0),
            "track_summary": self._describe_tracks(path),
            "stalled_for_s": (
                round(self.stalled_for(slug), 1) if self.stalled_for(slug) is not None else None
            ),
            "series": {
                "bitrate_kbps": list(entry.get("bitrate") or [])[-METRICS_SERIES_POINTS:],
                "rtt_ms": list(entry.get("rtt") or [])[-METRICS_SERIES_POINTS:],
            },
        }


signal_metrics = SignalMetrics()


class StallProbe:
    """Measures the delivery gaps a publisher rides out, at sub-second resolution.

    Whether the fast failover threshold can safely drop is exactly the question
    of how often the link stalls just under it, and that cannot be answered in
    retrospect: sub-threshold stalls never reach any log, and per-connection
    SRT counters die with the connection. The 1 s metrics sampler cannot help
    either -- on a healthy feed its byte counter advances every tick, so its
    resolution floor is a whole tick. This probe polls the path byte counter at
    4 Hz for exactly the streams that currently have a live publisher and
    records every recovered gap of STALL_EVENT_MIN_SECONDS or more, so the
    dashboard can report the week's counts.
    """

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.watch: dict[str, dict[str, Any]] = {}
        self.kicks: dict[str, float] = {}

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def observe(
        self, slug: str, connection_id: Any, received: Any, moment: float
    ) -> float | None:
        """One poll's worth of evidence; returns a stall duration to record.

        A reconnect starts a fresh entry, so a real drop never reads as a
        recovered stall, and a hole in our own polling suppresses the event --
        our blindness is not their stall.
        """
        entry = self.watch.get(slug)
        if entry is None or entry["connection_id"] != connection_id:
            entry = {
                "connection_id": connection_id,
                "bytes": None,
                "advanced_at": moment,
                "polled_at": moment,
            }
            self.watch[slug] = entry
        if not isinstance(received, (int, float)):
            return None
        event: float | None = None
        if entry["bytes"] is not None and received > entry["bytes"]:
            gap = moment - entry["advanced_at"]
            probe_hole = moment - entry["polled_at"]
            if (
                gap >= STALL_EVENT_MIN_SECONDS
                and probe_hole <= STALL_PROBE_INTERVAL_SECONDS * 2 + 0.1
            ):
                event = gap
            entry["advanced_at"] = moment
        elif entry["bytes"] is None:
            entry["advanced_at"] = moment
        entry["bytes"] = received
        entry["polled_at"] = moment
        return event

    def _record_event(self, slug: str, duration: float) -> None:
        log.info("publisher on %s stalled %.2fs then recovered", slug, duration)
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=STALL_EVENT_RETENTION_DAYS)
        ).isoformat()
        with connect() as conn:
            row = conn.execute("SELECT id FROM streams WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                return
            conn.execute(
                "INSERT INTO stall_events(stream_id, duration_s, recorded_at) VALUES(?, ?, ?)",
                (row["id"], round(duration, 2), now()),
            )
            conn.execute("DELETE FROM stall_events WHERE recorded_at < ?", (cutoff,))

    def stall_in_progress(self, slug: str, moment: float) -> float | None:
        """How long this publisher's delivery has been silent, if trustworthy.

        Only meaningful straight after a poll: a hole in our own polling makes
        the age of the last advance unknowable, so it reports None rather than
        a number that would kick a healthy publisher.
        """
        entry = self.watch.get(slug)
        if entry is None or entry["bytes"] is None:
            return None
        if moment - entry["polled_at"] > STALL_PROBE_INTERVAL_SECONDS * 2 + 0.1:
            return None
        return moment - entry["advanced_at"]

    async def _enforce_ultra(self, slug: str, stalled: float) -> None:
        """The 0.5 s kick, from the only loop fast enough to see it.

        The regular fast-failover check reads the 1 s sampler and stays as the
        1.5 s backstop; this fires first only on streams that opted in. The
        cheap dedupe check runs before any database read because this is on
        the 4 Hz path.
        """
        if time.monotonic() - self.kicks.get(slug, 0.0) < 10:
            return
        with connect() as conn:
            row = conn.execute("SELECT id FROM streams WHERE slug = ?", (slug,)).fetchone()
        if row is None or not ultra_failover_enabled(row["id"]):
            return
        if current_program_mode(row["id"]) != "live":
            return
        self.kicks[slug] = time.monotonic()
        log.info(
            "ultra failover: dropping stalled publisher on %s after %.2fs", slug, stalled
        )
        await kick_stream_publishers(slug)

    async def _sample(self, client: httpx.AsyncClient) -> None:
        # signal_metrics owns publisher discovery; a slug is watched only while
        # it has a live SRT publisher, so slate playback never counts as data.
        publishers = dict(signal_metrics.publishers)
        for stale in set(self.watch) - set(publishers):
            self.watch.pop(stale, None)
        for slug, conn_info in publishers.items():
            try:
                response = await client.get(
                    f"{MEDIAMTX_API}/v3/paths/get/{quote(slug, safe='')}"
                )
                response.raise_for_status()
                path = response.json()
            except Exception:
                continue
            received = path.get("inboundBytes")
            if received is None:
                received = path.get("bytesReceived")
            moment = time.monotonic()
            event = self.observe(slug, conn_info.get("id"), received, moment)
            if event is not None:
                await asyncio.to_thread(self._record_event, slug, event)
            stalled = self.stall_in_progress(slug, moment)
            if stalled is not None and stalled >= ULTRA_FAILOVER_STALL_SECONDS:
                await self._enforce_ultra(slug, stalled)

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=2) as client:
            while True:
                try:
                    await self._sample(client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(STALL_PROBE_INTERVAL_SECONDS)


stall_probe = StallProbe()


def stall_report(stream_id: int) -> dict[str, Any]:
    """The week's recovered stalls, for the dashboard.

    Counts come from the persisted ledger rather than anything in memory, so
    they survive router redeploys and cover whole streams, not the last few
    minutes.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=STALL_REPORT_WINDOW_DAYS)
    ).isoformat()
    with connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN duration_s >= 1.0 THEN 1 ELSE 0 END) AS over_1s,
                      MAX(duration_s) AS longest
               FROM stall_events WHERE stream_id = ? AND recorded_at >= ?""",
            (stream_id, cutoff),
        ).fetchone()
    return {
        "window_days": STALL_REPORT_WINDOW_DAYS,
        "over_half_s": row["total"] or 0,
        "over_1s": row["over_1s"] or 0,
        "longest_s": round(row["longest"], 2) if row["longest"] is not None else None,
    }


class FailoverAdManager:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.fast_task: asyncio.Task | None = None
        self.states: dict[int, dict[str, Any]] = {}
        self.kicks: dict[int, float] = {}
        self.probes: dict[int, float] = {}
        self.last_token_sweep = 0.0

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())
        self.fast_task = asyncio.create_task(self._fast_loop())

    async def shutdown(self) -> None:
        for task in (self.task, self.fast_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _fast_loop(self) -> None:
        """Stall enforcement on its own cadence.

        The outage loop ticks every 3 seconds, which is fine for ad checks but
        dominated how long a dead feed stayed on air before fast failover kicked
        it: sampling, threshold and loop tick added up to 2-6 seconds of starved
        players. Enforcement therefore runs once per metrics sample instead.
        """
        while True:
            try:
                with connect() as conn:
                    streams = conn.execute(
                        """SELECT s.* FROM streams s JOIN users u ON u.id = s.user_id
                           WHERE u.enabled = 1 ORDER BY s.id"""
                    ).fetchall()
                for stream in streams:
                    await self._enforce_fast_failover(stream)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(FAST_FAILOVER_CHECK_SECONDS)

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

    async def _enforce_fast_failover(self, stream: sqlite3.Row) -> None:
        """Drop a publisher that has gone silent, instead of waiting it out.

        MediaMTX only notices a vanished SRT publisher after its peer-idle
        timeout, and it then adds the entire dead interval to the first slate
        frame's timestamp — so detection latency is re-presented to viewers
        one-for-one. Kicking the stalled connection collapses that wait.

        Opt-in per stream, because the threshold must stay above the negotiated
        SRT latency plus retransmission bursts or it will drop healthy feeds.
        """
        # A tick where MediaMTX could not be reached is not a stall: a failed
        # sample leaves the metrics snapshot stale rather than empty, so acting
        # on it could kick a publisher that is fine. Hold, exactly as the outage
        # state machine does.
        if not signal_metrics.reachable:
            return
        if not fast_failover_enabled(stream["id"]):
            return
        if current_program_mode(stream["id"]) != "live":
            return
        stalled = signal_metrics.stalled_for(stream["slug"])
        if stalled is None or stalled < FAST_FAILOVER_STALL_SECONDS:
            return
        if time.monotonic() - self.kicks.get(stream["id"], 0.0) < 10:
            return
        self.kicks[stream["id"]] = time.monotonic()
        log.info("fast failover: dropping stalled publisher on %s after %.1fs", stream["slug"], stalled)
        await kick_stream_publishers(stream["slug"])

    async def _prepare_brb(self, stream: sqlite3.Row) -> None:
        await self._learn_contribution(stream)
        if not media_asset_path(stream["slug"], "brb").exists():
            return
        # The caller sampled the program mode before probing, and the probe
        # ffprobes the live feed for seconds. A manual takeover landing inside
        # that await has already put its own screen on active.mp4, so staging
        # BRB now would overwrite the screen the user just selected and leave
        # the dashboard reading "starting soon" over a BRB file. Re-read the
        # mode instead of trusting the pre-probe sample.
        if current_program_mode(stream["id"]) != "live":
            return
        try:
            await activate_screen_file(stream, "brb", reload_path=False)
        except Exception:
            pass

    async def _learn_contribution(self, stream: sqlite3.Row) -> None:
        """Record what this streamer's encoder actually produces.

        Runs on the offline-to-online edge, so each user's failover screens are
        built to match their own feed rather than one reference setup. Probing is
        cheap and rate-limited to once per online transition.
        """
        if time.monotonic() - self.probes.get(stream["id"], 0.0) < 300:
            return
        self.probes[stream["id"]] = time.monotonic()
        profile = await probe_contribution(stream["slug"])
        if not profile:
            return
        if store_contribution_profile(stream["id"], profile):
            log.info(
                "contribution profile for %s: %sx%s @%.3ffps gop=%.2fs %s L%s refs=%s bframes=%s",
                stream["slug"], profile["width"], profile["height"], profile["fps"],
                profile["gop_seconds"], profile["profile"], profile["level"],
                profile["refs"], profile["bframes"],
            )
            # The slate has to follow the feed: a stream that changed shape is
            # otherwise still covered by a slate of the previous geometry.
            await refresh_stream_slate(stream["id"], stream["slug"])

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
                    if not media.get("known", True):
                        # MediaMTX was unreachable this tick. Holding the previous
                        # state is the only safe reading: treating it as an
                        # outage would fire an ad and re-copy the screen.
                        continue
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
                        # Returning to live input is not an outage. Recording
                        # last_online=True while OBS is still absent made the
                        # next tick look like a drop and ran a real commercial on
                        # a channel that never went down.
                        state["last_online"] = online
                        state["outage_started"] = None
                        state["outage_started_at"] = None
                        state["attempted"] = False
                        state["manual_override"] = False
                        if online:
                            await self._prepare_brb(stream)
                        continue
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
    # MediaMTX will not start until its always-available file exists, and its
    # health gate depends on this process, so the slate must be in place before
    # the router reports ready.
    await ensure_bootstrap_media()
    # Use the last known ingest immediately; probing every Twitch PoP takes tens
    # of seconds and must not delay forwarding or MediaMTX's health gate.
    twitch_ingests.load_saved()
    await path_reconciler.start()
    await signal_metrics.start()
    await stall_probe.start()
    await program_switch.start()
    await workers.start_enabled()
    await failover_ads.start()
    await twitch_ingests.start(probe_now=False)
    yield
    await failover_ads.shutdown()
    await stall_probe.shutdown()
    await signal_metrics.shutdown()
    await path_reconciler.shutdown()
    await media_conversions.shutdown()
    await workers.shutdown()
    # After the forwarders, so the last frames they carried were live ones.
    await program_switch.shutdown()
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
                      (SELECT COUNT(*) FROM destinations d
                       WHERE d.stream_id = s.id AND d.enabled = 1 AND d.state = 'forwarding')
                          AS forwarding_count,
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
                # Fleet health only. Per-stream detail stays on /api/state so the
                # isolation boundary is unchanged.
                "online": bool((signal_metrics.paths.get(member["slug"]) or {}).get("online")),
                "receive_mbps": (signal_metrics.publishers.get(member["slug"]) or {}).get(
                    "mbpsReceiveRate"
                ),
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
        program_switch.ensure(member["slug"])
        return {"status": "enabled"}

    for destination_id in destination_ids:
        await workers.stop(destination_id)
    await program_switch.stop(member["slug"])
    program_switch.forget(member["slug"])
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
    # The program path's state decides whether the file can be reopened now:
    # reload declines while the forwarders read it, whatever OBS is doing.
    status = await program_status(stream["slug"])
    if body.mode == "live":
        previous = current_program_mode(stream["id"])
        set_program_mode(stream["id"], "live")
        try:
            await activate_screen_file(stream, "brb", reload_path=not status["online"])
        except Exception:
            # Do not put OBS on air while the screen it would fall back to
            # failed to activate.
            set_program_mode(stream["id"], previous)
            raise
        # OBS goes on air as soon as it is publishing — at once if it has been
        # standing by behind the screen.
        program_switch.wake(stream["slug"])
        return {"status": "live"}

    if not media_asset_path(stream["slug"], body.mode).exists():
        raise HTTPException(
            status_code=409,
            detail=f"Upload the {body.mode.replace('_', ' ')} screen first",
        )
    previous = current_program_mode(stream["id"])
    set_program_mode(stream["id"], body.mode)
    try:
        await activate_screen_file(stream, body.mode, reload_path=not status["online"])
    except Exception:
        set_program_mode(stream["id"], previous)
        raise
    # Swap first, stop second: MediaMTX opens active.mp4 by name at the moment
    # the copy's publish session ends, so this order is what puts the chosen
    # screen on air rather than the previous one. OBS itself is left connected.
    await program_switch.stop(stream["slug"])
    return {"status": body.mode}

def _path_state(path: dict[str, Any] | None) -> dict[str, Any]:
    if not path:
        return {"known": True, "available": False, "online": False, "tracks": []}
    return {
        "known": True,
        "available": True,
        # Must be "online", not "ready": for an always-available path "ready" is
        # permanently true because the slate file can always be read, so it says
        # nothing about whether OBS is publishing.
        "online": bool(path.get("online", path.get("ready"))),
        "tracks": path.get("tracks", []),
        "tracks2": path.get("tracks2", []),
        "bytes_received": path.get("inboundBytes", path.get("bytesReceived", 0)),
    }


async def media_status(slug: str) -> dict[str, Any]:
    """Current MediaMTX view of one path.

    `known` distinguishes "MediaMTX says there is no publisher" from "MediaMTX
    could not be reached", which callers must not confuse: treating a transport
    hiccup as an outage triggers ads and screen copies mid-broadcast.
    """
    if signal_metrics.sampled_at is not None and (
        time.monotonic() - signal_metrics.sampled_at < METRICS_INTERVAL_SECONDS * 3
    ):
        return _path_state(signal_metrics.paths.get(slug))
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{MEDIAMTX_API}/v3/paths/list", params={"itemsPerPage": 500})
            response.raise_for_status()
            items = response.json().get("items", [])
        return _path_state(next((item for item in items if item.get("name") == slug), None))
    except Exception:
        return {"known": False, "available": False, "online": False, "tracks": []}


async def program_status(slug: str) -> dict[str, Any]:
    """The program path: what the forwarders and viewers are actually getting.

    `online` here means OBS is on air by way of the ProgramSwitch copy; `tracks`
    are the file's while a screen plays. media_status(slug) is the ingest view,
    and `online` there means OBS is connected — possibly standing by.
    """
    return await media_status(program_path(slug))


@app.get("/api/state")
async def state(request: Request) -> dict[str, Any]:
    user = require_user(request)
    stream = stream_for_user(user["id"])
    with connect() as conn:
        destinations = conn.execute(
            """SELECT id, name, platform, enabled, state, last_error,
                      restart_count, last_started_at, music_fallback
               FROM destinations WHERE stream_id = ? ORDER BY id""",
            (stream["id"],),
        ).fetchall()
    try:
        publish_password = decrypt(stream["publish_password_enc"])
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Stored credentials cannot be decrypted. Check that FERNET_KEY matches this data volume.",
        )
    stream_id = f"publish:{stream['slug']}:{stream['publish_user']}:{publish_password}"
    obs_url = f"srt://{PUBLIC_HOST}:{SRT_PORT}?streamid={stream_id}&pkt_size=1316&latency=500000"
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    outputs = []
    for row in destinations:
        entry = dict(row)
        entry["metrics"] = workers.snapshot(row["id"]) if row["enabled"] else workers.snapshot(-1)
        outputs.append(entry)
    return {
        "user": {"username": user["username"], "display_name": user["display_name"], "role": user["role"]},
        "csrf": request.session["csrf"],
        "stream": {
            "slug": stream["slug"],
            "obs_url": obs_url,
            "media": await media_status(stream["slug"]),
            # What is on air, as distinct from what OBS is sending: `media` is
            # OBS's connection, `program` is what reaches the destinations.
            "program": {
                **await program_status(stream["slug"]),
                "switch": program_switch.snapshot(stream["slug"]),
            },
            "signal": signal_metrics.snapshot(stream["slug"]),
            "fast_failover": fast_failover_enabled(stream["id"]),
            "fast_failover_seconds": FAST_FAILOVER_STALL_SECONDS,
            "ultra_failover": ultra_failover_enabled(stream["id"]),
            "ultra_failover_seconds": ULTRA_FAILOVER_STALL_SECONDS,
            "stalls": stall_report(stream["id"]),
        },
        "twitch_ingest": twitch_ingests.selection,
        "twitch": twitch_state(user["id"], stream["id"]),
        "screens": media_assets_state(stream),
        "destinations": outputs,
    }


def validate_output_url(value: str) -> None:
    if not value.startswith(("rtmp://", "rtmps://")):
        raise HTTPException(status_code=422, detail="Destination must use an RTMP or RTMPS address")


PLATFORM_LABELS = {"twitch": "Twitch", "youtube": "YouTube", "rplay": "RPLAY", "x": "X"}


def validate_stream_key(value: str, platform: str) -> None:
    label = PLATFORM_LABELS.get(platform, platform)
    if value.startswith(("rtmp://", "rtmps://")) or any(character.isspace() for character in value):
        raise HTTPException(status_code=422, detail=f"Paste only the {label} stream key, not an RTMP address")
    if len(value) < 12:
        raise HTTPException(status_code=422, detail=f"That {label} stream key looks incomplete")
    # A key containing URL syntax would be interpolated into the ingest address
    # and could silently redirect the stream or destroy query parameters.
    if not re.fullmatch(r"[A-Za-z0-9_.:%-]+", value):
        raise HTTPException(status_code=422, detail=f"That {label} stream key contains unexpected characters")


def validate_music_fallback(enabled: bool, platform: str) -> None:
    """Only YouTube and X have a music fallback to choose.

    Every other platform maps track 1 whatever this says, so storing True there
    would read back as an ON control in the dashboard that changes nothing —
    and on this particular setting a wrong belief about what is being published
    is the whole failure mode. Reject rather than silently ignore. False is
    harmless everywhere, so it is accepted for any platform.

    Only a value the caller actually sent reaches here. The stored default is
    now on, so a create request that never mentioned the flag must not be
    judged as if it had asked for it; resolve_music_fallback() is the guard.
    """
    if enabled and platform not in {"youtube", "x"}:
        raise HTTPException(
            status_code=422,
            detail="The music fallback only applies to YouTube and X destinations",
        )


def resolve_music_fallback(requested: bool | None, platform: str) -> bool:
    """Validate an explicit music-fallback choice, resolve an omitted one.

    Flipping the stored default to on made the naive spelling a trap: with
    `music_fallback: bool = True` on the request model, a plain Twitch, RPLAY or
    custom create carries True into validate_music_fallback and 422s on a field
    the form never sent. Keeping the model field tri-state separates "chose
    True" from "chose nothing", so an explicit True on a platform that ignores
    the flag is still rejected instead of quietly stored.

    An omitted flag resolves to the platform's own default: on for the
    clean-track platforms, where the choice exists, and off for the ones that
    map track 1 regardless. A stored 1 therefore always means a live YouTube or
    X opt-in, never a value left over on a destination that cannot act on it.
    """
    if requested is None:
        return platform in {"youtube", "x"}
    validate_music_fallback(requested, platform)
    return requested


@app.post("/api/destinations", status_code=201)
async def add_destination(body: DestinationBody, request: Request) -> dict[str, int]:
    user = require_user(request)
    require_csrf(request)
    if body.platform in {"twitch", "youtube", "rplay", "x"}:
        validate_stream_key(body.output_url.strip(), body.platform)
    else:
        validate_output_url(body.output_url.strip())
    music_fallback = resolve_music_fallback(body.music_fallback, body.platform)
    stream = stream_for_user(user["id"])
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO destinations(stream_id, name, platform, output_url_enc, music_fallback, created_at) VALUES(?,?,?,?,?,?)",
            (
                stream["id"],
                body.name.strip(),
                body.platform,
                encrypt(body.output_url.strip()),
                int(music_fallback),
                now(),
            ),
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


@app.patch("/api/destinations/{destination_id}/music-fallback")
async def set_destination_music_fallback(
    destination_id: int, body: MusicFallbackBody, request: Request
) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    stream = stream_for_user(user["id"])
    with connect() as conn:
        row = conn.execute(
            "SELECT id, platform FROM destinations WHERE id = ? AND stream_id = ?",
            (destination_id, stream["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Destination not found")
        validate_music_fallback(body.enabled, row["platform"])
        conn.execute(
            "UPDATE destinations SET music_fallback = ? WHERE id = ? AND stream_id = ?",
            (int(body.enabled), destination_id, stream["id"]),
        )
    # Deliberately no restart: the audio map is fixed when the worker's ffmpeg
    # starts, so a running destination keeps its current mapping until the next
    # start. Restarting from a settings toggle would drop a live output without
    # the confirmation the dashboard puts in front of stopping one.
    return {"status": "enabled" if body.enabled else "disabled"}


@app.post("/api/destinations/{destination_id}/restart")
async def restart_destination(destination_id: int, request: Request) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    stream = stream_for_user(user["id"])
    with connect() as conn:
        row = conn.execute(
            "SELECT id, enabled FROM destinations WHERE id = ? AND stream_id = ?",
            (destination_id, stream["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Destination not found")
    if not row["enabled"]:
        raise HTTPException(status_code=409, detail="Turn this destination on first")
    await workers.stop(destination_id, abrupt=True)
    with connect() as conn:
        conn.execute("UPDATE destinations SET state = 'connecting' WHERE id = ?", (destination_id,))
    workers.start(destination_id)
    return {"status": "restarting"}


@app.patch("/api/stream/fast-failover")
async def set_stream_fast_failover(body: FastFailoverBody, request: Request) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    stream = stream_for_user(user["id"])
    set_fast_failover(stream["id"], body.enabled)
    # The two speeds are one choice: enabling either supersedes the other, and
    # the exclusivity lives here so every caller gets it, not just the toggle.
    if body.enabled:
        set_ultra_failover(stream["id"], False)
    return {"status": "enabled" if body.enabled else "disabled"}


@app.patch("/api/stream/ultra-failover")
async def set_stream_ultra_failover(body: FastFailoverBody, request: Request) -> dict[str, str]:
    user = require_user(request)
    require_csrf(request)
    stream = stream_for_user(user["id"])
    set_ultra_failover(stream["id"], body.enabled)
    if body.enabled:
        set_fast_failover(stream["id"], False)
    return {"status": "enabled" if body.enabled else "disabled"}


@app.delete("/api/destinations/{destination_id}", status_code=204)
async def delete_destination(destination_id: int, request: Request) -> Response:
    user = require_user(request)
    require_csrf(request)
    stream = stream_for_user(user["id"])
    # The ownership-scoped delete must happen first: workers.stop() takes a raw
    # destination id with no stream predicate, so stopping before verifying let
    # any signed-in user kill another user's live output.
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM destinations WHERE id = ? AND stream_id = ?",
            (destination_id, stream["id"]),
        )
        deleted = cursor.rowcount
    if not deleted:
        raise HTTPException(status_code=404, detail="Destination not found")
    await workers.stop(destination_id)
    return Response(status_code=204)


@app.post("/internal/mediamtx-auth")
async def mediamtx_auth(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    # compare_digest raises on non-str input, which would surface as a 500 and be
    # far less obvious than a denial.
    action = payload.get("action") if isinstance(payload.get("action"), str) else ""
    path = payload.get("path") if isinstance(payload.get("path"), str) else ""
    user = payload.get("user") if isinstance(payload.get("user"), str) else ""
    password = payload.get("password") if isinstance(payload.get("password"), str) else ""
    if action == "api":
        return Response(status_code=204)
    internal = secrets.compare_digest(user, MEDIA_INTERNAL_USER) and secrets.compare_digest(password, MEDIA_INTERNAL_PASS)
    if action in {"read", "playback"} and internal:
        return Response(status_code=204)
    if action == "publish":
        # The ProgramSwitch copy may publish only to a program path; the bare
        # slug is OBS's, and the internal credentials are never accepted there.
        if internal:
            if path.endswith("/program"):
                return Response(status_code=204)
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        # OBS is admitted whatever is on air. Its feed lands on the ingest path
        # only; whether it reaches viewers is the ProgramSwitch's decision, so
        # a publish during a takeover is a publisher standing by, not a fight
        # with the screen.
        with connect() as conn:
            stream = conn.execute(
                """SELECT s.* FROM streams s JOIN users u ON u.id = s.user_id
                   WHERE s.slug = ? AND s.publish_user = ? AND u.enabled = 1""",
                (path, user),
            ).fetchone()
        if stream and secrets.compare_digest(password, decrypt(stream["publish_password_enc"])):
            return Response(status_code=204)
    return JSONResponse({"detail": "Forbidden"}, status_code=403)


@app.get("/media/{media_path:path}")
async def media_proxy(media_path: str, request: Request) -> Response:
    user = require_user(request)
    stream = stream_for_user(user["id"])
    # Reject traversal and encoded separators before the prefix test, so
    # "<slug>/../<other slug>/index.m3u8" cannot escape the caller's stream.
    if ".." in media_path or "\\" in media_path or "%2e" in media_path.lower():
        raise HTTPException(status_code=403, detail="This monitor belongs to another stream")
    if not (media_path == stream["slug"] or media_path.startswith(stream["slug"] + "/")):
        raise HTTPException(status_code=403, detail="This monitor belongs to another stream")
    if media_path in (stream["slug"], program_path(stream["slug"])):
        # MediaMTX would redirect a bare path name itself, but relative to its
        # own root, which would leave the player resolving index.m3u8 against
        # the wrong path.
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/media/{media_path}/{query}", status_code=307)
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
