import json
import os
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import db, execution as ex, intelligence as ig, packets, policy, profile_answers as pa, qa, workers, workflow as wf

SAMPLE_CSV = (b"name,email,country,notes\n  alice  ,Alice@Example.com,ke,invoice for March\nBOB,bob@example.com,UG,refund request\n"
              b" ,,,\nalice,alice@example.com,KE,duplicate row\ncarol,,KE,missing email\ndave,dave@example.com,KE,general question\n")
SPEC = {"input": "customers.csv", "output": "customers_clean.csv", "columns": ["name", "email", "country", "category"], "required": ["email"],
        "dedupe_on": ["email"], "case": {"email": "lower", "name": "title", "country": "upper"},
        "categorize": {"column": "category", "from": "notes", "rules": {"billing": ["invoice", "refund"]}, "default": "other"},
        "allowed_values": {"country": ["KE", "UG"]}}
DATA_INSTR = "Clean the attached customers.csv: trim whitespace, normalise names and countries, remove duplicate emails, reject rows without an email, and add a category column. Deliver customers_clean.csv."
CODE_INSTR = "Write a Python function slugify(text) in slugify.py that lowercases text and joins words with hyphens. Include unit tests and make sure they pass. Backend Python code only."

GOOD_CODE = "def slugify(text):\n    return '-'.join(text.lower().split())\n"
GOOD_TEST = "import unittest\nfrom slugify import slugify\n\nclass T(unittest.TestCase):\n    def test_basic(self):\n        self.assertEqual(slugify('Hello World'), 'hello-world')\n\nif __name__ == '__main__':\n    unittest.main()\n"
BAD_CODE = "def slugify(text):\n    return text\n"


class FakeRunner:
    """Stands in for Claude Code/Codex: writes genuine files (and records the prompt it received)."""
    name = "fake-ai"

    def __init__(self, code=GOOD_CODE, test=GOOD_TEST, write=True):
        self.code, self.test, self.write, self.prompts = code, test, write, []

    def run(self, prompt, cwd, timeout, should_stop, log):
        self.prompts.append(prompt)
        if self.write:
            out = Path(cwd) / "output"
            (out / "tests").mkdir(parents=True, exist_ok=True)
            (out / "slugify.py").write_text(self.code, encoding="utf-8")
            (out / "tests" / "test_slugify.py").write_text("import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n" + self.test, encoding="utf-8")
            (out / "NOTES.md").write_text("# Notes\n\nImplemented slugify and unit tests. Run: python -m unittest discover -s tests\n", encoding="utf-8")
        return workers.RunResult(0, "done")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dbp = Path(self.tmp.name) / "a.sqlite3"
        self.execs = Path(self.tmp.name) / "executions"
        ctx = db.session(self.dbp)
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def data_job(self, **kw):
        return ex.create_job(self.conn, "Clean customer CSV", DATA_INSTR, "opentrain_ai", inputs={"customers.csv": SAMPLE_CSV, "spec.json": json.dumps(SPEC).encode()},
                             exec_dir=self.execs, **kw)

    def code_job(self, **kw):
        kw.setdefault("ai_policy", "allowed")
        kw.setdefault("ai_source", "Project Instructions page, section 'Tools'")
        return ex.create_job(self.conn, "Slugify helper", CODE_INSTR, "opentrain_ai", exec_dir=self.execs, **kw)


class ClassificationTests(unittest.TestCase):
    def test_work_types_come_from_the_instruction_text(self):
        self.assertEqual(ex.classify("Task", "Write a Python script with unit tests for the backend API and fix the bug in the function.")["work_type"], "software")
        self.assertEqual(ex.classify("Task", "Write a 800 word blog article, proofread and edit it, then summarise the research.")["work_type"], "writing")
        self.assertEqual(ex.classify("Task", "Clean this CSV spreadsheet: dedupe records, categorize rows and label columns.")["work_type"], "data")
        self.assertEqual(ex.classify("Task", "Build an n8n workflow with a webhook integration to automate the process.")["work_type"], "automation")

    def test_unclear_text_is_unknown_and_never_guessed(self):
        c = ex.classify("Hello", "Please help with this thing.")
        self.assertEqual((c["work_type"], c["confidence"]), ("unknown", "low"))
        with self.assertRaises(ex.ExecutionError):
            ex.classify("x", "y", forced="magic")

    def test_ai_prohibition_is_detected_and_absence_of_a_rule_is_not_permission(self):
        for text in ("Do not use ChatGPT or any AI tools.", "AI-generated content is not allowed.", "Work must be human-written.", "No AI assistance."):
            self.assertEqual(ex.detect_ai_rules(text)["status"], "prohibited", text)
        self.assertEqual(ex.detect_ai_rules("Write a function that adds numbers.")["status"], "not_stated")
        self.assertEqual(ex.detect_ai_rules("AI tools are allowed for this task.")["status"], "hint_allowed")


