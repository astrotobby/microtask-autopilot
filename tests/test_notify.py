import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import db, intelligence as ig, notify, policy, profile_answers as pa, workflow as wf

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
CAPS = policy.load_capabilities()
PROFILE = ig.load_profile()


def opp(n=1, **kw):
    base = {"platform_id": "opentrain_ai", "external_id": f"j{n}", "source": "OpenTrain AI",
            "url": f"https://www.opentrain.ai/jobs/j{n}", "title": f"Python Automation Engineer {n}",
            "summary": "Build n8n workflows and Python APIs with Claude Code.", "kenya_eligibility": "kenya_explicitly_eligible",
            "payment_display": "USD 40/hour", "payment_usd_per_hour": 40, "published": NOW.isoformat(), "status": "open"}
    base.update(kw)
    return base


class Recorder:
    def __init__(self, result=True):
        self.calls, self.result = [], result

    def __call__(self, title, body):
        self.calls.append((title, body))
        return self.result


class NotifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ctx = db.session(Path(self.tmp.name) / "a.sqlite3")
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.desktop, self.email, self.telegram = Recorder(), Recorder(), Recorder()
        self.channels = {"desktop": self.desktop, "email": self.email, "telegram": self.telegram}

    def ingest(self, *items):
        with db.tx(self.conn):
            ig.ingest(self.conn, list(items), now=NOW, caps=CAPS, profile=PROFILE)

    def generate(self, **kw):
        with db.tx(self.conn):
            return notify.generate(self.conn, now=kw.pop("now", NOW), channels=self.channels, **kw)


class DeduplicationTests(NotifyBase):
    def test_same_opportunity_alerts_only_once_across_cycles(self):
        self.ingest(opp())
        first = self.generate()
        again = self.generate()
        later = self.generate(now=NOW + timedelta(days=1))
        self.assertEqual(len(first), 1)
        self.assertEqual((again, later), ([], []))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)
        self.assertEqual(len(self.desktop.calls), 1)

    def test_create_is_idempotent_per_fingerprint_and_state_change_realerts(self):
        with db.tx(self.conn):
            a = notify.create(self.conn, "payment_update", "Paid", key="p1", channels=self.channels)
            b = notify.create(self.conn, "payment_update", "Paid", key="p1", channels=self.channels)
            c = notify.create(self.conn, "application_response", "Answer", key="7", state="interview", channels=self.channels)
            d = notify.create(self.conn, "application_response", "Answer", key="7", state="accepted", channels=self.channels)
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        self.assertIsNotNone(c)
        self.assertIsNotNone(d)                      # a different state is a genuinely new event

    def test_unknown_type_or_severity_is_rejected(self):
        with self.assertRaises(ValueError):
            notify.create(self.conn, "spam", "x", channels={})
        with self.assertRaises(ValueError):
            notify.create(self.conn, "payment_update", "x", severity="loud", channels={})


