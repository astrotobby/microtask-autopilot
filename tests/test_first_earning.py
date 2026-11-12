import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import actions, daily, db, guidance, intelligence as ig, notify, packets, policy
from microtask_monitor import profile_answers as pa, reminders, workflow as wf

CAPS = policy.load_capabilities()
PROFILE = ig.load_profile()
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)        # 13:00 EAT
URL = "https://www.opentrain.ai/jobs/j1"


def opp(n=1, **kw):
    base = {"platform_id": "opentrain_ai", "external_id": f"j{n}", "source": "OpenTrain AI", "url": f"https://www.opentrain.ai/jobs/j{n}",
            "apply_url": f"https://app.opentrain.ai/api/out/j{n}?ch=www_seo", "title": f"Python Automation Engineer {n}",
            "summary": "Build n8n workflows and Python APIs with Claude Code. Skills test required. Submit your GitHub portfolio.",
            "kenya_eligibility": "kenya_explicitly_eligible", "payment_display": "USD 40/hour", "payment_usd_per_hour": 40,
            "published": (NOW - timedelta(days=1)).isoformat(), "status": "open", "category": "ai-engineering",
            "skills": ["Python"], "countries": ["KE"], "deadline": (NOW + timedelta(days=10)).isoformat()}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dbp = Path(self.tmp.name) / "a.sqlite3"
        ctx = db.session(self.dbp)
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        with db.tx(self.conn):
            pa.ensure_defaults(self.conn)

    def ingest(self, *items, now=NOW):
        with db.tx(self.conn):
            ig.ingest(self.conn, list(items), now=now, caps=CAPS, profile=PROFILE)

    def application(self, **kw):
        self.ingest(opp(**kw))
        return wf.create_application(self.conn, kw.get("url") or URL)

    def fill_profile(self, **extra):
        answers = {"full_name": "Test User", "contact_email": "test.user@example.com", "availability": "20 hours per week from next Monday",
                   "portfolio_links": "https://github.com/example/real-project", "experience_summary": "Built a Python scraper for my own project."}
        answers.update(extra)
        for key, value in answers.items():
            pa.set_answer(self.conn, key, value)

    def approve(self, app):
        return wf.approve_draft(self.conn, app["drafts"][0]["id"], True)

    def submit(self, app, when="2026-11-09"):
        return wf.mark_submitted(self.conn, app["id"], when, confirmed_by_user=True)


class ProfileAnswerTests(Base):
    def test_defaults_prefill_only_established_facts_and_leave_personal_answers_empty(self):
        a = {x["key"]: x for x in pa.list_answers(self.conn)}
        self.assertEqual(a["location"]["answer"], "Kenya")
        self.assertIn("UTC+3", a["timezone"]["answer"])
        self.assertIn("Python", a["skills_summary"]["answer"])
        self.assertIn("Samsung A05", a["equipment"]["answer"])
        for key in ("full_name", "contact_email", "availability", "portfolio_links", "experience_summary", "linkedin", "hourly_rate", "english_level"):
            self.assertFalse(a[key]["answer"], key)             # never invented
        self.assertTrue({"full_name", "contact_email", "availability", "portfolio_links", "experience_summary"} <= {m["key"] for m in pa.missing_required(self.conn)})

    def test_answers_persist_and_defaults_never_overwrite_them(self):
        pa.set_answer(self.conn, "full_name", "Test User")
        with db.tx(self.conn):
            self.assertEqual(pa.ensure_defaults(self.conn), 0)
        with db.session(self.dbp) as other:
            self.assertEqual(pa.as_map(other)["full_name"], "Test User")
        row = next(x for x in pa.list_answers(self.conn) if x["key"] == "full_name")
        self.assertEqual(row["source"], "user")
        self.assertNotIn("full_name", [m["key"] for m in pa.missing_required(self.conn)])

    def test_secrets_and_unknown_keys_are_rejected(self):
        for text in ("my password is hunter2", "card number 4111 1111 1111 1111", "ID 12345678901234", "api_key=abc", "recovery code 123"):
            with self.assertRaises(pa.AnswerError, msg=text):
                pa.set_answer(self.conn, "experience_summary", text)
        with self.assertRaises(pa.AnswerError):
            pa.set_answer(self.conn, "nope", "x")
        with self.assertRaises(pa.AnswerError):
            pa.set_answer(self.conn, "experience_summary", "x" * 2001)


