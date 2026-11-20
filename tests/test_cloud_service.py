from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import zipfile
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from microtask_monitor import cloud_export, db, secret_store, whop, workers
from microtask_monitor.cloud_service import _backup_database_once, _parse_multipart, authorize_header, filter_cloud_jobs, public_base_url


class CloudSecretStoreTests(unittest.TestCase):
    def test_reads_provider_secret_environment_vars_without_listing_values(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_SECRET__WHOP_WORKFORCE__ACCESS_0": "opaque-test-token"}, clear=False):
            store = secret_store.EncryptedFileStore(path=Path(tempfile.gettempdir()) / "autopilot-no-file.enc", key="")
            self.assertEqual(store.get("whop_workforce", "access_0"), "opaque-test-token")
            self.assertIn(("whop_workforce", "access_0"), store.names())

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "cryptography is not installed")
    def test_encrypts_and_persists_rotated_tokens(self):
        from cryptography.fernet import Fernet

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.enc"
            key = Fernet.generate_key().decode("ascii")
            store = secret_store.EncryptedFileStore(path=path, key=key)
            store.set("whop_workforce", "refresh_0", "rotated-refresh-token-value")
            raw = path.read_bytes()
            self.assertNotIn(b"rotated-refresh-token-value", raw)
            restarted = secret_store.EncryptedFileStore(path=path, key=key)
            self.assertEqual(restarted.get("whop_workforce", "refresh_0"), "rotated-refresh-token-value")
            self.assertTrue(restarted.delete("whop_workforce", "refresh_0"))
            self.assertIsNone(restarted.get("whop_workforce", "refresh_0"))

    def test_secret_values_are_not_printed_by_names(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "AUTOPILOT_SECRET__OWNER_GMAIL__APP_PASSWORD_0": "not-for-logs"
        }, clear=False):
            store = secret_store.EncryptedFileStore(path=Path(tmp) / "none.enc", key="")
            self.assertEqual(store.names(), [("owner_gmail", "app_password_0")])


    def test_cloud_ai_runner_stays_disabled_without_explicit_opt_in(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_RUNTIME":"cloud", "AUTOPILOT_ENABLE_CLOUD_AI":"false", "CODEX_API_KEY":"not-a-real-key"}, clear=True):
            with mock.patch.object(workers.shutil, "which", return_value="/usr/bin/codex"):
                self.assertIsNone(workers.available_runner())

    def test_cloud_ai_runner_requires_a_dedicated_api_key(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_RUNTIME":"cloud", "AUTOPILOT_ENABLE_CLOUD_AI":"true"}, clear=True):
            with mock.patch.object(workers.shutil, "which", return_value="/usr/bin/codex"):
                self.assertIsNone(workers.available_runner())

    def test_cloud_ai_runner_uses_codex_only_after_explicit_opt_in_and_key(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_RUNTIME":"cloud", "AUTOPILOT_ENABLE_CLOUD_AI":"true", "CODEX_API_KEY":"not-a-real-key"}, clear=True):
            with mock.patch.object(workers.shutil, "which", side_effect=lambda name: "/usr/bin/codex" if name == "codex" else None):
                runner = workers.available_runner()
                self.assertEqual(runner.kind, "codex")

    def test_admin_multipart_parser_extracts_upload_without_logging_values(self):
        boundary = "test-boundary-123"
        raw = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"archive\"; filename=\"migration.enc\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + b"encrypted-data" + (
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"passphrase\"\r\n\r\n"
            "private-passphrase-value\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        archive, passphrase = _parse_multipart(f"multipart/form-data; boundary={boundary}", raw)
        self.assertEqual(archive, b"encrypted-data")
        self.assertEqual(passphrase, "private-passphrase-value")

    def test_daily_sqlite_backup_is_consistent_and_separate_from_live_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "autopilot.sqlite3"
            conn = sqlite3.connect(live)
            conn.execute("CREATE TABLE marker (value TEXT)")
            conn.execute("INSERT INTO marker VALUES ('daily-snapshot')")
            conn.commit()
            conn.close()
            with mock.patch.object(db, "DATA_DIR", root), mock.patch.object(db, "DB_PATH", live):
                result = _backup_database_once()
            backup = Path(result["path"])
            self.assertTrue(backup.is_file())
            self.assertNotEqual(backup, live)
            check = sqlite3.connect(backup)
            try:
                self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(check.execute("SELECT value FROM marker").fetchone()[0], "daily-snapshot")
            finally:
                check.close()

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "cryptography is not installed")
    def test_migration_rejects_browser_session_paths_even_if_bundle_is_encrypted(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data/browser-profiles/cookies.sqlite", b"private-session")
        encrypted = cloud_export.encrypt_zip(zip_buffer.getvalue(), "very-private-bundle-passphrase")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cloud_export.CloudExportError):
                cloud_export.unpack_to_staging(encrypted, "very-private-bundle-passphrase", Path(tmp))



    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "cryptography is not installed")
    def test_migration_manifest_cannot_reference_paths_outside_staging(self):
        manifest = {
            "format": "microtask-autopilot-cloud-migration", "version": 1, "file_count": 1,
            "included": [{"path": "../../outside.txt", "size": 0, "sha256": "invalid"}],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("data/autopilot.sqlite3", b"not-a-database")
        bundle = cloud_export.encrypt_zip(buf.getvalue(), "safe-test-migration-passphrase")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cloud_export.CloudExportError):
                cloud_export.unpack_to_staging(bundle, "safe-test-migration-passphrase", Path(tmp))



class CloudMigrationTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "cryptography is not installed")
    def test_encrypted_export_preserves_database_and_private_profile_but_not_browser_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "microtask_monitor").mkdir()
            sqlite3 = __import__("sqlite3")
            conn = sqlite3.connect(root / "data" / "autopilot.sqlite3")
            conn.execute("CREATE TABLE marker (value TEXT)")
            conn.execute("INSERT INTO marker VALUES ('preserved-state')")
            conn.commit()
            conn.close()
            (root / "microtask_monitor" / "profile.json").write_text('{"country":"Kenya"}', encoding="utf-8")
            (root / "data" / "outreach_config.json").write_text('{"max_channels_per_run":0}', encoding="utf-8")
            (root / "data" / "browser-profiles").mkdir()
            (root / "data" / "browser-profiles" / "cookies.sqlite").write_text("must-not-export", encoding="utf-8")
            output = root / "cloud-migration.enc"
            result = cloud_export.build_bundle(root, output, "a-very-private-migration-passphrase")
            self.assertEqual(result["database_integrity"], "ok")
            self.assertTrue(output.is_file())
            with tempfile.TemporaryDirectory() as staged:
                manifest = cloud_export.unpack_to_staging(output.read_bytes(), "a-very-private-migration-passphrase", Path(staged))
                self.assertEqual(manifest["database_integrity"], "ok")
                self.assertTrue((Path(staged) / "data" / "autopilot.sqlite3").is_file())
                self.assertTrue((Path(staged) / "private" / "profile.json").is_file())
                self.assertFalse((Path(staged) / "data" / "browser-profiles").exists())
                dbconn = sqlite3.connect(Path(staged) / "data" / "autopilot.sqlite3")
                try:
                    self.assertEqual(dbconn.execute("SELECT value FROM marker").fetchone()[0], "preserved-state")
                finally:
                    dbconn.close()

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "cryptography is not installed")
    def test_wrong_passphrase_fails_closed(self):
        with self.assertRaises(cloud_export.CloudExportError):
            cloud_export.decrypt_bundle(cloud_export.encrypt_zip(b"zip", "correct-private-passphrase"), "incorrect-private-passphrase")



