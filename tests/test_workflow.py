import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import assistant, db, intelligence as ig, policy, workflow as wf

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
CAPS = policy.load_capabilities()
PROFILE = ig.load_profile()
URL = "https://www.opentrain.ai/jobs/j1"


def opp(**kw):
    base = {"platform_id": "opentrain_ai", "external_id": "j1", "source": "OpenTrain AI", "url": URL,
            "title": "Python Automation Engineer", "summary": "Build n8n workflows and Python APIs using Claude Code. "
            "Submit your GitHub portfolio.", "kenya_eligibility": "kenya_explicitly_eligible",
            "payment_display": "USD 40/hour", "payment_usd_per_hour": 40, "published": NOW.isoformat(), "status": "open"}
    base.update(kw)
    return base


class AssistantTests(unittest.TestCase):
    def test_draft_uses_only_established_skills_and_lists_gaps(self):
        o = opp(summary="Python and n8n work. PhD required, 5+ years experience. Needs an iPhone.")
        o["eligibility_class"] = ig.KENYA_CONFIRMED
        draft = assistant.draft_application(o, PROFILE)
        self.assertIn("Python", draft["matched_skills"])
        self.assertNotIn("PhD", draft["body"])
        text = " ".join(draft["missing_info"])
        self.assertIn("PhD", text)
        self.assertIn("5+ years", text)
        self.assertIn("Apple hardware", text)
        self.assertFalse(draft["blocked"])
        self.assertIn("Review and edit every placeholder.", draft["instructions"])

    def test_draft_never_claims_skills_the_user_lacks(self):
        o = opp(title="Rust Kernel Engineer", summary="Rust, C++ and Kubernetes expert needed.")
        o["eligibility_class"] = ig.KENYA_UNVERIFIED
        draft = assistant.draft_application(o, PROFILE)
        self.assertEqual(draft["matched_skills"], [])
        self.assertNotIn("Relevant skills and tools", draft["body"])      # no skills section: nothing is claimed
        self.assertTrue(any("No established skill" in m for m in draft["missing_info"]))

    def test_unsupported_and_expired_drafts_are_blocked(self):
        for cls in (ig.KENYA_UNSUPPORTED, ig.EXPIRED):
            o = opp()
            o["eligibility_class"] = cls
            d = assistant.draft_application(o, PROFILE)
            self.assertTrue(d["blocked"], cls)
            self.assertIn("Do not apply", d["eligibility_note"])

    def test_unverified_eligibility_is_flagged_in_the_draft_note(self):
        o = opp()
        o["eligibility_class"] = ig.KENYA_UNVERIFIED
        self.assertIn("NOT confirmed", assistant.draft_application(o, PROFILE)["eligibility_note"])

    def test_polish_guard_rejects_invented_numbers_links_and_bloat(self):
        original = "I work with Python and n8n."
        self.assertIsNone(assistant.guard_polish(original, "I have 10 years with Python and n8n."))
        self.assertIsNone(assistant.guard_polish(original, "I work with Python. See https://fake.example"))
        self.assertIsNone(assistant.guard_polish(original, original * 5))
        self.assertIsNone(assistant.guard_polish(original, ""))
        self.assertEqual(assistant.guard_polish(original, "I use Python and n8n."), "I use Python and n8n.")

    def test_provider_polish_is_applied_only_when_safe(self):
        class Good:
            name = "fake"
            def generate(self, prompt): return None
        o = opp(); o["eligibility_class"] = ig.KENYA_CONFIRMED
        base = assistant.draft_application(o, PROFILE, Good())

        class Polisher:
            name = "fake"
            def __init__(self, text): self.text = text
            def generate(self, prompt): return self.text
        evil = assistant.draft_application(o, PROFILE, Polisher(base["body"] + " I have 12 years of experience."))
        self.assertNotIn("12 years", evil["body"])
        safe = assistant.draft_application(o, PROFILE, Polisher(base["body"].replace("Hello,", "Hi there,")))
        self.assertIn("Hi there,", safe["body"])
        self.assertTrue(safe["provider"].endswith("+polish"))

    def test_default_provider_is_rules_and_needs_no_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
            self.assertEqual(assistant.get_provider().name, "rules")
            o = opp(); o["eligibility_class"] = ig.KENYA_CONFIRMED
            assistant.draft_application(o, PROFILE)

    def test_ollama_provider_only_accepts_loopback_and_degrades_gracefully(self):
        with self.assertRaises(ValueError):
            assistant.OllamaProvider(url="https://api.example.com")
        provider = assistant.OllamaProvider(url="http://127.0.0.1:9", timeout=1)   # nothing listens here
        self.assertIsNone(provider.generate("hi"))

    def test_simplify_instructions_extracts_steps_rules_evidence_and_deadline(self):
        text = ("1. Open the link and read the article.\n2. Write a 150 words summary.\n3. Upload a screenshot of your answer.\n"
                "You must submit within 24 hours. Do not use AI-generated text. You need a laptop and an account.")
        g = assistant.simplify_instructions(text)
        self.assertEqual(len(g["steps"]), 3)
        self.assertTrue(any("Do not use AI" in r for r in g["rules"]))
        self.assertTrue(any("screenshot" in e for e in g["evidence_requirements"]))
        self.assertTrue(any("within 24 hours" in d for d in g["deadlines"]))
        self.assertIn("a laptop/computer", g["materials"])
        self.assertTrue(any("use AI-generated text" in m for m in g["common_mistakes"]))
        self.assertTrue(any("150 words" in q for q in g["quality_checks"]))
        self.assertFalse(g["needs_human_reading"])

    def test_unstructured_instructions_ask_for_careful_reading_rather_than_inventing_steps(self):
        g = assistant.simplify_instructions("Hello there.")
        self.assertTrue(g["needs_human_reading"])
        self.assertEqual(g["steps"], [])
        self.assertIn("Read the original instructions", assistant.generate_checklist(g)[0]["text"])

    def test_error_explanations_and_work_plan(self):
        self.assertIn("slow or interrupted", assistant.explain_error("The read operation timed out"))
        self.assertIn("authentication", assistant.explain_error("HTTP 401: Unauthorized"))
        self.assertIn("Unrecognised", assistant.explain_error("weird"))
        plan = assistant.plan_work([{"id": 1, "status": "accepted", "due_at": "2026-12-01", "open_items": 2},
                                    {"id": 2, "status": "in_progress", "due_at": "2026-11-20", "open_items": 5},
                                    {"id": 3, "status": "paid", "due_at": "2026-11-01", "open_items": 0}])
        self.assertEqual([t["id"] for t in plan], [2, 1])


class WorkflowBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ctx = db.session(Path(self.tmp.name) / "a.sqlite3")
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.evidence_dir = Path(self.tmp.name) / "evidence"

    def add_opp(self, **kw):
        with db.tx(self.conn):
            ig.ingest(self.conn, [opp(**kw)], now=NOW, caps=CAPS, profile=PROFILE)


class ApplicationTests(WorkflowBase):
    def test_create_review_approve_submit_and_respond(self):
        self.add_opp()
        app = wf.create_application(self.conn, URL)
        self.assertEqual(app["status"], "draft")
        self.assertEqual(len(app["drafts"]), 1)
        self.assertTrue(app["drafts"][0]["missing_info"])
        draft_id = app["drafts"][0]["id"]
        app = wf.edit_draft(self.conn, draft_id, "My edited application")
        self.assertEqual(app["drafts"][0]["body"], "My edited application")
        app = wf.approve_draft(self.conn, draft_id, approved_by_user=True)
        self.assertEqual(app["status"], "approved")
        app = wf.mark_submitted(self.conn, app["id"], "2026-11-08", confirmed_by_user=True)
        self.assertEqual((app["status"], app["submitted_at"]), ("submitted", "2026-11-08"))
        app = wf.record_response(self.conn, app["id"], "interview", "They replied", "2026-11-10")
        self.assertEqual((app["status"], app["response_text"]), ("interview", "They replied"))
        actions = [r[0] for r in self.conn.execute("SELECT action FROM audit_log")]
        for needed in ("application.create", "draft.approve", "application.submitted_manually", "application.response"):
            self.assertIn(needed, actions)

    def test_submission_requires_approval_and_explicit_confirmation(self):
        self.add_opp()
        app = wf.create_application(self.conn, URL)
        with self.assertRaises(wf.WorkflowError):
            wf.mark_submitted(self.conn, app["id"], confirmed_by_user=True)         # no approved draft
        wf.approve_draft(self.conn, app["drafts"][0]["id"], True)
        with self.assertRaises(wf.WorkflowError):
            wf.mark_submitted(self.conn, app["id"], confirmed_by_user=False)        # not confirmed
        with self.assertRaises(wf.WorkflowError):
            wf.approve_draft(self.conn, app["drafts"][0]["id"], approved_by_user="yes")   # must be literally True

    def test_editing_an_approved_draft_revokes_approval(self):
        self.add_opp()
        app = wf.create_application(self.conn, URL)
        did = app["drafts"][0]["id"]
        wf.approve_draft(self.conn, did, True)
        app = wf.edit_draft(self.conn, did, "changed after approval")
        self.assertEqual(app["status"], "draft")
        self.assertFalse(app["drafts"][0]["approved"])
        with self.assertRaises(wf.WorkflowError):
            wf.mark_submitted(self.conn, app["id"], confirmed_by_user=True)

    def test_unsupported_expired_and_duplicate_applications_are_refused(self):
        self.add_opp(kenya_eligibility="restricted_other_countries")
        with self.assertRaises(wf.Blocked):
            wf.create_application(self.conn, URL)
        self.add_opp(url="https://x.example/mind", external_id="m1", platform_id="mindrift")
        with self.assertRaises(wf.Blocked):
            wf.create_application(self.conn, "https://x.example/mind")
        self.add_opp(url="https://x.example/old", external_id="o1", status="expired")
        with self.assertRaises(wf.Blocked):
            wf.create_application(self.conn, "https://x.example/old")
        self.add_opp(url="https://x.example/ok", external_id="ok1")
        wf.create_application(self.conn, "https://x.example/ok")
        with self.assertRaises(wf.WorkflowError):
            wf.create_application(self.conn, "https://x.example/ok")

    def test_regenerate_creates_a_new_version_and_unknown_records_error(self):
        self.add_opp()
        app = wf.create_application(self.conn, URL)
        app = wf.regenerate_draft(self.conn, app["id"])
        self.assertEqual([d["version"] for d in app["drafts"]], [2, 1])
        with self.assertRaises(wf.WorkflowError):
            wf.get_application(self.conn, 999)
        with self.assertRaises(wf.WorkflowError):
            wf.create_application(self.conn, "https://nope.example/")

    def test_response_before_submission_is_rejected(self):
        self.add_opp()
        app = wf.create_application(self.conn, URL)
        with self.assertRaises(wf.WorkflowError):
            wf.record_response(self.conn, app["id"], "accepted")


