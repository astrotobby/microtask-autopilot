import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import db, policy

CAPS = policy.load_capabilities()
PLATFORMS = json.loads((Path(policy.__file__).parent / "platforms.json").read_text(encoding="utf-8"))["platforms"]
ALL_IDS = [p["id"] for p in PLATFORMS]


class CapabilityRecordTests(unittest.TestCase):
    def test_every_registered_platform_has_a_complete_record(self):
        self.assertEqual(set(CAPS), set(ALL_IDS))
        required = ("levels", "public_discovery", "documented_api", "login_required", "manual_task_access",
                    "permitted_automation", "prohibited_automation", "application_submission",
                    "evidence_submission", "country_eligibility", "payment_verification",
                    "last_policy_verified", "policy_urls")
        for pid, rec in CAPS.items():
            for key in required:
                self.assertIn(key, rec, f"{pid} missing {key}")
            self.assertTrue(rec["levels"])
            self.assertTrue(set(rec["levels"]) <= set(policy.LEVELS), pid)

    def test_no_platform_permits_automated_submission_or_evidence(self):
        for pid, rec in CAPS.items():
            self.assertEqual(rec["application_submission"], "human_only", pid)
            self.assertEqual(rec["evidence_submission"], "human_only", pid)
            self.assertIn("automated_application_submission", rec["prohibited_automation"], pid)

    def test_unverified_policies_are_not_claimed_as_verified(self):
        for pid in ("atlas_capture", "clickworker", "microworkers", "rapidworkers", "sproutgigs", "handshake_ai"):
            self.assertIsNone(CAPS[pid]["last_policy_verified"], pid)
        for pid in ("opentrain_ai", "gainworkers", "mindrift", "reworkio"):
            self.assertEqual(CAPS[pid]["last_policy_verified"], "2026-11-08")
            self.assertTrue(CAPS[pid]["policy_urls"])

    def test_atlas_capture_records_the_samsung_device_incompatibility(self):
        eq = CAPS["atlas_capture"]["equipment"]
        self.assertFalse(eq["user_device_compatible"])
        self.assertIn("Samsung A05", eq["note"])
        self.assertIn("iPhone", eq["requirements"])

    def test_rapidworkers_domain_is_io_not_net(self):
        urls = " ".join(CAPS["rapidworkers"]["policy_urls"])
        self.assertIn("rapidworkers.io", urls)
        self.assertNotIn("rapidworkers.net", urls)

    def test_mindrift_is_recorded_as_kenya_unsupported(self):
        self.assertEqual(policy.kenya_status("mindrift"), "unsupported")
        self.assertIn("AUTOMATION_PROHIBITED", CAPS["mindrift"]["levels"])


