"""Milestone 1, the Platform Access & Execution Engine: capability profiles and evidence, the four-part gate, the standard
adapter interface for all 25 platforms, email discovery (.eml folder and a read-only inbox), the eight categories and
ranking preferences, the research worker, submission outcomes A/B/C, public states, ledger fields, endpoints and the
dashboard.

Temporary databases, fixture emails and fake IMAP connections only: no account, network call or AI tool is used.
"""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import (capabilities as capmod, control, db, email_discovery as ed, engine_api, ledger, lifecycle, orchestrator, permissions,
                               platform_adapters as pa, policy, qa, task_intel)

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples" / "email"
TODAY = date.today()


def eml(sender: str, subject: str, body: str, ctype: str = "text/plain") -> bytes:
    return (f"From: {sender}\r\nTo: you@example.com\r\nSubject: {subject}\r\nDate: Mon, 16 Nov 2026 08:00:00 +0000\r\nMIME-Version: 1.0\r\n"
            f"Content-Type: {ctype}; charset=utf-8\r\n\r\n{body}\r\n").encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ctx = db.session(Path(self.tmp.name) / "a.sqlite3")
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def evidence(self, entries):
        p = Path(self.tmp.name) / "evidence.json"
        p.write_text(json.dumps({"evidence": entries}), encoding="utf-8")
        patcher = mock.patch.object(permissions, "EVIDENCE_PATH", p)
        patcher.start()
        self.addCleanup(patcher.stop)


def grant(pid="opentrain_ai", capability="ai_execution", **kw):
    e = {"platform_id": pid, "capability": capability, "state": "allowed", "source_url": "https://www.opentrain.ai/terms", "source_type": "platform_terms",
         "clause": "Simulated clause for tests", "verified_at": TODAY.isoformat(), "expires_at": (TODAY + timedelta(days=90)).isoformat(), "notes": "test"}
    e.update(kw)
    return e


# --------------------------------------------------------------------------------------------- capabilities and evidence
class CapabilityTests(Base):
    def test_every_platform_has_all_eleven_capabilities_in_three_states(self):
        profiles = capmod.all_profiles()
        self.assertEqual(len(profiles), 25)
        for p in profiles:
            self.assertEqual(tuple(p["capabilities"]), capmod.CAPABILITIES)
            self.assertTrue(set(p["capabilities"].values()) <= set(capmod.STATES), p["platform_id"])
            self.assertTrue(all(p["reasons"][c] for c in capmod.CAPABILITIES), p["platform_id"])
            self.assertEqual(set(p["permission_evidence"]), {"source_url", "source_type", "verified_at", "expires_at", "notes"})

    def test_automatic_submission_stays_human_required_on_all_25(self):
        for p in capmod.all_profiles():
            self.assertEqual(p["capabilities"]["automatic_submission"], "prohibited", p["platform_id"])
            self.assertEqual(p["submission_mode"], "human_required", p["platform_id"])

    def test_unverified_fails_closed_in_the_gate(self):
        g = capmod.check(self.conn, "clickworker", "ai_execution", {"title": "x", "instructions": "y" * 30, "category": "data_processing"})
        self.assertFalse(g["allowed"])
        self.assertFalse(g["checks"]["platform"])
        self.assertTrue(any(r.startswith("platform:") for r in g["reasons"]))

    def test_unknown_capability_and_platform_fail_closed(self):
        self.assertFalse(capmod.check(self.conn, "opentrain_ai", "teleport")["allowed"])
        self.assertFalse(capmod.check(self.conn, "not_a_platform", "public_discovery")["allowed"])

    def test_gate_reports_four_separate_checks(self):
        g = capmod.check(self.conn, "opentrain_ai", "ai_execution", {"title": "t", "instructions": "i" * 30, "category": "data_processing"})
        self.assertEqual(set(g["checks"]), {"platform", "assignment", "authorized", "limits"})

    def test_global_stop_fails_the_limits_check(self):
        control.engage_stop(self.conn, "test")
        g = capmod.check(self.conn, "opentrain_ai", "public_discovery")
        self.assertFalse(g["checks"]["limits"])


