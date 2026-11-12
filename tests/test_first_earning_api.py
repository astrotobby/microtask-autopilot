import os
import unittest

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from tests.test_api import ApiBase
from microtask_monitor import db


class FirstEarningApiTests(ApiBase):
    def application(self):
        s, r = self.post("/api/applications", {"opportunity_url": "https://www.opentrain.ai/jobs/j1"})
        self.assertEqual(s, 200, r)
        return r["application"]

    def test_profile_answers_roundtrip_and_reject_secrets(self):
        s, r = self.get("/api/profile-answers")
        self.assertIn("full_name", r["missing_required"])
        s, r = self.call("PUT", "/api/profile-answers/full_name", {"answer": "Test User"})
        self.assertEqual((s, r["answer"]["answer"]), (200, "Test User"))
        s, r = self.call("PUT", "/api/profile-answers/experience_summary", {"answer": "my password is abc"})
        self.assertEqual(s, 400)
        s, r = self.call("PUT", "/api/profile-answers/bogus", {"answer": "x"})
        self.assertEqual(s, 400)
        self.assertNotIn("full_name", self.get("/api/profile-answers")[1]["missing_required"])

    def test_packet_endpoint_builds_saves_and_exports_without_submitting(self):
        app = self.application()
        s, r = self.get(f"/api/applications/{app['id']}/packet")
        self.assertEqual((s, r["saved"], r["packet"]["ready_to_apply"]), (200, False, False))
        s, r = self.post(f"/api/applications/{app['id']}/packet")
        self.assertEqual((s, r["packet"]["version"], r["saved"]), (200, 1, True))
        self.assertIn("APPLICATION PACKET", r["packet"]["text"])
        s, r = self.get(f"/api/applications/{app['id']}/packet")
        self.assertTrue(r["saved"])
        self.assertEqual(self.get(f"/api/applications/{app['id']}")[1]["application"]["status"], "draft")
        self.assertEqual(self.get("/api/applications/999/packet")[0], 404)

    def test_followup_events_over_http(self):
        app = self.application()
        s, r = self.post(f"/api/applications/{app['id']}/events", {"kind": "note", "detail": "x"})
        self.assertEqual(s, 200)                                    # notes are allowed any time
        did = r["application"]["drafts"][0]["id"]
        self.post(f"/api/applications/{app['id']}/approve", {"draft_id": did, "approved_by_user": True})
        for key, value in {"full_name": "Test User", "contact_email": "test.user@example.com",
                           "availability": "20 hours per week from next Monday",
                           "experience_summary": "Built a Python scraper for my own project.",
                           "portfolio_links": "https://github.com/example/real-project"}.items():
            self.call("PUT", f"/api/profile-answers/{key}", {"answer": value})
        s, r = self.post(f"/api/applications/{app['id']}/submit", {"confirmed_by_user": True, "submitted_at": "2026-11-09"})
        self.assertEqual((s, r["application"]["next_followup_at"]), (200, "2026-11-16"))
        s, r = self.post(f"/api/applications/{app['id']}/events", {"kind": "decision", "decision": "accepted", "event_date": "2026-11-12"})
        self.assertEqual((s, r["application"]["status"]), (200, "accepted"))
        s, r = self.post(f"/api/applications/{app['id']}/events", {"kind": "bogus"})
        self.assertEqual(s, 400)

    def test_actions_and_reminders_endpoints(self):
        s, r = self.get("/api/actions")
        self.assertEqual(s, 200)
        keys = [i["key"] for i in r["items"]]
        self.assertEqual(keys[0], "profile")
        self.assertGreater(r["summary"]["open"], 0)
        item = r["items"][0]
        s, r2 = self.post(f"/api/actions/{item['id']}/status", {"status": "skipped"})
        self.assertEqual((s, r2["action"]["status"]), (200, "skipped"))
        self.assertEqual(self.post(f"/api/actions/{item['id']}/status", {"status": "bogus"})[0], 400)
        self.assertEqual(self.post("/api/actions/9999/status", {"status": "done"})[0], 404)
        self.application()
        s, r = self.post("/api/reminders/generate")
        self.assertEqual(s, 200)
        s, r = self.get("/api/reminders")
        self.assertEqual(s, 200)
        self.assertEqual(self.post("/api/reminders/9999/status", {"status": "done"})[0], 404)

    def test_guidance_endpoint_labels_verification(self):
        s, r = self.get("/api/guidance/opentrain_ai")
        self.assertTrue(r["guidance"]["verified"])
        s, r = self.get("/api/guidance/clickworker")
        self.assertFalse(r["guidance"]["verified"])
        self.assertIn("UNVERIFIED", r["guidance"]["label"])

    def test_new_post_endpoints_enforce_json_and_origin(self):
        app = self.application()
        s, r = self.call("POST", f"/api/applications/{app['id']}/packet", raw_body=b"x=1", headers={"Content-Type": "text/plain"})
        self.assertEqual(s, 403)
        s, r = self.call("POST", "/api/reminders/generate", headers={"Origin": "https://evil.example"})
        self.assertEqual(s, 403)


if __name__ == "__main__":
    unittest.main()
