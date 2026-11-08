import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from microtask_monitor import opentrain as ot
from microtask_monitor import opportunity_common as oc

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def job(job_id="1", **overrides):
    base = {
        "id": job_id, "slug": f"job-{job_id}", "title": "AI Workflow Engineer (Python, LLM Integration)",
        "companyName": "OpenTrain AI", "jobOrigin": "OPENTRAIN_NATIVE",
        "descriptionText": "Build n8n and Python automation with an API and webhooks.\n- Location: Open worldwide.",
        "summary": "Automation work", "status": "OPEN",
        "datePosted": (NOW - timedelta(days=2)).isoformat(), "validThrough": (NOW + timedelta(days=30)).isoformat(),
        "employmentTypes": ["CONTRACTOR"], "countries": [], "languages": ["en"], "category": "ai-engineering",
        "domains": ["software"], "skills": ["Python"], "experienceLevel": "Mid",
        "pay": {"paymentType": "PAY_PER_HOUR", "currency": "USD", "hourlyRate": 40},
        "url": f"https://www.opentrain.ai/jobs/job-{job_id}", "applyUrl": f"https://app.opentrain.ai/api/out/{job_id}",
        "applyMode": "opentrain",
    }
    base.update(overrides)
    return base


class ParsingTests(unittest.TestCase):
    def test_extracts_all_requested_fields(self):
        item = ot.normalize_job(job(), NOW)
        self.assertEqual(item["platform_id"], "opentrain_ai")
        self.assertEqual(item["external_id"], "1")
        self.assertEqual(item["category"], "ai-engineering")
        self.assertEqual(item["skills"], ["Python"])
        self.assertEqual(item["languages"], ["en"])
        self.assertEqual(item["payment_display"], "USD 40/hour")
        self.assertEqual(item["url"], "https://www.opentrain.ai/jobs/job-1")
        self.assertTrue(item["apply_url"].startswith("https://app.opentrain.ai/"))
        self.assertTrue(item["published"].startswith("2026-11-06"))
        self.assertTrue(item["requires_human_verification"])

    def test_invalid_jobs_are_rejected(self):
        self.assertIsNone(ot.normalize_job({"id": "1", "title": "", "url": "https://x.test/"}, NOW))
        self.assertIsNone(ot.normalize_job({"id": "1", "title": "T", "url": "http://insecure.test/"}, NOW))
        self.assertIsNone(ot.normalize_job({"title": "T", "url": "https://x.test/"}, NOW))

    def test_listing_text_is_kept_raw_for_dashboard_escaping(self):
        item = ot.normalize_job(job(title="<script>alert(1)</script>"), NOW)
        self.assertIn("<script>", item["title"])   # escaping is the renderer's job (tested elsewhere)


class EligibilityTests(unittest.TestCase):
    def test_kenya_in_country_list_is_explicit(self):
        self.assertEqual(ot.classify_eligibility(["US", "ke"], ""), oc.KENYA_EXPLICIT)

    def test_other_countries_only_is_restricted(self):
        self.assertEqual(ot.classify_eligibility(["US", "CA"], "Open worldwide"), oc.RESTRICTED_OTHER)

    def test_empty_list_with_worldwide_text_is_worldwide(self):
        self.assertEqual(ot.classify_eligibility([], "- Location: Open worldwide."), oc.WORLDWIDE)

    def test_empty_list_without_statement_is_never_kenya_eligible(self):
        self.assertEqual(ot.classify_eligibility([], "Great role, apply now"), oc.UNSPECIFIED)
        self.assertEqual(ot.classify_eligibility(None, ""), oc.UNSPECIFIED)

    def test_kenya_explicit_outranks_worldwide_and_restricted(self):
        def score(countries, text):
            item = ot.normalize_job(job(countries=countries, descriptionText=text), NOW)
            return item["relevance_score"]
        self.assertGreater(score(["KE"], "x"), score([], "Open worldwide"))
        self.assertGreater(score([], "Open worldwide"), score([], "x"))
        self.assertGreater(score([], "x"), score(["US"], "x"))

    def test_restricted_roles_are_low_priority(self):
        item = ot.normalize_job(job(countries=["US"]), NOW)
        self.assertEqual(item["priority"], "low_or_unrelated")


