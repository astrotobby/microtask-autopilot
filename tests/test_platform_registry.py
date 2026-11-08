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
            if platform["identity_status"] != "official_homepage_known" and platform["identity_status"] != "user_confirmed_domain":
                self.assertNotIn("automated_task_completion", platform["allowed_actions"])
                self.assertNotIn("automated_browsing", platform["allowed_actions"])

    def test_every_platform_requires_human_verification(self):
        for platform in self.platforms:
            self.assertTrue(platform.get("automation_status"))
            self.assertTrue(platform.get("blocked_actions"))

if __name__ == "__main__":
    unittest.main()