class EvidenceTests(Base):
    def test_valid_grant_permits_execution(self):
        self.evidence([grant()])
        self.assertEqual(capmod.profile("opentrain_ai")["capabilities"]["ai_execution"], "allowed")

    def test_grants_with_problems_are_ignored(self):
        bad = [grant(source_url="http://insecure.example/terms"), grant(source_type="a blog post"), grant(clause=""),
               grant(verified_at=(TODAY + timedelta(days=3)).isoformat()), grant(expires_at=(TODAY - timedelta(days=1)).isoformat()),
               grant(expires_at=None)]
        for e in bad:
            self.assertTrue(permissions.evidence_problems(e), e)
        self.evidence(bad)
        self.assertNotEqual(capmod.profile("opentrain_ai")["capabilities"]["ai_execution"], "allowed")

    def test_prohibition_always_wins(self):
        self.evidence([grant(), grant(state="prohibited", source_url=None, clause="")])
        self.assertEqual(capmod.profile("opentrain_ai")["capabilities"]["ai_execution"], "prohibited")

    def test_submission_grant_without_execution_does_not_enable_auto_submission(self):
        self.evidence([grant(capability="automatic_submission")])
        p = capmod.profile("opentrain_ai")
        self.assertEqual(p["capabilities"]["automatic_submission"], "prohibited")
        self.assertEqual(p["submission_mode"], "human_required")

    def test_shipped_evidence_file_grants_nothing(self):
        self.assertEqual(permissions.load_evidence(), [])


# --------------------------------------------------------------------------------------------- adapters
class AdapterTests(Base):
    METHODS = ("get_platform_info", "check_connection", "discover_opportunities", "retrieve_opportunity", "classify_opportunity", "prepare_task",
               "execute_task", "verify_result", "prepare_submission", "submit_result", "retrieve_earnings")

    def test_every_platform_has_the_standard_interface(self):
        ads = pa.all_adapters(self.conn)
        self.assertEqual(len(ads), 25)
        for ad in ads:
            for m in self.METHODS:
                self.assertTrue(callable(getattr(ad, m)), f"{ad.platform_id}.{m}")
            for r in (ad.get_platform_info(), ad.check_connection(), ad.retrieve_earnings(), ad.retrieve_opportunity("nope")):
                self.assertTrue({"supported", "ok", "reason"} <= set(r), ad.platform_id)

    def test_manual_platforms_say_discovery_is_not_automated(self):
        r = pa.adapter(self.conn, "clickworker").discover_opportunities()
        self.assertFalse(r["supported"])
        self.assertIn("import", r["reason"])

    def test_unknown_platform_is_refused(self):
        with self.assertRaises(policy.PermissionDenied):
            pa.adapter(self.conn, "made_up_platform")

    def test_control_centre_lists_25_from_real_records(self):
        rows = pa.control_centre(self.conn)
        self.assertEqual(len(rows), 25)
        self.assertTrue(all(r["submission_mode"] == "human_required" for r in rows))

    def _import(self, pid, **row):
        from microtask_monitor import discovery
        base = {"title": "Clean a customer CSV", "instructions": "Clean the attached customers.csv: trim whitespace and remove duplicate emails. Deliver customers_clean.csv."}
        return discovery.ManualImportAdapter(pid).import_records(self.conn, [{**base, **row}])["assignment_ids"][0]

    def test_blocked_task_gets_outcome_c_and_never_submits(self):
        aid = self._import("whop_workforce", allowed_countries=["US"])
        ad = pa.adapter(self.conn, "whop_workforce")
        self.assertEqual(ad.prepare_submission(aid)["outcome"], "C")
        r = ad.submit_result(aid)
        self.assertFalse(r["ok"])
        self.assertEqual(r["outcome"], "C")

    def test_unverified_execution_is_outcome_c_not_b(self):
        aid = self._import("opentrain_ai")
        self.assertEqual(pa.adapter(self.conn, "opentrain_ai").prepare_submission(aid)["outcome"], "C")

    def test_execute_refused_without_permission(self):
        aid = self._import("opentrain_ai")
        r = pa.adapter(self.conn, "opentrain_ai").execute_task(aid)
        self.assertFalse(r["ok"])
        self.assertFalse(r["checks"]["platform"])


