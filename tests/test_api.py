import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import db, intelligence as ig, local_server, policy

NOW = datetime.now(timezone.utc)
CAPS = policy.load_capabilities()
PROFILE = ig.load_profile()


def opp(n, **kw):
    base = {"platform_id": "opentrain_ai", "external_id": f"j{n}", "source": "OpenTrain AI",
            "url": f"https://www.opentrain.ai/jobs/j{n}", "title": f"Python Automation Engineer {n}",
            "summary": "Build n8n workflows and Python APIs with Claude Code.", "kenya_eligibility": "kenya_explicitly_eligible",
            "payment_display": f"USD {20 + n}/hour", "payment_usd_per_hour": 20 + n, "published": NOW.isoformat(),
            "status": "open", "category": "ai-engineering", "skills": ["Python"]}
    base.update(kw)
    return base


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "a.sqlite3"
        self.latest = Path(self.tmp.name) / "latest_report.json"
        with db.session(self.db) as conn:
            policy.sync_to_db(conn)
            with db.tx(conn):
                ig.ingest(conn, [opp(1), opp(2, kenya_eligibility="worldwide_eligible"),
                                 opp(3, kenya_eligibility="kenya_eligibility_unspecified", payment_display="Not disclosed", payment_usd_per_hour=None),
                                 opp(4, kenya_eligibility="restricted_other_countries"),
                                 opp(5, platform_id="mindrift", url="https://x.example/mindrift/5"),
                                 {"title": "Remote Python dev", "url": "https://feed.example/1", "source": "WWR",
                                  "kenya_eligibility": "remote_scope_unclear_kenya_not_confirmed", "category": "general_remote_job"}],
                          now=NOW, caps=CAPS, profile=PROFILE)
        self.evidence = Path(self.tmp.name) / "evidence"       # never the real data/ folder
        self.server = local_server.serve("127.0.0.1", 0, self.db, self.latest, self.evidence)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, headers=None, raw_body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        elif method in ("POST", "PUT") and raw_body is None:
            hdrs["Content-Type"] = "application/json"
            data = b"{}"
        hdrs.update(headers or {})
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(payload)
        except ValueError:
            return resp.status, payload

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None):
        return self.call("POST", path, body or {})


class SingleInstanceTests(ApiBase):
    def test_a_second_server_cannot_take_the_same_port(self):
        with self.assertRaises(OSError):
            local_server.serve("127.0.0.1", self.port, self.db, self.latest, self.evidence)