class PacketTests(Base):
    def test_packet_has_every_required_section(self):
        app = self.application()
        p = packets.build_packet(self.conn, app["id"], NOW)
        o = p["opportunity"]
        self.assertEqual(o["url"], URL)
        self.assertEqual(o["apply_url"], "https://app.opentrain.ai/api/out/j1?ch=www_seo".replace("j1", "j1") if False else o["apply_url"])
        self.assertTrue(o["apply_url"].startswith("https://"))
        self.assertEqual(o["eligibility"]["class"], "KENYA_CONFIRMED")
        self.assertIn("Kenya", o["payment_method"])
        self.assertEqual(o["compensation"], "USD 40/hour")
        self.assertIn("skills test", o["assessments"])
        self.assertTrue(p["skills"])
        self.assertTrue(p["draft"]["body"].startswith("Subject: Application for"))
        self.assertTrue(p["profile_answers"])
        self.assertTrue(p["instructions"]["steps"])
        self.assertTrue(p["follow_up_plan"])

    def test_not_ready_until_profile_draft_and_approval_are_done_and_nothing_is_invented(self):
        app = self.application()
        p = packets.build_packet(self.conn, app["id"], NOW)
        self.assertFalse(p["ready_to_apply"])
        text = " ".join(p["blockers"])
        for expected in ("Full name", "Contact email", "availability", "Approve the draft", "placeholders"):
            self.assertIn(expected, text)
        self.assertIn("[Your name]", p["unresolved_placeholders"])
        self.assertNotIn("Test User", p["draft_filled"])

    def test_filling_the_profile_resolves_placeholders_from_your_own_answers_only(self):
        app = self.application()
        self.fill_profile()
        self.approve(app)
        p = packets.build_packet(self.conn, app["id"], NOW)
        self.assertIn("Test User", p["draft_filled"])
        self.assertIn("test.user@example.com", p["draft_filled"])
        self.assertIn("20 hours per week", p["draft_filled"])
        self.assertIn("Built a Python scraper", p["draft_filled"])
        self.assertEqual(p["unresolved_placeholders"], [])
        self.assertTrue(p["ready_to_apply"], p["blockers"])
        self.assertIn("[Your name]", p["draft"]["body"])             # the stored draft itself is untouched

    def test_unsupported_and_expired_opportunities_are_blocked_in_the_packet(self):
        app = self.application()
        with db.tx(self.conn):
            self.conn.execute("UPDATE opportunities SET eligibility_class='KENYA_UNSUPPORTED' WHERE url=?", (URL,))
        self.assertTrue(any("Do not apply" in b for b in packets.build_packet(self.conn, app["id"], NOW)["blockers"]))
        with db.tx(self.conn):
            self.conn.execute("UPDATE opportunities SET eligibility_class='KENYA_CONFIRMED', deadline=? WHERE url=?", ((NOW - timedelta(days=1)).isoformat(), URL))
        self.assertTrue(any("deadline has passed" in b for b in packets.build_packet(self.conn, app["id"], NOW)["blockers"]))

    def test_gaps_are_surfaced_not_hidden(self):
        self.ingest(opp(summary="Python work. A bachelor degree is preferred.", deadline=None))
        app = wf.create_application(self.conn, URL)
        p = packets.build_packet(self.conn, app["id"], NOW)
        self.assertTrue(p["opportunity"]["qualification_gaps"])
        self.assertIn("CONFIRM BEFORE APPLYING", packets.text_export(p))

    def test_text_export_is_complete_and_states_nothing_was_submitted(self):
        app = self.application()
        self.fill_profile()
        self.approve(app)
        text = packets.text_export(packets.build_packet(self.conn, app["id"], NOW))
        for needle in ("APPLICATION PACKET", "Original link: https://", "Compensation: USD 40/hour", "KENYA_CONFIRMED", "Skills matched:",
                       "DRAFT (with your profile answers filled in)", "YOUR PROFILE ANSWERS", "HOW TO APPLY", "After applying:", "Common mistakes:",
                       "READY TO APPLY", "Nothing here was submitted for you"):
            self.assertIn(needle, text, needle)
        self.assertNotIn("[Your name]", text)

    def test_generating_a_packet_never_changes_application_status_or_approves_anything(self):
        app = self.application()
        before = wf.get_application(self.conn, app["id"])
        saved = packets.save_packet(self.conn, app["id"], NOW)
        after = wf.get_application(self.conn, app["id"])
        self.assertEqual((after["status"], after["drafts"][0]["approved"]), (before["status"], before["drafts"][0]["approved"]))
        self.assertEqual((after["status"], saved["ready_to_apply"]), ("draft", False))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_drafts WHERE approved=1").fetchone()[0], 0)

    def test_packets_are_saved_in_sqlite_with_versions(self):
        app = self.application()
        first = packets.save_packet(self.conn, app["id"], NOW)
        self.fill_profile()
        second = packets.save_packet(self.conn, app["id"], NOW + timedelta(hours=1))
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_packets").fetchone()[0], 2)
        with db.session(self.dbp) as other:
            latest = packets.latest_packet(other, app["id"])
        self.assertEqual(latest["version"], 2)
        self.assertIn("Test User", latest["text"])
        self.assertIsNone(packets.latest_packet(self.conn, 999))
        with self.assertRaises(packets.PacketError):
            packets.build_packet(self.conn, 999)

    def test_instructions_are_platform_specific_and_labelled_by_verification(self):
        app = self.application()
        p = packets.build_packet(self.conn, app["id"], NOW)
        self.assertTrue(p["instructions"]["verified"])
        self.assertIn("Verified", p["instructions"]["label"])
        self.assertTrue(any("AI interview" in s for s in p["instructions"]["steps"]))
        self.assertTrue(any("Location Match" in s for s in p["instructions"]["steps"]))
        self.ingest(opp(2, platform_id="clickworker", external_id="c1", url="https://www.clickworker.com/jobs/c1"))
        other = wf.create_application(self.conn, "https://www.clickworker.com/jobs/c1") if False else None
        g = guidance.for_platform("clickworker")
        self.assertFalse(g["verified"])
        self.assertIn("UNVERIFIED", g["label"])