class CloudServicePolicyTests(unittest.TestCase):
    def test_admin_basic_auth_uses_constant_time_secret_comparison(self):
        import base64
        header = "Basic " + base64.b64encode(b"admin:01234567890123456789012345678901").decode("ascii")
        self.assertTrue(authorize_header(header, "01234567890123456789012345678901"))
        self.assertFalse(authorize_header(header, "different-0123456789012345678901"))
        self.assertFalse(authorize_header("Bearer not-basic", "01234567890123456789012345678901"))

    def test_outreach_and_lead_prospecting_are_off_by_default_in_cloud(self):
        names = ["report_sync", "discovery", "orchestrator", "submissions", "email_discovery",
                 "owner_shorts", "owner_outreach", "freelance_business"]
        jobs = [SimpleNamespace(name=n) for n in names]
        self.assertEqual([j.name for j in filter_cloud_jobs(jobs, {})],
                         ["report_sync", "discovery", "orchestrator", "submissions", "email_discovery", "owner_shorts"])

    def test_prospecting_requires_explicit_cloud_environment_flags(self):
        names = ["owner_outreach", "freelance_business"]
        jobs = [SimpleNamespace(name=n) for n in names]
        env = {"AUTOPILOT_ENABLE_OWNER_OUTREACH": "true", "AUTOPILOT_ENABLE_FREELANCE_BUSINESS": "true"}
        self.assertEqual([j.name for j in filter_cloud_jobs(jobs, env)], names)

    def test_whop_redirect_uses_render_https_domain_when_deployed(self):
        with mock.patch.dict(os.environ, {
            "AUTOPILOT_RUNTIME": "cloud",
            "RENDER_EXTERNAL_URL": "https://microtask-autopilot-cloud.onrender.com"
        }, clear=True):
            self.assertEqual(whop.redirect_uri(), "https://microtask-autopilot-cloud.onrender.com/api/whop/oauth/callback")
            self.assertEqual(public_base_url(), "https://microtask-autopilot-cloud.onrender.com")


if __name__ == "__main__":
    unittest.main()
