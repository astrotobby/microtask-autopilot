import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import connect, connections as cx, db, execution as ex, policy, secret_store as ss, workers
from tests.test_api import ApiBase

CAPS = policy.load_capabilities()
SECRET = "sk-TESTSECRETabcdefghijklmnopqrstuv"


def fake_caps(**over):
    rec = {"platform_id": "demo", "name": "Demo", "levels": ["HUMAN_APPROVAL_REQUIRED"], "access_class": "browser_automation_permitted",
           "permitted_automation": ["authenticated_inspect"], "prohibited_automation": [], "country_eligibility": {"kenya": "job_specific"}}
    rec.update(over)
    return {"demo": rec}


class RedactionTests(unittest.TestCase):
    def test_common_secret_shapes_are_redacted(self):
        samples = {
            "password=hunter2xyz": "hunter2xyz", "token: abcdef123456": "abcdef123456", "Authorization: Bearer eyJabc.def123456789.ghi45678": "eyJabc",
            f"key is {SECRET}": SECRET, "https://x.example/cb?code=SECRETCODE123&x=1": "SECRETCODE123", "cookie=sessid_998877": "sessid_998877",
            "ghp_" + "a" * 36: "ghp_" + "a" * 36, "api_key = 'zzzz9999yyyy'": "zzzz9999yyyy",
        }
        for text, secret in samples.items():
            out = ss.redact(text)
            self.assertNotIn(secret, out, text)
            self.assertIn("REDACTED", out)
        self.assertEqual(ss.redact("Ran 3 tests in 0.1s - OK"), "Ran 3 tests in 0.1s - OK")

    def test_audit_log_and_execution_events_are_redacted_in_sqlite(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "a.sqlite3"
            with db.session(path) as conn:
                with db.tx(conn):
                    db.audit(conn, "x", "y", 1, {"note": f"password={SECRET}"})
                j = ex.create_job(conn, "Job", "Write a Python script with unit tests for the backend API function.", exec_dir=Path(t) / "e")
                with db.tx(conn):
                    ex.event(conn, j["id"], "progress", f"tool printed token={SECRET}")
            raw = path.read_bytes()
            self.assertNotIn(SECRET.encode(), raw)


class SecretStoreTests(unittest.TestCase):
    def test_memory_store_and_target_names(self):
        s = ss.MemoryStore()
        s.set("opentrain_ai", "api_key", "v")
        self.assertEqual(s.get("opentrain_ai", "api_key"), "v")
        self.assertEqual(s.names(), [("opentrain_ai", "api_key")])
        self.assertTrue(s.delete("opentrain_ai", "api_key"))
        with self.assertRaises(ValueError):
            ss.target("../evil", "x")
        with self.assertRaises(ValueError):
            s.set("opentrain_ai", "api_key", "")

    def test_no_plaintext_fallback_off_windows(self):
        with mock.patch.object(ss.os, "name", "posix"):
            with self.assertRaises(ss.SecretStoreUnavailable):
                ss.WindowsCredentialStore()

    @unittest.skipUnless(os.name == "nt" and os.environ.get("AUTOPILOT_TEST_CREDMAN") == "1", "real Credential Manager round trip (opt-in)")
    def test_windows_credential_manager_round_trip(self):
        s = ss.WindowsCredentialStore()
        try:
            s.set("selftest", "probe", "value-123")
            self.assertEqual(s.get("selftest", "probe"), "value-123")
            self.assertIn(("selftest", "probe"), s.names())
        finally:
            s.delete("selftest", "probe")
        self.assertIsNone(s.get("selftest", "probe"))

    def test_connect_cli_uses_a_hidden_prompt_never_prints_the_value_and_refuses_platforms_without_an_api(self):
        store, printed = ss.MemoryStore(), []
        self.assertEqual(connect.main(["set-key", "opentrain_ai", "api_key"], store=store, ask=lambda p: SECRET, out=printed.append), 0)
        self.assertEqual(store.get("opentrain_ai", "api_key"), SECRET)
        self.assertEqual(connect.main(["set-key", "microworkers", "api_key"], store=store, ask=lambda p: SECRET, out=printed.append), 1)
        self.assertIsNone(store.get("microworkers", "api_key"))
        connect.main(["list"], store=store, out=printed.append)
        self.assertFalse(any(SECRET in str(p) for p in printed))
        self.assertEqual(connect.main(["delete-key", "opentrain_ai", "api_key"], store=store, out=printed.append), 0)
        self.assertEqual(connect.main(["set-key", "nope", "k"], store=store, out=printed.append), 2)


class PolicyTests(unittest.TestCase):
    def test_no_real_platform_permits_account_automation_today(self):
        for pid in CAPS:
            self.assertIn(CAPS[pid].get("access_class"), policy.ACCESS_CLASSES, pid)
            self.assertNotEqual(CAPS[pid]["access_class"], "browser_automation_permitted", pid)
            for action in policy.AUTHENTICATED:
                self.assertFalse(policy.check_action(pid, action, user_authorized=True).allowed, (pid, action))
        self.assertEqual(CAPS["opentrain_ai"]["access_class"], "official_api_permitted")

    def test_authenticated_actions_need_class_listing_and_authorization(self):
        caps = fake_caps()
        self.assertFalse(policy.check_action("demo", "authenticated_inspect", caps=caps).allowed)                 # not authorized
        self.assertTrue(policy.check_action("demo", "authenticated_inspect", caps=caps, user_authorized=True).allowed)
        self.assertFalse(policy.check_action("demo", "submit_via_workflow", caps=caps, user_authorized=True).allowed)  # not listed
        self.assertFalse(policy.check_action("demo", "authenticated_inspect", caps=fake_caps(levels=["AUTOMATION_PROHIBITED"]), user_authorized=True).allowed)
        self.assertFalse(policy.check_action("demo", "authenticated_inspect", caps=fake_caps(access_class=None), user_authorized=True).allowed)
        for blocked in ("bypass_captcha", "bypass_mfa", "bypass_kyc", "create_account"):
            self.assertFalse(policy.check_action("demo", blocked, caps=caps, user_authorized=True).allowed)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "a.sqlite3"
        ctx = db.session(self.path)
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.launched = []

    def open(self, pid="opentrain_ai", caps=None):
        return cx.open_login(self.conn, pid, caps, Path(self.tmp.name) / "profiles", launcher=self.launched.append, browser="edge.exe")

    def test_every_platform_has_a_status_and_nothing_starts_connected(self):
        rows = cx.list_status(self.conn)
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(r["status"] in ("not_connected", "blocked") for r in rows))
        self.assertEqual(next(r for r in rows if r["platform_id"] == "mindrift")["status"], "blocked")
        for r in rows:
            self.assertTrue(r["automation_level"])
            self.assertIsNone(r["verified_evidence"])

    def test_login_window_uses_a_dedicated_profile_and_no_automation_hooks(self):
        st = self.open()
        cmd = self.launched[0]
        self.assertEqual(cmd[0], "edge.exe")
        self.assertTrue(any(a.startswith("--user-data-dir=") and a.endswith("opentrain_ai") for a in cmd))
        self.assertFalse(any("remote-debugging" in a or "--headless" in a or "automation" in a.lower() for a in cmd))
        self.assertTrue(cmd[-1].startswith("https://"))
        self.assertEqual(st["status"], "login_in_progress")
        with self.assertRaises(cx.ConnectionError_):
            self.open("mindrift")                                   # Kenya unsupported: never connect
        with self.assertRaises(cx.ConnectionError_):
            cx.browser_command("b", Path("p"), "http://insecure.example")

    def test_only_you_can_confirm_a_connection_and_it_needs_rechecking(self):
        self.open()
        with self.assertRaises(cx.ConnectionError_):
            cx.confirm_signed_in(self.conn, "opentrain_ai", False)
        st = cx.confirm_signed_in(self.conn, "opentrain_ai", True)
        self.assertEqual((st["status"], st["verified_evidence"]), ("connected_user_confirmed", "user_confirmation"))
        later = datetime.now(timezone.utc) + timedelta(days=cx.SESSION_CHECK_DAYS + 1)
        st = cx.status(self.conn, "opentrain_ai", now=later)
        self.assertEqual(st["status"], "check_due")
        self.assertIn("confirm", st["required_action"])

    def test_human_steps_and_expired_sessions_pause_authenticated_work_until_you_recover_them(self):
        caps = fake_caps()
        with mock.patch.object(cx, "login_url", return_value="https://demo.example/login"):
            self.open("demo", caps)
        with self.assertRaises(cx.SessionRequired):                       # not confirmed yet
            cx.require_session(self.conn, "demo", "authenticated_inspect", True, caps)
        cx.confirm_signed_in(self.conn, "demo", True, caps)
        self.assertEqual(cx.require_session(self.conn, "demo", "authenticated_inspect", True, caps)["status"], "connected_user_confirmed")
        st = cx.report_human_step(self.conn, "demo", "captcha", caps)
        self.assertEqual(st["status"], "needs_user_action")
        self.assertIn("never bypasses", st["required_action"])
        with self.assertRaises(cx.SessionRequired):
            cx.require_session(self.conn, "demo", "authenticated_inspect", True, caps)
        cx.confirm_signed_in(self.conn, "demo", True, caps)
        cx.report_expired(self.conn, "demo", caps)
        with self.assertRaises(cx.SessionRequired):
            cx.require_session(self.conn, "demo", "authenticated_inspect", True, caps)
        cx.confirm_signed_in(self.conn, "demo", True, caps)                    # recovery
        self.assertTrue(cx.require_session(self.conn, "demo", "authenticated_inspect", True, caps))
        with self.assertRaises(policy.PermissionDenied):                     # permission is checked before the session
            cx.require_session(self.conn, "demo", "submit_via_workflow", True, caps)
        with self.assertRaises(cx.ConnectionError_):
            cx.report_human_step(self.conn, "demo", "magic", caps)

    def test_real_platforms_are_refused_even_when_connected(self):
        self.open()
        cx.confirm_signed_in(self.conn, "opentrain_ai", True)
        with self.assertRaises(policy.PermissionDenied):
            cx.require_session(self.conn, "opentrain_ai", "submit_via_workflow", True)

    def test_disconnect_and_unknown_platform(self):
        self.open()
        cx.confirm_signed_in(self.conn, "opentrain_ai", True)
        self.assertEqual(cx.disconnect(self.conn, "opentrain_ai")["status"], "not_connected")
        with self.assertRaises(cx.ConnectionError_):
            cx.status(self.conn, "nope")

    def test_the_connections_table_has_no_place_for_secrets(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(platform_connections)")}
        self.assertFalse(cols & {"password", "cookie", "token", "secret", "api_key", "session"})
        self.assertEqual(db.user_version(self.conn), db.LATEST_VERSION)


class ConnectionApiTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.launched = []
        self.server.RequestHandlerClass.api.launcher = self.launched.append

    def test_connection_flow_over_local_http(self):
        s, r = self.get("/api/connections")
        self.assertEqual((s, len(r["connections"])), (200, 12))
        body = json.dumps(r).lower()
        for banned in ("password", "cookie\"", "api_key\""):
            self.assertNotIn(banned, body)
        s, r = self.post("/api/connections/opentrain_ai/open")
        self.assertEqual((s, r["connection"]["status"]), (200, "login_in_progress"))
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.post("/api/connections/opentrain_ai/confirm", {})[0], 400)
        s, r = self.post("/api/connections/opentrain_ai/confirm", {"confirmed_by_user": True})
        self.assertEqual(r["connection"]["status"], "connected_user_confirmed")
        s, r = self.post("/api/connections/opentrain_ai/human-step", {"kind": "mfa"})
        self.assertEqual(r["connection"]["status"], "needs_user_action")
        self.assertEqual(self.post("/api/connections/mindrift/open")[0], 400)

    def test_credentials_are_refused_by_every_endpoint(self):
        for path in ("/api/connections/opentrain_ai/confirm", "/api/executions", "/api/applications"):
            for field in ("password", "otp", "cookie", "access_token", "api_key"):
                s, r = self.post(path, {field: "x", "confirmed_by_user": True})
                self.assertEqual(s, 400, (path, field))
                self.assertIn("Never send", r["error"])

    def test_local_only_host_check_applies_to_connections(self):
        s, r = self.call("GET", "/api/connections", host="evil.example:8765")
        self.assertEqual(s, 403)
        s, r = self.call("POST", "/api/connections/opentrain_ai/open", body={}, headers={"Origin": "https://evil.example"})
        self.assertEqual(s, 403)


class ClaudeEnvTests(unittest.TestCase):
    def test_a_non_anthropic_gateway_is_dropped_for_the_claude_runner_only(self):
        env = {"ANTHROPIC_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "gemini", "PATH": "p"}
        self.assertEqual(workers.claude_env(env), {"PATH": "p"})
        keep = {"ANTHROPIC_BASE_URL": "https://api.anthropic.com", "ANTHROPIC_API_KEY": "k"}
        self.assertEqual(workers.claude_env(dict(keep)), keep)
        self.assertEqual(workers.claude_env({"PATH": "p"}), {"PATH": "p"})
        self.assertEqual(workers.claude_env({"ANTHROPIC_MODEL": "gemini-2.5-flash", "PATH": "p"}), {"PATH": "p"})       # non-Claude model alone
        self.assertEqual(workers.claude_env({"ANTHROPIC_MODEL": "claude-sonnet-5-5"}), {"ANTHROPIC_MODEL": "claude-sonnet-5-5"})


if __name__ == "__main__":
    unittest.main()
