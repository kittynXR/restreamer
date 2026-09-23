from __future__ import annotations

import asyncio
import gc
import os
import shutil
import sqlite3
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

TEST_ROOT = Path(tempfile.mkdtemp(prefix="relay-team-tests-"))
os.environ["DB_PATH"] = str(TEST_ROOT / "relay.db")
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-long-enough"
os.environ["BOOTSTRAP_TOKEN"] = "bootstrap-test-code"
os.environ["FERNET_KEY"] = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
os.environ["MEDIA_INTERNAL_USER"] = "internal"
os.environ["MEDIA_INTERNAL_PASS"] = "internal-password"
os.environ["PUBLIC_HOST"] = "relay.example.test"

from fastapi.testclient import TestClient

from app import main

# Stand-ins for the destinations already live in production when music_fallback
# was added: name, platform, the retired audio_track value the row happens to
# carry, and what music_fallback must read once the upgrade has run. Everything
# except that new column has to survive exactly as the migration found it.
#
# The column arrives as `INTEGER NOT NULL DEFAULT 1`, and ADD COLUMN stamps that
# default onto every existing row -- including the platforms that map track 1
# whatever the flag says. Only YouTube and X can act on it, so only they come
# out of the upgrade on the new default; the rest are normalised straight back
# to 0 so a stored 1 always means a real opt-in.
LEGACY_DESTINATIONS = (
    ("Twitch", "twitch", 5, 0),
    ("YouTube", "youtube", 2, 1),
    ("YouTube Backup", "youtube", 2, 1),
    ("X", "x", 2, 1),
    ("RPLAY", "rplay", 1, 0),
    ("Custom", "custom", 1, 0),
    ("Custom Archive", "custom", 4, 0),
)