class PaymentTests(unittest.TestCase):
    def test_missing_or_invalid_payment_is_not_disclosed_and_not_invented(self):
        for pay in (None, {}, {"currency": "USD"}, {"hourlyRate": "abc"}, {"hourlyRate": -5}, {"hourlyRate": 0},
                    {"hourlyRate": float("nan")}, {"hourlyRate": True}, "free money"):
            text, hourly, flag = ot.format_payment(pay)
            self.assertEqual((text, hourly, flag), ("Not disclosed", None, ""), pay)

    def test_range_fixed_and_per_label(self):
        self.assertEqual(ot.format_payment({"currency": "USD", "hourlyMin": 20, "hourlyMax": 35})[0], "USD 20-35/hour")
        self.assertEqual(ot.format_payment({"currency": "USD", "fixedPrice": 500})[0], "USD 500 fixed")
        self.assertEqual(ot.format_payment({"currency": "USD", "perLabelRate": 0.2})[0], "USD 0.2 per label")

    def test_unusually_high_pay_is_flagged_and_not_rewarded(self):
        text, hourly, flag = ot.format_payment({"currency": "USD", "hourlyRate": 900})
        self.assertEqual(flag, "suspicious_verify_manually")
        item = ot.normalize_job(job(pay={"currency": "USD", "hourlyRate": 900}), NOW)
        normal = ot.normalize_job(job(pay={"currency": "USD", "hourlyRate": 40}), NOW)
        self.assertIn("suspicious_verify_manually", item["flags"])
        self.assertLessEqual(item["relevance_score"], normal["relevance_score"])

    def test_non_usd_rate_is_displayed_but_not_converted(self):
        text, hourly, flag = ot.format_payment({"currency": "EUR", "hourlyRate": 30})
        self.assertEqual((text, hourly), ("EUR 30/hour", None))


class StatusAndDedupeTests(unittest.TestCase):
    def test_expired_and_closed_jobs_are_not_reported_as_open(self):
        expired = job("e", validThrough=(NOW - timedelta(days=1)).isoformat())
        closed = job("c", status="CLOSED")
        items, stats = ot.normalize_jobs([expired, closed, job("ok")], NOW)
        self.assertEqual([i["external_id"] for i in items], ["ok"])
        self.assertEqual(stats["unavailable"], 2)

    def test_duplicates_by_id_and_by_url_are_removed(self):
        a = job("1")
        b = job("1", title="Same id")
        c = job("2", url=a["url"])          # different id, same URL
        items, stats = ot.normalize_jobs([a, b, c, job("3")], NOW)
        self.assertEqual(sorted(i["external_id"] for i in items), ["1", "3"])
        self.assertEqual(stats["duplicates"], 2)

    def test_partner_jobs_are_marked_external(self):
        ext = ot.normalize_job(job(jobOrigin="EXTERNAL_AGGREGATED", applyMode="external"), NOW)
        native = ot.normalize_job(job(), NOW)
        self.assertTrue(ext["external_application"])
        self.assertFalse(native["external_application"])

    def test_partner_origin_is_separate_from_external_application(self):
        partner_on_opentrain = ot.normalize_job(job(jobOrigin="EXTERNAL_AGGREGATED", applyMode="opentrain"), NOW)
        self.assertTrue(partner_on_opentrain["partner_job"])
        self.assertFalse(partner_on_opentrain["external_application"])

    def test_summary_counts_each_class(self):
        raws = [job("k", countries=["KE"]), job("w"), job("u", descriptionText="nothing", summary="x"),
                job("r", countries=["US"])]
        items, _ = ot.normalize_jobs(raws, NOW)
        counts = ot.summarize(items)
        self.assertEqual((counts["kenya_explicit"], counts["worldwide"], counts["unspecified"],
                          counts["restricted_other"]), (1, 1, 1, 1))


