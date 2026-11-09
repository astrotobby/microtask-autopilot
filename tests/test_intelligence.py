import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import db, intelligence as ig, opentrain as ot, policy

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
CAPS = policy.load_capabilities()
PROFILE = ig.load_profile()


def item(**kw):
    base = {"platform_id": "opentrain_ai", "external_id": "j1", "source": "OpenTrain AI",
            "title": "Python Backend Engineer for AI agents", "url": "https://www.opentrain.ai/jobs/j1",
            "summary": "Build Python APIs and n8n automation with Claude Code. Part-time.",
            "kenya_eligibility": "kenya_explicitly_eligible", "payment_display": "USD 40/hour",
            "payment_usd_per_hour": 40, "published": (NOW - timedelta(days=1)).isoformat(), "status": "open",
            "category": "ai-engineering", "skills": ["Python"]}
    base.update(kw)
    return base


class ClassificationTests(unittest.TestCase):
    def cls(self, **kw):
        return ig.classify(item(**kw), CAPS, NOW)[0]

    def test_explicit_kenya_worldwide_and_unsupported(self):
        self.assertEqual(self.cls(), ig.KENYA_CONFIRMED)
        self.assertEqual(self.cls(kenya_eligibility="worldwide_eligible"), ig.WORLDWIDE_CONFIRMED)
        self.assertEqual(self.cls(kenya_eligibility="restricted_other_countries"), ig.KENYA_UNSUPPORTED)

    def test_unspecified_and_keyword_only_signals_are_never_confirmed(self):
        for raw in ("kenya_eligibility_unspecified", "kenya_mentioned_verify_requirements",
                    "remote_scope_unclear_kenya_not_confirmed", "location_not_established"):
            self.assertEqual(self.cls(kenya_eligibility=raw), ig.KENYA_UNVERIFIED, raw)

    def test_restriction_wording_and_missing_data_need_manual_review(self):
        self.assertEqual(self.cls(kenya_eligibility="likely_restricted_check_listing"), ig.MANUAL_REVIEW_REQUIRED)
        self.assertEqual(self.cls(kenya_eligibility=""), ig.MANUAL_REVIEW_REQUIRED)
        self.assertEqual(self.cls(kenya_eligibility="something_new"), ig.MANUAL_REVIEW_REQUIRED)

    def test_site_accessibility_alone_is_not_eligibility(self):
        cls, conf, reason = ig.classify({"platform_id": None, "title": "Remote job", "url": "https://x.test/"}, CAPS, NOW)
        self.assertNotIn(cls, (ig.KENYA_CONFIRMED, ig.WORLDWIDE_CONFIRMED))

    def test_mindrift_is_unsupported_even_if_a_listing_claims_kenya(self):
        cls, conf, reason = ig.classify(item(platform_id="mindrift"), CAPS, NOW)
        self.assertEqual(cls, ig.KENYA_UNSUPPORTED)
        self.assertEqual(conf, "high")
        self.assertIn("policy", reason)

    def test_expired_by_status_or_deadline_wins_over_eligibility(self):
        self.assertEqual(self.cls(status="expired"), ig.EXPIRED)
        self.assertEqual(self.cls(status="filled"), ig.EXPIRED)
        self.assertEqual(self.cls(deadline=(NOW - timedelta(hours=1)).isoformat()), ig.EXPIRED)
        self.assertEqual(self.cls(deadline=(NOW + timedelta(days=2)).isoformat()), ig.KENYA_CONFIRMED)

    def test_manual_record_without_region_is_manual_review(self):
        self.assertEqual(self.cls(entry_method="manual_review", kenya_eligibility="kenya_eligibility_unspecified"),
                         ig.MANUAL_REVIEW_REQUIRED)

    def test_every_class_is_one_of_the_six_defined(self):
        for raw in ("kenya_explicitly_eligible", "worldwide_eligible", "restricted_other_countries", "", "x"):
            self.assertIn(self.cls(kenya_eligibility=raw), ig.CLASSES)


class RequirementTests(unittest.TestCase):
    def test_extracts_experience_qualifications_equipment_effort_and_application_needs(self):
        req = ig.extract_requirements("Requires PhD or Bachelor degree and 5+ years experience. Needs an iPhone and "
                                      "webcam. Less than 20 hours per week. Submit resume and portfolio; interview.")
        self.assertEqual(req["min_years_experience"], 5)
        self.assertIn("PhD/doctorate", req["qualifications"])
        self.assertIn("iPhone", req["equipment"])
        self.assertEqual(req["hours_per_week"], 20)
        self.assertIn("resume/CV", req["application_requirements"])
        self.assertIn("interview", req["application_requirements"])

    def test_gaps_flag_unestablished_qualifications_and_apple_hardware(self):
        req = ig.extract_requirements("PhD required. 4 years experience. iPhone 12 or newer.")
        gaps = " ".join(ig.qualification_gaps(req, PROFILE))
        self.assertIn("PhD", gaps)
        self.assertIn("4+ years", gaps)
        self.assertIn("Apple hardware", gaps)

    def test_no_gaps_when_nothing_is_required(self):
        self.assertEqual(ig.qualification_gaps(ig.extract_requirements("Simple data entry"), PROFILE), [])

    def test_profile_does_not_claim_a_degree_or_ios_device(self):
        self.assertFalse(PROFILE["devices"]["ios_available"])
        self.assertTrue(any("degree" in x.lower() for x in PROFILE["not_established"]))