# --------------------------------------------------------------------------------------------- email discovery
class EmailParseTests(Base):
    def test_samples(self):
        facts = {p.name: ed.parse(p.read_bytes()) for p in SAMPLES.glob("*.eml")}
        ot = facts["01-opentrain-new-job.eml"]
        self.assertEqual((ot["platform_id"], ot["reward"], ot["currency"], ot["deadline"]), ("opentrain_ai", 45.0, "USD", "2026-11-30"))
        self.assertEqual(ot["url"], "https://www.opentrain.ai/jobs/sample-python-data-cleaning")       # the unsubscribe link is never chosen
        self.assertEqual(ot["availability"], "3 spots left")
        self.assertEqual(ot["kenya"], "worldwide")
        self.assertIn("python", ot["skills"])
        wh = facts["02-whop-bounty.eml"]
        self.assertEqual((wh["platform_id"], wh["reward"], wh["allowed_countries"]), ("whop_workforce", 15.0, ["CA", "US"]))
        self.assertEqual(wh["url"], "https://whop.com/bounties/sample-product-descriptions")
        self.assertIsNone(facts["04-newsletter-not-a-platform.eml"])

    def test_platform_found_from_link_when_sender_is_generic(self):
        f = ed.parse(eml("Alerts <alerts@mailer.example.net>", "Fwd: New task", "See https://www.opentrain.ai/jobs/abc for details"))
        self.assertEqual(f["platform_id"], "opentrain_ai")
        self.assertEqual(f["title"], "New task")

    def test_lookalike_domain_is_not_a_platform(self):
        self.assertIsNone(ed.parse(eml("x@opentrain.ai.evil.example", "New job", "https://opentrain.ai.evil.example/jobs/1")))

    def test_oversized_message_ignored(self):
        self.assertIsNone(ed.parse(b"x" * (ed.MAX_BYTES + 1)))

    def test_html_scripts_are_dropped(self):
        f = ed.parse(eml("Whop <no-reply@whop.com>", "Bounty", "<script>alert(1)</script><p>Reward $5</p>", "text/html"))
        self.assertNotIn("alert", f["instructions"])


class EmailImportTests(Base):
    def test_import_is_idempotent_and_runs_every_rule(self):
        raws = ed.EmlFolder(SAMPLES).fetch()
        r1 = ed.import_messages(self.conn, raws, "eml_folder")
        self.assertEqual((r1["messages"], r1["imported"], r1["not_platform"]), (4, 2, 1))
        self.assertTrue(any(s.startswith("prolific:") for s in r1["skipped"]))            # platform listed as unsupported for Kenya
        r2 = ed.import_messages(self.conn, raws, "eml_folder")
        self.assertEqual((r2["imported"], r2["duplicates"]), (0, 4))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0], 2)
        whop = lifecycle.view(self.conn, self.conn.execute("SELECT id FROM assignments WHERE platform_id='whop_workforce'").fetchone()[0])
        self.assertEqual(whop["intel"]["automation_class"], "NOT_FEASIBLE")                # US/CA only
        self.assertEqual(whop["intel"]["extracted"]["category8"], 6)
        self.assertIn("Imported from a notification email", whop["instructions"])
        self.assertIn("eml_folder", ed.health(self.conn))

    def test_missing_folder_is_empty(self):
        self.assertEqual(ed.EmlFolder(Path(self.tmp.name) / "none").fetch(), [])