class ReadEndpointTests(ApiBase):
    def test_health_overview_and_legacy_status(self):
        s, h = self.get("/api/health")
        self.assertEqual((s, h["ok"], h["database_version"], h["capabilities"]), (200, True, db.LATEST_VERSION, 25))
        s, o = self.get("/api/overview")
        self.assertEqual(o["opportunities"]["kenya_confirmed"], 1)
        self.assertEqual(o["opportunities"]["total"], 6)
        self.assertGreaterEqual(o["opportunities"]["new_last_24h"], 6)
        self.assertEqual(o["applications"]["in_progress"], 0)
        self.assertEqual(o["earnings"]["by_currency"], {})
        s, legacy = self.get("/api/status")
        self.assertTrue(legacy["ok"] and legacy["database"])
        self.assertEqual(legacy["opportunity_count"], 6)

    def test_opportunities_filter_sort_and_paginate(self):
        s, r = self.get("/api/opportunities")
        self.assertEqual(r["total"], 6)
        scores = [i["relevance_score"] for i in r["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        s, r = self.get("/api/opportunities?eligibility=KENYA_CONFIRMED")
        self.assertEqual([i["eligibility_class"] for i in r["items"]], ["KENYA_CONFIRMED"])
        s, r = self.get("/api/opportunities?eligibility=KENYA_CONFIRMED,WORLDWIDE_CONFIRMED")
        self.assertEqual(r["total"], 2)
        s, r = self.get("/api/opportunities?platform=opentrain_ai&min_rate=22")
        self.assertTrue(all(i["payment_usd_per_hour"] >= 22 for i in r["items"]))
        s, r = self.get("/api/opportunities?platform=_feeds")
        self.assertEqual(r["total"], 1)
        s, r = self.get("/api/opportunities?q=remote&payment=undisclosed")
        self.assertEqual(r["total"], 1)
        s, r = self.get("/api/opportunities?limit=2&offset=1&sort=pay")
        self.assertEqual((len(r["items"]), r["limit"], r["offset"]), (2, 2, 1))
        s, r = self.get("/api/opportunities?skill=python")
        self.assertGreaterEqual(r["total"], 1)

    def test_invalid_filters_are_rejected_and_sql_injection_is_inert(self):
        for path in ("/api/opportunities?eligibility=NOPE", "/api/opportunities?sort=title;DROP%20TABLE%20opportunities",
                     "/api/opportunities?min_rate=abc"):
            self.assertEqual(self.get(path)[0], 400, path)
        s, r = self.get("/api/opportunities?q=%27%3B%20DROP%20TABLE%20opportunities%3B--")
        self.assertEqual(s, 200)
        self.assertEqual(self.get("/api/opportunities")[1]["total"], 6)

    def test_opportunity_detail_has_breakdown_requirements_and_reason(self):
        s, r = self.get("/api/opportunities/detail?url=https://www.opentrain.ai/jobs/j1")
        o = r["opportunity"]
        self.assertEqual(o["eligibility_class"], "KENYA_CONFIRMED")
        self.assertIn("eligibility", o["breakdown"])
        self.assertIn("min_years_experience", o["requirements"])
        self.assertTrue(o["eligibility_reason"])
        self.assertNotIn("raw_json", o)
        self.assertEqual(self.get("/api/opportunities/detail?url=https://nope.example/")[0], 404)

    def test_platform_monitor_lists_all_twenty_five_with_capabilities_and_mindrift_restriction(self):
        s, r = self.get("/api/platforms")
        by_id = {p["id"]: p for p in r["platforms"]}
        self.assertEqual(len(by_id), 25)
        self.assertEqual(by_id["mindrift"]["kenya_status"], "unsupported")
        self.assertIn("AUTOMATION_PROHIBITED", by_id["mindrift"]["levels"])
        self.assertFalse(by_id["atlas_capture"]["equipment"]["user_device_compatible"])
        self.assertEqual(by_id["opentrain_ai"]["opportunity_counts"]["kenya_confirmed"], 1)
        self.assertIn("DOCUMENTED_API_ALLOWED", by_id["opentrain_ai"]["levels"])
        self.assertTrue(by_id["gainworkers"]["restrictions"])

    def test_report_endpoint_serves_the_latest_synced_report(self):
        self.assertEqual(self.get("/api/report")[0], 404)
        self.latest.write_text(json.dumps({"report": {"items": []}, "sync": {}}))
        s, r = self.get("/api/report")
        self.assertEqual((s, r["ok"]), (200, True))

    def test_unknown_endpoint_is_404_and_delete_is_not_supported(self):
        self.assertEqual(self.get("/api/nope")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/applications/1")[0], 405)


class ApplicationFlowTests(ApiBase):
    URL = "https://www.opentrain.ai/jobs/j1"

    def test_full_application_lifecycle_over_http(self):
        s, r = self.post("/api/applications", {"opportunity_url": self.URL})
        self.assertEqual(s, 200)
        app = r["application"]
        self.assertEqual(app["status"], "draft")
        self.assertTrue(app["drafts"][0]["body"].startswith("Subject: Application for"))
        did = app["drafts"][0]["id"]
        s, r = self.call("PUT", f"/api/drafts/{did}", {"body": "Final text I wrote"})
        self.assertEqual(r["application"]["drafts"][0]["body"], "Final text I wrote")
        s, r = self.post(f"/api/applications/{app['id']}/approve", {"draft_id": did, "approved_by_user": True})
        self.assertEqual(r["application"]["status"], "approved")
        s, r = self.post(f"/api/applications/{app['id']}/submit", {"confirmed_by_user": True, "submitted_at": "2026-11-09"})
        self.assertEqual(r["application"]["status"], "submitted")
        s, r = self.post(f"/api/applications/{app['id']}/response", {"status": "interview", "text": "Book a slot"})
        self.assertEqual(r["application"]["status"], "interview")
        s, r = self.get("/api/applications")
        self.assertEqual(len(r["applications"]), 1)
        self.assertEqual(self.get("/api/overview")[1]["applications"]["in_progress"], 1)

    def test_human_approval_is_enforced_by_the_api(self):
        s, r = self.post("/api/applications", {"opportunity_url": self.URL})
        app = r["application"]
        s, r = self.post(f"/api/applications/{app['id']}/submit", {"confirmed_by_user": True})
        self.assertEqual(s, 400)
        self.assertIn("approve", r["error"].lower())
        s, r = self.post(f"/api/applications/{app['id']}/approve", {"draft_id": app["drafts"][0]["id"], "approved_by_user": "yes"})
        self.assertEqual(s, 400)

    def test_unsupported_platform_and_ineligible_applications_are_blocked(self):
        s, r = self.post("/api/applications", {"opportunity_url": "https://x.example/mindrift/5"})
        self.assertEqual((s, r["blocked"]), (409, True))
        s, r = self.post("/api/applications", {"opportunity_url": "https://www.opentrain.ai/jobs/j4"})
        self.assertEqual(s, 409)
        self.assertEqual(self.post("/api/applications", {"opportunity_url": "https://nowhere.example/"})[0], 400)

    def test_records_persist_across_a_server_restart(self):
        self.post("/api/applications", {"opportunity_url": self.URL})
        self.post("/api/earnings", {"amount": 50, "currency": "USD", "status": "expected"})
        self._stop()
        self.server = local_server.serve("127.0.0.1", 0, self.db, self.latest, self.evidence)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.assertEqual(len(self.get("/api/applications")[1]["applications"]), 1)
        self.assertEqual(self.get("/api/earnings")[1]["summary"]["by_currency"]["USD"]["expected"], 50.0)


class TaskAndEarningsFlowTests(ApiBase):
    def test_task_workspace_flow_with_evidence_and_confirmed_submission(self):
        s, r = self.post("/api/tasks", {"title": "Label images", "instructions": "1. Open the set.\n2. Label each image.\n3. Upload a screenshot.\nDo not guess.",
                                         "platform_id": "opentrain_ai", "due_at": "2026-12-01"})
        t = r["task"]
        self.assertEqual(t["status"], "accepted")
        self.assertGreaterEqual(len(t["checklist"]), 4)
        s, r = self.post(f"/api/tasks/{t['id']}/checklist/{t['checklist'][0]['id']}", {"done": True})
        self.assertEqual(r["task"]["open_items"], len(t["checklist"]) - 1)
        s, r = self.post(f"/api/tasks/{t['id']}/session/start")
        self.assertEqual(r["task"]["status"], "in_progress")
        s, r = self.post(f"/api/tasks/{t['id']}/session/stop", {"notes": "half done"})
        self.assertIsNotNone(r["task"]["sessions"][0]["ended_at"])
        content = base64.b64encode(b"fake image bytes").decode()
        s, r = self.post(f"/api/tasks/{t['id']}/evidence", {"kind": "screenshot", "description": "final", "file_name": "shot.png", "content_base64": content})
        self.assertEqual(r["task"]["evidence"][0]["size_bytes"], 16)
        self.assertEqual(self.post(f"/api/tasks/{t['id']}/evidence", {"description": "x", "content_base64": "!!notbase64!!"})[0], 400)
        self.assertTrue(any(self.evidence.rglob("*shot.png")))                   # stored under the temp folder, not data/
        self.assertEqual(self.post(f"/api/tasks/{t['id']}/submission", {"platform_reference": "R1"})[0], 400)   # needs confirmation
        s, r = self.post(f"/api/tasks/{t['id']}/submission", {"confirmed_by_user": True, "platform_reference": "R1"})
        self.assertEqual(r["task"]["status"], "submitted")
        s, r = self.get("/api/tasks")
        self.assertEqual(len(r["tasks"]), 1)
        s, r = self.get(f"/api/tasks/{t['id']}")
        self.assertEqual(r["task"]["submissions"][0]["method"], "manual")

    def test_earnings_separate_expected_pending_received(self):
        self.post("/api/earnings", {"amount": 100, "currency": "usd", "status": "expected", "platform_id": "opentrain_ai"})
        s, r = self.post("/api/earnings", {"amount": 60, "currency": "USD", "status": "pending"})
        s, bad = self.post("/api/earnings", {"amount": 10, "currency": "USD", "status": "received"})
        self.assertEqual(s, 400)
        s, r = self.post("/api/payments", {"amount": 25, "currency": "USD", "received_date": "2026-11-01", "platform_id": "opentrain_ai"})
        self.assertEqual(s, 200)
        summary = self.get("/api/earnings")[1]["summary"]["by_currency"]["USD"]
        self.assertEqual(summary, {"expected": 100.0, "pending": 60.0, "received": 25.0})
        self.assertEqual(self.get("/api/overview")[1]["earnings"]["by_currency"]["USD"]["received"], 25.0)
        # advertised opportunity pay never shows up as earnings
        self.assertNotIn("KES", self.get("/api/earnings")[1]["summary"]["by_currency"])

    def test_notifications_endpoints(self):
        with db.session(self.db) as conn:
            with db.tx(conn):
                from microtask_monitor import notify
                notify.generate(conn, now=NOW, channels={})
        s, r = self.get("/api/notifications?unread=1")
        self.assertGreaterEqual(len(r["notifications"]), 1)
        first = r["notifications"][0]["id"]
        s, r = self.post("/api/notifications/read", {"ids": [first]})
        self.assertEqual(r["marked"], 1)
        self.assertEqual(self.get("/api/overview")[1]["unread_notifications"], len(self.get("/api/notifications?unread=1")[1]["notifications"]))

    def test_assistant_helpers_are_stateless_and_local(self):
        s, r = self.post("/api/assist/simplify", {"text": "1. Read it.\n2. Write 50 words.\nDo not copy."})
        self.assertEqual(len(r["guidance"]["steps"]), 2)
        s, r = self.post("/api/assist/explain-error", {"message": "The read operation timed out"})
        self.assertIn("slow or interrupted", r["explanation"])
        self.assertEqual(self.post("/api/assist/unknown", {})[0], 404)

    def test_sync_endpoint_uses_the_trigger_and_reports_when_already_running(self):
        with mock.patch.object(local_server, "trigger_sync_async", return_value=True):
            self.server.RequestHandlerClass.api.trigger_sync = local_server.trigger_sync_async
            self.assertEqual(self.post("/api/sync")[1]["started"], True)
        with mock.patch.object(local_server, "trigger_sync_async", return_value=False):
            self.server.RequestHandlerClass.api.trigger_sync = local_server.trigger_sync_async
            self.assertEqual(self.post("/api/sync")[1]["started"], False)


class LocalStorageImportTests(ApiBase):
    OLD = {"applications": [{"id": "a1", "title": "Old gig", "platform": "clickworker", "status": "Applied", "date": "2026-10-01",
                             "url": "https://www.clickworker.com/x", "notes": "n"},
                            {"id": "a2", "title": "Saved one", "status": "Saved"},
                            {"id": "a3", "title": "", "status": "Saved"}],
           "earnings": [{"id": "e1", "platform": "microworkers", "amount": 12.5, "currency": "USD", "date": "2026-10-05", "notes": "ref"},
                        {"id": "e2", "amount": -3, "currency": "USD", "date": "2026-10-05"},
                        {"id": "e3", "amount": 5, "currency": "XX", "date": "2026-10-05"}]}

    def test_old_browser_records_are_imported_once_and_preserved(self):
        s, r = self.post("/api/import-local-storage", self.OLD)
        self.assertEqual((r["applications"], r["payments"], r["skipped"]), (2, 1, 3))
        apps = {a["title"]: a for a in self.get("/api/applications")[1]["applications"]}
        self.assertEqual(apps["Old gig"]["status"], "submitted")
        self.assertEqual(apps["Old gig"]["submitted_at"], "2026-10-01")
        self.assertEqual(apps["Saved one"]["status"], "draft")
        self.assertEqual(self.get("/api/earnings")[1]["summary"]["by_currency"]["USD"]["received"], 12.5)
        again = self.post("/api/import-local-storage", self.OLD)[1]
        self.assertEqual((again["applications"], again["payments"]), (0, 0))              # idempotent
        self.assertEqual(len(self.get("/api/applications")[1]["applications"]), 2)


class SecurityTests(ApiBase):
    def test_requests_with_a_foreign_host_header_are_rejected(self):
        for host in ("evil.example.com", "127.0.0.1.evil.com:80", f"localhost.evil.com:{self.port}"):
            s, r = self.call("GET", "/api/health", host=host)
            self.assertEqual(s, 403, host)
        self.assertEqual(self.call("GET", "/", host="evil.example.com")[0], 403)

    def test_cross_origin_and_non_json_posts_are_rejected(self):
        s, r = self.call("POST", "/api/earnings", {"amount": 1, "currency": "USD"}, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(s, 403)
        s, r = self.call("POST", "/api/earnings", raw_body=b"amount=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(s, 403)
        s, r = self.call("POST", "/api/earnings", {"amount": 1, "currency": "USD"}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(s, 200)
        self.assertEqual(self.get("/api/earnings")[1]["summary"]["by_currency"]["USD"]["expected"], 1.0)

    def test_malformed_bodies_are_rejected_cleanly(self):
        s, r = self.call("POST", "/api/earnings", raw_body=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(s, 400)
        s, r = self.call("POST", "/api/earnings", raw_body=b"[1,2]", headers={"Content-Type": "application/json"})
        self.assertEqual(s, 400)
        s, r = self.call("POST", "/api/earnings", raw_body=b"{}", headers={"Content-Type": "application/json", "Content-Length": str(40 * 1024 * 1024)})
        self.assertEqual(s, 413)

    def test_static_dashboard_is_served_and_post_outside_api_is_refused(self):
        s, page = self.get("/")
        self.assertEqual(s, 200)
        self.assertIn(b"Microtask Autopilot", page)
        self.assertEqual(self.call("POST", "/index.html", {})[0], 404)

    def test_server_binds_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(local_server.HOST, "127.0.0.1")


class BackendPolicyTests(unittest.TestCase):
    def test_api_never_exposes_a_platform_submission_endpoint(self):
        source = (Path(local_server.__file__).parent / "api.py").read_text(encoding="utf-8")
        for banned in ("urlopen", "requests.", "selenium", "playwright", "smtplib"):
            self.assertNotIn(banned, source)

    def test_sync_backoff_grows_and_resets(self):
        self.assertEqual(local_server.next_delay(0), 900)
        self.assertEqual([local_server.next_delay(n) for n in (1, 2, 3, 4)], [60, 120, 240, 480])
        self.assertEqual(local_server.next_delay(10), 900)                      # capped at the normal interval

    def test_request_allowed_logic(self):
        ok, _ = local_server.request_allowed("GET", {"Host": "127.0.0.1:8765"}, 8765)
        self.assertTrue(ok)
        self.assertFalse(local_server.request_allowed("GET", {"Host": "evil.com"}, 8765)[0])
        self.assertFalse(local_server.request_allowed("POST", {"Host": "127.0.0.1:8765"}, 8765)[0])   # no JSON content type
        self.assertTrue(local_server.request_allowed("POST", {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}, 8765)[0])


if __name__ == "__main__":
    unittest.main()