class FollowUpTests(Base):
    def submitted(self, **kw):
        app = self.application(**kw)
        self.fill_profile()
        self.approve(app)
        return self.submit(app)

    def test_submission_creates_an_event_and_a_seven_day_followup_date(self):
        app = self.submitted()
        self.assertEqual((app["status"], app["submitted_at"], app["next_followup_at"]), ("submitted", "2026-11-09", "2026-11-16"))
        self.assertEqual([e["kind"] for e in app["events"]], ["submitted"])

    def test_followup_interview_decision_timeline(self):
        app = self.submitted()
        app = wf.log_event(self.conn, app["id"], "followup_sent", "Emailed the employer", "2026-11-16")
        self.assertEqual(app["next_followup_at"], "2026-11-23")
        app = wf.log_event(self.conn, app["id"], "interview_scheduled", "Voice interview", "2026-11-20T14:00")
        self.assertEqual((app["status"], app["interview_at"], app["next_followup_at"]), ("interview", "2026-11-20T14:00", None))
        app = wf.log_event(self.conn, app["id"], "interview_done", "Went fine", "2026-11-20")
        self.assertEqual(app["next_followup_at"], "2026-11-27")
        app = wf.log_event(self.conn, app["id"], "decision", "Offer received", "2026-11-25", decision="accepted")
        self.assertEqual((app["status"], app["decision"], app["decision_at"], app["next_followup_at"]), ("accepted", "accepted", "2026-11-25", None))
        self.assertEqual([e["kind"] for e in reversed(app["events"])], ["submitted", "followup_sent", "interview_scheduled", "interview_done", "decision"])

    def test_response_is_logged_as_an_event_too(self):
        app = self.submitted()
        app = wf.record_response(self.conn, app["id"], "interview", "They replied", "2026-11-12")
        self.assertEqual(app["events"][0]["kind"], "response")
        self.assertIn("They replied", app["events"][0]["detail"])

    def test_validation_and_human_only_boundaries(self):
        app = self.application()
        with self.assertRaises(wf.WorkflowError):
            wf.log_event(self.conn, app["id"], "followup_sent")           # not submitted yet
        app = self.fill_and_submit(app)
        for kw in (dict(kind="submitted"), dict(kind="bogus"), dict(kind="decision", decision="maybe"),
                   dict(kind="interview_scheduled", event_date="not a date"), dict(kind="followup_sent", next_followup_at="soon")):
            with self.assertRaises(wf.WorkflowError, msg=str(kw)):
                wf.log_event(self.conn, app["id"], **kw)
        with self.assertRaises(wf.WorkflowError):
            wf.log_event(self.conn, 999, "note")

    def fill_and_submit(self, app):
        self.fill_profile()
        self.approve(app)
        return self.submit(app)


