import base64
import json
import os
import time
import unittest

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from tests.test_api import ApiBase
from tests.test_execution import DATA_INSTR, SAMPLE_CSV, SPEC, CODE_INSTR


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class ExecutionApiTests(ApiBase):
    def create_data_job(self):
        s, r = self.post("/api/executions", {"title": "Clean customer CSV", "instructions": DATA_INSTR, "platform_id": "opentrain_ai",
                                             "inputs": [{"name": "customers.csv", "content_base64": b64(SAMPLE_CSV)},
                                                        {"name": "spec.json", "content_base64": b64(json.dumps(SPEC).encode())}]})
        self.assertEqual(s, 200, r)
        return r["job"]

    def wait(self, jid, until=("awaiting_review", "qa_failed", "failed"), timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            j = self.get(f"/api/executions/{jid}")[1]["job"]
            if j["status"] in until:
                return j
            time.sleep(0.2)
        self.fail(f"job {jid} stuck in {j['status']}")

    def test_full_data_job_over_http_with_files_qa_and_gates(self):
        job = self.create_data_job()
        self.assertEqual(job["status"], "ready")
        s, r = self.post(f"/api/executions/{job['id']}/start", {})
        self.assertEqual(s, 200, r)
        j = self.wait(job["id"])
        self.assertEqual(j["status"], "awaiting_review", j["status_reason"])
        out = next(f for f in j["files"] if f["rel_path"] == "output/customers_clean.csv")
        s, f = self.get(f"/api/executions/{job['id']}/files/{out['id']}")
        self.assertIn("dave@example.com", f["text"])
        self.assertEqual(self.post(f"/api/executions/{job['id']}/approve", {"approved_by_user": False})[0], 400)
        s, r = self.post(f"/api/executions/{job['id']}/approve", {"approved_by_user": True})
        self.assertEqual((s, r["job"]["status"]), (200, "ready_for_submission"))
        self.assertEqual(self.post(f"/api/executions/{job['id']}/submitted", {})[0], 400)
        s, r = self.post(f"/api/executions/{job['id']}/submitted", {"confirmed_by_user": True, "reference": "R1", "submitted_at": "2026-11-09"})
        self.assertEqual(r["job"]["status"], "submitted")
        s, r = self.post(f"/api/executions/{job['id']}/outcome", {"outcome": "rejected"})
        self.assertEqual(r["job"]["status"], "rejected")
        s, listing = self.get("/api/executions")
        self.assertEqual(listing["summary"]["by_status"], {"rejected": 1})
        self.assertIn("opentrain_ai", listing["platform_rules"])
        self.assertIn("claude", listing["ai_tools"])

    def test_ai_job_needs_permission_and_explicit_run_approval_over_http(self):
        s, r = self.post("/api/executions", {"title": "Slugify", "instructions": CODE_INSTR, "platform_id": "opentrain_ai"})
        jid = r["job"]["id"]
        self.assertEqual(r["job"]["status"], "blocked")
        self.assertEqual(self.post(f"/api/executions/{jid}/start", {"approve_ai_run": True})[0], 400)
        self.assertEqual(self.post(f"/api/executions/{jid}/ai-permission", {"status": "allowed"})[0], 400)
        s, r = self.post(f"/api/executions/{jid}/ai-permission", {"status": "allowed", "source": "Brief: AI tools allowed"})
        self.assertEqual(r["job"]["status"], "ready")
        s, r = self.post(f"/api/executions/{jid}/start", {})
        self.assertEqual(s, 400)
        self.assertIn("approve_ai_run", r["error"])
        s, r = self.post(f"/api/executions/{jid}/cancel", {})
        self.assertEqual(r["job"]["status"], "cancelled")

    def test_bad_requests_are_rejected(self):
        self.assertEqual(self.post("/api/executions", {"title": "x", "instructions": "too short"})[0], 400)
        self.assertEqual(self.post("/api/executions", {"title": "x", "instructions": DATA_INSTR, "inputs": [{"name": "a", "content_base64": "%%%"}]})[0], 400)
        self.assertEqual(self.get("/api/executions/999")[0], 400)
        self.assertEqual(self.post("/api/executions/1/bogus", {})[0], 404)
        s, r = self.call("POST", "/api/executions", raw_body=b"x=1", headers={"Content-Type": "text/plain"})
        self.assertEqual(s, 403)


if __name__ == "__main__":
    unittest.main()