class DestinationMigrationTest(unittest.TestCase):
    """`initialize_db()` has to add `music_fallback` to a populated table.

    Seven real destinations exist in the deployment this ships to, so the sweep
    must add the column in place, run harmlessly on every subsequent boot, and
    leave every pre-existing row on a value that platform can actually act on.
    The default is on -- a one-track publish sends track 1 to YouTube and X
    rather than an FLV stream with no audio track at all -- so the sweep also
    has to clear the flag everywhere it is meaningless, and has to keep doing
    that on every later boot rather than only on the one that migrates.

    This class deliberately never starts the app: it points `main.DB_PATH` at a
    throwaway file and calls `initialize_db()` directly, so no background
    manager is reachable and none needs an `AsyncMock`.
    """

    def test_music_fallback_is_added_idempotently_and_rows_survive(self) -> None:
        legacy_db = TEST_ROOT / "legacy" / "relay.db"
        legacy_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(legacy_db)
        try:
            conn.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'streamer',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE streams (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    slug TEXT NOT NULL UNIQUE,
                    publish_user TEXT NOT NULL UNIQUE,
                    publish_password_enc TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE destinations (
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
                """
            )
            conn.execute(
                """INSERT INTO users(id, username, display_name, password_hash, role, created_at)
                   VALUES(1, 'legacy', 'Legacy', 'hash', 'owner', '2024-01-01T00:00:00+00:00')"""
            )
            conn.execute(
                """INSERT INTO streams(id, user_id, slug, publish_user, publish_password_enc, created_at)
                   VALUES(1, 1, 'legacy', 'legacy-publisher', 'legacy-enc', '2024-01-01T00:00:00+00:00')"""
            )
            for index, (name, platform, audio_track, _) in enumerate(LEGACY_DESTINATIONS, start=1):
                conn.execute(
                    """INSERT INTO destinations(id, stream_id, name, platform, output_url_enc,
                                                audio_track, enabled, state, created_at)
                       VALUES(?, 1, ?, ?, ?, ?, 1, 'forwarding', '2024-01-01T00:00:00+00:00')""",
                    (index, name, platform, f"legacy-enc-{index}", audio_track),
                )
            conn.commit()
        finally:
            conn.close()

        original_db_path = main.DB_PATH
        main.DB_PATH = legacy_db
        try:
            # Three sweeps: the boot that migrates, then two ordinary restarts.
            # A migration that is not idempotent fails on the second one.
            for _ in range(3):
                main.initialize_db()
            with main.connect() as migrated:
                columns = {
                    row["name"]: row
                    for row in migrated.execute("PRAGMA table_info(destinations)")
                }
                rows = migrated.execute(
                    """SELECT id, name, platform, output_url_enc, audio_track, enabled, state,
                              music_fallback
                       FROM destinations ORDER BY id"""
                ).fetchall()
                # Rows written by code that predates the column, or by any
                # future route that forgets resolve_music_fallback: they take
                # the raw schema default, which is now on, whatever the platform
                # can do with it.
                for name, platform, enc in (
                    ("Added Later", "youtube", "later-enc"),
                    ("Added Later Twitch", "twitch", "later-enc-2"),
                ):
                    migrated.execute(
                        """INSERT INTO destinations(stream_id, name, platform, output_url_enc,
                                                    created_at)
                           VALUES(1, ?, ?, ?, '2024-06-01T00:00:00+00:00')""",
                        (name, platform, enc),
                    )
                added_later = {
                    row["name"]: row["music_fallback"]
                    for row in migrated.execute(
                        "SELECT name, music_fallback FROM destinations WHERE name LIKE 'Added Later%'"
                    )
                }
                # A deliberate opt-out on a platform that *can* act on the flag.
                # Later boots must leave it exactly where the operator put it --
                # the normalisation is allowed to clear meaningless values, not
                # to overwrite real choices.
                migrated.execute(
                    "UPDATE destinations SET music_fallback = 0 WHERE name = 'YouTube Backup'"
                )
            # Two more ordinary restarts, now with an unsettable 1 and a real
            # opt-out sitting in the table.
            for _ in range(2):
                main.initialize_db()
            with main.connect() as settled:
                after_restart = {
                    row["name"]: row["music_fallback"]
                    for row in settled.execute("SELECT name, music_fallback FROM destinations")
                }
        finally:
            main.DB_PATH = original_db_path

        self.assertIn("music_fallback", columns)
        self.assertEqual(columns["music_fallback"]["notnull"], 1)
        # On, matching the create default: when only one track arrives, YouTube
        # and X get track 1 rather than an FLV stream with no audio track at
        # all, whose acceptance by either ingest is unverified.
        self.assertEqual(columns["music_fallback"]["dflt_value"], "1")
        # audio_track is retained-but-unread. Dropping it would rewrite the
        # table, which is the one thing a migration on live data must not do.
        self.assertIn("audio_track", columns)

        self.assertEqual(len(rows), len(LEGACY_DESTINATIONS))
        for row, (name, platform, audio_track, music_fallback) in zip(rows, LEGACY_DESTINATIONS):
            self.assertEqual(row["name"], name)
            self.assertEqual(row["platform"], platform)
            self.assertEqual(row["output_url_enc"], f"legacy-enc-{row['id']}")
            self.assertEqual(row["audio_track"], audio_track, name)
            self.assertEqual(row["enabled"], 1, name)
            self.assertEqual(row["state"], "forwarding", name)
            # Existing YouTube and X destinations come out of the upgrade on the
            # new default; every other platform is normalised back to 0, because
            # a stored 1 there would show as an ON control that changes nothing.
            self.assertEqual(row["music_fallback"], music_fallback, name)

        # The raw INSERTs took the schema default on both platforms...
        self.assertEqual(added_later["Added Later"], 1)
        self.assertEqual(added_later["Added Later Twitch"], 1)
        # ...and the next boot cleared the one that cannot act on it, which is
        # what makes the normalisation a standing invariant rather than a
        # one-shot repair of the migrating boot.
        self.assertEqual(after_restart["Added Later"], 1)
        self.assertEqual(after_restart["Added Later Twitch"], 0)
        # The operator's opt-out survived both restarts untouched.
        self.assertEqual(after_restart["YouTube Backup"], 0)
        # ...while its neighbours on the same platform were left alone too.
        self.assertEqual(after_restart["YouTube"], 1)
        self.assertEqual(after_restart["X"], 1)
        self.assertEqual(after_restart["Twitch"], 0)


class FailoverPrestageRaceTest(unittest.TestCase):
    """A manual takeover must survive the BRB pre-stage that races it.

    `_prepare_brb` runs on the offline-to-online edge and starts by probing the
    live feed with ffprobe, which takes seconds. The loop sampled the program
    mode *before* that await, so a `PATCH /api/screens/mode` landing during the
    probe had already written its own screen over `active.mp4` by the time the
    pre-stage resumed -- and the pre-stage then copied BRB over it. Production
    showed exactly that: `program_mode:1` read `starting_soon` while
    `screen_mode:1` read `brb`, written 6ms after the probe stored its profile,
    so the user got the BRB screen with the Starting Soon button lit.

    Like `DestinationMigrationTest` this never starts the app: it points
    `main.DB_PATH` at a throwaway file and drives the manager method directly.
    """

    def _prepare(self, mode_during_probe: str) -> tuple[bool, str]:
        db = TEST_ROOT / f"race-{mode_during_probe}" / "relay.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        original_db_path = main.DB_PATH
        main.DB_PATH = db
        try:
            main.initialize_db()
            with main.connect() as conn:
                conn.execute(
                    "INSERT INTO users (id, username, display_name, password_hash,"
                    " role, created_at) VALUES (1, 'kittyn', 'kittyn', 'x',"
                    " 'owner', ?)",
                    (main.now(),),
                )
                conn.execute(
                    "INSERT INTO streams (id, user_id, slug, publish_user,"
                    " publish_password_enc, created_at) VALUES"
                    " (1, 1, 'studio', 'pub_test', ?, ?)",
                    (main.encrypt("pw"), main.now()),
                )
            stream = {"id": 1, "slug": "studio"}
            main.media_asset_path("studio", "brb").write_bytes(b"brb")
            main.set_program_mode(1, "live")

            manager = main.FailoverAdManager()

            async def learn(_stream: object) -> None:
                # Stands in for the ffprobe round trip: the takeover lands here.
                main.set_program_mode(1, mode_during_probe)

            manager._learn_contribution = learn
            activate = AsyncMock()
            with patch.object(main, "activate_screen_file", activate):
                asyncio.run(manager._prepare_brb(stream))
            return activate.await_count > 0, main.current_program_mode(1)
        finally:
            main.DB_PATH = original_db_path

    def test_takeover_during_the_probe_keeps_its_own_screen(self) -> None:
        staged, mode = self._prepare("starting_soon")
        self.assertEqual(mode, "starting_soon")
        self.assertFalse(
            staged,
            "pre-stage overwrote the screen the takeover had just activated",
        )

    def test_still_stages_brb_when_the_stream_stays_live(self) -> None:
        staged, mode = self._prepare("live")
        self.assertEqual(mode, "live")
        self.assertTrue(staged, "failover cover was not staged for a live stream")

    def _refresh(self, screen_mode: str) -> tuple[bytes, bytes]:
        """Re-seed a seeded slate while `screen_mode` is on active.mp4.

        Returns what brb.mp4 and active.mp4 hold afterwards. The stream has no
        media_assets row for brb, so the slate is ours to replace; the variant
        is pre-written because there is no ffmpeg here.
        """
        db = TEST_ROOT / f"refresh-{screen_mode}" / "relay.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        original_db_path = main.DB_PATH
        main.DB_PATH = db
        try:
            main.initialize_db()
            with main.connect() as conn:
                conn.execute(
                    "INSERT INTO users (id, username, display_name, password_hash,"
                    " role, created_at) VALUES (1, 'kittyn', 'kittyn', 'x',"
                    " 'owner', ?)",
                    (main.now(),),
                )
                conn.execute(
                    "INSERT INTO streams (id, user_id, slug, publish_user,"
                    " publish_password_enc, created_at) VALUES"
                    " (1, 1, 'studio', 'pub_test', ?, ?)",
                    (main.encrypt("pw"), main.now()),
                )
            variant = main.slate_variant_path(
                main.snap_slate_variant(main.contribution_profile(1))
            )
            variant.parent.mkdir(parents=True, exist_ok=True)
            variant.write_bytes(b"fresh-slate-variant")
            main.media_asset_path("studio", "brb").write_bytes(b"stale-slate")
            main.active_media_path("studio").write_bytes(b"starting-soon-screen")
            main.set_screen_mode(1, screen_mode)
            with (
                patch.object(main, "ensure_slate_variants", AsyncMock()),
                patch.object(main, "reload_fallback_path", AsyncMock()),
            ):
                asyncio.run(main.refresh_stream_slate(1, "studio"))
            return (
                main.media_asset_path("studio", "brb").read_bytes(),
                main.active_media_path("studio").read_bytes(),
            )
        finally:
            main.DB_PATH = original_db_path

    def test_reseed_leaves_a_starting_soon_screen_on_air(self) -> None:
        brb, active = self._refresh("starting_soon")
        self.assertEqual(brb, b"fresh-slate-variant")
        self.assertEqual(
            active, b"starting-soon-screen", "re-seed put the slate over the takeover"
        )

    def test_reseed_updates_active_while_brb_is_on_air(self) -> None:
        brb, active = self._refresh("brb")
        self.assertEqual(brb, b"fresh-slate-variant")
        self.assertEqual(active, b"fresh-slate-variant")


class FreshVolumeSlateTest(unittest.TestCase):
    """The first boot on an empty `relay_data` volume must render a slate.

    `generate_placeholder_slate` has ffmpeg write its pending file straight into
    `media/_default`, and on a brand-new volume nothing else has created that
    directory. It used to be made only after ffmpeg exited, so the encode failed
    with "Error opening output ... No such file or directory", no slate landed,
    and MediaMTX had no `alwaysAvailableFile` to open. Like the tests above this
    never starts the app: it points `main.DB_PATH` at a throwaway file and swaps
    ffmpeg for a stand-in that notes whether its output directory exists yet.
    """

    def test_first_boot_creates_the_slate_directory_before_ffmpeg_runs(self) -> None:
        db = TEST_ROOT / "fresh-volume" / "relay.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        original_db_path = main.DB_PATH
        main.DB_PATH = db
        try:
            main.initialize_db()
            target = main.slate_variant_path(
                main.snap_slate_variant(dict(main.DEFAULT_CONTRIBUTION))
            )
            self.assertFalse(
                target.parent.exists(), "not a fresh volume: media/_default already exists"
            )
            output_dir_ready: list[bool] = []

            class FakeRender:
                returncode = 0

                async def communicate(self) -> tuple[bytes, bytes]:
                    return b"", b""

            async def fake_exec(*command: str, stdout: object = None, stderr: object = None) -> FakeRender:
                output = Path(command[-1])
                output_dir_ready.append(output.parent.is_dir())
                output.write_bytes(b"placeholder-slate")
                return FakeRender()

            with patch.object(main.asyncio, "create_subprocess_exec", fake_exec):
                asyncio.run(main.ensure_slate_variants())
            landed = target.exists()
        finally:
            main.DB_PATH = original_db_path

        self.assertTrue(output_dir_ready, "no slate render was attempted")
        self.assertNotIn(
            False, output_dir_ready, "output directory missing when ffmpeg was spawned"
        )
        self.assertTrue(landed, "the rendered slate never reached its variant path")


class FakeEncode:
    """Just enough of an asyncio subprocess to stand in for the screen encode."""

    def __init__(self, stdout: object) -> None:
        self.stdout = stdout
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class ScreenConversionProgressTest(unittest.TestCase):
    """What the dashboard is told while an uploaded screen converts.

    Runs the real conversion task end to end, with ffmpeg replaced by a stand-in
    that writes its output file and plays back -progress blocks. Like the tests
    above it never starts the app: it points `main.DB_PATH` at a throwaway file
    and reads `media_assets_state()`, which is what `/api/state` returns under
    `screens`.
    """

    def test_the_gauge_tracks_the_encode_and_clears_when_it_lands(self) -> None:
        db = TEST_ROOT / "conversion-progress" / "relay.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        original_db_path = main.DB_PATH
        main.DB_PATH = db
        try:
            main.initialize_db()
            with main.connect() as conn:
                conn.execute(
                    "INSERT INTO users (id, username, display_name, password_hash,"
                    " role, created_at) VALUES (1, 'kittyn', 'kittyn', 'x',"
                    " 'owner', ?)",
                    (main.now(),),
                )
                conn.execute(
                    "INSERT INTO streams (id, user_id, slug, publish_user,"
                    " publish_password_enc, created_at) VALUES"
                    " (1, 1, 'studio', 'pub_test', ?, ?)",
                    (main.encrypt("pw"), main.now()),
                )
                stream = conn.execute("SELECT * FROM streams WHERE id = 1").fetchone()
            source = main.stream_media_dir("studio") / ".brb-test.upload"
            source.write_bytes(b"uploaded-video")

            manager = main.MediaConversionManager()
            # A 20 s upload, written by ffmpeg in quarters.
            manager._probe = AsyncMock(return_value=(20.0, True))
            positions = [5.0, 10.0, 15.0, 20.0]
            seen: list[dict | None] = []
            spawned: list[tuple[tuple[str, ...], object]] = []

            class Stdout:
                def __init__(self) -> None:
                    self.lines: list[bytes] = []

                def __aiter__(self) -> "Stdout":
                    return self

                async def __anext__(self) -> bytes:
                    if not self.lines:
                        # What a dashboard poll landing between two blocks reads.
                        seen.append(main.media_assets_state(stream)["brb"]["progress"])
                        if not positions:
                            raise StopAsyncIteration
                        position = positions.pop(0)
                        self.lines = [
                            f"out_time_us={int(position * 1_000_000)}\n".encode(),
                            b"progress=continue\n",
                        ]
                    return self.lines.pop(0)

            async def fake_exec(*command: str, stdout: object = None, stderr: object = None) -> FakeEncode:
                spawned.append((command, stdout))
                Path(command[-1]).write_bytes(b"converted-screen")
                return FakeEncode(Stdout())

            async def upload() -> None:
                # The upload route's two calls, in its order.
                manager._set_status(1, "brb", "converting", "clip.mov")
                manager.start(stream, "brb", source, "clip.mov")
                await manager.tasks[(1, "brb")]

            with (
                patch.object(main, "media_conversions", manager),
                patch.object(main.asyncio, "create_subprocess_exec", fake_exec),
                patch.object(
                    main,
                    "media_status",
                    AsyncMock(return_value={"known": True, "available": True, "online": False, "tracks": []}),
                ),
                patch.object(main, "activate_screen_file", AsyncMock()),
            ):
                asyncio.run(upload())
                landed = main.media_assets_state(stream)["brb"]
            converted = main.media_asset_path("studio", "brb").read_bytes()
        finally:
            main.DB_PATH = original_db_path

        self.assertEqual(len(spawned), 1)
        command, stdout = spawned[0]
        # The gauge is fed from ffmpeg's own -progress on a piped stdout; a
        # DEVNULL there would leave the bar at zero for the whole encode.
        self.assertIn("-progress pipe:1", " ".join(command))
        self.assertIs(stdout, asyncio.subprocess.PIPE)
        self.assertNotIn(None, seen, "a poll during the encode got no gauge")
        self.assertEqual({entry["stage"] for entry in seen}, {"encoding"})
        self.assertEqual([entry["fraction"] for entry in seen], [0.0, 0.25, 0.5, 0.75, 1.0])
        # Landed: the row reads ready, the gauge is gone, and nothing is left
        # behind to draw a bar on the next poll.
        self.assertEqual(landed["status"], "ready")
        self.assertIsNone(landed["progress"])
        self.assertEqual(manager.progress, {})
        self.assertEqual(converted, b"converted-screen")
        self.assertFalse(source.exists(), "the upload was not cleaned up")


class TeamInvitationFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        conn = sqlite3.connect(main.DB_PATH)
        try:
            conn.execute(
                """CREATE TABLE users (
                       id INTEGER PRIMARY KEY,
                       username TEXT NOT NULL UNIQUE,
                       display_name TEXT NOT NULL,
                       password_hash TEXT NOT NULL,
                       role TEXT NOT NULL DEFAULT 'streamer',
                       created_at TEXT NOT NULL
                   )"""
            )
            conn.commit()
        finally:
            conn.close()

        main.path_reconciler.start = AsyncMock()
        main.path_reconciler.shutdown = AsyncMock()
        main.twitch_ingests.start = AsyncMock()
        main.twitch_ingests.shutdown = AsyncMock()
        main.workers.start_enabled = AsyncMock()
        main.workers.shutdown = AsyncMock()
        main.failover_ads.start = AsyncMock()
        main.failover_ads.shutdown = AsyncMock()
        main.media_conversions.shutdown = AsyncMock()
        main.ensure_fallback_path = AsyncMock(return_value=True)
        main.kick_stream_publishers = AsyncMock()
        main.remove_fallback_path = AsyncMock()
        # The ingest->program copy is a subprocess supervisor; its verbs are
        # what the routes are checked against, never a real ffmpeg.
        main.program_switch.start = AsyncMock()
        main.program_switch.shutdown = AsyncMock()
        main.program_switch.stop = AsyncMock()
        main.program_switch.wake = Mock()
        main.program_switch.ensure = Mock()
        main.program_switch.forget = Mock()
        cls.client_context = TestClient(main.app, base_url="https://testserver")
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        for _ in range(10):
            gc.collect()
            try:
                shutil.rmtree(TEST_ROOT)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_invite_accept_isolation_suspend_restore_and_revoke(self) -> None:
        created = self.client.post(
            "/api/setup",
            json={
                "token": "bootstrap-test-code",
                "username": "owner",
                "display_name": "Owner",
                "password": "owner-password-123",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)

        with main.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        self.assertIn("enabled", columns)

        owner_brb = main.media_asset_path("studio", "brb")
        owner_brb.write_bytes(b"test-fallback")
        main.media_asset_path("studio", "starting_soon").write_bytes(b"test-starting")
        # Seeding now hands each stream the cached slate for its own geometry
        # rather than copying whatever another tenant uploaded, so stand in for
        # the variant ensure_slate_variants() would have rendered (there is no
        # ffmpeg in the test environment).
        self.slate_variant = main.slate_variant_path(
            main.snap_slate_variant(dict(main.DEFAULT_CONTRIBUTION))
        )
        self.slate_variant.parent.mkdir(parents=True, exist_ok=True)
        self.slate_variant.write_bytes(b"test-slate-variant")

        owner_session = self.client.get("/api/session").json()
        owner_csrf = owner_session["csrf"]
        with main.connect() as conn:
            owner_stream = conn.execute(
                "SELECT * FROM streams WHERE slug = 'studio'"
            ).fetchone()
        main.media_status = AsyncMock(
            return_value={"available": True, "online": True, "tracks": ["H264"]}
        )
        main.kick_stream_publishers.reset_mock()
        main.program_switch.stop.reset_mock()
        main.program_switch.wake.reset_mock()
        brb_takeover = self.client.patch(
            "/api/screens/mode",
            headers={"X-CSRF-Token": owner_csrf},
            json={"mode": "brb"},
        )
        self.assertEqual(brb_takeover.status_code, 200, brb_takeover.text)
        self.assertEqual(main.current_program_mode(owner_stream["id"]), "brb")
        # A takeover takes OBS off air by stopping the ingest->program copy.
        # OBS's own connection is left alone: it may stand by behind the screen.
        main.program_switch.stop.assert_awaited_once_with("studio")
        main.kick_stream_publishers.assert_not_awaited()

        publish_auth = {
            "action": "publish",
            "path": "studio",
            "user": owner_stream["publish_user"],
            "password": main.decrypt(owner_stream["publish_password_enc"]),
        }
        # ...and it is admitted while the screen is on air. This is the whole
        # point of the split: connecting OBS during Starting Soon used to be a
        # 403 that OBS reported as "cannot connect to server".
        self.assertEqual(
            self.client.post("/internal/mediamtx-auth", json=publish_auth).status_code,
            204,
        )
        # Only to the ingest, though. OBS's credentials never open the program
        # path, and the internal credentials never open OBS's.
        internal = {"user": "internal", "password": "internal-password"}
        self.assertEqual(
            self.client.post(
                "/internal/mediamtx-auth",
                json={**publish_auth, "path": "studio/program"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/internal/mediamtx-auth",
                json={"action": "publish", "path": "studio/program", **internal},
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.post(
                "/internal/mediamtx-auth",
                json={"action": "publish", "path": "studio", **internal},
            ).status_code,
            403,
        )
        starting_takeover = self.client.patch(
            "/api/screens/mode",
            headers={"X-CSRF-Token": owner_csrf},
            json={"mode": "starting_soon"},
        )
        self.assertEqual(starting_takeover.status_code, 200, starting_takeover.text)
        self.assertEqual(main.current_program_mode(owner_stream["id"]), "starting_soon")
        main.program_switch.wake.assert_not_called()
        return_live = self.client.patch(
            "/api/screens/mode",
            headers={"X-CSRF-Token": owner_csrf},
            json={"mode": "live"},
        )
        self.assertEqual(return_live.status_code, 200, return_live.text)
        self.assertEqual(main.current_program_mode(owner_stream["id"]), "live")
        # Returning to live puts OBS on air by waking the copy, not by
        # reopening a gate.
        main.program_switch.wake.assert_called_once_with("studio")
        self.assertEqual(
            self.client.post("/internal/mediamtx-auth", json=publish_auth).status_code,
            204,
        )
        state = self.client.get("/api/state").json()
        # The dashboard gets both views: OBS's connection and what is on air.
        self.assertIn("online", state["stream"]["program"])
        self.assertIn("state", state["stream"]["program"]["switch"])
        self.assertEqual(
            self.client.get("/api/state").json()["screens"]["program_mode"],
            "live",
        )

        x_destination = self.client.post(
            "/api/destinations",
            headers={"X-CSRF-Token": owner_csrf},
            json={
                "name": "X",
                "platform": "x",
                "output_url": "x-test-stream-key-123",
                # Audio routing is derived from the platform, so a client that
                # still sends the retired field must not be able to steer it.
                "audio_track": 4,
            },
        )
        self.assertEqual(x_destination.status_code, 201, x_destination.text)
        with main.connect() as conn:
            saved_x = conn.execute(
                """SELECT platform, output_url_enc, audio_track, music_fallback
                   FROM destinations WHERE id = ?""",
                (x_destination.json()["id"],),
            ).fetchone()
        self.assertEqual(saved_x["platform"], "x")
        self.assertEqual(main.decrypt(saved_x["output_url_enc"]), "x-test-stream-key-123")
        # The column survives so production rows are preserved, but nothing
        # writes it any more and new rows just carry the schema default.
        self.assertEqual(saved_x["audio_track"], 1)
        # It must not reach the dashboard either -- there is nothing to choose.
        state = self.client.get("/api/state").json()
        self.assertNotIn("audio_track", state["destinations"][0])

        # The one audio setting that is left: what a YouTube or X destination
        # does when OBS sends a single track. This request never mentioned it,
        # so it arrives on -- the platform default -- because the alternative is
        # publishing FLV with no audio track at all and neither ingest is known
        # to accept that. The music mix reaching a scanned archive is the known
        # cost; the dashboard is what has to make that state loud.
        self.assertEqual(saved_x["music_fallback"], 1)
        # sqlite hands it back as 0/1 rather than a JSON boolean, the same as
        # enabled and restart_count; the dashboard reads it as truthy.
        self.assertEqual(state["destinations"][0]["music_fallback"], 1)

        x_destination_id = x_destination.json()["id"]

        def stored_music_fallback(destination_id: int) -> int:
            with main.connect() as conn:
                return conn.execute(
                    "SELECT music_fallback FROM destinations WHERE id = ?",
                    (destination_id,),
                ).fetchone()["music_fallback"]

        # Opting out is now the direction that changes something, so it is the
        # one worth watching the worker through. Toggling must not touch it: the
        # audio map is fixed when ffmpeg starts, so a running destination keeps
        # its mapping until the next start, and restarting from a settings
        # toggle would drop a live output without the confirmation the dashboard
        # puts in front of stopping one.
        with patch.object(main.workers, "start") as started, patch.object(
            main.workers, "stop", new=AsyncMock()
        ) as stopped:
            cleared = self.client.patch(
                f"/api/destinations/{x_destination_id}/music-fallback",
                headers={"X-CSRF-Token": owner_csrf},
                json={"enabled": False},
            )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(cleared.json()["status"], "disabled")
        started.assert_not_called()
        stopped.assert_not_called()
        # A real 1 -> 0 transition, not a write that happened to match what was
        # already stored.
        self.assertEqual(stored_music_fallback(x_destination_id), 0)
        self.assertEqual(
            self.client.get("/api/state").json()["destinations"][0]["music_fallback"], 0
        )
        allowed = self.client.patch(
            f"/api/destinations/{x_destination_id}/music-fallback",
            headers={"X-CSRF-Token": owner_csrf},
            json={"enabled": True},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["status"], "enabled")
        self.assertEqual(stored_music_fallback(x_destination_id), 1)
        self.assertEqual(
            self.client.get("/api/state").json()["destinations"][0]["music_fallback"], 1
        )
        # Left opted out, so the cross-user and CSRF-less PATCHes further down --
        # all of which try to turn it back on -- have something to fail to do.
        opted_out_again = self.client.patch(
            f"/api/destinations/{x_destination_id}/music-fallback",
            headers={"X-CSRF-Token": owner_csrf},
            json={"enabled": False},
        )
        self.assertEqual(opted_out_again.status_code, 200, opted_out_again.text)
        self.assertEqual(stored_music_fallback(x_destination_id), 0)

        # The regression the flipped default introduces: the create form only
        # shows this control for YouTube and X, so every other platform's create
        # arrives with no music_fallback key at all. A request model spelled
        # `bool = True` would hand True to validate_music_fallback and 422 a
        # field the user never set. Omitting it has to mean "no choice made",
        # and resolve to the platform's own default.
        for platform, output_url in (
            ("twitch", "twitch-omitted-key-123"),
            ("rplay", "rplay-omitted-key-123"),
            ("custom", "rtmp://example.test/live/omitted-key"),
        ):
            created_without_flag = self.client.post(
                "/api/destinations",
                headers={"X-CSRF-Token": owner_csrf},
                json={
                    "name": f"{platform} default",
                    "platform": platform,
                    "output_url": output_url,
                },
            )
            self.assertEqual(
                created_without_flag.status_code,
                201,
                f"{platform}: {created_without_flag.text}",
            )
            # Off, because these platforms map track 1 whatever the flag says --
            # a stored 1 has to keep meaning a live YouTube or X opt-in.
            self.assertEqual(
                stored_music_fallback(created_without_flag.json()["id"]), 0, platform
            )

        for platform, output_url in (
            ("youtube", "youtube-omitted-key-123"),
            ("x", "x-omitted-key-123"),
        ):
            created_without_flag = self.client.post(
                "/api/destinations",
                headers={"X-CSRF-Token": owner_csrf},
                json={
                    "name": f"{platform} default",
                    "platform": platform,
                    "output_url": output_url,
                },
            )
            self.assertEqual(
                created_without_flag.status_code,
                201,
                f"{platform}: {created_without_flag.text}",
            )
            # On: the same omission resolves the other way where the flag means
            # something, which is the whole point of keeping the field tri-state.
            self.assertEqual(
                stored_music_fallback(created_without_flag.json()["id"]), 1, platform
            )

        # Moving the default did not move the rule. Only YouTube and X have a
        # fallback to choose; every other platform maps track 1 regardless.
        # Storing True there would read back as an ON control that changes
        # nothing, and on this setting a wrong belief about what is being
        # published is the entire failure mode -- so an explicit True is still
        # refused, not ignored. Omitting the field, tested above, is the only
        # thing that got quieter.
        for platform, output_url in (
            ("twitch", "twitch-test-stream-key-123"),
            ("rplay", "rplay-test-stream-key-123"),
            ("custom", "rtmp://example.test/live/custom-key"),
        ):
            rejected = self.client.post(
                "/api/destinations",
                headers={"X-CSRF-Token": owner_csrf},
                json={
                    "name": platform,
                    "platform": platform,
                    "output_url": output_url,
                    "music_fallback": True,
                },
            )
            self.assertEqual(rejected.status_code, 422, f"{platform}: {rejected.text}")
            self.assertIn("YouTube and X", rejected.json()["detail"])

        # False is no longer the schema default, but it still changes nothing
        # outside YouTube and X, so every platform has to keep tolerating it --
        # including a create form that sends the field unconditionally.
        twitch_destination = self.client.post(
            "/api/destinations",
            headers={"X-CSRF-Token": owner_csrf},
            json={
                "name": "Twitch",
                "platform": "twitch",
                "output_url": "twitch-test-stream-key-123",
                "music_fallback": False,
            },
        )
        self.assertEqual(twitch_destination.status_code, 201, twitch_destination.text)
        twitch_destination_id = twitch_destination.json()["id"]
        self.assertEqual(stored_music_fallback(twitch_destination_id), 0)
        # The route enforces the same rule as the create form, and leaves the
        # stored value alone when it refuses.
        refused = self.client.patch(
            f"/api/destinations/{twitch_destination_id}/music-fallback",
            headers={"X-CSRF-Token": owner_csrf},
            json={"enabled": True},
        )
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("YouTube and X", refused.json()["detail"])
        self.assertEqual(stored_music_fallback(twitch_destination_id), 0)

        # Restating the default explicitly is still accepted -- the dashboard's
        # create form sends the field whenever it shows the control, so the
        # request that agrees with the default must not be treated as redundant
        # or refused.
        youtube_destination = self.client.post(
            "/api/destinations",
            headers={"X-CSRF-Token": owner_csrf},
            json={
                "name": "YouTube",
                "platform": "youtube",
                "output_url": "youtube-test-stream-key-123",
                "music_fallback": True,
            },
        )
        self.assertEqual(youtube_destination.status_code, 201, youtube_destination.text)
        self.assertEqual(stored_music_fallback(youtube_destination.json()["id"]), 1)

        # The direction that now carries an operator decision: a YouTube channel
        # whose owner would rather archive silence than the music mix. It has to
        # be storable at creation time, not only reachable by creating a
        # destination and then turning it off.
        youtube_opted_out = self.client.post(
            "/api/destinations",
            headers={"X-CSRF-Token": owner_csrf},
            json={
                "name": "YouTube Silent",
                "platform": "youtube",
                "output_url": "youtube-silent-stream-key",
                "music_fallback": False,
            },
        )
        self.assertEqual(youtube_opted_out.status_code, 201, youtube_opted_out.text)
        self.assertEqual(stored_music_fallback(youtube_opted_out.json()["id"]), 0)

        invitation = self.client.post(
            "/api/team/invites",
            headers={"X-CSRF-Token": owner_csrf},
            json={"label": "Partner Streamer", "expires_in_days": 7},
        )
        self.assertEqual(invitation.status_code, 201, invitation.text)
        token = invitation.json()["invite_url"].split("#invite=", 1)[1]

        self.client.post("/api/logout", headers={"X-CSRF-Token": owner_csrf})
        lookup = self.client.post("/api/invite", json={"token": token})
        self.assertEqual(lookup.status_code, 200, lookup.text)
        self.assertEqual(lookup.json()["label"], "Partner Streamer")

        accepted = self.client.post(
            "/api/invite/accept",
            json={
                "token": token,
                "username": "partner",
                "display_name": "Partner Streamer",
                "password": "partner-password-123",
            },
        )
        self.assertEqual(accepted.status_code, 201, accepted.text)
        partner_session = self.client.get("/api/session").json()
        self.assertTrue(partner_session["authenticated"])
        self.assertEqual(partner_session["user"]["role"], "streamer")
        self.assertEqual(self.client.get("/api/team").status_code, 403)
        self.assertEqual(self.client.post("/api/invite", json={"token": token}).status_code, 410)

        with main.connect() as conn:
            partner = conn.execute(
                """SELECT u.id, s.slug FROM users u JOIN streams s ON s.user_id = u.id
                   WHERE u.username = 'partner'"""
            ).fetchone()
        self.assertIsNotNone(partner)
        partner_brb = main.media_asset_path(partner["slug"], "brb")
        self.assertTrue(partner_brb.exists())
        self.assertTrue(main.active_media_path(partner["slug"]).exists())
        # The invited member gets the generated slate for their geometry, never
        # the owner's uploaded screen. Copying it across tenants both leaked one
        # operator's card onto another's channel and handed them the wrong
        # resolution.
        self.assertEqual(partner_brb.read_bytes(), b"test-slate-variant")
        self.assertNotEqual(partner_brb.read_bytes(), owner_brb.read_bytes())

        self.client.post(
            "/api/logout",
            headers={"X-CSRF-Token": partner_session["csrf"]},
        )
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"username": "owner", "password": "owner-password-123"},
            ).status_code,
            200,
        )
        owner_session = self.client.get("/api/session").json()
        owner_csrf = owner_session["csrf"]
        team = self.client.get("/api/team")
        self.assertEqual(team.status_code, 200, team.text)
        members = {member["username"]: member for member in team.json()["members"]}
        self.assertEqual(set(members), {"owner", "partner"})
        self.assertEqual(team.json()["invitations"], [])

        self.assertEqual(
            self.client.patch(
                f"/api/team/users/{members['owner']['id']}",
                headers={"X-CSRF-Token": owner_csrf},
                json={"enabled": False},
            ).status_code,
            409,
        )
        main.program_switch.stop.reset_mock()
        main.kick_stream_publishers.reset_mock()
        main.remove_fallback_path.reset_mock()
        suspended = self.client.patch(
            f"/api/team/users/{partner['id']}",
            headers={"X-CSRF-Token": owner_csrf},
            json={"enabled": False},
        )
        self.assertEqual(suspended.status_code, 200, suspended.text)
        self.assertEqual(suspended.json()["status"], "suspended")
        # Suspension is the one case that still disconnects OBS outright, and
        # it also takes the copy and both paths down with it.
        main.program_switch.stop.assert_awaited_once_with(partner["slug"])
        main.program_switch.forget.assert_called_with(partner["slug"])
        main.kick_stream_publishers.assert_awaited_once_with(partner["slug"])
        main.remove_fallback_path.assert_awaited_once_with(partner["slug"])

        self.client.post("/api/logout", headers={"X-CSRF-Token": owner_csrf})
        self.assertEqual(
            self.client.post(
                "/api/login",
                json={"username": "partner", "password": "partner-password-123"},
            ).status_code,
            401,
        )

        self.client.post(
            "/api/login",
            json={"username": "owner", "password": "owner-password-123"},
        )
        owner_csrf = self.client.get("/api/session").json()["csrf"]
        restored = self.client.patch(
            f"/api/team/users/{partner['id']}",
            headers={"X-CSRF-Token": owner_csrf},
            json={"enabled": True},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["status"], "enabled")

        owner_destination_id = x_destination.json()["id"]

        # The per-destination audio mode is gone: the route it was changed
        # through must not answer, for the owner or anyone else.
        self.assertEqual(
            self.client.patch(
                f"/api/destinations/{owner_destination_id}/audio",
                headers={"X-CSRF-Token": owner_csrf},
                json={"audio_track": 2},
            ).status_code,
            404,
        )

        # Every destination route must be scoped to the caller's own stream.
        # Stopping the worker before the ownership check previously let any
        # signed-in user kill another user's live output.
        self.client.post("/api/logout", headers={"X-CSRF-Token": owner_csrf})
        self.client.post(
            "/api/login",
            json={"username": "partner", "password": "partner-password-123"},
        )
        partner_csrf = self.client.get("/api/session").json()["csrf"]
        headers = {"X-CSRF-Token": partner_csrf}
        self.assertEqual(
            self.client.patch(
                f"/api/destinations/{owner_destination_id}",
                headers=headers,
                json={"enabled": True},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/destinations/{owner_destination_id}/restart", headers=headers
            ).status_code,
            404,
        )
        # Including the newest one: nobody may decide what another user's
        # YouTube or X archive is allowed to contain.
        self.assertEqual(
            self.client.patch(
                f"/api/destinations/{owner_destination_id}/music-fallback",
                headers=headers,
                json={"enabled": True},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/destinations/{owner_destination_id}", headers=headers
            ).status_code,
            404,
        )
        # Mutations without the session CSRF token must be refused outright.
        self.assertEqual(
            self.client.delete(f"/api/destinations/{owner_destination_id}").status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/destinations/{owner_destination_id}/music-fallback",
                json={"enabled": True},
            ).status_code,
            403,
        )
        # The owner's destination survived every one of those attempts.
        with main.connect() as conn:
            survivor = conn.execute(
                "SELECT enabled, state, music_fallback FROM destinations WHERE id = ?",
                (owner_destination_id,),
            ).fetchone()
        self.assertIsNotNone(survivor)
        self.assertEqual(survivor["enabled"], 0)
        self.assertEqual(survivor["state"], "off")
        # Neither the cross-user PATCH nor the CSRF-less one wrote anything: the
        # owner left this destination opted out, and both attempts asked for it
        # back on, so 0 is the proof rather than a coincidence of the default.
        self.assertEqual(survivor["music_fallback"], 0)

        # The monitor proxy must not be reachable across streams, by name or by
        # traversal out of the caller's own prefix.
        self.assertEqual(self.client.get("/media/studio/index.m3u8").status_code, 403)
        self.assertEqual(
            self.client.get(f"/media/{partner['slug']}/../studio/index.m3u8").status_code,
            403,
        )
        # The program path lives under the same prefix, so the same rule holds
        # there, and the bare program name redirects to its own directory
        # rather than letting MediaMTX send the player up to the ingest.
        self.assertEqual(self.client.get("/media/studio/program/index.m3u8").status_code, 403)
        program_redirect = self.client.get(
            f"/media/{partner['slug']}/program?muted=false", follow_redirects=False
        )
        self.assertEqual(program_redirect.status_code, 307)
        self.assertEqual(
            program_redirect.headers["location"], f"/media/{partner['slug']}/program/?muted=false"
        )

        # A publish must be refused when the credentials belong to another path.
        self.assertEqual(
            self.client.post(
                "/internal/mediamtx-auth",
                json={
                    "action": "publish",
                    "path": partner["slug"],
                    "user": owner_stream["publish_user"],
                    "password": main.decrypt(owner_stream["publish_password_enc"]),
                },
            ).status_code,
            403,
        )
        # Malformed payloads are denials, not 500s.
        self.assertEqual(
            self.client.post("/internal/mediamtx-auth", json={"action": 5}).status_code,
            403,
        )

        self.client.post("/api/logout", headers=headers)
        self.client.post(
            "/api/login",
            json={"username": "owner", "password": "owner-password-123"},
        )
        owner_csrf = self.client.get("/api/session").json()["csrf"]

        invitation = self.client.post(
            "/api/team/invites",
            headers={"X-CSRF-Token": owner_csrf},
            json={"label": "Revoked Test", "expires_in_days": 1},
        )
        revoked_token = invitation.json()["invite_url"].split("#invite=", 1)[1]
        invite_id = invitation.json()["id"]
        revoked = self.client.delete(
            f"/api/team/invites/{invite_id}",
            headers={"X-CSRF-Token": owner_csrf},
        )
        self.assertEqual(revoked.status_code, 204, revoked.text)
        self.assertEqual(
            self.client.post("/api/invite", json={"token": revoked_token}).status_code,
            410,
        )


if __name__ == "__main__":
    unittest.main()