class TriggerTests(NotifyBase):
    def test_only_high_scoring_kenya_or_worldwide_opportunities_notify(self):
        self.ingest(opp(1), opp(2, kenya_eligibility="worldwide_eligible"),
                    opp(3, kenya_eligibility="kenya_eligibility_unspecified"),
                    opp(4, kenya_eligibility="restricted_other_countries"),
                    opp(5, title="Truck driver", summary="Drive", skills=[], category="logistics", payment_usd_per_hour=None))
        titles = [n["title"] for n in self.generate()]
        self.assertEqual(len(titles), 2)
        self.assertTrue(any("Kenya confirmed" in t for t in titles))
        self.assertTrue(any("Worldwide" in t for t in titles))

    def test_first_run_creates_a_few_individual_alerts_plus_one_digest(self):
        self.ingest(*[opp(i) for i in range(1, 31)])
        made = self.generate(settings={**notify.DEFAULT_SETTINGS, "max_individual_new": 5})
        titles = [n["title"] for n in made]
        self.assertEqual(sum(t.startswith("New Kenya confirmed") for t in titles), 5)
        self.assertEqual(sum("more high-scoring opportunities" in t for t in titles), 1)
        self.assertTrue(any(t.startswith("25 more") for t in titles))
        self.assertEqual(self.generate(settings={**notify.DEFAULT_SETTINGS, "max_individual_new": 5}), [])    # nothing re-alerts
        self.ingest(opp(99))                                                                                    # a genuinely new one still alerts
        self.assertEqual(len(self.generate(settings={**notify.DEFAULT_SETTINGS, "max_individual_new": 5})), 1)

    def test_expired_and_duplicate_opportunities_do_not_notify(self):
        self.ingest(opp(1, status="expired"))
        self.ingest(opp(2))
        self.ingest(opp(2, url="https://www.opentrain.ai/jobs/j2-copy"))
        self.assertEqual(len(self.generate()), 1)

    def test_expiring_soon_alert(self):
        self.ingest(opp(1, deadline=(NOW + timedelta(days=2)).isoformat(), kenya_eligibility="kenya_eligibility_unspecified"))
        types = [r["type"] for r in self.conn.execute("SELECT type FROM notifications")]
        self.generate()
        types = {r["type"] for r in self.conn.execute("SELECT type FROM notifications")}
        self.assertIn("expiring_opportunity", types)

    def test_application_response_and_task_and_payment_alerts(self):
        self.ingest(opp(1))
        app = wf.create_application(self.conn, "https://www.opentrain.ai/jobs/j1")
        with db.tx(self.conn):
            pa.ensure_defaults(self.conn)
        for key, value in {"full_name": "Test User", "contact_email": "test.user@example.com",
                           "availability": "20 hours per week from next Monday",
                           "experience_summary": "Built a Python scraper for my own project.",
                           "portfolio_links": "https://github.com/example/real-project"}.items():
            pa.set_answer(self.conn, key, value)
        wf.approve_draft(self.conn, app["drafts"][0]["id"], True)
        wf.mark_submitted(self.conn, app["id"], confirmed_by_user=True)
        wf.record_response(self.conn, app["id"], "interview", "Please book a slot")
        wf.create_task(self.conn, "Due task", "Read it.", due_at=(NOW + timedelta(hours=5)).isoformat())
        wf.record_payment(self.conn, 25, "USD", "2026-11-01", platform_id="opentrain_ai")
        types = {n["type"] for n in self.generate()}
        self.assertTrue({"application_response", "task_attention", "payment_update"} <= types)

    def test_sync_failure_alert_needs_three_consecutive_failures(self):
        with db.tx(self.conn):
            db.set_state(self.conn, "sync_health", {"consecutive_failures": 2, "last_error": "timed out"})
        self.assertEqual([n["type"] for n in self.generate()], [])
        with db.tx(self.conn):
            db.set_state(self.conn, "sync_health", {"consecutive_failures": 3, "last_error": "timed out",
                                                    "failure_streak_started": "2026-11-09T10:00"})
        self.assertEqual([n["type"] for n in self.generate()], ["sync_failed"])
        self.assertEqual(self.generate(), [])                      # same streak: no repeat

    def test_policy_change_alert_is_urgent(self):
        with db.tx(self.conn):
            db.set_state(self.conn, "eligibility_monitors", {"mindrift": {"needs_manual_review": True, "alert": "KENYA NO LONGER LISTED"}})
        made = self.generate()
        self.assertEqual(made[0]["type"], "policy_change")
        row = self.conn.execute("SELECT severity FROM notifications").fetchone()
        self.assertEqual(row["severity"], "urgent")