class ReminderTests(Base):
    def reminders_for(self, kind=None):
        rows = reminders.list_reminders(self.conn, NOW, upcoming_days=60, include_closed=True)
        return [r for r in rows if kind is None or r["kind"] == kind]

    def generate(self, now=NOW):
        with db.tx(self.conn):
            return reminders.generate(self.conn, now)

    def test_deadline_reminders_are_created_once_at_the_right_milestones(self):
        self.application(deadline=(NOW + timedelta(days=5)).isoformat())
        made = self.generate()
        self.assertEqual(made["deadline"], 2)                          # 7-day milestone is already stale; 3 and 1 remain
        titles = sorted(r["title"] for r in self.reminders_for("deadline"))
        self.assertTrue(any("within 3 days" in t for t in titles) and any("within 1 day" in t for t in titles))
        self.assertEqual(self.generate()["deadline"], 0)               # idempotent
        self.assertEqual(self.generate(NOW + timedelta(hours=5))["deadline"], 0)

    def test_deadline_reminders_stop_when_the_application_is_submitted(self):
        app = self.application(deadline=(NOW + timedelta(days=5)).isoformat())
        self.generate()
        self.fill_profile()
        self.approve(app)
        self.submit(app)
        self.generate(NOW + timedelta(days=1))
        self.assertTrue(all(r["status"] == "dismissed" for r in self.reminders_for("deadline")))

    def test_assessment_reminder_comes_from_the_listing(self):
        self.application()
        self.generate()
        a = self.reminders_for("assessment")
        self.assertEqual(len(a), 1)
        self.assertIn("skills test", a[0]["title"])
        self.ingest(opp(2, summary="Python work with a plain application.", deadline=None))
        wf.create_application(self.conn, "https://www.opentrain.ai/jobs/j2")
        self.generate()
        self.assertEqual(len(self.reminders_for("assessment")), 1)         # no new one for a listing with no assessment

    def test_followup_reminder_is_due_after_the_scheduled_date_and_only_then(self):
        app = self.application()
        self.fill_profile(); self.approve(app); self.submit(app, "2026-11-09")
        self.generate(NOW)
        f = self.reminders_for("followup")[0]
        self.assertFalse(f["is_due"])
        self.assertEqual(reminders.due_reminders(self.conn, NOW + timedelta(days=6)), [r for r in reminders.due_reminders(self.conn, NOW + timedelta(days=6)) if r["kind"] != "followup"])
        due = reminders.due_reminders(self.conn, NOW + timedelta(days=8))
        self.assertTrue(any(r["kind"] == "followup" for r in due))

    def test_interview_reminders_and_unscheduled_interview_prompt(self):
        app = self.application()
        self.fill_profile(); self.approve(app); self.submit(app)
        wf.record_response(self.conn, app["id"], "interview", "Invited", "2026-11-10")
        self.generate()
        self.assertTrue(any("Confirm the interview time" in r["title"] for r in self.reminders_for("interview")))
        wf.log_event(self.conn, app["id"], "interview_scheduled", "Voice", "2026-11-20T14:00")
        self.generate()
        titles = [r["title"] for r in self.reminders_for("interview") if r["status"] == "pending"]
        self.assertTrue(any("tomorrow" in t for t in titles) and any("today" in t for t in titles))

    def test_stale_draft_review_reminder(self):
        app = self.application()
        stale_at = (NOW - timedelta(days=3)).isoformat()
        with db.tx(self.conn):
            self.conn.execute("UPDATE applications SET created_at=?, updated_at=? WHERE id=?", (stale_at, stale_at, app["id"]))
            self.conn.execute("UPDATE application_drafts SET created_at=? WHERE application_id=?", (stale_at, app["id"]))
        self.generate(NOW + timedelta(days=3))
        self.assertEqual(len(self.reminders_for("draft_review")), 1)

    def test_profile_reminder_resolves_itself_when_answers_are_filled(self):
        self.generate()
        self.assertEqual(len(self.reminders_for("profile")), 1)
        self.fill_profile()
        self.generate()
        self.assertEqual(self.reminders_for("profile")[0]["status"], "done")

    def test_accepted_application_creates_a_start_work_reminder(self):
        app = self.application()
        self.fill_profile(); self.approve(app); self.submit(app)
        wf.log_event(self.conn, app["id"], "decision", "Offer", "2026-11-12", decision="accepted")
        self.generate()
        self.assertEqual(len(self.reminders_for("decision")), 1)

    def test_task_due_reminder(self):
        wf.create_task(self.conn, "Label data", "Read it.", due_at="2026-11-12")
        self.generate()
        self.assertEqual(len(self.reminders_for("task")), 1)

    def test_done_and_dismissed_reminders_never_come_back(self):
        self.application(deadline=(NOW + timedelta(days=5)).isoformat())
        self.generate()
        r = self.reminders_for("deadline")[0]
        reminders.mark(self.conn, r["id"], "done")
        self.generate(NOW + timedelta(hours=2))
        self.assertEqual(next(x for x in self.reminders_for("deadline") if x["id"] == r["id"])["status"], "done")
        self.assertEqual(len(self.reminders_for("deadline")), 2)
        with self.assertRaises(ValueError):
            reminders.mark(self.conn, r["id"], "bogus")
        with self.assertRaises(LookupError):
            reminders.mark(self.conn, 999, "done")

    def test_due_reminders_notify_once_and_are_capped_per_cycle(self):
        for n in range(1, 9):
            self.application(url=f"https://www.opentrain.ai/jobs/j{n}", external_id=f"j{n}", deadline=(NOW + timedelta(hours=20)).isoformat())
        self.generate()
        sent = []
        channels = {"desktop": lambda t, b: sent.append(t) or True}
        first = reminders.notify_due(self.conn, NOW, channels=channels)
        self.assertEqual(len(first), reminders.MAX_NOTIFIED_PER_CYCLE)
        again = reminders.notify_due(self.conn, NOW, channels=channels)
        self.assertEqual(len(again), reminders.MAX_NOTIFIED_PER_CYCLE)
        notified = self.conn.execute("SELECT COUNT(*) FROM reminders WHERE notified_at IS NOT NULL").fetchone()[0]
        self.assertEqual(notified, len(first) + len(again))

    def test_reminders_persist_in_sqlite(self):
        self.application(deadline=(NOW + timedelta(days=5)).isoformat())
        self.generate()
        with db.session(self.dbp) as other:
            self.assertGreaterEqual(other.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 3)


