"""Reminders for business proposals: an unsent draft nags, a sent one gets follow-ups, a reply gets an answer task.

Temporary database and synthetic lead only. Nothing is sent and no status is changed by a reminder.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import biz_config as bc, biz_leads as bl, biz_proposals as bp, db, reminders as rm

ENQUIRY = {"name": "Jane Doe", "email": "jane@example.org", "company": "Doe Weather Ltd",
           "subject": "Python REST API integration for our weather dashboard", "budget": 400,
           "message": "We need a Python backend that integrates the OpenWeather REST API and stores readings in SQLite. "
                      "It must convert Fahrenheit to Celsius, expose a /readings endpoint, and include tests. Deliver within 3 weeks."}


class ProposalReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        for target, value in ((bc, ("USER_PATH", t / "biz.json")),):
            p = mock.patch.object(target, *value)
            p.start()
            self.addCleanup(p.stop)
        ctx = db.session(t / "a.sqlite3")
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        bl.import_enquiry(self.conn, ENQUIRY)
        self.lead = bl.ranked(self.conn)[0]
        self.proposal = bp.propose(self.conn, self.lead["id"])

    def kinds(self):
        return [(r["kind"], r["title"]) for r in rm.list_reminders(self.conn, include_closed=True)]

    def test_an_unsent_draft_starts_nagging_after_two_days_and_only_once(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(rm.generate(self.conn, now)["draft_review"], 0)          # too fresh to nag
        later = now + timedelta(days=3)
        made = rm.generate(self.conn, later)
        self.assertEqual(made["draft_review"], 1)
        self.assertEqual(rm.generate(self.conn, later)["draft_review"], 0)        # idempotent
        pending = rm.due_reminders(self.conn, later)
        self.assertEqual([r["kind"] for r in pending], ["draft_review"])
        self.assertEqual(pending[0]["ref_type"], "proposal")
        self.assertIn("still unsent", pending[0]["detail"])

    def test_a_sent_proposal_gets_follow_ups_but_is_never_marked_anything(self):
        bp.mark_sent(self.conn, self.proposal["proposal_id"], "OpenTrain application form")
        sent_at = self.conn.execute("SELECT sent_at FROM biz_proposals WHERE id=?", (self.proposal["proposal_id"],)).fetchone()["sent_at"]
        now = datetime.now(timezone.utc)
        rm.generate(self.conn, now)
        self.assertEqual([r for r in rm.due_reminders(self.conn, now) if r["kind"] == "followup"], [])   # not three days later yet
        later = now + timedelta(days=4)
        rm.generate(self.conn, later)
        due = [r for r in rm.due_reminders(self.conn, later) if r["kind"] == "followup"]
        self.assertEqual(len(due), 1)
        self.assertIn("no reply recorded", due[0]["detail"])
        self.assertIn("never marks anything", due[0]["detail"])
        row = self.conn.execute("SELECT status, sent_at FROM biz_proposals WHERE id=?", (self.proposal["proposal_id"],)).fetchone()
        self.assertEqual((row["status"], row["sent_at"]), ("sent", sent_at))     # a reminder changes nothing

    def test_an_accepted_reply_dismisses_follow_ups_and_asks_you_to_start(self):
        bp.mark_sent(self.conn, self.proposal["proposal_id"], "OpenTrain application form")
        later = datetime.now(timezone.utc) + timedelta(days=4)
        rm.generate(self.conn, later)
        self.assertTrue(any(r["kind"] == "followup" for r in rm.list_reminders(self.conn, later)))
        bp.record_reply(self.conn, self.proposal["proposal_id"], "You are hired, AI help is fine.", "accepted")
        rm.generate(self.conn, later)
        pending = rm.list_reminders(self.conn, later)
        self.assertEqual([r["kind"] for r in pending], ["decision"])
        self.assertIn("written permission", pending[0]["detail"])

    def test_a_client_reply_gets_an_answer_task_and_a_decline_dismisses_everything(self):
        bp.mark_sent(self.conn, self.proposal["proposal_id"], "OpenTrain application form")
        bp.record_reply(self.conn, self.proposal["proposal_id"], "Can you start Monday?", "replied")
        now = datetime.now(timezone.utc)
        rm.generate(self.conn, now)
        pending = rm.list_reminders(self.conn, now)
        self.assertEqual([r["kind"] for r in pending], ["task"])
        self.assertIn("Reply to the client", pending[0]["title"])
        bp.record_reply(self.conn, self.proposal["proposal_id"], "No thanks", "declined")
        rm.generate(self.conn, now)
        self.assertEqual(rm.list_reminders(self.conn, now), [])                  # nothing to chase any more


if __name__ == "__main__":
    unittest.main()
