from __future__ import annotations

import gc
import os
import shutil
import sqlite3
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

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
        main.ensure_default_media_template()

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
        brb_takeover = self.client.patch(
            "/api/screens/mode",
            headers={"X-CSRF-Token": owner_csrf},
            json={"mode": "brb"},
        )
        self.assertEqual(brb_takeover.status_code, 200, brb_takeover.text)
        self.assertEqual(main.current_program_mode(owner_stream["id"]), "brb")
        main.kick_stream_publishers.assert_awaited_once_with("studio")

        publish_auth = {
            "action": "publish",
            "path": "studio",
            "user": owner_stream["publish_user"],
            "password": main.decrypt(owner_stream["publish_password_enc"]),
        }
        self.assertEqual(
            self.client.post("/internal/mediamtx-auth", json=publish_auth).status_code,
            403,
        )
        starting_takeover = self.client.patch(
            "/api/screens/mode",
            headers={"X-CSRF-Token": owner_csrf},
            json={"mode": "starting_soon"},
        )
        self.assertEqual(starting_takeover.status_code, 200, starting_takeover.text)
        self.assertEqual(main.current_program_mode(owner_stream["id"]), "starting_soon")
        return_live = self.client.patch(
            "/api/screens/mode",
            headers={"X-CSRF-Token": owner_csrf},
            json={"mode": "live"},
        )
        self.assertEqual(return_live.status_code, 200, return_live.text)
        self.assertEqual(main.current_program_mode(owner_stream["id"]), "live")
        self.assertEqual(
            self.client.post("/internal/mediamtx-auth", json=publish_auth).status_code,
            204,
        )
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
                "audio_track": 4,
            },
        )
        self.assertEqual(x_destination.status_code, 201, x_destination.text)
        with main.connect() as conn:
            saved_x = conn.execute(
                "SELECT platform, output_url_enc, audio_track FROM destinations WHERE id = ?",
                (x_destination.json()["id"],),
            ).fetchone()
        self.assertEqual(saved_x["platform"], "x")
        self.assertEqual(main.decrypt(saved_x["output_url_enc"]), "x-test-stream-key-123")
        self.assertEqual(saved_x["audio_track"], 4)
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
        self.assertTrue(main.media_asset_path(partner["slug"], "brb").exists())
        self.assertTrue(main.active_media_path(partner["slug"]).exists())

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
        suspended = self.client.patch(
            f"/api/team/users/{partner['id']}",
            headers={"X-CSRF-Token": owner_csrf},
            json={"enabled": False},
        )
        self.assertEqual(suspended.status_code, 200, suspended.text)
        self.assertEqual(suspended.json()["status"], "suspended")

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