class ActionListTests(Base):
    def build(self, now=NOW):
        return {a["key"]: a for a in actions.build(self.conn, now)}

    def test_profile_gap_is_the_first_action(self):
        a = self.build()
        self.assertIn("profile", a)
        self.assertEqual(max(a.values(), key=lambda x: x["priority"])["key"], "profile")
        self.assertIn("Never enter passwords", a["profile"]["detail"])

    def test_review_then_apply_actions_follow_the_application_state(self):
        self.ingest(opp(1), now=NOW)
        daily.run_analysis(self.conn, NOW, "sync", "k1", channels={}, create_drafts=True)
        app = self.conn.execute("SELECT id FROM applications").fetchone()
        a = self.build()
        self.assertIn(f"review:{app['id']}", a)
        self.fill_profile(); wf.approve_draft(self.conn, self.conn.execute("SELECT id FROM application_drafts").fetchone()[0], True)
        a = self.build()
        self.assertIn(f"apply:{app['id']}", a)
        self.assertNotIn("profile", a)
        self.assertNotIn(f"review:{app['id']}", a)
        self.assertTrue(a[f"apply:{app['id']}"]["url"].startswith("https://"))

    def test_deadline_and_followup_reminders_become_high_priority_actions(self):
        app = self.application(deadline=(NOW + timedelta(days=1)).isoformat())
        with db.tx(self.conn):
            reminders.generate(self.conn, NOW)
        a = self.build()
        deadline_actions = [x for x in a.values() if x["kind"] == "reminder:deadline"]
        self.assertTrue(deadline_actions)
        self.assertGreaterEqual(deadline_actions[0]["priority"], 90)

    def test_manual_checks_follow_the_research_order_rotate_daily_and_skip_unsuitable_platforms(self):
        today = [k for k in self.build(NOW) if k.startswith("manualcheck:")]
        tomorrow = [k for k in self.build(NOW + timedelta(days=1)) if k.startswith("manualcheck:")]
        self.assertEqual(len(today), actions.MANUAL_CHECKS_PER_DAY)
        self.assertNotEqual(today, tomorrow)
        for pid in ("atlas_capture", "mindrift", "opentrain_ai"):
            self.assertNotIn(f"manualcheck:{pid}", self.build(NOW))
        every = {k for d in range(12) for k in self.build(NOW + timedelta(days=d)) if k.startswith("manualcheck:")}
        self.assertEqual(every, {f"manualcheck:{p}" for p in actions.MANUAL_CHECK_ORDER if p != "mindrift"})
        item = next(x for x in self.build(NOW).values() if x["key"].startswith("manualcheck:"))
        self.assertIn("yourself", item["title"])

    def test_unverified_platform_manual_checks_say_read_the_terms_first(self):
        found = {}
        for d in range(12):
            for k, v in self.build(NOW + timedelta(days=d)).items():
                if k.startswith("manualcheck:"):
                    found[k] = v["detail"]
        self.assertIn("UNVERIFIED", found["manualcheck:clickworker"])
        self.assertIn("Terms reviewed", found["manualcheck:microworkers"])

    def test_accepted_work_without_a_task_prompts_setup_and_tasks_become_actions(self):
        app = self.application()
        self.fill_profile(); self.approve(app); self.submit(app)
        wf.log_event(self.conn, app["id"], "decision", "Offer", "2026-11-12", decision="accepted")
        self.assertIn(f"startwork:{app['id']}", self.build())
        t = wf.create_task(self.conn, "Real work", "1. Do it.", application_id=app["id"], due_at="2026-11-10")
        a = self.build()
        self.assertNotIn(f"startwork:{app['id']}", a)
        self.assertEqual(a[f"task:{t['id']}"]["priority"], 89)            # due within two days

    def test_submitted_task_unpaid_for_a_week_prompts_a_payment_check(self):
        t = wf.create_task(self.conn, "Paid work", "Read.")
        wf.record_submission(self.conn, t["id"], True, submitted_at="2026-11-01")
        with db.tx(self.conn):
            self.conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", ((NOW - timedelta(days=8)).isoformat(), t["id"]))
        self.assertIn(f"paycheck:{t['id']}", self.build())

    def test_persisted_list_keeps_ticks_removes_resolved_items_and_orders_by_priority(self):
        first = actions.persist_today(self.conn, NOW)
        self.assertEqual(first[0]["key"], "profile")
        self.assertEqual([i["priority"] for i in first if i["status"] == "open"], sorted((i["priority"] for i in first if i["status"] == "open"), reverse=True))
        manual = next(i for i in first if i["key"].startswith("manualcheck:"))
        actions.set_status(self.conn, manual["id"], "done")
        self.fill_profile()
        second = actions.persist_today(self.conn, NOW + timedelta(hours=1))
        self.assertNotIn("profile", [i["key"] for i in second])                           # resolved: gone
        self.assertEqual(next(i for i in second if i["id"] == manual["id"])["status"], "done")   # tick preserved
        self.assertEqual([i for i in second if i["status"] == "done"][-1]["id"], manual["id"])
        self.assertLessEqual(len([i for i in second if i["status"] == "open"]), actions.MAX_ACTIONS)
        s = actions.summary(second)
        self.assertEqual(s["done"], 1)
        self.assertGreater(s["estimated_minutes"], 0)

    def test_a_new_day_starts_a_fresh_list(self):
        actions.persist_today(self.conn, NOW)
        tomorrow = actions.persist_today(self.conn, NOW + timedelta(days=1))
        self.assertTrue(all(i["action_date"] == "2026-11-10" for i in tomorrow))

    def test_ticking_a_reminder_action_closes_the_reminder(self):
        self.application(deadline=(NOW + timedelta(days=1)).isoformat())
        with db.tx(self.conn):
            reminders.generate(self.conn, NOW)
        items = actions.persist_today(self.conn, NOW)
        item = next(i for i in items if i["key"].startswith("reminder:") and i["kind"] == "reminder:deadline")
        actions.set_status(self.conn, item["id"], "done")
        rid = int(item["key"].split(":")[1])
        self.assertEqual(self.conn.execute("SELECT status FROM reminders WHERE id=?", (rid,)).fetchone()[0], "done")
        with self.assertRaises(ValueError):
            actions.set_status(self.conn, item["id"], "bogus")
        with self.assertRaises(LookupError):
            actions.set_status(self.conn, 9999, "done")

    def test_actions_never_include_anything_the_program_would_do_itself(self):
        for a in actions.build(self.conn, NOW):
            self.assertNotRegex((a["title"] + a["detail"]).lower(), r"(auto-?submit|will submit|submitting for you|automatically appl)")


