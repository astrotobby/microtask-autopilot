import json
import re
import unittest
from pathlib import Path
from unittest import mock

from microtask_monitor import scan_feeds

ROOT = Path(__file__).resolve().parents[1]
MON = ROOT / "microtask_monitor"
PLATFORMS = json.loads((MON / "platforms.json").read_text(encoding="utf-8"))["platforms"]
INTEGRATIONS = json.loads((MON / "integrations.json").read_text(encoding="utf-8"))["platforms"]
BY_ID = {p["id"]: p for p in PLATFORMS}
DASH = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")

MICROTASK = {"atlas_capture", "clickworker", "microworkers", "rapidworkers", "sproutgigs", "gainworkers"}
AI_TRAINING = {"afterquery", "handshake_ai", "opentrain_ai", "mindrift"}
PROFESSIONAL = {"braintrust", "reworkio"}


class ClassificationTests(unittest.TestCase):
    def test_all_twelve_platforms_are_classified_into_three_groups(self):
        self.assertEqual(len(PLATFORMS), 12)
        for ids, kind in ((MICROTASK, "microtask"), (AI_TRAINING, "ai_training"),
                          (PROFESSIONAL, "professional_marketplace")):
            for pid in ids:
                self.assertEqual(BY_ID[pid]["platform_type"], kind, pid)
        self.assertEqual(len(MICROTASK | AI_TRAINING | PROFESSIONAL), 12)

    def test_reworkio_is_a_professional_marketplace_not_a_microtask_site(self):
        self.assertEqual(BY_ID["reworkio"]["platform_type"], "professional_marketplace")

    def test_every_platform_has_integration_mode_and_blocked_actions(self):
        self.assertEqual(set(INTEGRATIONS), set(BY_ID))
        for pid, mode in INTEGRATIONS.items():
            for key in ("public_discovery", "authenticated_manual_review", "automation_permitted", "health"):
                self.assertIn(key, mode, pid)
            self.assertTrue(BY_ID[pid]["blocked_actions"], pid)

    def test_only_documented_read_only_automation_is_permitted(self):
        automated = {pid for pid, m in INTEGRATIONS.items() if m["automation_permitted"] != "none"}
        self.assertEqual(automated, {"opentrain_ai", "mindrift"})
        self.assertEqual(INTEGRATIONS["opentrain_ai"]["automation_permitted"], "read_only_public_job_listing")
        self.assertEqual(INTEGRATIONS["mindrift"]["automation_permitted"], "read_only_public_policy_monitor")

    def test_manual_platforms_are_manual(self):
        for pid in ("gainworkers", "reworkio"):
            self.assertEqual(INTEGRATIONS[pid]["health"], "manual_review_required")
            self.assertIn("none", INTEGRATIONS[pid]["automation_permitted"])

    def test_no_new_platform_allows_automated_submission_or_acceptance(self):
        risky = ("automated_application_submission", "automated_task_acceptance", "automated_applications")
        for pid in ("opentrain_ai", "gainworkers", "mindrift", "reworkio"):
            for action in BY_ID[pid]["allowed_actions"]:
                self.assertNotIn(action, risky)
                self.assertNotIn("submit", action)


class EligibilityCountingTests(unittest.TestCase):
    def test_unverified_platforms_are_not_counted_as_kenya_eligible(self):
        eligibility = {"mindrift": {"eligibility_status": "restricted_for_user_location"}}
        active = scan_feeds.active_kenya_eligible_sources(PLATFORMS, eligibility)
        self.assertNotIn("mindrift", active)
        self.assertEqual(active, [])
        # Absence from a restriction feed is not positive country evidence.
        self.assertNotIn("mindrift", scan_feeds.active_kenya_eligible_sources(PLATFORMS, {}))

    def test_only_explicitly_confirmed_country_classes_count(self):
        platforms = [
            {"id": "kenya", "kenya_eligibility_class": "KENYA_CONFIRMED"},
            {"id": "world", "kenya_eligibility_class": "WORLDWIDE_CONFIRMED"},
            {"id": "unknown", "kenya_eligibility_class": "KENYA_UNVERIFIED"},
            {"id": "manual", "kenya_eligibility_class": "MANUAL_REVIEW_REQUIRED"},
            {"id": "unsupported", "kenya_eligibility_class": "KENYA_UNSUPPORTED"},
        ]
        self.assertEqual(scan_feeds.active_kenya_eligible_sources(platforms, {}), ["kenya", "world"])