class EnforcementTests(unittest.TestCase):
    def test_always_blocked_actions_are_denied_on_every_platform(self):
        for pid in ALL_IDS:
            for action in sorted(policy.ALWAYS_BLOCKED):
                self.assertFalse(policy.check_action(pid, action).allowed, f"{pid}:{action}")

    def test_user_authorization_cannot_unblock_hard_blocks(self):
        for action in ("bypass_captcha", "use_vpn_or_proxy", "scrape_logged_in", "fabricate_evidence"):
            self.assertFalse(policy.check_action("opentrain_ai", action, user_authorized=True).allowed)

    def test_automated_submission_is_blocked_everywhere_even_if_user_authorizes(self):
        for pid in ALL_IDS:
            self.assertFalse(policy.check_action(pid, "auto_submit_application", user_authorized=True).allowed, pid)

    def test_automated_submission_needs_platform_permission_and_user_authorization(self):
        permissive = {"x": {"platform_id": "x", "levels": ["HUMAN_APPROVAL_REQUIRED"],
                            "permitted_automation": ["application_submission"],
                            "application_submission": "automation_permitted", "prohibited_automation": []}}
        self.assertFalse(policy.check_action("x", "auto_submit_application", caps=permissive).allowed)
        self.assertTrue(policy.check_action("x", "auto_submit_application", caps=permissive, user_authorized=True).allowed)
        prohibited = copy.deepcopy(permissive)
        prohibited["x"]["levels"].append("AUTOMATION_PROHIBITED")
        self.assertFalse(policy.check_action("x", "auto_submit_application", caps=prohibited, user_authorized=True).allowed)

    def test_only_opentrain_may_use_public_discovery_and_api_read(self):
        allowed_discovery = [p for p in ALL_IDS if policy.check_action(p, "public_discovery").allowed]
        allowed_api = [p for p in ALL_IDS if policy.check_action(p, "api_read").allowed]
        self.assertEqual(allowed_discovery, ["opentrain_ai"])
        self.assertEqual(allowed_api, ["opentrain_ai"])

    def test_restricted_platforms_cannot_be_discovered_or_read_by_api(self):
        for pid in ("microworkers", "sproutgigs", "gainworkers", "reworkio", "mindrift"):
            self.assertFalse(policy.check_action(pid, "public_discovery").allowed, pid)
            self.assertFalse(policy.check_action(pid, "api_read").allowed, pid)

    def test_gainworkers_employer_api_cannot_be_used_for_worker_discovery(self):
        self.assertTrue(CAPS["gainworkers"]["documented_api"]["available"])        # it exists...
        self.assertFalse(policy.check_action("gainworkers", "api_read").allowed)   # ...but is not permitted
        self.assertIn("employer_api_misuse", CAPS["gainworkers"]["prohibited_automation"])

    def test_only_mindrift_may_monitor_its_policy_page(self):
        self.assertEqual([p for p in ALL_IDS if policy.check_action(p, "policy_monitor").allowed], ["mindrift"])

    def test_local_assistance_is_always_allowed_and_platform_free(self):
        for action in ("assist_application", "assist_task", "record_manual_submission", "track_status"):
            self.assertTrue(policy.check_action("mindrift", action).allowed)

    def test_unknown_platform_unknown_action_and_missing_data_fail_closed(self):
        self.assertFalse(policy.check_action("nope", "public_discovery").allowed)
        self.assertFalse(policy.check_action("opentrain_ai", "teleport").allowed)
        self.assertFalse(policy.check_action("opentrain_ai", "api_read", caps={}).allowed)
        self.assertEqual(policy.load_capabilities(Path("does-not-exist.json")), {})

    def test_require_raises_permission_denied(self):
        with self.assertRaises(policy.PermissionDenied):
            policy.require("microworkers", "public_discovery")
        policy.require("opentrain_ai", "api_read")           # permitted: no exception


class AdapterEnforcementTests(unittest.TestCase):
    def test_opentrain_scan_is_blocked_when_the_capability_is_removed(self):
        from microtask_monitor import opentrain as ot
        original = policy.load_capabilities
        try:
            stripped = copy.deepcopy(CAPS)
            stripped["opentrain_ai"]["levels"] = ["MANUAL_ONLY"]
            policy.load_capabilities = lambda path=policy.CAPABILITIES_PATH: stripped
            def must_not_fetch(url):
                raise AssertionError("network used without permission")
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(policy.PermissionDenied):
                ot.scan(must_not_fetch, cache_path=Path(tmp) / "c.json", use_fresh_cache=False)
        finally:
            policy.load_capabilities = original

    def test_mindrift_monitor_is_blocked_when_permission_is_removed(self):
        from microtask_monitor import mindrift as mi
        original = policy.load_capabilities
        try:
            stripped = copy.deepcopy(CAPS)
            stripped["mindrift"]["permitted_automation"] = []
            policy.load_capabilities = lambda path=policy.CAPABILITIES_PATH: stripped
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(policy.PermissionDenied):
                mi.scan(lambda: "x", cache_path=Path(tmp) / "c.json", force=True)
        finally:
            policy.load_capabilities = original

    def test_manual_review_adapters_cannot_reach_the_network(self):
        source = (Path(policy.__file__).parent / "manual_review.py").read_text(encoding="utf-8")
        self.assertNotIn("urllib", source)


class PersistenceTests(unittest.TestCase):
    def test_capabilities_are_mirrored_into_the_database(self):
        with tempfile.TemporaryDirectory() as tmp, db.session(Path(tmp) / "a.sqlite3") as conn:
            self.assertEqual(policy.sync_to_db(conn), 12)
            self.assertEqual(policy.sync_to_db(conn), 12)         # idempotent
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM platform_capabilities").fetchone()[0], 12)
            rec = json.loads(conn.execute("SELECT record_json FROM platform_capabilities WHERE platform_id='mindrift'").fetchone()[0])
            self.assertEqual(rec["country_eligibility"]["kenya"], "unsupported")


if __name__ == "__main__":
    unittest.main()