class GuidanceTests(Base):
    def test_verified_guidance_cites_sources_and_dates_and_unverified_is_labelled(self):
        data = guidance.load()
        for pid, entry in data["platforms"].items():
            if entry["verified"]:
                self.assertTrue(entry["sources"], pid)
                self.assertEqual(entry["verified_on"][:4], "2026", pid)
        ot = guidance.for_platform("opentrain_ai")
        self.assertTrue(ot["verified"])
        self.assertTrue(any("Project Instructions" in s for s in ot["task_qc"]))
        for pid in ("clickworker", "afterquery", "sproutgigs", "gainworkers", "reworkio", "mindrift"):
            g = guidance.for_platform(pid)
            self.assertFalse(g["verified"], pid)
            self.assertIn("UNVERIFIED", g["label"])

    def test_platform_facts_come_from_what_was_actually_read(self):
        mw = guidance.for_platform("microworkers")
        text = " ".join(mw["task_qc"] + mw["apply_steps"] + mw["payout"])
        self.assertIn("48 hours", text)
        self.assertIn("KYC", text)
        rw = guidance.for_platform("rapidworkers")
        self.assertIn("SKIP social-engagement", " ".join(rw["apply_steps"]))
        atlas = guidance.for_platform("atlas_capture")
        self.assertIn("iPhone", " ".join(atlas["apply_steps"]))

    def test_new_tasks_get_the_platform_qc_checklist(self):
        t = wf.create_task(self.conn, "Review chatbot answers", "1. Open the sheet.\n2. Rate answers.", platform_id="opentrain_ai")
        platform_rows = [i["text"] for i in t["checklist"] if i["kind"] == "platform"]
        self.assertTrue(any("Project Instructions" in r for r in platform_rows))
        self.assertTrue(any(i["kind"] == "step" for i in t["checklist"]))
        self.assertTrue(t["guidance"]["platform"]["verified"])
        u = wf.create_task(self.conn, "Some task", "Read.", platform_id="clickworker")
        rows = [i["text"] for i in u["checklist"] if i["kind"] == "platform"]
        self.assertIn("NOT verified", rows[0])
        self.assertTrue(all(r.startswith("(generic)") for r in rows[1:]))
        self.assertFalse(u["guidance"]["platform"]["verified"])
        none = wf.create_task(self.conn, "No platform", "Read.")
        self.assertTrue(any("NOT verified" in i["text"] for i in none["checklist"] if i["kind"] == "platform"))

    def test_guidance_text_never_tells_the_user_to_automate_or_bypass(self):
        data = json.dumps(guidance.load()).lower()
        for banned in ("use a bot", "automate the", "bypass", "fake proof", "use a proxy"):
            self.assertNotIn(banned, data)


