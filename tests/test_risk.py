import json
import unittest
from datetime import datetime, timezone

from microtask_monitor import db, intelligence as ig, policy, risk


class RiskAssessmentTests(unittest.TestCase):
    def test_clean_registered_official_listing_is_verified_source(self):
        result = risk.assess({
            "platform_id": "opentrain_ai",
            "url": "https://www.opentrain.ai/jobs/example",
            "source": "OpenTrain AI",
            "title": "Python evaluator",
            "summary": "Review code samples. Pay is USD 20/hour.",
            "payment_display": "USD 20/hour",
        })
        self.assertEqual(result["label"], risk.VERIFIED_SOURCE)
        self.assertEqual(result["flags"], [])

    def test_upfront_payment_language_is_high_risk(self):
        result = risk.assess({
            "platform_id": "opentrain_ai",
            "url": "https://www.opentrain.ai/jobs/example",
            "source": "OpenTrain AI",
            "title": "Remote task",
            "summary": "Pay a registration fee and deposit before starting.",
            "payment_display": "USD 10 fixed",
        })
        self.assertEqual(result["label"], risk.HIGH_RISK)
        self.assertIn("registration_fee", result["flags"])
        self.assertIn("deposit_required", result["flags"])

    def test_external_apply_url_is_reviewable_not_silently_verified(self):
        result = risk.assess({
            "platform_id": "opentrain_ai",
            "url": "https://example.net/jobs/example",
            "source": "OpenTrain AI",
            "title": "Python evaluator",
            "summary": "Review code samples.",
            "payment_display": "Not disclosed",
        })
        self.assertEqual(result["label"], risk.REQUIRES_REVIEW)
        self.assertIn("source_domain_mismatch", result["flags"])


class RiskPersistenceTests(unittest.TestCase):
    def test_ingestion_persists_risk_fields_and_breakdown(self):
        with db.session(":memory:") as conn:
            with db.tx(conn):
                out = ig.ingest(conn, [{
                    "platform_id": "opentrain_ai", "external_id": "risk-1",
                    "source": "OpenTrain AI", "url": "https://www.opentrain.ai/jobs/risk-1",
                    "title": "Python evaluator", "summary": "Review code samples.",
                    "kenya_eligibility": "kenya_explicitly_eligible",
                    "payment_display": "USD 20/hour", "published": datetime.now(timezone.utc).isoformat(),
                }], caps=policy.load_capabilities(), profile=ig.load_profile())
            self.assertEqual(out["new"], 1)
            row = conn.execute("SELECT risk_label,risk_score,risk_flags_json,score_breakdown_json FROM opportunities").fetchone()
            self.assertEqual(row[0], risk.VERIFIED_SOURCE)
            self.assertEqual(row[1], 0)
            self.assertEqual(json.loads(row[2]), [])
            self.assertEqual(json.loads(row[3])["risk"]["label"], risk.VERIFIED_SOURCE)


if __name__ == "__main__":
    unittest.main()
