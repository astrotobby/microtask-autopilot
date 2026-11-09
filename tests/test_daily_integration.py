import http.client
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import daily, db, intelligence as ig, local_server, policy, scan_feeds, sync_reports as sr

ROOT = Path(daily.__file__).resolve().parents[1]
CAPS = policy.load_capabilities()
PROFILE = ig.load_profile()
NOW = datetime.now(timezone.utc)


def opp(n=1, **kw):
    base = {"platform_id": "opentrain_ai", "external_id": f"j{n}", "source": "OpenTrain AI",
            "url": f"https://www.opentrain.ai/jobs/j{n}", "title": f"Python Automation Engineer {n}",
            "summary": "Build n8n workflows and Python APIs with Claude Code.", "kenya_eligibility": "kenya_explicitly_eligible",
            "payment_display": "USD 40/hour", "payment_usd_per_hour": 40, "published": (NOW - timedelta(days=1)).isoformat(),
            "status": "open", "category": "ai-engineering", "skills": ["Python"], "countries": ["KE"]}
    base.update(kw)
    return base


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "a.sqlite3"
        self.latest = Path(self.tmp.name) / "latest.json"
        with db.session(self.db) as conn:
            policy.sync_to_db(conn)
            with db.tx(conn):
                ig.ingest(conn, [opp(n) for n in range(1, 8)] + [opp(20, kenya_eligibility="restricted_other_countries")],
                          now=NOW, caps=CAPS, profile=PROFILE)
        self.server = local_server.serve("127.0.0.1", 0, self.db, self.latest, Path(self.tmp.name) / "evidence")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        data = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            data = json.dumps(body or {}).encode()
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, payload


