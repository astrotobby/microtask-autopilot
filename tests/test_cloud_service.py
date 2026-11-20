from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from microtask_monitor import cloud_export, secret_store, whop
from microtask_monitor.cloud_service import authorize_header, filter_cloud_jobs, public_base_url


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
