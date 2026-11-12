import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from microtask_monitor import manual_review as mr
from microtask_monitor import opportunity_common as oc

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def gw(task_id="t1", **kw):
    row = {"id": task_id, "title": "Review an AI chatbot answer", "url": f"https://www.gainworkers.com/task/{task_id}",
           "payment": "$0.05", "region": "International", "remaining_slots": 40,
           "description": "Rate chatbot answers using Python-based tooling", "category": "evaluation"}
    row.update(kw)
    return row


def rw(brief_id="b1", **kw):
    row = {"id": brief_id, "title": "n8n workflow with LLM integration and webhooks",
           "url": f"https://reworkio.com/projects/{brief_id}", "description": "Build an n8n and Python automation",
           "skills": ["n8n", "Python", "LLM"], "budget": "$800", "location": "Worldwide", "status": "open",
           "discovered_at": "2026-11-07T10:00:00+00:00", "last_verified": "2026-11-08"}
    row.update(kw)
    return row


class GainWorkersTests(unittest.TestCase):
    def test_international_vs_country_restricted_vs_kenya_vs_blank(self):
        n = lambda **kw: mr.normalize_row("gainworkers", gw(**kw), NOW)["kenya_eligibility"]
        self.assertEqual(n(region="International"), oc.WORLDWIDE)
        self.assertEqual(n(region="Kenya, Nigeria"), oc.KENYA_EXPLICIT)
        self.assertEqual(n(region="US, Canada"), oc.RESTRICTED_OTHER)
        self.assertEqual(n(region=""), oc.UNSPECIFIED)           # blank is never assumed eligible
        self.assertEqual(n(region=None), oc.UNSPECIFIED)

    def test_missing_or_invalid_payment_is_not_disclosed(self):
        for value in (None, "", "free", "-3", "abc", float("nan"), True, "$-1"):
            item = mr.normalize_row("gainworkers", gw(payment=value), NOW)
            self.assertEqual(item["payment_display"], "Not disclosed", value)

    def test_payment_is_copied_never_estimated(self):
        item = mr.normalize_row("gainworkers", gw(payment="$0.05"), NOW)
        self.assertEqual(item["payment_display"], "USD 0.05")
        self.assertNotIn("estimated", json.dumps(item).lower())

    def test_unusually_high_microtask_pay_is_flagged(self):
        item = mr.normalize_row("gainworkers", gw(payment="$250"), NOW)
        self.assertEqual(item["payment_flag"], "suspicious_verify_manually")
        self.assertIn("suspicious_verify_manually", item["flags"])

    def test_filled_expired_and_unavailable_tasks_are_detected(self):
        for kw, expected in ((dict(remaining_slots=0), "filled"), (dict(status="Filled"), "filled"),
                             (dict(status="expired"), "expired"),
                             (dict(deadline=(NOW - timedelta(hours=1)).isoformat()), "expired"),
                             (dict(status="Paused"), "unavailable")):
            self.assertEqual(mr.normalize_row("gainworkers", gw(**kw), NOW)["status"], expected, kw)
        self.assertEqual(mr.normalize_row("gainworkers", gw(), NOW)["status"], "open")

    def test_ingest_drops_unavailable_dedupes_and_tracks_last_seen(self):
        rows = [gw("a"), gw("a", title="duplicate id"), gw("b", url=gw("a")["url"]), gw("c", remaining_slots=0),
                {"title": "no url"}, gw("d")]
        result = mr.ingest("gainworkers", rows, now=NOW)
        self.assertEqual(sorted(i["external_id"] for i in result["items"]), ["a", "d"])
        self.assertEqual(result["stats"]["duplicates"], 2)
        self.assertEqual(result["stats"]["invalid"], 1)
        self.assertEqual([u["status"] for u in result["unavailable"]], ["filled"])
        self.assertTrue(all(i["last_seen_at"] == NOW.isoformat() for i in result["items"]))

    def test_tasks_missing_from_a_new_review_are_marked_not_seen(self):
        first = mr.ingest("gainworkers", [gw("a"), gw("b")], now=NOW)["items"]
        second = mr.ingest("gainworkers", [gw("a")], previous=first, now=NOW + timedelta(days=1))
        self.assertEqual([i["external_id"] for i in second["items"]], ["a"])
        self.assertEqual(second["unavailable"][0]["status"], "not_seen_since_last_review")
        self.assertEqual(second["unavailable"][0]["last_seen_at"], NOW.isoformat())   # old timestamp preserved

    def test_kenya_explicit_and_worldwide_outrank_unknown(self):
        score = lambda region: mr.normalize_row("gainworkers", gw(region=region), NOW)["relevance_score"]
        self.assertGreater(score("Kenya"), score("International"))
        self.assertGreater(score("International"), score(""))
        self.assertGreater(score(""), score("US"))