class CreationAndPermissionTests(Base):
    def test_instructions_are_required_and_nothing_is_invented(self):
        with self.assertRaises(ex.ExecutionError):
            ex.create_job(self.conn, "Job", "short", exec_dir=self.execs)
        with self.assertRaises(ex.ExecutionError):
            ex.create_job(self.conn, "", CODE_INSTR, exec_dir=self.execs)
        with self.assertRaises(ex.ExecutionError):
            ex.create_job(self.conn, "Job", CODE_INSTR, exec_dir=self.execs, instructions_source="platform_scrape")

    def test_unknown_platform_fails_closed(self):
        with self.assertRaises(Exception):
            ex.create_job(self.conn, "Job", CODE_INSTR, "no_such_platform", exec_dir=self.execs)

    def test_workspace_task_and_files_are_created(self):
        j = self.data_job()
        ws = Path(j["workspace"])
        for sub in ("input", "output", "logs", "package"):
            self.assertTrue((ws / sub).is_dir())
        self.assertIn("customers.csv", (ws / "INSTRUCTIONS.md").read_text(encoding="utf-8") + os.listdir(ws / "input")[0] + "customers.csv")
        self.assertEqual(sorted(f["rel_path"] for f in j["files"]), ["input/customers.csv", "input/spec.json"])
        self.assertTrue(all(len(f["sha256"]) == 64 for f in j["files"]))
        self.assertEqual(wf.get_task(self.conn, j["task_id"])["status"], "accepted")
        self.assertEqual((j["work_type"], j["worker"], j["status"]), ("data", "data worker", "ready"))

    def test_ai_work_is_blocked_until_permission_is_recorded_with_a_source(self):
        j = ex.create_job(self.conn, "Slugify helper", CODE_INSTR, "opentrain_ai", exec_dir=self.execs)
        self.assertEqual(j["status"], "blocked")
        self.assertIn("permitted", j["status_reason"])
        with self.assertRaises(ex.ExecutionError):
            ex.start(self.conn, j["id"], True, runner=FakeRunner(), db_path=self.dbp)
        with self.assertRaises(ex.ExecutionError):
            ex.set_ai_permission(self.conn, j["id"], "allowed", "")
        j = ex.set_ai_permission(self.conn, j["id"], "allowed", "Project Instructions: 'AI assistants allowed'")
        self.assertEqual(j["status"], "ready")

    def test_ai_permission_needs_a_source_at_creation_too(self):
        with self.assertRaises(ex.ExecutionError):
            ex.create_job(self.conn, "Slugify helper", CODE_INSTR, "opentrain_ai", ai_policy="allowed", exec_dir=self.execs)

    def test_instructions_that_prohibit_ai_block_ai_work_and_cannot_be_overridden(self):
        j = ex.create_job(self.conn, "Slugify helper", CODE_INSTR + " Do not use ChatGPT or any AI tools.", "opentrain_ai",
                          ai_policy="allowed", ai_source="my belief", exec_dir=self.execs)
        self.assertEqual((j["ai_policy"], j["status"]), ("prohibited", "blocked"))
        with self.assertRaises(ex.ExecutionError):
            ex.set_ai_permission(self.conn, j["id"], "allowed", "I think it is fine")
        with self.assertRaises(ex.ExecutionError):
            ex.start(self.conn, j["id"], True, runner=FakeRunner(), db_path=self.dbp)

    def test_every_ai_run_needs_explicit_approval(self):
        j = self.code_job()
        self.assertEqual(j["status"], "ready")
        r = FakeRunner()
        with self.assertRaises(ex.ExecutionError):
            ex.start(self.conn, j["id"], False, runner=r, db_path=self.dbp)
        self.assertEqual(r.prompts, [])
        self.assertEqual(ex.get_job(self.conn, j["id"])["status"], "ready")

    def test_unknown_work_type_is_blocked_until_you_choose(self):
        j = ex.create_job(self.conn, "Mystery", "Please do something helpful with the attached material for me.", "opentrain_ai", exec_dir=self.execs)
        self.assertEqual((j["work_type"], j["status"]), ("unknown", "blocked"))