class FakeImap:
    def __init__(self, messages):
        self.messages, self.calls = messages, []

    def login(self, user, pw):
        self.calls.append(("login", user))

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.calls.append(("search",) + criteria)
        dom = criteria[1].strip('"').lstrip("@.")
        ids = [str(i + 1).encode() for i, (d, _) in enumerate(self.messages) if d == dom]
        return "OK", [b" ".join(ids)]

    def fetch(self, num, what):
        self.calls.append(("fetch", what))
        return "OK", [(b"1 (BODY[] {n}", self.messages[int(num) - 1][1])]

    def logout(self):
        self.calls.append(("logout",))

    def __getattr__(self, name):                                   # store, expunge, copy, move, append: never allowed
        raise AssertionError(f"IMAP {name} must never be called")


class ImapTests(Base):
    def test_inbox_is_read_only(self):
        msg = (SAMPLES / "01-opentrain-new-job.eml").read_bytes()
        fake = FakeImap([("opentrain.ai", msg)])
        got = ed.ImapInbox("me@example.com", "app-password", connect=lambda: fake).fetch()
        self.assertEqual(got, [msg])
        self.assertIn(("select", "INBOX", True), fake.calls)
        self.assertTrue(all(c[1] == "(BODY.PEEK[])" for c in fake.calls if c[0] == "fetch"))
        self.assertEqual(fake.calls[-1], ("logout",))

    def test_logout_even_on_error(self):
        fake = FakeImap([])
        fake.search = mock.Mock(side_effect=OSError("network"))
        with self.assertRaises(OSError):
            ed.ImapInbox("me@example.com", "pw", connect=lambda: fake).fetch()
        self.assertEqual(fake.calls[-1], ("logout",))

    def test_monitor_is_off_by_default(self):
        self.assertFalse(control.DEFAULTS["email_monitor_enabled"])


# --------------------------------------------------------------------------------------------- classification and ranking
class Category8Tests(Base):
    def _x(self, **kw):
        x = {"country_restrictions": {"allowed_countries": None, "stated_in_task": None}, "upfront_cost_flags": [], "hard_restrictions": []}
        x.update(kw)
        return x

    def test_mapping(self):
        s, a = dict(control.DEFAULTS), {"platform_id": "opentrain_ai"}
        cases = {"HUMAN_REQUIRED": 4, "PLATFORM_AUTOMATION_PROHIBITED": 5, "AI_PROHIBITED": 5, "ELIGIBILITY_UNVERIFIED": 7, "MANUAL_REVIEW_REQUIRED": 7,
                 "AI_ASSISTED_WITH_PERMISSION": 3, "DETERMINISTIC_LOCAL_PROCESSING": 3}
        for cls, want in cases.items():
            self.assertEqual(task_intel.category8(a, cls, self._x(), {}, s)[0], want, cls)
        self.assertEqual(task_intel.category8(a, "NOT_FEASIBLE", self._x(country_restrictions={"allowed_countries": ["US"], "stated_in_task": None}), {}, s)[0], 6)
        self.assertEqual(task_intel.category8(a, "NOT_FEASIBLE", self._x(upfront_cost_flags=["deposit"]), {}, s)[0], 8)
        self.assertEqual(task_intel.category8(a, "NOT_FEASIBLE", self._x(hard_restrictions=[{"class": "NOT_FEASIBLE"}]), {}, s)[0], 5)
        self.assertEqual(task_intel.category8({"platform_id": "prolific"}, "NOT_FEASIBLE", self._x(), {}, s)[0], 6)

    def test_economics_demotes_to_8(self):
        s = {**control.DEFAULTS, "min_reward_usd": 10.0}
        cat, why = task_intel.category8({"platform_id": "opentrain_ai"}, "AI_ASSISTED_WITH_PERMISSION", self._x(), {"advertised": 2.0, "currency": "USD"}, s)
        self.assertEqual(cat, 8)
        self.assertIn("minimum reward", why)
        s = {**control.DEFAULTS, "min_hourly_rate_usd": 20.0}
        self.assertEqual(task_intel.category8({"platform_id": "opentrain_ai"}, "MANUAL_REVIEW_REQUIRED", self._x(), {"hourly_comparable_usd": 5.0}, s)[0], 8)

    def test_preferences_raise_the_score(self):
        from microtask_monitor import discovery
        row = {"title": "Clean a CSV", "instructions": "Clean the attached customers.csv: trim whitespace and remove duplicate emails.", "reward": 20, "currency": "USD"}
        aid = discovery.ManualImportAdapter("opentrain_ai").import_records(self.conn, [row])["assignment_ids"][0]
        before = task_intel.load(self.conn, aid)["score"]
        control.update_settings(self.conn, {"preferred_platforms": ["opentrain_ai"], "preferred_categories": ["data_processing"]})
        after = task_intel.analyse(self.conn, lifecycle.view(self.conn, aid))
        self.assertEqual(after["score_parts"]["preferred_platform"], 3.0)
        self.assertEqual(after["score_parts"]["preferred_category"], 3.0)
        self.assertGreater(after["score"], before)

    def test_new_settings_are_validated(self):
        for bad in ({"preferred_platforms": ["nope"]}, {"preferred_categories": ["flying"]}, {"min_reward_usd": -1}, {"max_daily_tasks": 10**6}):
            with self.assertRaises(control.ControlError):
                control.update_settings(self.conn, bad)
        s = control.update_settings(self.conn, {"min_reward_usd": 5, "max_daily_tasks": 3})
        self.assertEqual((s["min_reward_usd"], s["max_daily_tasks"]), (5.0, 3))

    def test_authorize_enforces_min_reward_and_daily_task_limit(self):
        control.update_settings(self.conn, {"min_reward_usd": 50, "max_daily_tasks": 0})
        a = {"id": 1, "platform_id": "opentrain_ai", "title": "t", "instructions": "i", "ai_use": "unknown", "stage": "accepted",
             "intel": {"automation_class": "DETERMINISTIC_LOCAL_PROCESSING", "category": "data_processing", "economics": {"advertised": 10.0, "currency": "USD", "estimated_minutes": 5}}}
        reasons = orchestrator.authorize(self.conn, a, probe={"free_disk_mb": 10**6, "free_memory_mb": 10**6})["reasons"]
        self.assertTrue(any("minimum reward" in r for r in reasons))
        self.assertTrue(any("daily task limit" in r for r in reasons))


