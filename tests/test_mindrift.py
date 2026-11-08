import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from microtask_monitor import mindrift as mi

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
COUNTRIES = [f"Country {i:02d}" for i in range(30)]


def policy_html(updated="May 27, 2026", countries=None, include_kenya=True):
    names = list(COUNTRIES if countries is None else countries)
    if include_kenya:
        names.append("Kenya")
    lis = "".join(f'<li class="framer-text"><p class="framer-text">{n}</p></li>' for n in names)
    return (f"<html><body><script>var x='<li>ignored</li>'</script><h1>Eligibility and Geographic Restrictions</h1>"
            f"<p>Last updated: {updated}</p><p>The following countries are not currently supported:</p><ul>{lis}</ul>"
            f"<p>Kenya is mentioned in prose too.</p></body></html>")


def baseline_for(html):
    parsed = mi.parse_policy(html)
    return {"policy_last_updated": parsed["last_updated"], "list_sha256": parsed["list_sha256"],
            "verified_on": "2026-11-01", "kenya_unsupported": True}


class PolicyParsingTests(unittest.TestCase):
    def test_parses_date_entries_and_detects_kenya(self):
        parsed = mi.parse_policy(policy_html())
        self.assertEqual(parsed["last_updated"], "May 27, 2026")
        self.assertTrue(parsed["kenya_listed"])
        self.assertGreaterEqual(parsed["entry_count"], 30)
        self.assertTrue(mi.looks_valid(parsed))

    def test_kenya_in_prose_only_does_not_count_as_listed(self):
        parsed = mi.parse_policy(policy_html(include_kenya=False))
        self.assertFalse(parsed["kenya_listed"])

    def test_scripts_are_ignored_and_layout_failure_is_invalid(self):
        self.assertFalse(mi.looks_valid(mi.parse_policy("<html><body>Oops, maintenance</body></html>")))


class RestrictionTests(unittest.TestCase):
    def test_kenya_restriction_is_the_default_state(self):
        html = policy_html()
        result = mi.evaluate(mi.parse_policy(html), baseline_for(html), now=NOW)
        self.assertEqual(result["eligibility_status"], "restricted_for_user_location")
        self.assertEqual(result["user_location"], "Kenya")
        self.assertIn("Kenya is explicitly listed", result["reason"])
        self.assertEqual(result["policy_url"], "https://mindrift.ai/legal/eligibility-and-geographic-restrictions")
        self.assertEqual(result["last_eligibility_verification"], "2026-11-08")
        self.assertFalse(result["opportunity_discovery_enabled"])
        self.assertFalse(result["recommend_applications"])
        self.assertFalse(result["change_detected"])

    def test_registry_records_restriction_and_baseline_is_consistent(self):
        root = Path(mi.__file__).resolve().parents[1] / "microtask_monitor"
        platforms = {p["id"]: p for p in json.loads((root / "platforms.json").read_text(encoding="utf-8"))["platforms"]}
        self.assertEqual(platforms["mindrift"]["kenya_eligibility"], "restricted_for_user_location")
        self.assertIn("vpn_or_proxy_or_alternate_identity", platforms["mindrift"]["blocked_actions"])
        base = mi.load_baseline()
        self.assertEqual(base["status"], "restricted_for_user_location")
        self.assertEqual(base["policy_url"], mi.POLICY_URL)
        self.assertTrue(base["verified_on"])


class ChangeDetectionTests(unittest.TestCase):
    def test_kenya_removed_flags_manual_review_and_never_enables_discovery(self):
        old = policy_html()
        new = policy_html(updated="December 1, 2026", include_kenya=False)
        result = mi.evaluate(mi.parse_policy(new), baseline_for(old), now=NOW)
        self.assertTrue(result["change_detected"])
        self.assertTrue(result["needs_manual_review"])
        self.assertIn("KENYA NO LONGER LISTED", result["alert"])
        self.assertEqual(result["eligibility_status"], "restricted_for_user_location")   # still restricted
        self.assertFalse(result["opportunity_discovery_enabled"])
        self.assertFalse(result["recommend_applications"])

    def test_other_policy_change_is_flagged_but_kenya_stays_restricted(self):
        old = policy_html()
        new = policy_html(updated="July 2, 2026", countries=COUNTRIES + ["New Place"])
        result = mi.evaluate(mi.parse_policy(new), baseline_for(old), now=NOW)
        self.assertTrue(result["change_detected"])
        self.assertTrue(result["needs_manual_review"])
        self.assertEqual(result["eligibility_status"], "restricted_for_user_location")

    def test_date_only_change_is_detected(self):
        old = policy_html()
        result = mi.evaluate(mi.parse_policy(policy_html(updated="June 1, 2026")), baseline_for(old), now=NOW)
        self.assertTrue(result["change_detected"])

    def test_unrecognised_layout_is_never_mistaken_for_kenya_removed(self):
        html = policy_html()
        result = mi.evaluate(mi.parse_policy("<html>maintenance page</html>"), baseline_for(html), now=NOW)
        self.assertFalse(result["needs_manual_review"] and "KENYA NO LONGER" in (result["alert"] or ""))
        self.assertEqual(result["health"], "error_using_baseline")
        self.assertEqual(result["eligibility_status"], "restricted_for_user_location")


class ScanTests(unittest.TestCase):
    def run_scan(self, fetch, tmp, baseline_html=None, **kw):
        base = Path(tmp) / "baseline.json"
        base.write_text(json.dumps({"mindrift": baseline_for(baseline_html or policy_html())}))
        return mi.scan(fetch, baseline_path=base, cache_path=Path(tmp) / "cache.json", now=NOW, force=True, **kw)

    def test_scan_never_emits_opportunity_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_scan(lambda: policy_html(), tmp)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["eligibility"]["health"], "ok")

    def test_network_failure_keeps_restriction_and_reports_error(self):
        def broken():
            raise mi.PolicyError("Network error: timed out")
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_scan(broken, tmp)
        self.assertEqual(result["eligibility"]["eligibility_status"], "restricted_for_user_location")
        self.assertIn("timed out", result["eligibility"]["error"])

    def test_fresh_cache_skips_the_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_scan(lambda: policy_html(), tmp)
            base = Path(tmp) / "baseline.json"
            def must_not_fetch():
                raise AssertionError("fetched inside the check interval")
            result = mi.scan(must_not_fetch, baseline_path=base, cache_path=Path(tmp) / "cache.json", now=NOW)
        self.assertEqual(result["eligibility"]["eligibility_status"], "restricted_for_user_location")

    def test_fetch_refuses_other_hosts(self):
        with self.assertRaises(mi.PolicyError):
            mi.fetch_policy_html("https://evil.example.com/policy")

    def test_module_has_no_account_or_workaround_code(self):
        source = Path(mi.__file__).read_text(encoding="utf-8").lower()
        for banned in ("proxies", "proxyhandler", "socks", "login", "password", "cookie"):
            self.assertNotIn(banned, source.replace("vpn, proxy", ""))


if __name__ == "__main__":
    unittest.main()