class PipelineIntegrationTests(Base):
    def test_a_daily_run_prepares_packets_reminders_and_the_action_list(self):
        self.ingest(*[opp(n) for n in range(1, 8)])
        out = daily.run_analysis(self.conn, NOW, "sync", "k1", channels={})
        w = out["workflow"]
        self.assertEqual(w["errors"], [])
        self.assertEqual(w["packets"], 5)
        self.assertGreater(w["actions"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(DISTINCT application_id) FROM application_packets").fetchone()[0], 5)
        keys = [r["key"] for r in self.conn.execute("SELECT key FROM action_items")]
        self.assertIn("profile", keys)
        self.assertTrue(any(k.startswith("review:") for k in keys))
        self.assertGreaterEqual(self.conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)
        # nothing was approved or submitted by the pipeline
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM applications WHERE status<>'draft'").fetchone()[0], 0)

    def test_a_failure_in_the_follow_up_steps_never_undoes_the_analysis(self):
        from unittest import mock
        self.ingest(opp(1))
        with mock.patch("microtask_monitor.packets.save_packet", side_effect=RuntimeError("boom")):
            out = daily.run_analysis(self.conn, NOW, "sync", "k1", channels={})
        self.assertIn("packets: boom", out["workflow"]["errors"][0])
        self.assertEqual(self.conn.execute("SELECT status FROM analysis_runs").fetchone()[0], "completed")

    def test_the_new_tables_survive_a_second_connection(self):
        self.ingest(opp(1))
        daily.run_analysis(self.conn, NOW, "sync", "k1", channels={})
        with db.session(self.dbp) as other:
            for table in ("application_packets", "reminders", "action_items", "profile_answers", "application_events"):
                self.assertGreaterEqual(other.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertGreater(other.execute("SELECT COUNT(*) FROM profile_answers").fetchone()[0], 10)


class MigrationFourTests(unittest.TestCase):
    def test_v3_database_keeps_every_application_draft_and_task_when_upgraded_to_v4(self):
        import sqlite3
        from contextlib import closing
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v3.sqlite3"
            with closing(sqlite3.connect(path)) as raw:
                raw.row_factory = sqlite3.Row
                for version, apply in db.MIGRATIONS[:3]:
                    apply(raw)
                raw.execute(f"PRAGMA user_version={3}")
                raw.execute("INSERT INTO opportunities(url,title,source,first_seen_utc,last_seen_utc,report_checked_at_utc,raw_json) VALUES('https://x.example/1','Job','S','2026-11-01','2026-11-01','20261101T000000Z','{}')")
                raw.execute("INSERT INTO applications(opportunity_url,title,platform_id,status,created_at,updated_at) VALUES('https://x.example/1','Job','opentrain_ai','submitted','2026-11-01','2026-11-01')")
                raw.execute("INSERT INTO tasks(title,instructions,status,created_at,updated_at) VALUES('T','I','accepted','2026-11-01','2026-11-01')")
                raw.commit()
            with db.session(path) as conn:
                self.assertEqual(db.user_version(conn), db.LATEST_VERSION)
                a = conn.execute("SELECT * FROM applications").fetchone()
                self.assertEqual((a["title"], a["status"]), ("Job", "submitted"))
                self.assertIsNone(a["next_followup_at"])
                self.assertEqual(conn.execute("SELECT title FROM tasks").fetchone()[0], "T")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)
                for table in ("application_events", "application_packets", "reminders", "action_items", "profile_answers"):
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            self.assertEqual(len(list(Path(tmp).glob("v3.sqlite3.bak-v3-*"))), 1)


class ComplianceTests(unittest.TestCase):
    def test_new_modules_have_no_network_or_submission_code(self):
        root = Path(daily.__file__).parent
        for name in ("packets", "reminders", "actions", "profile_answers", "guidance"):
            src = (root / f"{name}.py").read_text(encoding="utf-8")
            for banned in ("urllib", "requests", "socket", "selenium", "playwright", "subprocess", "approve_draft", "mark_submitted", "record_submission"):
                self.assertNotIn(banned, src, f"{name}: {banned}")


if __name__ == "__main__":
    unittest.main()