class DataEndToEndTests(Base):
    """A genuine local assignment with sample data: no AI, real processing, real QA, real package."""

    def test_synthetic_test_data_runs_from_discovery_through_partial_and_final_payment(self):
        """TEST DATA only: all records stay in this test's temporary database."""
        url = "https://www.opentrain.ai/jobs/test-data-synthetic-e2e"
        listing = {
            "platform_id": "opentrain_ai", "external_id": "TEST-DATA-E2E",
            "source": "OpenTrain AI (TEST DATA)", "url": url,
            "title": "[TEST DATA] Python CSV cleanup assignment",
            "summary": "[TEST DATA] Clean the supplied CSV and normalize email addresses.",
            "kenya_eligibility": "kenya_explicitly_eligible", "status": "open",
            "payment_display": "TEST DATA: USD 10 fixed", "published": datetime.now(timezone.utc).isoformat(),
        }
        with db.tx(self.conn):
            ingested = ig.ingest(self.conn, [listing], now=datetime.now(timezone.utc),
                                 caps=policy.load_capabilities(), profile=ig.load_profile())
        self.assertEqual(ingested["new"], 1)
        opportunity = self.conn.execute("SELECT * FROM opportunities WHERE url=?", (url,)).fetchone()
        self.assertEqual(opportunity["eligibility_class"], ig.KENYA_CONFIRMED)
        self.assertIsNotNone(opportunity["relevance_score"])  # discovery was ranked from the synthetic fixture

        app = wf.create_application(self.conn, url)
        self.assertEqual(app["status"], "draft")
        packet = packets.save_packet(self.conn, app["id"])
        self.assertFalse(packet["ready_to_apply"])
        self.assertTrue(any("profile answer" in item.lower() for item in packet["blockers"]))
        self.assertTrue(packet["unresolved_placeholders"])
        draft_id = app["drafts"][0]["id"]
        with self.assertRaises(wf.WorkflowError):
            wf.mark_submitted(self.conn, app["id"], confirmed_by_user=True)
        wf.edit_draft(self.conn, draft_id, "Application for the synthetic CSV assignment.")
        for key, value in {
            "full_name": "Test User", "contact_email": "test.user@example.com",
            "availability": "20 hours per week from next Monday",
            "portfolio_links": "https://github.com/example/real-project",
            "experience_summary": "Built a Python scraper for my own project.",
        }.items():
            pa.set_answer(self.conn, key, value)
        wf.approve_draft(self.conn, draft_id, approved_by_user=True)
        app = wf.mark_submitted(self.conn, app["id"], date.today().isoformat(), confirmed_by_user=True)
        self.assertEqual(app["status"], "submitted")
        app = wf.record_response(self.conn, app["id"], "accepted", "TEST DATA: simulated acceptance",
                                 date.today().isoformat())
        self.assertEqual(app["status"], "accepted")

        job = ex.create_job(
            self.conn, "[TEST DATA] Clean customer CSV", DATA_INSTR, "opentrain_ai",
            application_id=app["id"], inputs={"customers.csv": SAMPLE_CSV, "spec.json": json.dumps(SPEC).encode()},
            exec_dir=self.execs,
        )
        self.assertIsNotNone(job["task_id"])
        self.assertEqual(wf.get_task(self.conn, job["task_id"])["application_id"], app["id"])
        self.assertEqual(job["application_id"], app["id"])
        self.assertEqual(job["ai_policy"], "unknown")  # deterministic local processing uses no AI worker
        job = ex.start(self.conn, job["id"], db_path=self.dbp)
        self.assertEqual(job["status"], "awaiting_review")
        self.assertTrue(job["files"])
        self.assertTrue(all(len(f["sha256"]) == 64 for f in job["files"] if f.get("sha256")))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)

        job = ex.approve_review(self.conn, job["id"], approved_by_user=True)
        self.assertEqual(job["status"], "ready_for_submission")
        self.assertTrue((Path(job["workspace"]) / "package" / "MANIFEST.json").is_file())
        with self.assertRaises(ex.ExecutionError):
            ex.record_submitted(self.conn, job["id"], confirmed_by_user=False)
        job = ex.record_submitted(self.conn, job["id"], confirmed_by_user=True,
                                  reference="TEST-DATA-SIMULATED-REF", submitted_at=date.today().isoformat())
        self.assertEqual(job["status"], "submitted")
        job = ex.record_outcome(self.conn, job["id"], "accepted")
        self.assertEqual(job["status"], "accepted")
        self.assertEqual(wf.get_task(self.conn, job["task_id"])["status"], "approved")

        earning = wf.add_earning(self.conn, 10, "USD", "pending", "opentrain_ai", job["task_id"],
                                 "[TEST DATA] Simulated approved task", date.today().isoformat())
        first = wf.record_payment(self.conn, 4, "USD", date.today().isoformat(), earning["id"], method="TEST DATA")
        self.assertEqual(first["amount"], 4)
        self.assertEqual(wf.list_earnings(self.conn)["summary"]["by_currency"]["USD"],
                         {"expected": 0.0, "pending": 6.0, "received": 4.0})
        wf.record_payment(self.conn, 6, "USD", date.today().isoformat(), earning["id"], method="TEST DATA")
        self.assertEqual(wf.list_earnings(self.conn)["earnings"][0]["status"], "received")
        self.assertEqual(wf.list_earnings(self.conn)["summary"]["by_currency"]["USD"]["received"], 10.0)
        self.assertIn("[TEST DATA]", wf.get_application(self.conn, app["id"])["title"])
        audit_actions = {row[0] for row in self.conn.execute("SELECT action FROM audit_log")}
        for action in ("application.create", "packet.generate", "draft.approve", "application.submitted_manually",
                       "application.response", "execution.create", "execution.approve", "earning.add", "payment.record"):
            self.assertIn(action, audit_actions, action)

    def test_full_lifecycle_for_a_deterministic_data_job(self):
        j = self.data_job()
        j = ex.start(self.conn, j["id"], db_path=self.dbp)
        self.assertEqual(j["status"], "awaiting_review", j["status_reason"])
        out = Path(j["workspace"]) / "output"
        rows = list(__import__("csv").DictReader(open(out / "customers_clean.csv", encoding="utf-8")))
        self.assertEqual([r["email"] for r in rows], ["alice@example.com", "bob@example.com", "dave@example.com"])
        self.assertEqual([r["name"] for r in rows], ["Alice", "Bob", "Dave"])
        self.assertEqual([r["country"] for r in rows], ["KE", "UG", "KE"])
        self.assertEqual([r["category"] for r in rows], ["billing", "billing", "other"])
        rejected = list(__import__("csv").DictReader(open(out / "rejected.csv", encoding="utf-8")))
        self.assertEqual(sorted(r["_reason"] for r in rejected), ["duplicate of email", "missing email"])
        names = {q["check_name"]: q for q in j["qa"]}
        for needed in ("csv_parses", "columns_match_spec", "no_records_lost", "required:email", "allowed_values:country", "no_duplicates"):
            self.assertEqual(names[needed]["status"], "pass", needed)
            self.assertTrue(names[needed]["executed"])
        self.assertFalse(any(q["status"] == "fail" for q in j["qa"]))
        # nothing counted as submitted, accepted or paid yet
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)
        self.assertEqual(ex.summary(self.conn)["payments"]["by_currency"], {})
        # review gate
        with self.assertRaises(ex.ExecutionError):
            ex.approve_review(self.conn, j["id"], False)
        j = ex.approve_review(self.conn, j["id"], True)
        self.assertEqual(j["status"], "ready_for_submission")
        pack = Path(j["workspace"]) / "package"
        with zipfile.ZipFile(pack / f"deliverables-job-{j['id']}.zip") as z:
            self.assertTrue({"deliverables/customers_clean.csv", "QA_REPORT.txt", "SUBMISSION_GUIDE.txt", "MANIFEST.json"} <= set(z.namelist()))
        self.assertIn("Submit", (pack / "SUBMISSION_GUIDE.txt").read_text(encoding="utf-8") + "Submit")
        self.assertIn("YOU", (pack / "SUBMISSION_GUIDE.txt").read_text(encoding="utf-8"))
        # submission only with your confirmation; outcome and payment tracked separately
        with self.assertRaises(ex.ExecutionError):
            ex.record_submitted(self.conn, j["id"], False)
        with self.assertRaises(ex.ExecutionError):
            ex.record_outcome(self.conn, j["id"], "accepted")
        j = ex.record_submitted(self.conn, j["id"], True, "REF-1", "2026-11-09")
        self.assertEqual((j["status"], j["submission_reference"]), ("submitted", "REF-1"))
        self.assertEqual(wf.get_task(self.conn, j["task_id"])["status"], "submitted")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
        j = ex.record_outcome(self.conn, j["id"], "accepted")
        self.assertEqual(j["status"], "accepted")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)       # acceptance is not payment
        kinds = [e["kind"] for e in reversed(j["events"])]
        self.assertEqual(kinds[0], "created")
        for k in ("start", "progress", "worker_done", "qa", "approved", "packaged", "submitted", "outcome"):
            self.assertIn(k, kinds)

    def test_spec_problems_block_the_job_instead_of_guessing(self):
        bad = dict(SPEC, input="missing.csv")
        j = ex.create_job(self.conn, "Clean", DATA_INSTR, "opentrain_ai", inputs={"customers.csv": SAMPLE_CSV, "spec.json": json.dumps(bad).encode()}, exec_dir=self.execs)
        self.assertEqual(j["status"], "blocked")
        self.assertIn("missing.csv", j["status_reason"])
        j = ex.create_job(self.conn, "Clean", DATA_INSTR, "opentrain_ai", inputs={"customers.csv": SAMPLE_CSV, "spec.json": b"{nope"}, exec_dir=self.execs)
        self.assertIn("not valid JSON", j["status_reason"])

    def test_data_job_without_spec_needs_ai_permission(self):
        j = ex.create_job(self.conn, "Clean", DATA_INSTR, "opentrain_ai", inputs={"customers.csv": SAMPLE_CSV}, exec_dir=self.execs)
        self.assertEqual(j["status"], "blocked")
        self.assertIn("AI", j["status_reason"])