class TaskTests(WorkflowBase):
    TEXT = "1. Read the brief.\n2. Write a 100 words answer.\n3. Upload a screenshot.\nDo not copy text. Submit within 2 hours."

    def test_task_gets_guidance_checklist_and_tracks_progress(self):
        t = wf.create_task(self.conn, "Review answers", self.TEXT, platform_id="opentrain_ai", due_at="2026-11-20")
        self.assertEqual(t["status"], "accepted")
        self.assertTrue(t["simplified"])
        kinds = {i["kind"] for i in t["checklist"]}
        self.assertTrue({"step", "evidence", "quality"} <= kinds)
        first = t["checklist"][0]["id"]
        t = wf.toggle_item(self.conn, first, True)
        self.assertEqual(t["open_items"], len(t["checklist"]) - 1)
        self.assertIsNotNone(t["checklist"][0]["done_at"])
        t = wf.toggle_item(self.conn, first, False)
        self.assertEqual(t["open_items"], len(t["checklist"]))

    def test_work_sessions_record_time_and_prevent_overlap(self):
        t = wf.create_task(self.conn, "x", "Read it.")
        t = wf.start_session(self.conn, t["id"])
        self.assertEqual(t["status"], "in_progress")
        with self.assertRaises(wf.WorkflowError):
            wf.start_session(self.conn, t["id"])
        t = wf.stop_session(self.conn, t["id"], "done reading")
        self.assertIsNotNone(t["sessions"][0]["ended_at"])
        self.assertGreaterEqual(t["minutes_worked"], 0)
        with self.assertRaises(wf.WorkflowError):
            wf.stop_session(self.conn, t["id"])

    def test_evidence_is_associated_hashed_and_stored_safely(self):
        t = wf.create_task(self.conn, "x", "Read it.")
        t = wf.add_evidence(self.conn, t["id"], "screenshot", "final answer", "../../evil name.png", b"\x89PNG data",
                            evidence_dir=self.evidence_dir)
        ev = t["evidence"][0]
        self.assertEqual(len(ev["sha256"]), 64)
        self.assertEqual(ev["size_bytes"], 9)
        self.assertNotIn("..", ev["file_name"])
        stored = list((self.evidence_dir / f"task-{t['id']}").iterdir())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].read_bytes(), b"\x89PNG data")
        self.assertEqual(stored[0].resolve().parent, (self.evidence_dir / f"task-{t['id']}").resolve())

    def test_evidence_validation(self):
        t = wf.create_task(self.conn, "x", "Read it.")
        with self.assertRaises(wf.WorkflowError):
            wf.add_evidence(self.conn, t["id"])
        with self.assertRaises(wf.WorkflowError):
            wf.add_evidence(self.conn, t["id"], content=b"x" * (wf.MAX_EVIDENCE_BYTES + 1), evidence_dir=self.evidence_dir)
        with self.assertRaises(wf.WorkflowError):
            wf.add_evidence(self.conn, 999, description="x")
        wf.add_evidence(self.conn, t["id"], "note", "just a note")
        self.assertEqual(len(wf.get_task(self.conn, t["id"])["evidence"]), 1)

    def test_submission_needs_confirmation_and_moves_status(self):
        t = wf.create_task(self.conn, "x", "Read it.")
        with self.assertRaises(wf.WorkflowError):
            wf.record_submission(self.conn, t["id"], confirmed_by_user=False)
        with self.assertRaises(wf.WorkflowError):
            wf.set_task_status(self.conn, t["id"], "submitted")          # cannot bypass the confirmation
        t = wf.record_submission(self.conn, t["id"], True, "REF-1", submitted_at="2026-11-09")
        self.assertEqual(t["status"], "submitted")
        self.assertEqual(t["submissions"][0]["method"], "manual")
        self.assertEqual(t["submissions"][0]["platform_reference"], "REF-1")

    def test_status_flow_is_enforced(self):
        t = wf.create_task(self.conn, "x", "Read it.")
        with self.assertRaises(wf.WorkflowError):
            wf.set_task_status(self.conn, t["id"], "paid")
        t = wf.set_task_status(self.conn, t["id"], "completed")
        t = wf.record_submission(self.conn, t["id"], True)
        t = wf.set_task_status(self.conn, t["id"], "approved")
        t = wf.set_task_status(self.conn, t["id"], "paid")
        self.assertEqual(t["status"], "paid")

    def test_task_can_be_linked_to_an_application(self):
        self.add_opp()
        app = wf.create_application(self.conn, URL)
        t = wf.create_task(self.conn, "Job work", "Read it.", application_id=app["id"])
        self.assertEqual((t["application_id"], t["platform_id"], t["opportunity_url"]), (app["id"], "opentrain_ai", URL))
        self.assertEqual(len(wf.list_tasks(self.conn)), 1)
        with self.assertRaises(wf.WorkflowError):
            wf.create_task(self.conn, "  ")