class ReworkioTests(unittest.TestCase):
    def test_brief_fields_are_tracked(self):
        item = mr.normalize_row("reworkio", rw(), NOW)
        self.assertEqual(item["platform_id"], "reworkio")
        self.assertEqual(item["title"], rw()["title"])
        self.assertEqual(item["skills"], ["n8n", "Python", "LLM"])
        self.assertEqual(item["payment_display"], "USD 800")
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["apply_url"], rw()["url"])
        self.assertEqual(item["discovered_at"], "2026-11-07T10:00:00+00:00")
        self.assertEqual(item["last_verified"], "2026-11-08")
        self.assertEqual(item["entry_method"], "manual_review")

    def test_user_skills_are_matched(self):
        item = mr.normalize_row("reworkio", rw(), NOW)
        self.assertIn("ai_automation", item["matched_skill_groups"])
        self.assertIn("python", item["matched_skill_groups"])

    def test_upfront_costs_are_flagged_and_cap_priority(self):
        item = mr.normalize_row("reworkio", rw(description="Requires a paid membership and escrow funding upfront"), NOW)
        self.assertTrue(any(f.startswith("possible_upfront_cost:") for f in item["flags"]))
        self.assertNotEqual(item["priority"], "high_review")

    def test_duplicates_detected_by_id_and_url(self):
        result = mr.ingest("reworkio", [rw("1"), rw("1"), rw("2", url=rw("1")["url"]), rw("3")], now=NOW)
        self.assertEqual(sorted(i["external_id"] for i in result["items"]), ["1", "3"])

    def test_non_https_application_url_rejected(self):
        self.assertIsNone(mr.normalize_row("reworkio", rw(url="http://reworkio.com/x"), NOW))
        self.assertIsNone(mr.normalize_row("reworkio", rw(url="javascript:alert(1)"), NOW))

    def test_closed_brief_is_not_reported_open(self):
        result = mr.ingest("reworkio", [rw("1", status="Closed")], now=NOW)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["unavailable"][0]["status"], "closed")


class ManualFallbackTests(unittest.TestCase):
    def test_scan_without_records_reports_manual_review_and_unavailable_discovery(self):
        for pid in ("gainworkers", "reworkio", "usertesting", "paidviewpoint", "whop_workforce"):
            with tempfile.TemporaryDirectory() as tmp:
                result = mr.scan(pid, Path(tmp), NOW)
            self.assertEqual(result["items"], [])
            self.assertEqual(result["health"]["status"], "manual_review_required")
            self.assertEqual(result["health"]["automated_discovery"], "unavailable")
            self.assertTrue(result["health"]["portal_url"].startswith("https://"))

    def test_scan_never_touches_the_network(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")), \
             mock.patch("socket.socket", side_effect=AssertionError("socket used")):
            for pid in ("gainworkers", "reworkio", "usertesting", "paidviewpoint", "whop_workforce"):
                mr.scan(pid, Path(tmp), NOW)

    def test_scan_reads_user_records_and_reports_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "gainworkers_tasks.json").write_text(json.dumps({"records": [gw("a"), gw("b", remaining_slots=0)]}))
            result = mr.scan("gainworkers", Path(tmp), NOW)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["health"]["manual_records"], 2)
        self.assertEqual(result["health"]["unavailable"], 1)

    def test_corrupt_manual_file_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "whop_workforce_bounties.json").write_text("{not json")
            result = mr.scan("whop_workforce", Path(tmp), NOW)
        self.assertEqual(result["items"], [])
        self.assertIn("Could not read", result["health"]["error"])

    def test_module_does_not_import_network_libraries(self):
        source = (Path(mr.__file__)).read_text(encoding="utf-8")
        for banned in ("import urllib", "import requests", "import socket", "import http", "playwright", "selenium"):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main()