class DailyApiTests(ApiBase):
    def test_before_any_analysis_the_api_says_so(self):
        s, r = self.call("GET", "/api/daily")
        self.assertEqual(s, 200)
        self.assertIsNone(r["analysis"])
        self.assertIn("No analysis has run yet", r["message"])
        self.assertEqual(r["priority_order"][0], "opentrain_ai")
        self.assertIn("08:00 East Africa Time", r["schedule"])

    def test_run_now_saves_a_shortlist_with_drafts_and_the_dashboard_api_reads_it_back(self):
        s, r = self.call("POST", "/api/daily/run")
        self.assertEqual((s, r["started"]), (200, True))
        self.assertEqual(r["stats"]["suitable"], 7)
        self.assertEqual(r["drafts"]["created"], 5)
        s, d = self.call("GET", "/api/daily")
        self.assertEqual(d["analysis"]["status"], "completed")
        self.assertEqual(len(d["top5"]), 5)
        self.assertEqual(len(d["shortlist"]), 7)
        self.assertEqual(d["top5"][0]["application"]["status"], "draft")
        self.assertTrue(d["top5"][0]["application"]["has_draft"])
        self.assertEqual(len(d["coverage"]), 12)
        self.assertEqual(d["coverage"][0]["platform_id"], "opentrain_ai")
        # the drafts are real records in the existing Application Manager
        s, apps = self.call("GET", "/api/applications")
        self.assertEqual(len(apps["applications"]), 5)
        self.assertEqual({a["status"] for a in apps["applications"]}, {"draft"})

    def test_running_again_immediately_is_refused_and_creates_no_duplicates(self):
        self.call("POST", "/api/daily/run")
        s, r = self.call("POST", "/api/daily/run")
        self.assertEqual((s, r["started"]), (200, False))
        self.assertIn("moments ago", r["skipped"])
        with closing(db.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 5)

    def test_review_status_is_saved_in_sqlite_and_shown_on_the_next_read(self):
        self.call("POST", "/api/daily/run")
        top = self.call("GET", "/api/daily")[1]["top5"][0]
        for status in ("reviewed", "shortlisted", "applied", "rejected", "accepted"):
            s, r = self.call("POST", "/api/daily/review", {"url": top["url"], "status": status, "notes": "n"})
            self.assertEqual((s, r["status"]), (200, status))
        self.assertEqual(self.call("GET", "/api/daily")[1]["top5"][0]["review_status"], "accepted")
        with closing(db.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM opportunity_review WHERE url=?", (top["url"],)).fetchone()[0], "accepted")
        self.assertEqual(self.call("POST", "/api/daily/review", {"url": top["url"], "status": "bogus"})[0], 400)
        self.assertEqual(self.call("POST", "/api/daily/review", {"url": "https://nope.example/", "status": "reviewed"})[0], 404)
        self.assertEqual(self.call("POST", "/api/daily/nope")[0], 404)

    def test_cloud_analysis_info_is_shown_when_a_synced_report_carried_one(self):
        with closing(db.connect(self.db)) as conn, db.tx(conn):
            db.set_state(conn, "cloud_daily", {"generated_at": NOW.isoformat(), "trigger": "daily", "suitable_count": 7})
        self.call("POST", "/api/daily/run")
        self.assertEqual(self.call("GET", "/api/daily")[1]["cloud"]["trigger"], "daily")

    def test_reading_the_shortlist_repeatedly_never_runs_an_analysis(self):
        for _ in range(4):
            self.call("GET", "/api/daily")
            self.call("GET", "/api/overview")
        with closing(db.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 0)

    def test_an_empty_database_gives_a_clear_no_suitable_result(self):
        with closing(db.connect(self.db)) as conn, db.tx(conn):
            conn.execute("DELETE FROM opportunities")
        s, r = self.call("POST", "/api/daily/run")
        self.assertEqual(r["stats"]["suitable"], 0)
        d = self.call("GET", "/api/daily")[1]
        self.assertIn("No suitable opportunities today", d["message"])
        self.assertEqual(d["top5"], [])


class SchedulerTests(unittest.TestCase):
    def test_scheduler_checks_repeatedly_and_survives_errors(self):
        calls = []
        stop = threading.Event()

        def fake_run_if_due(db_path, now=None, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            if len(calls) >= 3:
                stop.set()
            return {"analysis_id": len(calls)} if len(calls) == 2 else None

        with mock.patch.object(daily, "run_if_due", side_effect=fake_run_if_due):
            t = threading.Thread(target=local_server.daily_scheduler, args=(stop, 0.01, Path("x.sqlite3")), daemon=True)
            t.start()
            t.join(10)
        self.assertFalse(t.is_alive())
        self.assertGreaterEqual(len(calls), 3)                      # the error did not stop the loop

    def test_scheduler_only_reads_the_local_database(self):
        source = Path(local_server.__file__).read_text(encoding="utf-8")
        body = source[source.index("def daily_scheduler"):source.index("def allowed_hosts")]
        for banned in ("urlopen", "sync_once", "requests"):
            self.assertNotIn(banned, body)

    def test_scheduler_thread_is_started_by_main(self):
        source = Path(local_server.__file__).read_text(encoding="utf-8")
        self.assertIn('target=daily_scheduler, daemon=True, name="daily-analysis-scheduler"', source)


class CloudEmbedTests(unittest.TestCase):
    def test_hourly_scan_embeds_the_daily_shortlist_in_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports, state = Path(tmp) / "reports", Path(tmp) / "daily_state.json"
            health = {"opentrain_ai": {"status": "ok"}}
            with mock.patch.object(scan_feeds, "REPORTS", reports), mock.patch.object(scan_feeds, "DAILY_STATE", state), \
                 mock.patch.object(scan_feeds, "parse_feed", return_value=[]), \
                 mock.patch.object(scan_feeds, "run_integrations", return_value=(health, [opp(1, relevance_score=70, priority="high_review"), opp(2, relevance_score=65, priority="high_review")], {})), \
                 mock.patch.dict(os.environ, {"ANALYSIS_TRIGGER": "daily"}):
                scan_feeds.main()
            report = json.loads(next(reports.glob("hourly-*.json")).read_text(encoding="utf-8"))
        shortlist = report["daily_shortlist"]
        self.assertEqual(shortlist["trigger"], "daily")
        self.assertEqual(shortlist["suitable_count"], 2)
        self.assertEqual(len(shortlist["top5"]), 2)
        self.assertIsNone(report["daily_shortlist_error"])
        self.assertTrue(state.exists() is False or json.loads(state.read_text())["seen"])
        sr.validate_report(report)                                       # still a valid report for the sync

    def test_a_failing_analysis_never_breaks_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            with mock.patch.object(scan_feeds, "REPORTS", reports), mock.patch.object(scan_feeds, "DAILY_STATE", Path(tmp) / "s.json"), \
                 mock.patch.object(scan_feeds, "parse_feed", return_value=[]), \
                 mock.patch.object(scan_feeds, "run_integrations", return_value=({}, [opp(1, relevance_score=70, priority="high_review")], {})), \
                 mock.patch.object(daily, "cloud_analysis", side_effect=RuntimeError("boom")):
                scan_feeds.main()
            report = json.loads(next(reports.glob("hourly-*.json")).read_text(encoding="utf-8"))
        self.assertIsNone(report["daily_shortlist"])
        self.assertIn("boom", report["daily_shortlist_error"])
        self.assertEqual(len(report["items"]), 1)


class SyncHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db, self.latest = Path(self.tmp.name) / "a.sqlite3", Path(self.tmp.name) / "latest.json"
        for p in (mock.patch.object(sr, "DB_PATH", self.db), mock.patch.object(sr, "LATEST_REPORT", self.latest)):
            p.start()
            self.addCleanup(p.stop)

    def report(self, items, key="1"):
        cloud = {"generated_at": NOW.isoformat(), "analysis_date": "2026-11-09", "trigger": "hourly", "suitable_count": len(items),
                 "candidate_count": len(items), "message": None, "top5": [{"title": i["title"], "url": i["url"], "daily_score": 80} for i in items[:5]]}
        return {"checked_at_utc": f"2026110{key}T100000Z", "items": items, "daily_shortlist": cloud}, {"run_id": key, "artifact_id": key}

    def analyses(self):
        with closing(db.connect(self.db)) as conn:
            return [dict(r) for r in conn.execute("SELECT source_key,status,trigger FROM analysis_runs ORDER BY id")]

    def test_a_changed_report_triggers_one_analysis_and_stores_the_cloud_record(self):
        with mock.patch.object(sr, "get_latest_report", return_value=self.report([opp(1), opp(2)])):
            sr.sync_once()
        self.assertEqual(self.analyses(), [{"source_key": "report:1:1", "status": "completed", "trigger": "sync"}])
        with closing(db.connect(self.db)) as conn:
            cloud = db.get_state(conn, "cloud_daily")
            self.assertEqual(cloud["trigger"], "hourly")
            self.assertEqual(len(cloud["top5"]), 2)
            self.assertEqual(daily.load_shortlist(conn)["stats"]["suitable"], 2)

    def test_identical_data_in_a_new_report_does_not_repeat_the_analysis(self):
        with mock.patch.object(sr, "get_latest_report", return_value=self.report([opp(1)], "1")):
            sr.sync_once()
        with mock.patch.object(sr, "get_latest_report", return_value=self.report([opp(1)], "2")):
            sr.sync_once()
        self.assertEqual(len(self.analyses()), 1)
        with mock.patch.object(sr, "get_latest_report", return_value=self.report([opp(1), opp(2)], "3")):
            sr.sync_once()
        self.assertEqual(len(self.analyses()), 2)

    def test_the_same_report_imported_twice_is_analysed_once(self):
        data = self.report([opp(1)], "1")
        with mock.patch.object(sr, "get_latest_report", return_value=data):
            sr.sync_once()
            sr.sync_once()
        self.assertEqual(len(self.analyses()), 1)

    def test_an_analysis_failure_does_not_fail_or_roll_back_the_sync(self):
        with mock.patch.object(sr, "get_latest_report", return_value=self.report([opp(1)])), \
             mock.patch.object(daily, "run_after_sync", side_effect=RuntimeError("analysis broke")):
            result = sr.sync_once()
        self.assertEqual(result["inserted"], 1)
        with closing(db.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)
            self.assertEqual(db.get_state(conn, "sync_health")["consecutive_failures"], 0)

    def test_opting_out_with_the_environment_variable_skips_the_analysis(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_NO_DAILY": "1"}), \
             mock.patch.object(sr, "get_latest_report", return_value=self.report([opp(1)])):
            sr.sync_once()
        self.assertEqual(self.analyses(), [])

    def test_a_failed_download_never_triggers_an_analysis(self):
        with mock.patch.object(sr, "get_latest_report", side_effect=sr.SyncStageError("network", "timed out")), \
             self.assertRaises(sr.SyncStageError):
            sr.sync_once()
        self.assertEqual(self.analyses(), [])


class NoPopupTests(unittest.TestCase):
    def test_no_notify_environment_disables_every_external_channel(self):
        from microtask_monitor import notify
        sent = []
        fake = {"desktop": lambda t, b: sent.append(t) or True, "email": lambda t, b: sent.append(t) or True}
        with tempfile.TemporaryDirectory() as tmp, closing(db.connect(Path(tmp) / "a.sqlite3")) as conn, db.tx(conn):
            with mock.patch.dict(os.environ, {"AUTOPILOT_NO_NOTIFY": "1"}), mock.patch.object(notify, "CHANNELS", fake):
                res = notify.create(conn, "policy_change", "x", severity="urgent", key="k")          # default (real) channels
        self.assertEqual(res["delivered"], ["dashboard"])
        self.assertEqual(sent, [])


class WorkflowAndDashboardTests(unittest.TestCase):
    WORKFLOW = (ROOT / ".github" / "workflows" / "hourly-opportunity-monitor.yml").read_text(encoding="utf-8")
    DASH = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")

    def test_workflow_has_the_hourly_and_the_0800_nairobi_schedules(self):
        self.assertIn('cron: "17 * * * *"', self.WORKFLOW)
        self.assertIn('cron: "0 5 * * *"', self.WORKFLOW)
        self.assertIn("08:00 East Africa Time", self.WORKFLOW)
        self.assertIn("ANALYSIS_TRIGGER", self.WORKFLOW)
        self.assertIn("GitHub may delay", self.WORKFLOW)
        self.assertIn("concurrency:", self.WORKFLOW)                    # the two schedules can never overlap

    def test_workflow_stays_free_and_secret_free(self):
        self.assertNotIn("secrets.", self.WORKFLOW)
        self.assertIn("contents: read", self.WORKFLOW)

    def test_dashboard_has_the_daily_earning_opportunities_section(self):
        for needle in ('data-tab="daily"', 'id="daily"', "DAILY EARNING OPPORTUNITIES", "Today's top five",
                       "Newly discovered since the last analysis", "Full shortlist", "Kenya-confirmed", "Worldwide-confirmed",
                       "Last analysis:", "last cloud analysis", "Run analysis now", "Open draft application",
                       "Open the original opportunity", "Eligibility evidence", "Payment method", "Assessments / interviews",
                       "/api/daily", "/api/daily/review", "/api/daily/run", "Research priority and coverage"):
            self.assertIn(needle, self.DASH, needle)
        for status in ("new", "reviewed", "shortlisted", "applied", "rejected", "accepted"):
            self.assertIn(f'"{status}"', self.DASH)

    def test_daily_records_are_not_kept_in_browser_storage(self):
        writes = [l for l in self.DASH.splitlines() if "localStorage.setItem" in l]
        self.assertEqual(len(writes), 1)
        self.assertNotIn("localStorage.setItem(\"daily", self.DASH)

    def test_dashboard_escapes_listing_content_in_the_daily_section(self):
        block = self.DASH[self.DASH.index("function dailyCard"):self.DASH.index("function bindDaily")]
        self.assertIn("esc(e.title)", block)
        self.assertIn("esc(p.evidence", block)
        self.assertIn("link(e.apply_url", block)                        # https-only link helper


if __name__ == "__main__":
    unittest.main()