class ReportIntegrationTests(unittest.TestCase):
    def fake_adapters(self):
        item = {"platform_id": "opentrain_ai", "source": "OpenTrain AI", "title": "Job <b>1</b>",
                "url": "https://www.opentrain.ai/jobs/1", "priority": "high_review", "relevance_score": 90,
                "kenya_eligibility": "kenya_explicitly_eligible", "payment_display": "USD 40/hour",
                "summary": "s", "category": "ai"}
        return (mock.patch("microtask_monitor.opentrain.scan", return_value={
                    "items": [item], "health": {"status": "ok", "last_successful_scan": "2026-11-08T10:00:00+00:00",
                                                "error": None}}),
                mock.patch("microtask_monitor.mindrift.scan", return_value={
                    "items": [], "eligibility": {"eligibility_status": "restricted_for_user_location",
                                                 "health": "ok", "checked_at": "2026-11-08T10:00:00+00:00",
                                                 "error": None, "alert": None}}))

    def test_run_integrations_reports_every_platform_independently(self):
        ot, mi = self.fake_adapters()
        with ot, mi:
            health, items, eligibility = scan_feeds.run_integrations(PLATFORMS)
        self.assertEqual(set(health), set(BY_ID))
        self.assertEqual(health["opentrain_ai"]["status"], "ok")
        self.assertEqual(health["mindrift"]["status"], "ok")
        self.assertEqual(health["gainworkers"]["status"], "manual_review_required")
        self.assertEqual(health["reworkio"]["status"], "manual_review_required")
        self.assertEqual(health["reworkio"]["platform_type"], "professional_marketplace")
        self.assertEqual(len(items), 1)
        self.assertEqual(eligibility["mindrift"]["eligibility_status"], "restricted_for_user_location")

    def test_one_failing_adapter_does_not_stop_the_others(self):
        _, mi = self.fake_adapters()
        with mock.patch("microtask_monitor.opentrain.scan", side_effect=RuntimeError("boom")), mi:
            health, items, _ = scan_feeds.run_integrations(PLATFORMS)
        self.assertEqual(health["opentrain_ai"]["status"], "error")
        self.assertIn("boom", health["opentrain_ai"]["error"])
        self.assertEqual(health["mindrift"]["status"], "ok")

    def test_html_report_shows_integration_health_and_escapes_listing_text(self):
        ot, mi = self.fake_adapters()
        with ot, mi:
            health, items, eligibility = scan_feeds.run_integrations(PLATFORMS)
        report = {"checked_at_utc": "20261108T100000Z", "feed_count": 4, "platforms": PLATFORMS,
                  "discovery_sources": {"sources": [], "current_reviewed_roles": []}, "items": items,
                  "integrations": health, "unique_listing_count": 1, "high_review_count": 1,
                  "medium_review_count": 0, "important_note": "Verify."}
        page = scan_feeds.render_html(report)
        self.assertIn("Integration health", page)
        self.assertIn("Twelve-platform", page)
        self.assertIn("REWORKIO", page)
        self.assertIn("Job &lt;b&gt;1&lt;/b&gt;", page)
        self.assertNotIn("<b>1</b>", page)
        self.assertIn("USD 40/hour", page)

    def test_every_platform_has_a_discovery_directory_entry(self):
        sources = json.loads((MON / "discovery_sources.json").read_text(encoding="utf-8"))["sources"]
        self.assertTrue(set(BY_ID).issubset({s["platform_id"] for s in sources}))


class DashboardDisplayTests(unittest.TestCase):
    def test_twelve_platforms_in_three_groups_with_independent_eligibility_and_health(self):
        self.assertIn("Platforms (12)", DASH)
        for pid in BY_ID:
            self.assertIn(f'id:"{pid}"', DASH)
        for title in ("Microtask platforms", "AI training and evaluation platforms",
                      "Professional freelance and expert marketplaces"):
            self.assertIn(title, DASH)
        self.assertIn("Eligibility: restricted for your location", DASH)
        self.assertIn("Integration: ", DASH)
        self.assertIn('id="healthTable"', DASH)

    def test_mindrift_is_marked_restricted_and_not_counted_active(self):
        match = re.search(r'\{id:"mindrift"[^\n]*', DASH)
        self.assertIsNotNone(match)
        self.assertIn("restricted:true", match.group(0))
        self.assertIn("Kenya is listed as unsupported", match.group(0))
        self.assertIn("PLATFORMS.filter(p=>!p.restricted).length", DASH)

    def test_dashboard_shows_required_opportunity_columns(self):
        for column in ("Platform", "Kenya eligibility", "Task / job", "Payment", "Discovered", "Status", "Link"):
            self.assertIn(f"<th>{column}</th>", DASH)
        for text in ("New (24h)", "Last successful scan", "Integration health", "Errors", "Verified earnings (received)", "Last local sync"):
            self.assertIn(text, DASH)

    def test_dashboard_has_platform_eligibility_category_and_payment_filters(self):
        for element in ("platformFilter", "kenyaFilter", "categoryFilter", "paymentFilter", "minRate", "skillFilter", "sortBy"):
            self.assertIn(f'id="{element}"', DASH)
        for value in ("KENYA_CONFIRMED", "WORLDWIDE_CONFIRMED", "KENYA_UNVERIFIED", "KENYA_UNSUPPORTED", "EXPIRED", "MANUAL_REVIEW_REQUIRED"):
            self.assertIn(value, DASH)

    def test_dashboard_escapes_content_and_has_no_external_scripts_or_secret_inputs(self):
        self.assertIn("function esc(", DASH)
        self.assertNotIn("<script src=", DASH.lower())
        self.assertNotIn('type="password"', DASH.lower())
        self.assertIn("rel=\"noopener noreferrer\"", DASH)
        self.assertIn("safeUrl(", DASH)

    def test_existing_dashboard_features_survive(self):
        for needle in ("exportBackup", "importBackup", "importReport", "localStorage", "taskForm", "earningForm", "paymentForm",
                       "refreshFromLocalAgent", "/api/overview", "/api/status", "setInterval(refreshFromLocalAgent"):
            self.assertIn(needle, DASH)


