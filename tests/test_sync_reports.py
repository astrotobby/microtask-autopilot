import json
import tempfile
import unittest
from pathlib import Path

from microtask_monitor.sync_reports import import_report, validate_report
from microtask_monitor.local_server import HOST, PORT


class SyncReportTests(unittest.TestCase):
    def sample(self):
        return {
            "checked_at_utc": "20261108T120000Z",
            "items": [{
                "title": "AI evaluator",
                "url": "https://example.com/jobs/1",
                "source": "Example feed",
                "summary": "Remote role",
                "priority": "high_review",
                "relevance_score": 42,
                "category": "ai_related_role",
                "kenya_eligibility": "remote_scope_unclear_kenya_not_confirmed",
                "published": "2026-11-08"
            }]
        }

    def metadata(self, run_id="101", artifact_id="201"):
        return {"run_id": run_id, "artifact_id": artifact_id,
                "run_url": "https://github.com/example/run/101"}

    def test_import_creates_sqlite_database_and_latest_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = import_report(self.sample(), self.metadata(),
                                   root / "agent.sqlite3", root / "latest.json")
            self.assertEqual(result["inserted"], 1)
            self.assertTrue((root / "agent.sqlite3").exists())
            saved = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["report"]["items"][0]["title"], "AI evaluator")

    def test_same_artifact_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, latest = root / "agent.sqlite3", root / "latest.json"
            first = import_report(self.sample(), self.metadata(), db, latest)
            second = import_report(self.sample(), self.metadata(), db, latest)
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["already_imported"], 1)
            self.assertEqual(second["inserted"], 0)

    def test_new_report_updates_existing_url_without_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, latest = root / "agent.sqlite3", root / "latest.json"
            first_report = self.sample()
            import_report(first_report, self.metadata(), db, latest)
            second_report = self.sample()
            second_report["checked_at_utc"] = "20261108T130000Z"
            second_report["items"][0]["title"] = "Updated AI evaluator"
            result = import_report(second_report, self.metadata("102", "202"), db, latest)
            self.assertEqual(result["inserted"], 0)
            self.assertEqual(result["updated"], 1)


    def test_local_server_is_loopback_only(self):
        self.assertEqual(HOST, "127.0.0.1")
        self.assertEqual(PORT, 8765)

    def test_rejects_non_https_listing_url(self):
        report = self.sample()
        report["items"][0]["url"] = "http://example.com/job"
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_rejects_non_report_payload(self):
        with self.assertRaises(ValueError):
            validate_report({"hello": "world"})


if __name__ == "__main__":
    unittest.main()