# --------------------------------------------------------------------------------------------- research worker
class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "output"
        self.out.mkdir()
        self.paths = {"output": self.out, "input": Path(self.tmp.name) / "input", "base": Path(self.tmp.name), "logs": Path(self.tmp.name) / "logs"}
        self.job = {"instructions": "Research the market for X and cite sources.", "work_type": "research"}

    def checks(self):
        return {r["name"]: r["status"] for r in qa.research_checks(self.job, self.paths)}

    def test_routes_and_worker_exist(self):
        from microtask_monitor import execution, workers
        self.assertIn("research", execution.WORK_TYPES)
        self.assertEqual(workers.select("research").name, "research specialist")
        self.assertEqual(task_intel.WORKER_FOR["research"], "research")
        self.assertIs(qa.CHECKS["research"], qa.research_checks)

    def test_sources_required(self):
        (self.out / "report.md").write_text("# Findings\n\nSomething without sources.\n", encoding="utf-8")
        c = self.checks()
        self.assertEqual((c["sources_section"], c["independent_sources"]), ("fail", "fail"))

    def test_good_report_passes_and_still_asks_for_a_spot_check(self):
        (self.out / "report.md").write_text("# Findings\n\nA claim [1]. Another [2].\n\n## Sources\n- https://www.example.org/a\n- https://example.com/b\n", encoding="utf-8")
        c = self.checks()
        self.assertEqual((c["sources_section"], c["independent_sources"]), ("pass", "pass"))
        self.assertEqual(c["sources_support_claims"], "manual")


