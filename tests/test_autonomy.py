import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from microtask_monitor import api, autonomy, db, policy


class AutonomousReadinessTests(unittest.TestCase):
    def test_current_capabilities_have_no_eligible_autonomous_source(self):
        result = autonomy.assess(policy.load_capabilities())
        self.assertFalse(result["enabled"])
        self.assertEqual(result["eligible_platforms"], [])
        self.assertIn("No platform", result["message"])
        self.assertEqual(len(result["blocked_platforms"]), 17)

    def test_missing_contract_fails_closed(self):
        result = autonomy.evaluate("demo", {
            "country_eligibility": {"kenya": "confirmed"},
            "payment_verification": {"status": "documented"},
        })
        self.assertFalse(result["eligible"])
        self.assertIn("no autonomous_earning capability record", result["reasons"])
        self.assertIn("application_submission is unverified, not explicitly permitted for unattended operation", result["reasons"])

    def test_example_test_is_not_an_official_policy_source(self):
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
        self.assertFalse(result["eligible"])
        self.assertIn("official platform domain is missing or invalid", result["reasons"])
        self.assertTrue(any("official-domain source" in reason for reason in result["reasons"]))
        self.assertFalse(policy.check_action("demo", "autonomous_earning", caps={"demo": rec}).allowed)
        self.assertFalse(policy.check_action("demo", "autonomous_earning", caps={"demo": rec}, user_authorized=True).allowed)

    def test_missing_policy_clause_and_unrelated_official_looking_url_block(self):
        rec = {"autonomous_earning": {"status": "verified", "official_api": True,
              "official_domain": "platform.example", "terms_url": "https://other.example/terms",
              "documentation_url": "https://platform.example/docs", "last_verified": date.today().isoformat(),
              "next_review": (date.today() + timedelta(days=1)).isoformat(),
              "stages": {stage: "permitted" for stage in autonomy.REQUIRED_STAGES}},
              "country_eligibility": {"kenya": "confirmed", "evidence": "fixture"},
              "payment_verification": {"status": "documented", "evidence": "fixture"}}
        result = autonomy.evaluate("demo", rec)
        self.assertFalse(result["eligible"])
        self.assertIn("an exact reviewed policy clause is missing", result["reasons"])
        self.assertTrue(any("official-domain source" in reason for reason in result["reasons"]))

    def test_expired_evidence_prohibited_and_human_required_stages_block(self):
        rec = {"autonomous_earning": {"status": "verified", "official_api": True,
              "official_domain": "platform.example", "terms_url": "https://platform.example/terms",
              "documentation_url": "https://platform.example/docs", "last_verified": "2020-01-01",
              "next_review": "2020-01-02", "policy_clause": "fixture clause",
              "evidence_sha256": "a" * 64, "reviewer": "test reviewer",
              "api_scope": "read/write", "api_operations": ["apply"], "api_authentication": "OAuth",
              "rate_limits": "documented", "required_user_authorization": "user grant",
              "stages": {stage: "permitted" for stage in autonomy.REQUIRED_STAGES}},
              "country_eligibility": {"kenya": "confirmed", "evidence": "fixture"},
              "payment_verification": {"status": "documented", "evidence": "fixture"}}
        rec["autonomous_earning"]["stages"]["task_execution"] = "prohibited"
        rec["autonomous_earning"]["stages"]["evidence_submission"] = "human_required"
        result = autonomy.evaluate("demo", rec)
        self.assertFalse(result["eligible"])
        self.assertTrue(any("expired" in reason for reason in result["reasons"]))
        self.assertIn("task_execution is prohibited, not explicitly permitted for unattended operation", result["reasons"])
        self.assertIn("evidence_submission is human_required, not explicitly permitted for unattended operation", result["reasons"])

    def test_unverified_kenya_or_payout_evidence_blocks(self):
        result = autonomy.evaluate("demo", {
            "autonomous_earning": {"stages": {stage: "permitted" for stage in autonomy.REQUIRED_STAGES}},
            "country_eligibility": {"kenya": "confirmed"},
            "payment_verification": {"status": "documented"},
        })
        self.assertFalse(result["eligible"])
        self.assertIn("Kenya eligibility is not directly evidenced and explicitly confirmed", result["reasons"])
        self.assertIn("Kenya-compatible payout route is not directly evidenced", result["reasons"])

    def test_api_exposes_blocked_readiness_without_contacting_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "autopilot.sqlite3"
            client = api.Api(p)
            status, payload = client.handle("GET", "/api/autonomy")
            self.assertEqual(status, 200)
            self.assertFalse(payload["enabled"])
            self.assertEqual(payload["eligible_platforms"], [])
            self.assertIn("required_contract", payload)
            self.assertEqual(len(payload["blocked_platforms"]), 17)


if __name__ == "__main__":
    unittest.main()