class PagingAndFailureTests(unittest.TestCase):
    def pages(self, n, per=2):
        def fetch(url):
            idx = int(url.split("cursor=p")[1]) if "cursor=p" in url else 0
            jobs = [job(f"{idx}-{i}", url=f"https://www.opentrain.ai/jobs/{idx}-{i}") for i in range(per)]
            return {"jobs": jobs, "nextCursor": f"p{idx + 1}" if idx + 1 < n else None}
        return fetch

    def test_follows_cursor_to_the_end(self):
        sleeps = []
        raw, info = ot.fetch_all_jobs(self.pages(3), sleep=sleeps.append)
        self.assertEqual((len(raw), info["pages"], info["complete"]), (6, 3, True))
        self.assertTrue(all(s >= 0.6 for s in sleeps))        # stays under 120 requests/minute

    def test_repeated_cursor_stops_pagination(self):
        fetch = lambda url: {"jobs": [job("x")], "nextCursor": "same"}
        raw, info = ot.fetch_all_jobs(fetch, sleep=lambda s: None)
        self.assertFalse(info["complete"])
        self.assertIn("repeated", info["error"])
        self.assertLessEqual(info["pages"], 3)

    def test_rate_limit_429_honours_retry_after_then_succeeds(self):
        calls, sleeps = [], []
        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise ot.OpenTrainError("api", "Too Many Requests", 429, retry_after=7)
            return {"jobs": [job("1")], "nextCursor": None}
        raw, info = ot.fetch_all_jobs(fetch, sleep=sleeps.append)
        self.assertEqual(len(raw), 1)
        self.assertIn(7, sleeps)

    def test_client_error_is_not_retried_and_keeps_partial_results(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                return {"jobs": [job("1")], "nextCursor": "c2"}
            raise ot.OpenTrainError("api", "Bad Request", 400)
        raw, info = ot.fetch_all_jobs(fetch, sleep=lambda s: None)
        self.assertEqual(len(raw), 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("400", info["error"])

    def test_network_failure_without_cache_reports_error_not_crash(self):
        def fetch(url):
            raise ot.OpenTrainError("network", "unreachable")
        with tempfile.TemporaryDirectory() as tmp:
            result = ot.scan(fetch, cache_path=Path(tmp) / "c.json", now=NOW, sleep=lambda s: None)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["health"]["status"], "error")
        self.assertIn("unreachable", result["health"]["error"])

    def test_network_failure_falls_back_to_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            good = ot.scan(lambda url: {"jobs": [job("1")], "nextCursor": None}, cache_path=cache,
                           now=NOW - timedelta(hours=3), sleep=lambda s: None)
            self.assertEqual(good["health"]["status"], "ok")
            def broken(url):
                raise ot.OpenTrainError("network", "down")
            degraded = ot.scan(broken, cache_path=cache, now=NOW, sleep=lambda s: None)
        self.assertEqual(degraded["health"]["status"], "degraded_using_cache")
        self.assertEqual(len(degraded["items"]), 1)

    def test_fresh_cache_avoids_polling_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            ot.scan(lambda url: {"jobs": [job("1")], "nextCursor": None}, cache_path=cache, now=NOW,
                    sleep=lambda s: None)
            def must_not_call(url):
                raise AssertionError("polled within the polling interval")
            result = ot.scan(must_not_call, cache_path=cache, now=NOW + timedelta(minutes=10))
        self.assertEqual(result["health"]["status"], "ok_cached")

    def test_invalid_json_shape_is_reported(self):
        def fetch(url):
            raise ot.OpenTrainError("parse", "Unexpected response shape (no 'jobs' list)")
        with tempfile.TemporaryDirectory() as tmp:
            result = ot.scan(fetch, cache_path=Path(tmp) / "c.json", now=NOW, sleep=lambda s: None)
        self.assertIn("parse", result["health"]["error"])

    def test_scan_queries_kenya_filter_first_and_reclassifies_locally(self):
        urls = []
        def fetch(url):
            urls.append(url)
            if "country=KE" in url:   # server filter lets a US-only partner job through
                return {"jobs": [job("k", countries=["KE"]), job("us", countries=["US"], url="https://www.opentrain.ai/jobs/us")],
                        "nextCursor": None}
            return {"jobs": [job("w")], "nextCursor": None}
        with tempfile.TemporaryDirectory() as tmp:
            result = ot.scan(fetch, cache_path=Path(tmp) / "c.json", now=NOW, sleep=lambda s: None)
        self.assertIn("country=KE", urls[0])
        by_id = {i["external_id"]: i["kenya_eligibility"] for i in result["items"]}
        self.assertEqual(by_id, {"k": oc.KENYA_EXPLICIT, "us": oc.RESTRICTED_OTHER, "w": oc.WORLDWIDE})
        self.assertEqual(result["health"]["status"], "ok")
        self.assertEqual(result["health"]["kenya_explicit"], 1)

    def test_incomplete_unfiltered_pass_is_reported_partial(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if "country=KE" in url:
                return {"jobs": [job("k", countries=["KE"])], "nextCursor": None}
            raise ot.OpenTrainError("network", "timed out")
        with tempfile.TemporaryDirectory() as tmp:
            result = ot.scan(fetch, cache_path=Path(tmp) / "c.json", now=NOW, sleep=lambda s: None)
        self.assertEqual(result["health"]["status"], "partial")
        self.assertEqual(len(result["items"]), 1)

    def test_adapter_refuses_non_opentrain_urls(self):
        with self.assertRaises(ot.OpenTrainError):
            ot.http_get_json("https://evil.example.com/api/public/v1/jobs")


if __name__ == "__main__":
    unittest.main()