# --------------------------------------------------------------------------------------------- states, ledger, api, dashboard
class StateLedgerApiTests(Base):
    def test_spec_states(self):
        self.assertEqual(len(lifecycle.SPEC_STATES), 17)
        f = lifecycle.spec_state
        self.assertEqual(f({"state": "DISCOVERED"}), "DISCOVERED")
        self.assertEqual(f({"state": "DISCOVERED", "intel": {"x": 1}}), "POLICY_CHECKED")
        self.assertEqual(f({"state": "EXECUTING"}, "created"), "PREPARING")
        self.assertEqual(f({"state": "EXECUTING"}, "running"), "EXECUTING")
        self.assertEqual(f({"state": "QUALITY_ASSURANCE"}, "awaiting_review"), "QA_PASSED")
        self.assertEqual(f({"state": "QUALITY_ASSURANCE"}, "qa_failed"), "QA_PENDING")
        self.assertEqual(f({"state": "PAYMENT_PENDING"}), "APPROVED")
        self.assertEqual(f({"state": "POLICY_BLOCKED"}), "BLOCKED")
        self.assertEqual(f({"state": "EXPIRED"}), "FAILED")
        for st in lifecycle.STATES:
            self.assertIn(f({"state": st}), lifecycle.SPEC_STATES)

    def test_earnings_overview_counts_only_received_money(self):
        ed.import_messages(self.conn, ed.EmlFolder(SAMPLES).fetch(), "eml_folder")
        o = ledger.earnings_overview(self.conn)
        self.assertEqual(o["received_by_currency"], {})
        self.assertIsNone(o["usd_per_hour_of_execution"])
        e = ledger.entries(self.conn)[0]
        self.assertTrue({"actual_minutes", "approved_at", "rejected_at", "rejection_reason", "estimated_minutes"} <= set(e))

    def test_endpoints(self):
        ed.import_messages(self.conn, ed.EmlFolder(SAMPLES).fetch(), "eml_folder")
        r = engine_api.route(None, self.conn, "GET", ["capabilities"], {}, {})
        self.assertEqual(len(r["platforms"]), 25)
        r = engine_api.route(None, self.conn, "GET", ["opportunities"], {}, {})
        self.assertEqual(len(r["opportunities"]), 2)
        self.assertEqual(sum(c["count"] for c in r["category_counts"]), 2)
        self.assertTrue(all(o["state"] in lifecycle.SPEC_STATES for o in r["opportunities"]))
        r = engine_api.route(None, self.conn, "GET", ["health"], {}, {})
        self.assertIn("eml_folder", r["email_discovery"])
        self.assertEqual(r["database_version"], db.user_version(self.conn))
        r = engine_api.route(None, self.conn, "GET", ["tasks", "1", "submission"], {}, {})
        self.assertTrue(r["ok"])
        self.assertIn(r["submission"]["outcome"], ("A", "B", "C"))
        with mock.patch.object(ed, "INBOX_DIR", Path(self.tmp.name) / "inbox"):
            r = engine_api.route(None, self.conn, "POST", ["email", "import"], {}, {})
        self.assertEqual(r["result"]["messages"], 0)

    def test_scheduler_has_email_job_and_respects_switches(self):
        from microtask_monitor import scheduler
        self.assertIn("email_discovery", [j.name for j in scheduler.Scheduler(Path(self.tmp.name) / "a.sqlite3").default_jobs()])
        control.update_settings(self.conn, {"email_import_enabled": False, "email_monitor_enabled": False})
        self.assertEqual(scheduler._email_discovery(None, self.conn, None), "switched off")

    def test_dashboard_has_the_engine_sections(self):
        dash = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        for needle in ('data-tab="access"', "A &middot; Platform Control Centre", "B &middot; Opportunity Intelligence", "C &middot; Autonomous Execution",
                       "D &middot; Submission Centre", "E &middot; Earnings", "F &middot; System Health", "/api/engine/capabilities", "/api/engine/health"):
            self.assertIn(needle, dash)
        self.assertNotIn("<script src=", dash.lower())


