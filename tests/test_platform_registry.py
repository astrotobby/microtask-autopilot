import json
import unittest

from microtask_monitor.scan_feeds import render_html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "microtask_monitor" / "platforms.json"

EXPECTED = {
    "atlas_capture", "clickworker", "microworkers", "rapidworkers",
    "sproutgigs", "afterquery", "handshake_ai", "braintrust"
}
NEW_FOUR = {"opentrain_ai", "gainworkers", "mindrift", "reworkio"}
ALL_TWELVE = EXPECTED | NEW_FOUR


class PlatformRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.platforms = cls.data["platforms"]

    def test_original_eight_platforms_are_preserved(self):
        self.assertTrue(EXPECTED.issubset({p["id"] for p in self.platforms}))

    def test_registry_contains_exactly_twelve_platforms(self):
        self.assertEqual({p["id"] for p in self.platforms}, ALL_TWELVE)
        self.assertEqual(len(self.platforms), 12)

    def test_platform_ids_are_unique(self):
        ids = [p["id"] for p in self.platforms]
        self.assertEqual(len(ids), len(set(ids)))

    def test_urls_are_https_or_explicitly_unknown(self):
        for platform in self.platforms:
            url = platform["official_url"]
            self.assertTrue(url is None or url.startswith("https://"), platform["id"])

    def test_unknown_identity_cannot_be_enabled(self):
        for platform in self.platforms:
            if platform["identity_status"] not in ("official_homepage_known", "user_confirmed_domain"):
                self.assertNotIn("automated_task_completion", platform["allowed_actions"])
                self.assertNotIn("automated_browsing", platform["allowed_actions"])

    def test_every_platform_requires_human_verification(self):
        for platform in self.platforms:
            self.assertTrue(platform.get("automation_status"))
            self.assertTrue(platform.get("blocked_actions"))

    def test_discovery_sources_are_manual_and_have_https_urls(self):
        sources_path = ROOT / "microtask_monitor" / "discovery_sources.json"
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        for source in sources["sources"]:
            self.assertTrue(source["url"].startswith("https://"))
            self.assertFalse(source["automated_fetch_enabled"])

    def test_afterquery_roles_have_official_apply_destination(self):
        sources_path = ROOT / "microtask_monitor" / "discovery_sources.json"
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        afterquery_roles = [
            role for role in sources["current_reviewed_roles"]
            if "sidequest.expert" in role.get("source_url", "")
        ]
        self.assertGreaterEqual(len(afterquery_roles), 1)
        for role in afterquery_roles:
            self.assertEqual(role["apply_via"], "https://experts.afterquery.com/home")


    def test_html_report_lists_platforms_and_escapes_listing_content(self):
        sample = {
            "checked_at_utc": "20261108T100000Z",
            "feed_count": 4,
            "platforms": [{"id": "atlas_capture", "name": "Atlas Capture"}],
            "discovery_sources": {
                "sources": [{
                    "platform_id": "atlas_capture",
                    "name": "Atlas Capture <script>alert(1)</script>",
                    "url": "https://www.atlascapture.io/opportunities",
                    "use": "Review manually"
                }],
                "current_reviewed_roles": []
            },
            "items": [{
                "title": "AI evaluator <script>alert(2)</script>",
                "url": "https://example.com/job",
                "priority": "high_review",
                "relevance_score": 50,
                "kenya_eligibility": "remote_scope_unclear_kenya_not_confirmed",
                "source": "Test feed",
                "summary": "Review <b>carefully</b>"
            }],
            "unique_listing_count": 1,
            "high_review_count": 1,
            "medium_review_count": 0,
            "important_note": "Verify all listings."
        }
        output = render_html(sample)
        self.assertIn("Atlas Capture", output)
        self.assertIn("https://www.atlascapture.io/opportunities", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertNotIn("<script>alert(1)</script>", output)
        self.assertIn("remote_scope_unclear_kenya_not_confirmed", output)


    def test_local_dashboard_has_all_core_workspaces_and_backup(self):
        dashboard = ROOT / "dashboard" / "index.html"
        self.assertTrue(dashboard.exists())
        page = dashboard.read_text(encoding="utf-8")
        for section in ("overview", "platforms", "opportunities", "applications", "earnings"):
            self.assertIn('id="' + section + '"', page)
        for platform_id in ALL_TWELVE:
            self.assertIn('id:"' + platform_id + '"', page)
        self.assertIn("importReport", page)
        self.assertIn("exportBackup", page)
        self.assertIn("localStorage", page)

    def test_local_dashboard_does_not_request_secrets_or_external_scripts(self):
        page = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8").lower()
        self.assertNotIn("<script src=", page)
        self.assertNotIn('type="password"', page)
        self.assertNotIn("session_token", page)
        self.assertIn("never enter passwords", page)

    def test_each_target_platform_has_a_discovery_entry(self):
        sources_path = ROOT / "microtask_monitor" / "discovery_sources.json"
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        platform_ids = {p["id"] for p in self.platforms}
        discovered_ids = {s["platform_id"] for s in sources["sources"]}
        self.assertTrue(platform_ids.issubset(discovered_ids), platform_ids - discovered_ids)

    def test_no_account_portal_is_marked_for_automated_fetch(self):
        sources_path = ROOT / "microtask_monitor" / "discovery_sources.json"
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        for source in sources["sources"]:
            self.assertFalse(source.get("automated_fetch_enabled", False), source["id"])


if __name__ == "__main__":
    unittest.main()
