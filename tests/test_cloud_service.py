from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from microtask_monitor import secret_store, whop
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