class ScoringTests(unittest.TestCase):
    def total(self, **kw):
        it = item(**kw)
        enriched = ig.enrich(it, CAPS, PROFILE, NOW)
        return enriched["score"], enriched

    def test_priority_order_kenya_then_worldwide_then_unverified_then_unsupported(self):
        k = self.total()[0]
        w = self.total(kenya_eligibility="worldwide_eligible")[0]
        u = self.total(kenya_eligibility="kenya_eligibility_unspecified")[0]
        x = self.total(kenya_eligibility="restricted_other_countries")[0]
        self.assertGreater(k, w)
        self.assertGreater(w, u)
        self.assertGreater(u, x)

    def test_skill_match_raises_the_score(self):
        matched = self.total()[0]
        unrelated = self.total(title="Truck driver", summary="Drive a truck", skills=[], category="logistics")[0]
        self.assertGreater(matched, unrelated)

    def test_compensation_counts_but_suspicious_pay_does_not(self):
        base = self.total(payment_usd_per_hour=40)[0]
        none = self.total(payment_usd_per_hour=None, payment_display="Not disclosed")[0]
        flagged = self.total(payment_usd_per_hour=900, payment_flag="suspicious_verify_manually")[0]
        self.assertGreater(base, none)
        self.assertLessEqual(flagged, none)

    def test_freshness_matters(self):
        fresh = self.total(published=(NOW - timedelta(days=1)).isoformat())[0]
        old = self.total(published=(NOW - timedelta(days=90)).isoformat())[0]
        self.assertGreater(fresh, old)

    def test_requirements_and_equipment_penalties_apply(self):
        plain = self.total()[0]
        degree = self.total(summary="Python APIs. PhD required and 6+ years experience.")[0]
        iphone = self.total(summary="Python APIs. Requires an iPhone 12 or newer.")[0]
        self.assertLess(degree, plain)
        self.assertLess(iphone, plain)

    def test_expired_and_unsupported_are_low_priority(self):
        self.assertEqual(self.total(status="expired")[1]["priority"], "low_or_unrelated")
        self.assertEqual(self.total(kenya_eligibility="restricted_other_countries")[1]["priority"], "low_or_unrelated")

    def test_high_priority_requires_kenya_or_worldwide_and_a_good_score(self):
        score, rec = self.total()
        self.assertGreaterEqual(score, 60)
        self.assertEqual(rec["priority"], "high_review")
        self.assertNotEqual(self.total(kenya_eligibility="kenya_eligibility_unspecified")[1]["priority"], "high_review")

    def test_breakdown_is_transparent_and_scores_are_bounded(self):
        score, rec = self.total()
        for key in ("eligibility", "skills", "compensation", "availability", "freshness", "payment_reliability"):
            self.assertIn(key, rec["breakdown"])
        self.assertTrue(0 <= score <= 100)

    def test_payment_reliability_only_for_documented_payout_routes(self):
        documented = self.total()[1]["breakdown"]["payment_reliability"]
        unknown = self.total(platform_id="clickworker")[1]["breakdown"]["payment_reliability"]
        self.assertGreater(documented, unknown)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ctx = db.session(Path(self.tmp.name) / "a.sqlite3")
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def ingest(self, items, **kw):
        with db.tx(self.conn):
            return ig.ingest(self.conn, items, now=kw.pop("now", NOW), caps=CAPS, profile=PROFILE, **kw)

    def row(self, url="https://www.opentrain.ai/jobs/j1"):
        return self.conn.execute("SELECT * FROM opportunities WHERE url=?", (url,)).fetchone()

    def test_new_opportunity_is_stored_with_structured_fields(self):
        stats = self.ingest([item()])
        self.assertEqual(stats["new"], 1)
        r = self.row()
        self.assertEqual(r["eligibility_class"], ig.KENYA_CONFIRMED)
        self.assertEqual(r["eligibility_confidence"], "high")
        self.assertEqual(r["platform_id"], "opentrain_ai")
        self.assertEqual(r["status"], "open")
        self.assertEqual(r["payment_text"], "USD 40/hour")
        self.assertIsNotNone(r["last_verified_utc"])
        self.assertIn("eligibility", json.loads(r["score_breakdown_json"]))
        self.assertIn("python", json.loads(r["skills_json"]))

    def test_rescan_updates_without_duplicating_and_keeps_first_seen(self):
        self.ingest([item()])
        first = self.row()["first_seen_utc"]
        stats = self.ingest([item()], now=NOW + timedelta(hours=1))
        self.assertEqual((stats["new"], stats["updated"], stats["changed"]), (0, 1, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)
        self.assertEqual(self.row()["first_seen_utc"], first)
        self.assertNotEqual(self.row()["last_seen_utc"], first)

    def test_changes_between_scans_are_tracked(self):
        self.ingest([item()])
        self.ingest([item(payment_display="USD 55/hour", payment_usd_per_hour=55, title="Python Backend Engineer II")],
                    now=NOW + timedelta(hours=1))
        fields = {r["field"]: (r["old_value"], r["new_value"]) for r in self.conn.execute("SELECT * FROM opportunity_changes")}
        self.assertEqual(fields["payment_text"], ("USD 40/hour", "USD 55/hour"))
        self.assertEqual(fields["title"][1], "Python Backend Engineer II")

    def test_vanished_listings_expire_only_when_the_platform_scan_was_complete(self):
        self.ingest([item(), item(external_id="j2", url="https://www.opentrain.ai/jobs/j2")])
        stats = self.ingest([item()], now=NOW + timedelta(hours=1), complete_platforms=set())
        self.assertEqual(stats["expired"], 0)                      # incomplete scan: do not guess
        stats = self.ingest([item()], now=NOW + timedelta(hours=2), complete_platforms={"opentrain_ai"})
        self.assertEqual(stats["expired"], 1)
        gone = self.row("https://www.opentrain.ai/jobs/j2")
        self.assertEqual((gone["status"], gone["eligibility_class"]), ("expired", "EXPIRED"))
        self.assertIsNotNone(gone["expired_utc"])

    def test_feed_items_go_stale_after_fourteen_days_only(self):
        feed = {"title": "Remote Python dev", "url": "https://feed.example/jobs/1", "source": "WWR",
                "kenya_eligibility": "remote_scope_unclear_kenya_not_confirmed"}
        self.ingest([feed], now=NOW)
        self.assertEqual(self.ingest([], now=NOW + timedelta(days=5))["expired"], 0)
        self.assertEqual(self.ingest([], now=NOW + timedelta(days=15))["expired"], 1)

    def test_reappearing_listing_is_reopened(self):
        self.ingest([item()])
        self.ingest([], now=NOW + timedelta(hours=1), complete_platforms={"opentrain_ai"})
        self.assertEqual(self.row()["status"], "expired")
        self.ingest([item()], now=NOW + timedelta(hours=2))
        self.assertEqual(self.row()["status"], "open")

    def test_duplicates_across_urls_are_detected_by_platform_and_external_id(self):
        self.ingest([item()])
        stats = self.ingest([item(url="https://www.opentrain.ai/jobs/j1-copy")], now=NOW + timedelta(hours=1))
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(self.row("https://www.opentrain.ai/jobs/j1-copy")["duplicate_of"], "https://www.opentrain.ai/jobs/j1")

    def test_duplicate_feed_titles_are_detected(self):
        a = {"title": "Senior Python Developer", "url": "https://a.example/1", "source": "A"}
        b = {"title": "Senior  Python Developer!", "url": "https://b.example/2", "source": "B"}
        self.ingest([a])
        self.assertEqual(self.ingest([b], now=NOW + timedelta(hours=1))["duplicates"], 1)

    def test_non_https_and_malformed_items_are_skipped(self):
        stats = self.ingest([{"title": "x", "url": "http://insecure.test/"}, {"title": "y"}, item()])
        self.assertEqual(stats["new"], 1)

    def test_legacy_rows_are_backfilled_with_classes(self):
        self.conn.execute("""INSERT INTO opportunities(url,title,source,summary,priority,relevance_score,category,
            kenya_eligibility,published,first_seen_utc,last_seen_utc,report_checked_at_utc,raw_json)
            VALUES('https://legacy.example/1','Legacy','Feed','s','high_review',40,'x','kenya_mentioned_verify_requirements',
            '','a','b','c',?)""", (json.dumps({"title": "Legacy", "kenya_eligibility": "kenya_mentioned_verify_requirements"}),))
        with db.tx(self.conn):
            self.assertEqual(ig.backfill(self.conn, NOW), 1)
        r = self.row("https://legacy.example/1")
        self.assertEqual(r["eligibility_class"], ig.KENYA_UNVERIFIED)
        self.assertEqual(r["first_seen_utc"], "a")                    # original timestamp preserved

    def test_real_opentrain_normalization_flows_into_classes(self):
        raw = {"id": "k1", "title": "Python AI Engineer", "url": "https://www.opentrain.ai/jobs/k1", "status": "OPEN",
               "descriptionText": "Python and LLM", "countries": ["KE"], "datePosted": NOW.isoformat(),
               "validThrough": (NOW + timedelta(days=10)).isoformat(), "applyMode": "opentrain",
               "pay": {"currency": "USD", "hourlyRate": 30}}
        normalized = ot.normalize_job(raw, NOW)
        self.ingest([normalized])
        r = self.row("https://www.opentrain.ai/jobs/k1")
        self.assertEqual(r["eligibility_class"], ig.KENYA_CONFIRMED)
        self.assertEqual(r["payment_usd_per_hour"], 30)
        self.assertTrue(r["deadline"])


if __name__ == "__main__":
    unittest.main()