class EndToEndTests(Base):
    """A permitted test task through the standard adapter interface only: evidence → classify → prepare → (you take it) → execute → QA → outcome B."""
    SAMPLE = (b"name,email,country,notes\n  alice  ,Alice@Example.com,ke,invoice\nBOB,bob@example.com,UG,refund\nalice,alice@example.com,KE,dup\n")
    SPEC = {"input": "customers.csv", "output": "customers_clean.csv", "columns": ["name", "email", "country"], "required": ["email"], "dedupe_on": ["email"],
            "case": {"email": "lower", "name": "title", "country": "upper"}}

    def test_permitted_task_runs_end_to_end_and_stops_at_your_submission(self):
        from microtask_monitor import assignments as asg, discovery
        self.evidence([grant(capability="ai_execution")])                        # simulated platform permission, with source and expiry
        execs = Path(self.tmp.name) / "executions"
        row = {"title": "Clean a customer CSV", "id": "e2e-1", "allowed_countries": ["KE"], "reward": 5, "currency": "USD", "deterministic_spec": True,
               "instructions": "Clean the attached customers.csv: trim whitespace, normalise names, remove duplicate emails. Deliver customers_clean.csv."}
        aid = discovery.ManualImportAdapter("opentrain_ai").import_records(self.conn, [row])["assignment_ids"][0]
        ad = pa.adapter(self.conn, "opentrain_ai")
        c = ad.classify_opportunity(aid)
        self.assertEqual((c["automation_class"], c["category8"]), ("DETERMINISTIC_LOCAL_PROCESSING", 3))
        self.assertTrue(ad.prepare_task(aid, {"customers.csv": self.SAMPLE, "spec.json": json.dumps(self.SPEC).encode()}, execs)["ok"])
        control.update_settings(self.conn, {"auto_execute": True, "auto_approve_clean_qa": True})
        probe = {"disk_mb": 50000, "memory_mb": 8000}
        refused = ad.execute_task(aid, None, Path(self.tmp.name) / "a.sqlite3", execs, probe=probe)
        self.assertFalse(refused["ok"])                                         # the engine never takes a task on the platform for you
        self.assertIn("take it yourself", refused["reason"])
        asg.advance(self.conn, aid, "accepted", "user_confirmation", "Taken on the platform (test)", confirmed_by_user=True)
        run = ad.execute_task(aid, None, Path(self.tmp.name) / "a.sqlite3", execs, probe=probe)
        self.assertTrue(run["ok"], run["reason"])
        self.assertTrue(ad.verify_result(aid)["ok"])
        sub = ad.prepare_submission(aid)
        self.assertEqual(sub["outcome"], "B")                                   # submission stays with you on every platform
        self.assertTrue(sub["ok"])
        self.assertFalse(ad.submit_result(aid)["ok"])
        a = lifecycle.view(self.conn, aid)
        self.assertEqual(lifecycle.spec_state(a), "SUBMISSION_READY")
        from microtask_monitor import execution
        out = Path(execution.get_job(self.conn, a["execution_job_id"])["workspace"]) / "output"
        self.assertEqual((out / "customers_clean.csv").read_text(encoding="utf-8").count("\n"), 3)   # header + alice + bob (duplicate removed)


class SourceRuleTests(unittest.TestCase):
    def test_new_modules_have_no_web_client_browser_automation_or_cookie_access(self):
        for name in ("capabilities", "platform_adapters", "email_discovery"):
            src = (ROOT / "microtask_monitor" / f"{name}.py").read_text(encoding="utf-8")
            for banned in ("urllib.request", "import requests", "http.client", "selenium", "playwright", "webdriver", "Cookies", "smtplib", ".store(", "expunge"):
                self.assertNotIn(banned, src, f"{name} must not contain {banned}")

    def test_imap_is_read_only(self):
        src = (ROOT / "microtask_monitor" / "email_discovery.py").read_text(encoding="utf-8")
        self.assertIn('readonly=True', src)
        self.assertIn("BODY.PEEK[]", src)
        self.assertNotIn('"(RFC822)"', src)


if __name__ == "__main__":
    unittest.main()