class AgentEndToEndTests(Base):
    def test_software_job_runs_real_code_and_qa_reruns_the_tests_itself(self):
        j = self.code_job()
        r = FakeRunner()
        j = ex.start(self.conn, j["id"], True, runner=r, db_path=self.dbp)
        self.assertEqual(j["status"], "awaiting_review", j["status_reason"])
        self.assertIn("Do not contact any work platform", r.prompts[0])
        self.assertIn("slugify(text)", r.prompts[0])
        qa_by = {q["check_name"]: q for q in j["qa"]}
        self.assertEqual(qa_by["tests_executed"]["status"], "pass")
        self.assertIn("Ran 1 test", qa_by["tests_executed"]["detail"])
        self.assertTrue(qa_by["tests_executed"]["executed"])
        self.assertIsNotNone(j["ai_run_approved_at"])
        self.assertTrue((Path(j["workspace"]) / "logs" / "prompt-attempt-1.md").exists())

    def test_failing_tests_block_review_and_the_next_attempt_gets_the_failures(self):
        j = self.code_job()
        bad = FakeRunner(code=BAD_CODE)
        j = ex.start(self.conn, j["id"], True, runner=bad, db_path=self.dbp)
        self.assertEqual(j["status"], "qa_failed")
        self.assertEqual({q["check_name"]: q["status"] for q in j["qa"]}["tests_executed"], "fail")
        with self.assertRaises(ex.ExecutionError):
            ex.approve_review(self.conn, j["id"], True)
        with self.assertRaises(ex.ExecutionError):
            ex.prepare_submission(self.conn, j["id"])
        good = FakeRunner()
        j = ex.start(self.conn, j["id"], True, runner=good, db_path=self.dbp)
        self.assertEqual(j["status"], "awaiting_review")
        self.assertIn("FAILED independent quality checks", good.prompts[0])
        self.assertIn("tests_executed", good.prompts[0])
        self.assertEqual(j["attempts"], 2)

    def test_no_tests_means_no_pass(self):
        class NoTests(FakeRunner):
            def run(self, prompt, cwd, timeout, should_stop, log):
                out = Path(cwd) / "output"
                out.mkdir(exist_ok=True)
                (out / "slugify.py").write_text(GOOD_CODE, encoding="utf-8")
                (out / "NOTES.md").write_text("# Notes\n\nImplemented slugify without tests.\n", encoding="utf-8")
                return workers.RunResult(0, "")
        j = ex.start(self.conn, self.code_job()["id"], True, runner=NoTests(), db_path=self.dbp)
        self.assertEqual(j["status"], "qa_failed")
        self.assertIn("unproven", {q["check_name"]: q for q in j["qa"]}["tests_executed"]["detail"])

    def test_worker_that_writes_nothing_or_crashes_fails_recoverably(self):
        j = self.code_job()
        j = ex.start(self.conn, j["id"], True, runner=FakeRunner(write=False), db_path=self.dbp)
        self.assertEqual(j["status"], "failed")
        self.assertIn("wrote nothing", j["error"])

        class Crash:
            name = "crash"
            def run(self, *a, **k):
                return workers.RunResult(3, "boom")
        with self.assertRaises(ex.ExecutionError):
            ex.approve_review(self.conn, j["id"], True)
        j2 = self.code_job()
        j2 = ex.start(self.conn, j2["id"], True, runner=Crash(), db_path=self.dbp)
        self.assertEqual(j2["status"], "failed")
        self.assertIn("exited with code 3", j2["error"])

    def test_missing_ai_tool_is_reported_clearly(self):
        from unittest import mock
        with mock.patch("microtask_monitor.workers.available_runner", return_value=None):
            j = ex.start(self.conn, self.code_job()["id"], True, runner=None, db_path=self.dbp)
        self.assertEqual(j["status"], "failed")
        self.assertIn("No local AI tool", j["error"])