class ChannelTests(NotifyBase):
    def test_dashboard_always_external_only_for_important(self):
        with db.tx(self.conn):
            low = notify.create(self.conn, "payment_update", "Info only", severity="info", key="a", channels=self.channels)
            high = notify.create(self.conn, "policy_change", "Important", severity="urgent", key="b", channels=self.channels)
        self.assertEqual(low["delivered"], ["dashboard"])
        self.assertEqual(high["delivered"], ["dashboard", "desktop", "email", "telegram"])

    def test_desktop_can_be_disabled_and_failed_channels_are_not_reported_delivered(self):
        self.email.result = False
        settings = {**notify.DEFAULT_SETTINGS, "desktop_notifications": False}
        with db.tx(self.conn):
            res = notify.create(self.conn, "policy_change", "x", severity="urgent", key="b", settings=settings, channels=self.channels)
        self.assertEqual(res["delivered"], ["dashboard", "telegram"])
        self.assertEqual(self.desktop.calls, [])

    def test_flood_protection_pushes_only_a_few_and_one_summary(self):
        self.ingest(*[opp(i) for i in range(1, 13)])
        made = self.generate(settings={**notify.DEFAULT_SETTINGS, "max_external_per_cycle": 3, "max_individual_new": 50})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 13)     # 12 + 1 summary
        self.assertEqual(len(self.desktop.calls), 4)                                                   # 3 pushes + the summary
        self.assertTrue(any("more notifications" in n["title"] for n in made))

    def test_email_and_telegram_are_off_without_environment_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(notify.email_configured())
            self.assertFalse(notify.telegram_configured())
            with mock.patch("smtplib.SMTP") as smtp, mock.patch("urllib.request.urlopen") as url:
                self.assertFalse(notify.email_notify("t", "b"))
                self.assertFalse(notify.telegram_notify("t", "b"))
                smtp.assert_not_called()
                url.assert_not_called()

    def test_email_uses_environment_credentials_and_never_stores_them(self):
        env = {"AUTOPILOT_SMTP_HOST": "smtp.example.com", "AUTOPILOT_SMTP_USER": "me@example.com",
               "AUTOPILOT_SMTP_PASSWORD": "fixture-" + "pw" * 4, "AUTOPILOT_SMTP_TO": "me@example.com"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("smtplib.SMTP") as smtp:
            self.assertTrue(notify.email_notify("Hello", "Body"))
            server = smtp.return_value
            server.starttls.assert_called_once()
            server.login.assert_called_once_with("me@example.com", "fixture-pwpwpwpw")
            server.send_message.assert_called_once()
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertNotIn("fixture-pwpwpwpw", notify.scrub("login failed for fixture-pwpwpwpw"))

    def test_telegram_token_is_scrubbed_and_not_stored_in_notifications(self):
        token = "123456789" + ":" + "Q" * 35          # built at runtime: no token-shaped literal in the source
        with mock.patch.dict(os.environ, {"AUTOPILOT_TELEGRAM_BOT_TOKEN": token, "AUTOPILOT_TELEGRAM_CHAT_ID": "42"}, clear=True):
            with db.tx(self.conn):
                notify.create(self.conn, "sync_failed", "Failure", f"POST https://api.telegram.org/bot{token}/sendMessage failed",
                              severity="info", key="t", channels={})
            stored = self.conn.execute("SELECT title, body FROM notifications").fetchone()
            self.assertNotIn(token, stored["body"])
            self.assertIn("bot<redacted>", stored["body"])

    def test_desktop_notify_passes_text_through_environment_not_the_command_line(self):
        with mock.patch("sys.platform", "win32"), mock.patch("subprocess.Popen") as popen:
            self.assertTrue(notify.desktop_notify("Title '; calc; '", "Body"))
            args, kwargs = popen.call_args
            self.assertNotIn("calc", " ".join(args[0]))
            self.assertEqual(kwargs["env"]["AP_TITLE"], "Title '; calc; '")
        with mock.patch("sys.platform", "linux"):
            self.assertFalse(notify.desktop_notify("t", "b"))

    def test_read_and_list(self):
        self.ingest(opp(1), opp(2))
        self.generate()
        self.assertEqual(len(notify.list_notifications(self.conn, unread_only=True)), 2)
        first = notify.list_notifications(self.conn)[0]["id"]
        with db.tx(self.conn):
            self.assertEqual(notify.mark_read(self.conn, [first]), 1)
        self.assertEqual(len(notify.list_notifications(self.conn, unread_only=True)), 1)
        with db.tx(self.conn):
            self.assertEqual(notify.mark_read(self.conn), 1)

    def test_settings_load_defaults_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            self.assertEqual(notify.load_settings(path)["min_score"], 60)
            path.write_text('{"min_score": 80, "desktop_notifications": false}')
            s = notify.load_settings(path)
            self.assertEqual((s["min_score"], s["desktop_notifications"], s["expiring_days"]), (80, False, 3))
            path.write_text("{broken")
            self.assertEqual(notify.load_settings(path)["min_score"], 60)


if __name__ == "__main__":
    unittest.main()
