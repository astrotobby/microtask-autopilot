import json
import tempfile
import unittest
from pathlib import Path

from microtask_monitor import api, autonomy, db, policy


class AutonomousReadinessTests(unittest.TestCase):
    def test_current_capabilities_have_no_eligible_autonomous_source(self):
        result = autonomy.assess(policy.load_capabilities())
        self.assertFalse(result["enabled"])
        self.assertEqual(result["eligible_platforms"], [])
        self.assertIn("No platform", result["message"])
        self.assertEqual(len(result["blocked_platforms"]), 12)

    def test_missing_contract_fails_closed(self):
        result = autonomy.evaluate("demo", {
            "country_eligibility": {"kenya": "confirmed"},
            "payment_verification": {"status": "documented"},
        })
        self.assertFalse(result["eligible"])
        self.assertIn("no autonomous_earning capability record", result["reasons"])
        self.assertIn("application_submission is not explicitly permitted", result["reasons"])

    def test_complete_contract_requires_user_authorization_at_policy_boundary(self):
        rec = {
            "autonomous_earning": {
                "status": "verified", "official_api": True, "terms_url": "https://example.test/terms",
                "last_verified": "2026-11-12",
                "stages": {stage: "permitted" for stage in autonomy.REQUIRED_STAGES},
            },
            "country_eligibility": {"kenya": "confirmed"},
            "payment_verification": {"status": "documented"},
        }
        result = autonomy.evaluate("demo", rec)
        self.assertTrue(result["eligible"])
        self.assertFalse(policy.check_action("demo", "autonomous_earning", caps={"demo": rec}).allowed)
        self.assertTrue(policy.check_action("demo", "autonomous_earning", caps={"demo": rec}, user_authorized=True).allowed)

    def test_api_exposes_blocked_readiness_without_contacting_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "autopilot.sqlite3"
            client = api.Api(p)
            status, payload = client.handle("GET", "/api/autonomy")
            self.assertEqual(status, 200)
            self.assertFalse(payload["enabled"])
            self.assertEqual(payload["eligible_platforms"], [])
            self.assertIn("required_contract", payload)
            self.assertEqual(len(payload["blocked_platforms"]), 12)


if __name__ == "__main__":
    unittest.main()
