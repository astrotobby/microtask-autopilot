import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "microtask_monitor" / "platforms.json"

EXPECTED = {
    "atlas_capture", "clickworker", "microworkers", "rapidworkers",
    "sproutgigs", "afterquery", "handshake_ai", "braintrust"
}


class PlatformRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.platforms = cls.data["platforms"]

    def test_all_eight_requested_platforms_are_present(self):
        self.assertEqual({p["id"] for p in self.platforms}, EXPECTED)

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
