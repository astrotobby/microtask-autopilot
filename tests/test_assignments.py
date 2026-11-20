"""Assignment lifecycle: evidence gates, state integrity, resumption, duplicate prevention, payment accuracy, migration safety.

All mock/local: temp databases and the deterministic data worker (no AI, no network, never ``data/``).
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import assignments as asg, db, execution as ex, workflow as wf
from tests.test_api import ApiBase
from tests.test_execution import Base, DATA_INSTR, SAMPLE_CSV, SPEC

TODAY = date.today().isoformat()


class LifecycleBase(Base):
    def make(self, ext="task-1", **kw):
        return asg.create(self.conn, "opentrain_ai", ext, "Clean customer CSV", "manual", DATA_INSTR, "https://app.opentrain.ai/", **kw)

    def to_accepted(self, a):
        asg.qualify(self.conn, a["id"], True, ["Kenya listed"], "listing-check-1")
        asg.set_ai_use(self.conn, a["id"], "prohibited")
        asg.mark_ready(self.conn, a["id"])
        return asg.advance(self.conn, a["id"], "accepted", "user_confirmation", "offer email 2026-11-10", confirmed_by_user=True)

    def run_job(self, a):
        a = asg.start_execution(self.conn, a["id"], exec_dir=self.execs, inputs={"customers.csv": SAMPLE_CSV, "spec.json": json.dumps(SPEC).encode()})
        ex.start(self.conn, a["execution_job_id"], db_path=self.dbp)
        return asg.sync_from_job(self.conn, a["id"])


class CreationAndDuplicateTests(LifecycleBase):
    def test_an_assignment_needs_a_tracked_platform_and_a_real_identifier(self):
        with self.assertRaises(asg.AssignmentError):
            asg.create(self.conn, "not_a_platform", "x", "T", "manual")
        with self.assertRaises(asg.AssignmentError):
            asg.create(self.conn, "opentrain_ai", "", "T", "manual")

    def test_the_same_listing_is_never_created_twice(self):
        a, b = self.make(), self.make()
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0], 1)
        self.assertEqual(len(b["history"]), 1)

    def test_a_discovered_listing_is_not_an_accepted_task(self):
        self.make()
        f = asg.funnel(self.conn)
        self.assertEqual((f["assignments_qualified"], f["assignments_accepted"], f["work_performed"], f["submissions_confirmed"]), (0, 0, 0, 0))


class StateIntegrityTests(LifecycleBase):
    def test_stages_cannot_be_skipped(self):
        a = self.make()
        for target in ("accepted", "submitted", "work_accepted", "paid"):
            with self.assertRaises(asg.AssignmentError):
                asg.advance(self.conn, a["id"], target, "user_confirmation", "ref", confirmed_by_user=True)
        self.assertEqual(asg.get(self.conn, a["id"])["stage"], "discovered")

    def test_every_transition_stores_its_evidence(self):
        a = self.to_accepted(self.make())
        self.assertEqual([h["to_stage"] for h in a["history"]], ["discovered", "qualified", "ready", "accepted"])
        self.assertTrue(all(h["evidence_kind"] and h["evidence_ref"] for h in a["history"]))
        self.assertEqual(a["history"][-1]["actor"], "user")

    def test_wrong_or_missing_evidence_is_refused(self):
        a = self.make()
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "qualified", "user_confirmation", "because I say so", confirmed_by_user=True)
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "qualified", "eligibility_check", "")
        asg.qualify(self.conn, a["id"], True, ["ok"], "check-1")
        with self.assertRaises(asg.AssignmentError):                      # AI rule still unknown
            asg.mark_ready(self.conn, a["id"])

    def test_taking_a_task_needs_your_confirmation_and_a_reference(self):
        a = self.make()
        asg.qualify(self.conn, a["id"], True, ["ok"], "check-1")
        asg.set_ai_use(self.conn, a["id"], "prohibited")
        asg.mark_ready(self.conn, a["id"])
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "accepted", "user_confirmation", "ref", confirmed_by_user=False)
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "accepted", "user_confirmation", "  ", confirmed_by_user=True)

    def test_ai_cannot_be_enabled_against_the_instructions_or_without_a_source(self):
        a = asg.create(self.conn, "opentrain_ai", "t9", "Essay", "manual", "Write the essay yourself. Do not use AI tools such as ChatGPT for this task at all.")
        with self.assertRaises(asg.AssignmentError):
            asg.set_ai_use(self.conn, a["id"], "allowed", "the brief")
        b = self.make("t10")
        with self.assertRaises(asg.AssignmentError):
            asg.set_ai_use(self.conn, b["id"], "allowed", "")

    def test_executing_needs_a_job_that_really_started(self):
        a = self.to_accepted(self.make())
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "executing", "execution_job", "execution_job:999")
        a = asg.start_execution(self.conn, a["id"], exec_dir=self.execs, inputs={"customers.csv": SAMPLE_CSV, "spec.json": json.dumps(SPEC).encode()})
        self.assertEqual(asg.sync_from_job(self.conn, a["id"])["stage"], "accepted")      # created but not started: nothing was worked on

    def test_work_cannot_start_before_the_task_is_actually_taken(self):
        a = self.make()
        with self.assertRaises(asg.AssignmentError):
            asg.start_execution(self.conn, a["id"], exec_dir=self.execs)


class ExecutionAndDeliveryTests(LifecycleBase):
    def test_real_instructions_produce_real_files_and_qa_before_delivery(self):
        a = self.run_job(self.to_accepted(self.make()))
        self.assertEqual(a["stage"], "qa")                                 # QA ran; not yet approved, so not ready for delivery
        job = ex.get_job(self.conn, a["execution_job_id"])
        self.assertTrue((Path(job["workspace"]) / "output" / "customers_clean.csv").is_file())
        self.assertTrue(all(q["executed"] for q in job["qa"] if q["status"] == "pass"))
        with self.assertRaises(asg.AssignmentError):                       # a generated file is not a submission
            asg.advance_through(self.conn, a["id"], "submitted", "user_confirmation", "REF-1", confirmed_by_user=True)
        ex.approve_review(self.conn, job["id"], True)
        a = asg.sync_from_job(self.conn, a["id"])
        self.assertEqual(a["stage"], "ready_for_delivery")
        self.assertTrue(any(f["role"] == "package" for f in ex.get_job(self.conn, job["id"])["files"]))

    def test_qa_failure_blocks_delivery(self):
        a = self.run_job(self.to_accepted(self.make()))
        jid = a["execution_job_id"]
        with db.tx(self.conn):                                             # simulate a failed independent check on the latest attempt
            self.conn.execute("UPDATE execution_qa SET status='fail' WHERE job_id=? AND id=(SELECT MIN(id) FROM execution_qa WHERE job_id=?)", (jid, jid))
            self.conn.execute("UPDATE execution_jobs SET status='qa_failed' WHERE id=?", (jid,))
        with self.assertRaises(ex.ExecutionError):
            ex.approve_review(self.conn, jid, True)
        self.assertEqual(asg.sync_from_job(self.conn, a["id"])["stage"], "qa")
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "ready_for_delivery", "execution_job", f"execution_job:{jid}")

    def test_resuming_never_creates_a_second_job_or_repeats_events(self):
        a = self.run_job(self.to_accepted(self.make()))
        again = asg.start_execution(self.conn, a["id"], exec_dir=self.execs)
        self.assertEqual(again["execution_job_id"], a["execution_job_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0], 1)
        before = len(asg.get(self.conn, a["id"])["history"])
        for _ in range(3):
            asg.sync_from_job(self.conn, a["id"])
        self.assertEqual(len(asg.get(self.conn, a["id"])["history"]), before)

    def test_an_interrupted_job_is_resumed_in_place(self):
        a = self.to_accepted(self.make())
        a = asg.start_execution(self.conn, a["id"], exec_dir=self.execs, inputs={"customers.csv": SAMPLE_CSV, "spec.json": json.dumps(SPEC).encode()})
        jid = a["execution_job_id"]
        with db.tx(self.conn):                                             # the program died mid-run
            self.conn.execute("UPDATE execution_jobs SET status='running', runner='pid:1', attempts=1, updated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (jid,))
        self.assertEqual(ex.recover_interrupted(self.conn), 1)
        self.assertEqual(ex.get_job(self.conn, jid)["status"], "interrupted")
        ex.start(self.conn, jid, db_path=self.dbp)
        self.assertEqual(ex.get_job(self.conn, jid)["status"], "awaiting_review")
        self.assertEqual(asg.sync_from_job(self.conn, a["id"])["stage"], "qa")

    def delivered(self):
        a = self.run_job(self.to_accepted(self.make()))
        ex.approve_review(self.conn, a["execution_job_id"], True)
        return asg.sync_from_job(self.conn, a["id"])

    def test_submission_evidence_then_review_then_payment(self):
        a = self.delivered()
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "submitted", "user_confirmation", "", confirmed_by_user=True)
        a = asg.advance(self.conn, a["id"], "submitted", "user_confirmation", "Upload receipt #A-778", confirmed_by_user=True)
        self.assertEqual((a["stage"], a["submission_reference"]), ("submitted", "Upload receipt #A-778"))
        a = asg.advance(self.conn, a["id"], "awaiting_review", "user_confirmation", "Status page shows 'In review'", confirmed_by_user=True)
        self.assertEqual(asg.funnel(self.conn)["work_accepted"], 0)         # a QA pass and a submission are not acceptance
        with self.assertRaises(asg.AssignmentError):
            asg.record_payment_pending(self.conn, a["id"], 40, "USD")
        a = asg.advance(self.conn, a["id"], "work_accepted", "user_confirmation", "Approval email 2026-11-12", confirmed_by_user=True)
        f = asg.funnel(self.conn)
        self.assertEqual((f["work_accepted"], f["payments_confirmed"], f["money"]["by_currency"]), (1, 0, {}))
        a = asg.record_payment_pending(self.conn, a["id"], 40, "USD")
        self.assertEqual(asg.funnel(self.conn)["money"]["by_currency"]["USD"], {"expected": 0.0, "pending": 40.0, "received": 0.0})
        with self.assertRaises(asg.AssignmentError):                       # no reference, no confirmation: not paid
            asg.record_paid(self.conn, a["id"], 40, "USD", TODAY, "M-Pesa", "", True)
        with self.assertRaises(asg.AssignmentError):
            asg.record_paid(self.conn, a["id"], 40, "USD", TODAY, "M-Pesa", "TX1", False)
        a = asg.record_paid(self.conn, a["id"], 40, "USD", TODAY, "M-Pesa", "TX-QK12345", True)
        self.assertEqual(a["stage"], "paid")
        f = asg.funnel(self.conn)
        self.assertEqual((f["payments_confirmed"], f["money"]["by_currency"]["USD"]["received"], f["money"]["by_currency"]["USD"]["pending"]), (1, 40.0, 0.0))
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "cancelled", "user_confirmation", "x", confirmed_by_user=True)

    def test_paid_cannot_be_reached_without_a_recorded_payment(self):
        a = self.delivered()
        asg.advance_through(self.conn, a["id"], "work_accepted", "user_confirmation", "REF", confirmed_by_user=True)
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "payment_pending", "earning", "earning:1")
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "paid", "payment", "payment:1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)

    def test_rejection_only_after_submission_and_owes_nothing(self):
        a = self.delivered()
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "rejected", "user_confirmation", "REF", confirmed_by_user=True)
        asg.advance_through(self.conn, a["id"], "awaiting_review", "user_confirmation", "REF", confirmed_by_user=True)
        a = asg.advance(self.conn, a["id"], "rejected", "user_confirmation", "Reviewer note 14 Nov", "Did not follow the naming rule", True)
        self.assertEqual(a["stage"], "rejected")
        with self.assertRaises(asg.AssignmentError):
            asg.record_payment_pending(self.conn, a["id"], 40, "USD")

    def test_personally_done_work_needs_your_word_and_no_engine_job(self):
        a = self.to_accepted(self.make("human-1"))
        with self.assertRaises(asg.AssignmentError):
            asg.advance(self.conn, a["id"], "executing", "human_work", "my post link", confirmed_by_user=False)
        a = asg.advance_through(self.conn, a["id"], "submitted", "user_confirmation", "https://example.org/my-post", "", True)
        self.assertEqual(a["stage"], "submitted")
        self.assertEqual(asg.funnel(self.conn)["work_performed"], 0)        # the engine did not do this work and does not claim it


class MigrationSafetyTests(unittest.TestCase):
    def test_version_6_data_survives_the_upgrade_and_a_backup_is_taken(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "a.sqlite3"
            raw = sqlite3.connect(path)
            raw.isolation_level = None
            for version, apply in db.MIGRATIONS[:6]:
                apply(raw)
            raw.execute("PRAGMA user_version = 6")
            raw.execute("INSERT INTO applications(title,status,created_at,updated_at) VALUES('Keep me','draft','x','x')")
            raw.execute("INSERT INTO opportunities(url,title,first_seen_utc,last_seen_utc,report_checked_at_utc,raw_json) VALUES('https://a.example/1','Opp','x','x','x','{}')")
            raw.execute("INSERT INTO earnings(amount,currency,status,created_at,updated_at) VALUES(5,'USD','pending','x','x')")
            raw.execute("INSERT INTO platform_connections(platform_id,status,updated_at) VALUES('opentrain_ai','connected_user_confirmed','x')")
            raw.close()
            with db.session(path) as conn:
                self.assertEqual(db.user_version(conn), db.LATEST_VERSION)
                self.assertGreaterEqual(db.LATEST_VERSION, 7)
                for table, n in (("applications", 1), ("opportunities", 1), ("earnings", 1), ("platform_connections", 1), ("assignments", 0), ("whop_bounties", 0)):
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], n, table)
                row = conn.execute("SELECT status, verified_by FROM platform_connections").fetchone()
                self.assertEqual((row["status"], row["verified_by"]), ("connected_user_confirmed", None))
            self.assertEqual(len(list(Path(t).glob("a.sqlite3.bak-v6-*"))), 1)

    def test_migration_is_additive_and_reversible(self):
        with tempfile.TemporaryDirectory() as t:
            with db.session(Path(t) / "a.sqlite3") as conn:
                before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("assignment_events", "assignments", "whop_submissions", "whop_bounties"):
                    conn.execute(f"DROP TABLE {table}")
                after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertEqual(before - after, {"assignment_events", "assignments", "whop_submissions", "whop_bounties"})
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 0)      # older tables still usable


class CentreApiTests(ApiBase):
    def test_centre_separates_every_kind_of_count(self):
        s, r = self.get("/api/centre")
        self.assertEqual(s, 200)
        f = r["funnel"]
        for key in ("discovered_opportunities", "applications_submitted", "assignments_accepted", "work_performed", "deliverables_completed",
                    "submissions_confirmed", "work_accepted", "payments_confirmed"):
            self.assertIn(key, f)
        self.assertGreater(f["discovered_opportunities"], 0)
        self.assertEqual((f["assignments_accepted"], f["work_performed"], f["payments_confirmed"]), (0, 0, 0))
        self.assertEqual(r["connected"], {"api_verified": 0, "confirmed_by_you": 0})

    def test_assignment_endpoints_enforce_confirmation(self):
        with db.session(self.db) as conn:
            a = asg.create(conn, "opentrain_ai", "api-1", "Clean customer CSV", "manual", DATA_INSTR)
            asg.qualify(conn, a["id"], True, ["ok"], "c1")
        aid = a["id"]
        self.assertEqual(self.post(f"/api/assignments/{aid}/ready")[0], 400)
        self.assertEqual(self.post(f"/api/assignments/{aid}/ai-use", {"ai_use": "prohibited"})[0], 200)
        self.assertEqual(self.post(f"/api/assignments/{aid}/ready")[0], 200)
        self.assertEqual(self.post(f"/api/assignments/{aid}/accepted", {"reference": "offer"})[0], 400)
        s, r = self.post(f"/api/assignments/{aid}/accepted", {"reference": "offer 10 Nov", "confirmed_by_user": True})
        self.assertEqual((s, r["assignment"]["stage"]), (200, "accepted"))
        self.assertEqual(self.post(f"/api/assignments/{aid}/paid", {"amount": 5, "currency": "USD", "received_date": TODAY, "reference": "x", "confirmed_by_user": True})[0], 400)
        s, r = self.get("/api/registry")
        self.assertEqual((s, len(r["registry"])), (200, 27))

    def test_registry_rows_carry_sources_and_dates(self):
        rows = {x["platform_id"]: x for x in self.get("/api/registry")[1]["registry"]}
        self.assertEqual({x["class"] for x in rows.values()} - {1, 2, 3, 4, 5}, set())
        self.assertEqual((rows["opentrain_ai"]["class"], rows["whop_workforce"]["class"], rows["mindrift"]["class"]), (1, 1, 4))
        for pid, row in rows.items():
            self.assertTrue(row["sources"], pid)
            self.assertFalse(row["auto_acceptance"], pid)
            self.assertFalse(row["browser_automation"], pid)
            if row["class"] != 5:
                self.assertTrue(row["checked_on"], pid)


if __name__ == "__main__":
    unittest.main()