class EarningsTests(WorkflowBase):
    def test_expected_pending_and_received_are_kept_separate(self):
        wf.add_earning(self.conn, 100, "usd", "expected", platform_id="opentrain_ai")
        wf.add_earning(self.conn, 50, "USD", "pending")
        wf.add_earning(self.conn, 2000, "KES", "expected")
        wf.record_payment(self.conn, 30, "USD", "2026-11-01", platform_id="opentrain_ai")
        s = wf.earnings_summary(self.conn)["by_currency"]
        self.assertEqual(s["USD"], {"expected": 100.0, "pending": 50.0, "received": 30.0})
        self.assertEqual(s["KES"], {"expected": 2000.0, "pending": 0.0, "received": 0.0})

    def test_advertised_pay_can_never_be_recorded_as_received(self):
        for status in ("received", "paid", "bogus"):
            with self.assertRaises(wf.WorkflowError):
                wf.add_earning(self.conn, 10, "USD", status)
        self.assertEqual(wf.earnings_summary(self.conn)["by_currency"], {})

    def test_payment_marks_the_earning_received_only_when_fully_paid(self):
        e = wf.add_earning(self.conn, 100, "USD", "pending")
        wf.record_payment(self.conn, 40, "USD", "2026-11-02", earning_id=e["id"])
        s = wf.earnings_summary(self.conn)["by_currency"]["USD"]
        self.assertEqual((s["pending"], s["received"]), (60.0, 40.0))
        self.assertEqual(wf.list_earnings(self.conn)["earnings"][0]["status"], "pending")
        wf.record_payment(self.conn, 60, "USD", "2026-11-03", earning_id=e["id"])
        s = wf.earnings_summary(self.conn)["by_currency"]["USD"]
        self.assertEqual((s["pending"], s["received"]), (0.0, 100.0))
        self.assertEqual(wf.list_earnings(self.conn)["earnings"][0]["status"], "received")

    def test_payment_validation(self):
        e = wf.add_earning(self.conn, 10, "USD", "pending")
        future = (date.today() + timedelta(days=3)).isoformat()
        for kwargs in (dict(amount=0, currency="USD", received_date="2026-11-01"),
                       dict(amount=-5, currency="USD", received_date="2026-11-01"),
                       dict(amount="abc", currency="USD", received_date="2026-11-01"),
                       dict(amount=5, currency="US", received_date="2026-11-01"),
                       dict(amount=5, currency="USD", received_date="not-a-date"),
                       dict(amount=5, currency="USD", received_date=future),
                       dict(amount=5, currency="KES", received_date="2026-11-01", earning_id=e["id"]),
                       dict(amount=5, currency="USD", received_date="2026-11-01", earning_id=999)):
            with self.assertRaises(wf.WorkflowError, msg=str(kwargs)):
                wf.record_payment(self.conn, **kwargs)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)

    def test_earning_validation(self):
        for amount, currency in ((-1, "USD"), ("x", "USD"), (float("nan"), "USD"), (5, "dollars"), (5, "")):
            with self.assertRaises(wf.WorkflowError, msg=f"{amount}{currency}"):
                wf.add_earning(self.conn, amount, currency)

    def test_dashboard_never_counts_opportunity_advertised_pay_as_earnings(self):
        self.add_opp()                                   # advertised USD 40/hour lives only on the opportunity
        self.assertEqual(wf.earnings_summary(self.conn)["by_currency"], {})


if __name__ == "__main__":
    unittest.main()