class DashboardBackendTests(unittest.TestCase):
    def test_all_management_sections_exist_and_use_the_backend(self):
        for section in ("overview", "opportunities", "applications", "tasks", "earnings", "platforms", "notifications"):
            self.assertIn(f'id="{section}"', DASH)
            self.assertIn(f'data-tab="{section}"', DASH)
        for endpoint in ("/api/applications", "/api/tasks", "/api/earnings", "/api/payments", "/api/notifications",
                         "/api/platforms", "/api/opportunities", "/api/import-local-storage", "/api/sync"):
            self.assertIn(endpoint, DASH)

    def test_business_records_are_not_stored_in_browser_storage(self):
        # localStorage is used only for UI conveniences and for reading old records to import them once.
        writes = [l for l in DASH.splitlines() if "localStorage.setItem" in l]
        self.assertEqual(len(writes), 1)
        self.assertIn("UI_KEY", writes[0])
        self.assertNotIn("state.applications", DASH)
        self.assertNotIn("state.earnings", DASH)
        self.assertIn("migrateLegacyBrowserRecords", DASH)
        self.assertIn("Nothing is deleted", DASH)

    def test_human_approval_controls_and_never_auto_submit_copy(self):
        for needle in ("approved_by_user:true", "confirmed_by_user:true", "I submitted it myself", "I submitted this myself",
                       "does NOT send anything", "you do those steps yourself"):
            self.assertIn(needle, DASH)
        self.assertNotIn("auto-submit", DASH.lower().replace("never auto-submit", ""))

    def test_earnings_ui_separates_expected_pending_and_received(self):
        for text in ("Expected", "Pending", "Received", "Record a payment I actually received", "never counted as received"):
            self.assertIn(text, DASH)
        self.assertNotIn('<option value="received">', DASH)       # advertised pay cannot be filed as received

    def test_mobile_responsive_rules_and_dialog_escaping(self):
        self.assertIn("@media(max-width:820px)", DASH)
        self.assertIn('name="viewport"', DASH)
        self.assertEqual(DASH.count("innerHTML=") > 10, True)
        self.assertIn("function esc(", DASH)


class RepositorySafetyTests(unittest.TestCase):
    def test_no_secret_like_strings_in_tracked_sources(self):
        patterns = [r"ghp_[A-Za-z0-9]{20,}", r"gw_live_[A-Za-z0-9]{8,}", r"ot_pat_[A-Za-z0-9]{8,}",
                    r"sk-[A-Za-z0-9]{20,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"(?i)password\s*=\s*['\"][^'\"]+['\"]"]
        files = [*MON.glob("*.py"), *MON.glob("*.json"), ROOT / "dashboard" / "index.html",
                 *(ROOT / ".github").rglob("*.yml")]
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in patterns:
                self.assertIsNone(re.search(pattern, text), f"{path.name} matches {pattern}")

    def test_manual_records_and_caches_are_gitignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/", ignore)
        self.assertIn("microtask_monitor/cache/", ignore)

    def test_workflow_publishes_nothing_secret_and_keeps_minimal_permissions(self):
        text = (ROOT / ".github" / "workflows" / "hourly-opportunity-monitor.yml").read_text(encoding="utf-8")
        self.assertNotIn("secrets.", text)
        self.assertIn("contents: read", text)


if __name__ == "__main__":
    unittest.main()