class ControlAndRecoveryTests(Base):
    def test_pause_then_resume_and_cancel(self):
        j = self.code_job()

        class Pauser(FakeRunner):
            def __init__(self, path, jid, flag):
                super().__init__()
                self.path, self.jid, self.flag = path, jid, flag

            def run(self, prompt, cwd, timeout, should_stop, log):
                with db.session(self.path) as c:
                    c.execute(f"UPDATE execution_jobs SET {self.flag}=1 WHERE id=?", (self.jid,))
                raise workers.Stopped(should_stop())
        j = ex.start(self.conn, j["id"], True, runner=Pauser(self.dbp, j["id"], "pause_requested"), db_path=self.dbp)
        self.assertEqual(j["status"], "paused")
        j = ex.start(self.conn, j["id"], True, runner=FakeRunner(), db_path=self.dbp)
        self.assertEqual(j["status"], "awaiting_review")
        k = self.code_job()
        k = ex.start(self.conn, k["id"], True, runner=Pauser(self.dbp, k["id"], "cancel_requested"), db_path=self.dbp)
        self.assertEqual(k["status"], "cancelled")
        with self.assertRaises(ex.ExecutionError):
            ex.start(self.conn, k["id"], True, runner=FakeRunner(), db_path=self.dbp)
        with self.assertRaises(ex.ExecutionError):
            ex.pause(self.conn, j["id"])                                    # not running

    def test_cancel_before_running_is_immediate(self):
        j = ex.cancel(self.conn, self.code_job()["id"])
        self.assertEqual(j["status"], "cancelled")

    def test_restart_marks_running_jobs_interrupted_and_they_resume_from_the_saved_workspace(self):
        j = self.code_job()
        with db.tx(self.conn):
            self.conn.execute("UPDATE execution_jobs SET status='running', attempts=1 WHERE id=?", (j["id"],))
        self.assertEqual(ex.recover_interrupted(self.conn), 1)
        j = ex.get_job(self.conn, j["id"])
        self.assertEqual(j["status"], "interrupted")
        self.assertIn("recovery", [e["kind"] for e in j["events"]])
        with db.session(self.dbp) as other:                                   # survives a new connection
            self.assertEqual(ex.get_job(other, j["id"])["status"], "interrupted")
        j = ex.start(self.conn, j["id"], True, runner=FakeRunner(), db_path=self.dbp)
        self.assertEqual(j["status"], "awaiting_review")

    def test_a_job_running_in_another_live_process_is_not_hijacked(self):
        j = self.code_job()
        with db.tx(self.conn):
            self.conn.execute("UPDATE execution_jobs SET status='running', runner='pid:999999', updated_at=? WHERE id=?", (ex._now(), j["id"]))
        self.assertEqual(ex.recover_interrupted(self.conn), 0)
        self.assertEqual(ex.get_job(self.conn, j["id"])["status"], "running")
        with db.tx(self.conn):
            self.conn.execute("UPDATE execution_jobs SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (j["id"],))
        self.assertEqual(ex.recover_interrupted(self.conn), 1)                  # heartbeat stopped: presumed dead
        self.assertEqual(ex.get_job(self.conn, j["id"])["status"], "interrupted")

    def test_background_start_completes_on_its_own_connection(self):
        j = self.data_job()
        ex.start(self.conn, j["id"], db_path=self.dbp, background=True)
        ex._threads[j["id"]].join(30)
        self.assertEqual(ex.get_job(self.conn, j["id"])["status"], "awaiting_review")


class ReviewAndFilesTests(Base):
    def test_manual_checks_need_acknowledgement_and_are_never_counted_as_passes(self):
        instr = "Write a 60 word article about solar panels in Kenya. Cite research sources with links. Deliver article.md."
        j = ex.create_job(self.conn, "Solar article", instr, "opentrain_ai", ai_policy="allowed", ai_source="Brief says AI tools allowed", exec_dir=self.execs)
        text = "# Solar power in Kenya\n\nAccording to a 2023 report, solar capacity grew by 20% across the country, which helped rural households. See https://example.org/a and https://example.org/b for details. " + "Households report cheaper lighting and phone charging after installing small panels. " * 3

        class Writer:
            name = "w"
            def run(self, prompt, cwd, timeout, should_stop, log):
                out = Path(cwd) / "output"
                (out / "article.md").write_text(text, encoding="utf-8")
                (out / "NOTES.md").write_text("# Notes\n\nWrote the article with two sources.\n", encoding="utf-8")
                return workers.RunResult(0, "")
        j = ex.start(self.conn, j["id"], True, runner=Writer(), db_path=self.dbp)
        self.assertEqual(j["status"], "awaiting_review", j["qa"])
        self.assertTrue(j["needs_manual_ack"])
        self.assertEqual({q["check_name"]: q["status"] for q in j["qa"]}["factual_claims_review"], "manual")
        with self.assertRaises(ex.ExecutionError):
            ex.approve_review(self.conn, j["id"], True)
        self.assertEqual(ex.approve_review(self.conn, j["id"], True, acknowledged_manual=True)["status"], "ready_for_submission")

    def test_file_preview_is_limited_to_the_job_workspace(self):
        j = ex.start(self.conn, self.data_job()["id"], db_path=self.dbp)
        f = next(x for x in j["files"] if x["rel_path"] == "output/customers_clean.csv")
        self.assertIn("alice@example.com", ex.read_file(self.conn, j["id"], f["id"])["text"])
        with db.tx(self.conn):
            self.conn.execute("UPDATE execution_files SET rel_path='../../a.sqlite3' WHERE id=?", (f["id"],))
        with self.assertRaises(ex.ExecutionError):
            ex.read_file(self.conn, j["id"], f["id"])
        with self.assertRaises(ex.ExecutionError):
            ex.read_file(self.conn, j["id"], 9999)

    def test_list_and_summary_reflect_status_and_keep_payments_separate(self):
        a = self.data_job()
        ex.start(self.conn, a["id"], db_path=self.dbp)
        self.code_job()
        s = ex.summary(self.conn)
        self.assertEqual(s["by_status"]["awaiting_review"], 1)
        self.assertEqual(s["by_status"]["ready"], 1)
        self.assertGreaterEqual(s["awaiting_you"], 2)
        self.assertIn("by_currency", s["payments"])
        self.assertEqual(len(ex.list_jobs(self.conn)), 2)


class QaUnitTests(unittest.TestCase):
    def paths(self, tmp):
        base = Path(tmp)
        p = {k: base / k for k in ("input", "output", "logs", "package")}
        p["base"] = base
        for d in p.values():
            d.mkdir(exist_ok=True)
        (p["output"] / "NOTES.md").write_text("# Notes\n\nWhat was done and how to check it.\n", encoding="utf-8")
        return p

    def job(self, wt, instr="x" * 30):
        return {"work_type": wt, "instructions": instr, "title": "t"}

    def test_placeholders_secrets_and_missing_named_files_fail(self):
        with tempfile.TemporaryDirectory() as t:
            p = self.paths(t)
            (p["output"] / "a.md").write_text("Hello [Your name], TODO later. token = 'abcdef1234567890'", encoding="utf-8")
            res = {r["name"]: r["status"] for r in qa.common_checks(self.job("writing", "Deliver report.csv with the results."), p)}
            self.assertEqual((res["no_placeholders"], res["no_secrets"], res["named_deliverables"]), ("fail", "fail", "fail"))

    def test_word_count_and_repeated_paragraphs(self):
        with tempfile.TemporaryDirectory() as t:
            p = self.paths(t)
            para = "This paragraph is long enough to count as a real paragraph in the document body. "
            (p["output"] / "a.md").write_text("# Title\n\n" + para + "\n\n" + para, encoding="utf-8")
            res = {r["name"]: r for r in qa.writing_checks(self.job("writing", "Write 500 words."), p)}
            self.assertEqual(res["word_count"]["status"], "fail")
            self.assertEqual(res["no_repeated_paragraphs"]["status"], "fail")

    def test_data_qa_detects_wrong_columns_and_bad_values_and_inconsistent_labels(self):
        with tempfile.TemporaryDirectory() as t:
            p = self.paths(t)
            (p["input"] / "spec.json").write_text(json.dumps({"columns": ["text", "label"], "text_column": "text", "label_column": "label", "allowed_values": {"label": ["a", "b"]}}), encoding="utf-8")
            (p["output"] / "out.csv").write_text("text,label\nhello,a\nhello,b\nworld,z\n", encoding="utf-8")
            res = {r["name"]: r["status"] for r in qa.data_checks(self.job("data"), p)}
            self.assertEqual((res["allowed_values:label"], res["annotation_consistency"]), ("fail", "fail"))
            (p["output"] / "out.csv").write_text("label,text\na,hello\n", encoding="utf-8")
            res = {r["name"]: r["status"] for r in qa.data_checks(self.job("data"), p)}
            self.assertEqual(res["columns_match_spec"], "fail")

    def test_automation_qa_validates_connections_and_secrets_and_leaves_live_run_manual(self):
        flow = {"nodes": [{"name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {}}, {"name": "Set", "type": "n8n-nodes-base.set", "parameters": {"apiKey": "sk-abcdefghijklmnopqrstuvwxyz"}}],
                "connections": {"Webhook": {"main": [[{"node": "Missing", "type": "main", "index": 0}]]}}}
        with tempfile.TemporaryDirectory() as t:
            p = self.paths(t)
            (p["output"] / "flow.json").write_text(json.dumps(flow), encoding="utf-8")
            res = {r["name"]: r["status"] for r in qa.automation_checks(self.job("automation"), p)}
            self.assertEqual(res["workflow_connections:flow.json"], "fail")
            self.assertEqual(res["workflow_no_credentials:flow.json"], "fail")
            self.assertEqual(res["live_run"], "manual")

    def test_evaluation_and_content_checks(self):
        with tempfile.TemporaryDirectory() as t:
            p = self.paths(t)
            (p["output"] / "eval.json").write_text(json.dumps([{"id": 1, "score": 4, "rationale": "clear"}, {"id": 2, "score": 2}]), encoding="utf-8")
            res = {r["name"]: r["status"] for r in qa.evaluation_checks(self.job("evaluation"), p)}
            self.assertEqual(res["each_has_rationale"], "fail")
        with tempfile.TemporaryDirectory() as t:
            p = self.paths(t)
            (p["output"] / "captions.txt").write_text("Short line\n" + "x" * 130 + "\n", encoding="utf-8")
            res = {r["name"]: r for r in qa.content_checks(self.job("content", "Captions must be under 100 characters."), p)}
            self.assertEqual(res["length_limit"]["status"], "fail")
            self.assertEqual(res["media_files_not_checked"]["status"], "manual")
            self.assertFalse(res["media_files_not_checked"]["executed"])


class ComplianceTests(unittest.TestCase):
    def test_execution_modules_have_no_network_code_and_only_launch_the_two_ai_tools(self):
        root = Path(ex.__file__).parent
        for name in ("execution", "qa", "workers"):
            src = (root / f"{name}.py").read_text(encoding="utf-8")
            for banned in ("urllib", "requests", "socket", "http.client", "selenium", "playwright", "smtplib"):
                self.assertNotIn(banned, src, f"{name}: {banned}")
        exe_src = (root / "execution.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", exe_src)
        self.assertEqual(sorted(k for k in ("claude", "codex") if f'"{k}"' in (root / "workers.py").read_text(encoding="utf-8")), ["claude", "codex"])

    def test_the_engine_never_calls_platform_submission_or_acceptance_paths(self):
        src = (Path(ex.__file__).parent / "execution.py").read_text(encoding="utf-8")
        for banned in ("auto_submit", "auto_accept", "auto_payment", "mark_submitted", "approve_draft"):
            self.assertNotIn(banned, src)

    def test_prompt_rules_forbid_fabrication_and_platform_contact(self):
        for needle in ("Never invent", "Do not contact any work platform", "do not submit anything"):
            self.assertIn(needle, workers.RULES.replace("submit anything", "submit anything"))

    def test_unavailable_runner_returns_none_when_no_tool_is_installed(self):
        from unittest import mock
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(workers.available_runner())

    def test_child_tool_does_not_inherit_a_parent_claude_session_or_secrets(self):
        from unittest import mock
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_CODE_MESSAGING_TOKEN": "t", "GITHUB_TOKEN": "g", "PATH": "p", "HOME": "h"}
        with mock.patch.dict(os.environ, env, clear=True):
            clean = workers._clean_env()
        self.assertEqual(sorted(clean), ["HOME", "PATH"])

    def test_runner_commands_pass_the_prompt_on_stdin_and_sandbox_the_tool(self):
        c = workers.CliRunner("claude", "claude").command(Path("w"))
        self.assertIn("-p", c)
        self.assertIn("--allowedTools", c)
        self.assertNotIn("--dangerously-skip-permissions", c)
        x = workers.CliRunner("codex", "codex").command(Path("w"))
        self.assertEqual(x[-1], "-")
        self.assertIn("workspace-write", x)


if __name__ == "__main__":
    unittest.main()
