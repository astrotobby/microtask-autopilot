"""Freelance business engine: configuration, lead discovery and scoring, estimation and pricing, proposals (modes A/B/C),
projects (award, isolated execution, independent QA, handover, delivery, scope control), finance (invoices, unified
payments, costs, profitability, gap analysis, monthly review), recurring revenue, the scheduled cycle, API, website and
dashboard. Synthetic clients, temporary databases, fake feeds, a fake AI runner and a fake mailer only: nothing is sent.
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

from microtask_monitor import (biz_api, biz_config as bc, biz_engine as be, biz_finance as bf, biz_leads as bl, biz_portfolio as bport, biz_projects as bpj,
                               biz_proposals as bp, control, db, ledger, workers)

ROOT = Path(__file__).resolve().parents[1]
ENQUIRY = {"name": "Jane Doe", "email": "jane@example.org", "company": "Doe Weather Ltd", "subject": "Python REST API integration for our weather dashboard", "budget": 400,
           "message": "We need a Python backend that integrates the OpenWeather REST API and stores readings in SQLite. It must convert Fahrenheit to Celsius, "
                      "expose a /readings endpoint, and include tests. Deliver within 3 weeks. Payment by milestones."}


class Runner:
    name = "fake-ai"

    def __init__(self, broken=False):
        self.broken, self.calls = broken, 0

    def run(self, prompt, cwd, timeout, should_stop, log):
        self.calls += 1
        out = Path(cwd) / "output"
        (out / "tests").mkdir(parents=True, exist_ok=True)
        (out / "weather.py").write_text("def to_celsius(f):\n    return round((f - 32) * 5 / 9, 2)\n", encoding="utf-8")
        expected = 99 if self.broken else 100
        (out / "tests" / "test_weather.py").write_text("import unittest\nfrom weather import to_celsius\n"
                                                       f"class T(unittest.TestCase):\n    def test_c(self):\n        self.assertEqual(to_celsius(212), {expected})\n", encoding="utf-8")
        (out / "README.md").write_text("# Weather helper\n\nRun `python -m unittest discover -s tests`.\n", encoding="utf-8")
        (out / "NOTES.md").write_text("Implemented to_celsius with a unit test.", encoding="utf-8")
        return workers.RunResult(0, "done")


class Mailer:
    address = "me@example.com"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.dbp, self.execs, self.ucfg = t / "a.sqlite3", t / "exec", t / "biz.json"
        for target, value in ((bc, ("USER_PATH", self.ucfg)), (bpj, ("PROJECTS_DIR", t / "projects")), (be, ("INBOX", t / "inbox")),
                              (bport, ("SITE_DIR", t / "site")), (bc, ("OUTREACH_PATH", t / "outreach.json"))):
            p = mock.patch.object(target, *value)
            p.start()
            self.addCleanup(p.stop)
        (t / "outreach.json").write_text(json.dumps({"from_address": "owner@example.com", "sender_name": "Test Owner"}), encoding="utf-8")
        ctx = db.session(self.dbp)
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.probe = {"disk_mb": 50000, "memory_mb": 8000}

    def lead(self, form=None):
        bl.import_enquiry(self.conn, form or ENQUIRY)
        return bl.ranked(self.conn)[0]

    def won(self, **award):
        lead = self.lead()
        p = bp.propose(self.conn, lead["id"])
        bp.mark_sent(self.conn, p["proposal_id"], "Replied by email")
        bp.record_reply(self.conn, p["proposal_id"], "Go ahead, AI-assisted coding is fine.", "accepted")
        a = bpj.award(self.conn, p["proposal_id"], "Client email accepting the proposal", **{"ai_permitted": True, "ai_source": "Client email", **award})
        return lead, p, a["project_id"]

    def built(self, runner=None, **award):
        lead, p, pid = self.won(**award)
        bpj.start(self.conn, pid, exec_dir=self.execs)
        out = bpj.execute(self.conn, pid, runner or Runner(), self.dbp, self.execs, probe=self.probe)
        return pid, out


# --------------------------------------------------------------------------------------------- configuration and sources
class ConfigTests(Base):
    def test_defaults_merge_with_your_overrides(self):
        cfg = bc.save_overrides({"pricing": {"min_project_usd": 150}, "targets": {"primary": 600}})
        self.assertEqual((cfg["pricing"]["min_project_usd"], cfg["pricing"]["target_hourly_usd"], cfg["targets"]["primary"]), (150, 30, 600))

    def test_overrides_are_validated(self):
        for bad in ({"pricing": {"min_project_usd": -1}}, {"autonomy": {"deliver_by_email": "yes"}}, {"unknown": 1},
                    {"testimonials": [{"client": "A", "text": "Great"}]}):
            with self.assertRaises(bc.BizError):
                bc.save_overrides(bad)

    def test_profile_uses_only_your_stated_details(self):
        p = bc.profile()
        self.assertIn("Python", p["skills"])
        self.assertEqual(p["contact"], {"name": "Test Owner", "email": "owner@example.com"})
        self.assertTrue(p["not_established"])

    def test_sources_exclude_fiverr_and_upwork_and_respect_terms(self):
        ids = {s["id"]: s for s in bc.sources()}
        self.assertFalse(any("fiverr" in json.dumps(s).lower() or "upwork" in (s.get("url") or "") for s in ids.values()))
        self.assertEqual(set(bc.excluded_domains()), {"fiverr.com", "upwork.com"})
        self.assertEqual(ids["freelancer_com"]["policy"]["automated_proposals"], "prohibited")
        self.assertIsNone(ids["freelancer_com"]["fetch"])                               # no scraper where automation is not permitted
        for s in ids.values():
            if s.get("fetch") in ("rss", "hn_thread"):
                self.assertEqual(s["policy"]["discovery"], "allowed")
                self.assertTrue(s["url"].startswith("https://"))
        with self.assertRaises(bc.BizError):
            bc.source("fiverr")

    def test_portfolio_is_own_projects_and_no_testimonials_are_shipped(self):
        cfg = bc.load()
        self.assertTrue(all(p["kind"] == "own project" for p in cfg["portfolio"]))
        self.assertEqual(cfg["testimonials"], [])


# --------------------------------------------------------------------------------------------- leads
class ExtractionTests(unittest.TestCase):
    cfg = bc.load()

    def x(self, title, desc="", **kw):
        return bl.extract({"title": title, "description": desc, **kw}, self.cfg)

    def test_budgets(self):
        self.assertEqual(self.x("Fix API", "Budget $300")[ "budget_max"], 300)
        r = self.x("Build site", "Budget: $250-$500 fixed")
        self.assertEqual((r["budget_min"], r["budget_max"], r["budget_type"]), (250, 500, "fixed"))
        self.assertEqual(self.x("n8n help", "Paying $45/hr")["budget_type"], "hourly")
        self.assertEqual(self.x("Engineer", "$120k - $150k salary")["budget_type"], "annual")
        self.assertIsNone(self.x("Engineer", "no money mentioned")["budget_max"])

    def test_service_lines_need_real_evidence(self):
        self.assertEqual(self.x("n8n workflow to sync Airtable and Gmail")["service_line"], "automation")
        self.assertEqual(self.x("Python backend bug fix")["service_line"], "software")
        self.assertEqual(self.x("Edit 10 TikTok shorts with captions")["service_line"], "content")
        self.assertIsNone(self.x("Marketing coordinator", "We love our website.")["service_line"])

    def test_facts(self):
        r = self.x("Flask API", "We need a Flask REST API with Postgres. Must include tests. US only. 25 proposals so far. Payment verified. Deadline: 2026-12-20. Milestones.")
        self.assertTrue({"flask", "postgres", "rest"} <= set(r["technologies"]))
        self.assertEqual(r["deadline"], "2026-12-20")
        self.assertIn("US only", r["country_restrictions"])
        self.assertEqual(r["competition"], "25 proposals")
        self.assertTrue(r["client_history"] and r["payment_terms"])
        self.assertTrue(any("tests" in q for q in r["requirements"]))
        self.assertFalse(any("Deadline" in q for q in r["requirements"]))


class LeadTests(Base):
    def test_enquiry_becomes_a_qualified_lead_with_a_client(self):
        lead = self.lead()
        self.assertEqual((lead["status"], lead["service_line"], lead["source_id"]), ("qualified", "software", "website_enquiry"))
        self.assertGreaterEqual(lead["score"], bl.QUALIFY_AT)
        self.assertTrue(set(lead["score_parts"]) >= {"skill_match", "value", "clarity", "client_credibility", "win_probability", "portfolio_relevance", "policy"})
        self.assertEqual(self.conn.execute("SELECT name,email FROM biz_clients").fetchone()["email"], "jane@example.org")

    def test_duplicates_are_ignored(self):
        bl.import_enquiry(self.conn, ENQUIRY)
        self.assertEqual(bl.import_enquiry(self.conn, ENQUIRY)["duplicates"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM biz_leads").fetchone()[0], 1)

    def test_enquiry_validation(self):
        for bad in ({**ENQUIRY, "email": "nope"}, {**ENQUIRY, "message": "short"}, {**ENQUIRY, "name": ""}):
            with self.assertRaises(bc.BizError):
                bl.import_enquiry(self.conn, bad)

    def test_blockers(self):
        items = [{"title": "Senior Backend Engineer", "description": "Full-time role with benefits and equity. Python.", "job_board": True},
                 {"title": "Marketing Manager", "description": "Grow our website traffic and social media content."},
                 {"title": "Python script for clinical trial data", "description": "PhD in biology required. Python data cleaning."},
                 {"title": "Python bug fix", "description": "Fix a Python bug. Budget $40."},
                 {"title": "Telegram bot", "description": "Python bot. Pay a registration fee first. Telegram only."},
                 {"title": "Laravel PHP website", "description": "Build a PHP Laravel website."},
                 {"title": "React website", "description": "Build a React website. US only."}]
        bl.ingest(self.conn, items, "manual")
        reasons = {r["title"]: r["status_reason"] for r in self.conn.execute("SELECT title,status,status_reason FROM biz_leads")}
        statuses = {r["title"]: r["status"] for r in self.conn.execute("SELECT title,status FROM biz_leads")}
        self.assertTrue(all(s == "rejected" for s in statuses.values()), statuses)
        self.assertIn("employment role", reasons["Senior Backend Engineer"])
        self.assertIn("non-technical role", reasons["Marketing Manager"])
        self.assertIn("specialist", reasons["Python script for clinical trial data"])
        self.assertIn("below your minimum project price", reasons["Python bug fix"])
        self.assertIn("warning signs", reasons["Telegram bot"])
        self.assertIn("country restriction", reasons["React website"])

    def test_excluded_marketplaces(self):
        bl.ingest(self.conn, [{"title": "Python API", "description": "Build a Python API with tests and docs for our app.", "url": "https://www.upwork.com/jobs/1"}], "manual")
        self.assertIn("upwork.com is excluded", self.conn.execute("SELECT status_reason FROM biz_leads").fetchone()[0])
        with self.assertRaises(bc.BizError):
            bl.http_get("https://www.fiverr.com/x")
        with self.assertRaises(bc.BizError):
            bl.http_get("http://insecure.example.com/feed")


class SourceReaderTests(Base):
    def test_hn_reads_only_client_posts(self):
        pages = {bc.source("hn_seeking_freelancer")["url"]: {"submitted": [1, 2]},
                 "https://hacker-news.firebaseio.com/v0/item/1.json": {"title": "Ask HN: Who is hiring? (November 2026)", "kids": []},
                 "https://hacker-news.firebaseio.com/v0/item/2.json": {"title": "Ask HN: Freelancer? Seeking freelancer? (November 2026)", "kids": [10, 11, 12]},
                 "https://hacker-news.firebaseio.com/v0/item/10.json": {"by": "acme", "time": 1763000000, "text": "SEEKING FREELANCER | Remote<p>We need a Python developer to build a Flask REST API with Postgres and tests. Budget $800. Contact jobs@acme.example"},
                 "https://hacker-news.firebaseio.com/v0/item/11.json": {"by": "dev", "text": "SEEKING WORK | Python developer available"},
                 "https://hacker-news.firebaseio.com/v0/item/12.json": {"by": "x", "deleted": True}}
        http = lambda u: json.dumps(pages[u]).encode()                                     # noqa: E731
        r = bl.run_source(self.conn, "hn_seeking_freelancer", http, force=True)
        self.assertEqual((r["status"], r["seen"]), ("ok", 1))
        lead = bl.ranked(self.conn)[0]
        self.assertEqual((lead["contact"], lead["budget_max"], lead["status"]), ("jobs@acme.example", 800, "qualified"))
        self.assertEqual(bl.run_source(self.conn, "hn_seeking_freelancer", http)["status"], "skipped")   # respects its interval

    def test_rss_and_error_recording(self):
        feed = (b"<rss><channel><item><title>Acme: Contract Python developer</title><link>https://example.com/j/1</link>"
                b"<description>Freelance contract: build a FastAPI backend with tests. $50/hr.</description></item></channel></rss>")
        r = bl.run_source(self.conn, "wwr_programming", lambda u: feed, force=True)
        self.assertEqual((r["status"], r["new"]), ("ok", 1))
        def boom(u):
            raise OSError("network down")
        r = bl.run_source(self.conn, "himalayas", boom, force=True)
        self.assertEqual(r["status"], "error")
        self.assertIn("network down", self.conn.execute("SELECT last_error FROM biz_sources WHERE id='himalayas'").fetchone()[0])

    def test_hiring_thread_contract_posts_and_distinct_ids(self):
        now = int(__import__("time").time())
        pages = {bc.source("hn_seeking_freelancer")["url"]: {"submitted": [5]},
                 "https://hacker-news.firebaseio.com/v0/item/5.json": {"title": "Ask HN: Who is hiring? (November 2026)", "time": now, "kids": [20, 21, 22]},
                 "https://hacker-news.firebaseio.com/v0/item/20.json": {"by": "a", "text": "Acme | Contract | Remote<p>Freelance contract: build a Python FastAPI service with tests."},
                 "https://hacker-news.firebaseio.com/v0/item/21.json": {"by": "b", "text": "Beta | Full-time | Onsite<p>Senior engineer, salary and equity."},
                 "https://hacker-news.firebaseio.com/v0/item/22.json": {"by": "c", "text": "Gamma | Contract | Remote<p>Contract n8n workflow automation with webhooks and Airtable."},
                 "https://hacker-news.firebaseio.com/v0/item/23.json": {"by": "d", "text": "SEEKING FREELANCE - Egypt - Remote<p>Python developer available for contract work."},
                 "https://hacker-news.firebaseio.com/v0/item/24.json": {"by": "e", "text": "Delta | Contract | REMOTE (Canada)<p>Contract Python backend work with tests."}}
        pages["https://hacker-news.firebaseio.com/v0/item/5.json"]["kids"] += [23, 24]
        r = bl.run_source(self.conn, "hn_seeking_freelancer", lambda u: json.dumps(pages[u]).encode(), force=True)
        self.assertEqual((r["seen"], r["new"]), (3, 3))                               # distinct item ids; the freelancer's own ad is not a lead
        self.assertEqual({l["client_name"] for l in bl.ranked(self.conn, "qualified")}, {"Acme", "Gamma"})
        self.assertIn("country restriction", bl.ranked(self.conn, "rejected")[0]["status_reason"])   # REMOTE (Canada)

    def test_domain_expert_roles_are_rejected(self):
        bl.ingest(self.conn, [{"title": "Patent Agent Expert for AI Evaluation", "description": "Contract hourly $125/hr. Evaluate AI answers about patent law."},
                              {"title": "Life Sciences Research Expert (Python)", "description": "Contract hourly $120/hr Python data analysis for biology."}], "manual")
        self.assertEqual({r[0] for r in self.conn.execute("SELECT status FROM biz_leads")}, {"rejected"})

    def test_marketplaces_without_permission_are_not_fetched(self):
        self.assertEqual(bl.run_source(self.conn, "freelancer_com", lambda u: b"")["status"], "not_fetched")

    def test_engine_listings_are_reused(self):
        now = db.now_iso()
        self.conn.execute("INSERT INTO opportunities(url,title,summary,source,status,first_seen_utc,last_seen_utc,report_checked_at_utc,raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
                          ("https://example.com/c1", "Contract n8n automation build", "Freelance contract to build n8n workflows with webhooks.", "feed", "open", now, now, now, "{}"))
        r = bl.run_source(self.conn, "engine_listings", force=True)
        self.assertEqual(r["new"], 1)

    def test_marketplace_alert_emails_are_attributed(self):
        raw = (b"From: Freelancer <noreply@freelancer.com>\r\nSubject: New project: Python REST API\r\nContent-Type: text/plain\r\n\r\n"
               b"Build a Python REST API with tests. Budget $300-$500. https://www.freelancer.com/projects/python/123\r\n")
        excluded = b"From: Upwork <x@upwork.com>\r\nSubject: Job\r\nContent-Type: text/plain\r\n\r\nPython API\r\n"
        out = bl.import_email(self.conn, [raw, excluded])
        self.assertEqual(list(out), ["freelancer_com"])
        self.assertEqual(self.conn.execute("SELECT url FROM biz_leads").fetchone()[0], "https://www.freelancer.com/projects/python/123")


# --------------------------------------------------------------------------------------------- estimation and proposals
class EstimateTests(unittest.TestCase):
    cfg = bc.load()

    def est(self, title, desc, **kw):
        x = bl.extract({"title": title, "description": desc}, self.cfg)
        return bp.estimate({**x, "source_id": "manual", **kw}, self.cfg)

    def test_breakdown_dependencies_and_price_within_boundaries(self):
        e = self.est("Python REST API", "We need a Python REST API that integrates Stripe payments and stores orders in Postgres. Must include tests.")
        ids = {t["id"] for t in e["tasks"]}
        self.assertTrue(all(set(t["depends_on"]) <= ids for t in e["tasks"]))
        self.assertIn("Payments integration", [t["title"] for t in e["tasks"]])
        self.assertGreaterEqual(e["price"], self.cfg["pricing"]["min_project_usd"])
        self.assertGreaterEqual(e["effective_hourly"], self.cfg["pricing"]["min_effective_hourly_usd"])
        self.assertTrue(e["feasible"])
        self.assertIn("Budget", e["missing"])

    def test_deal_terms_do_not_inflate_effort(self):
        a = self.est("Python API", "We need a Python API. Payment by milestones.")
        b = self.est("Python API", "We need a Python API.")
        self.assertEqual(a["hours"], b["hours"])

    def test_flags(self):
        def codes(e):
            return {f["code"] for f in e["flags"] if f["blocking"]}
        self.assertIn("beyond_capability", codes(self.est("Laravel app", "Build a PHP Laravel website.")))
        self.assertIn("unauthorised_access_or_prohibited", codes(self.est("Python script", "Python script to hack into my ex's account")))
        self.assertIn("impossible_deadline", codes(self.est("Full-stack web app", "Build a full-stack web app with login, payments and an admin dashboard.", deadline="1 day")))
        x = bl.extract({"title": "Python API", "description": "We need a Python REST API with auth.", "budget": 60}, self.cfg)
        self.assertIn("unprofitable", {f["code"] for f in bp.estimate({**x, "source_id": "manual", "budget_type": "hourly", "budget_max": 5}, self.cfg)["flags"]})


class ProposalTests(Base):
    def test_tailored_honest_proposal(self):
        lead = self.lead()
        r = bp.propose(self.conn, lead["id"])
        self.assertTrue(r["created"])
        body = r["body"]
        self.assertIn("OpenWeather", body)                                            # this client's own requirements
        self.assertIn("(own project)", body)                                          # genuine work, labelled honestly
        self.assertIn(f"USD {r['estimate']['price']:,.0f}", body)
        for section in ("How I would approach it", "What you receive", "Timeline and price", "Assumptions and exclusions", "Questions before I start"):
            self.assertIn(section, body)
        self.assertNotRegex(body.lower(), r"years of experience|certified|testimonial|5-star|clients include")
        self.assertIn("Test Owner", body)
        self.assertEqual(r["mode"], "C")                                               # the client contacted you

    def test_modes(self):
        bl.ingest(self.conn, [{"title": "Python REST API build", "description": "Build a Python REST API with tests and documentation for our app. Budget $400.",
                               "url": "https://www.freelancer.com/projects/1"}], "freelancer_com")
        lead = bl.ranked(self.conn, "qualified")[0]
        self.assertEqual(bp.propose(self.conn, lead["id"])["mode"], "B")
        self.assertFalse(any(s["policy"]["automated_proposals"] == "allowed" and s["kind"] == "marketplace" for s in bc.sources()))   # no mode A today

    def test_idempotent_and_guarded(self):
        lead = self.lead()
        first = bp.propose(self.conn, lead["id"])
        self.assertFalse(bp.propose(self.conn, lead["id"])["created"])
        with self.assertRaises(bc.BizError):
            bp.mark_sent(self.conn, first["proposal_id"], "")
        bl.ingest(self.conn, [{"title": "Marketing Manager", "description": "x" * 40}], "manual")
        rejected = self.conn.execute("SELECT id FROM biz_leads WHERE status='rejected'").fetchone()["id"]
        with self.assertRaises(bc.BizError):
            bp.propose(self.conn, rejected)

    def test_hourly_roles_get_an_application_not_a_fixed_quote(self):
        bl.ingest(self.conn, [{"title": "Contract Backend Engineer (Python)", "description": "Freelance contract. Build and review Python backend coding tasks with tests. $60/hr.",
                               "url": "https://www.opentrain.ai/jobs/x"}], "engine_listings")
        lead = bl.ranked(self.conn, "qualified")[0]
        r = bp.propose(self.conn, lead["id"])
        self.assertEqual((r["kind"], r["rate"]), ("hourly_application", 60.0))
        self.assertIn("USD 60/hour", r["body"])
        self.assertNotIn("Fixed price", r["body"])
        self.assertEqual(bf.overview(self.conn)["pipeline_usd"], 0)                 # an hourly rate is not pipeline value

    def test_monthly_contracts_get_an_application_with_an_honesty_checklist(self):
        x = bl.extract({"title": "Acme | Full-Stack Engineer | Contract, 4 months | $3,000-4,000/month",
                        "description": "Contract role. You should have personally shipped a production SaaS. Build a TypeScript and Postgres backend with tests."}, bc.load())
        self.assertEqual((x["budget_type"], x["budget_max"]), ("monthly", 4000))
        bl.ingest(self.conn, [{"title": "Acme | Full-Stack Engineer | Contract, 4 months | $3,000-4,000/month",
                               "description": "Contract role. You should have personally shipped a production SaaS. Build a TypeScript and Postgres backend with tests.",
                               "url": "https://news.ycombinator.com/item?id=1"}], "hn_seeking_freelancer")
        r = bp.propose(self.conn, bl.ranked(self.conn, "qualified")[0]["id"])
        self.assertEqual(r["kind"], "monthly_application")
        self.assertTrue(r["body"].startswith("BEFORE YOU SUBMIT"))
        self.assertIn("[ ] You should have personally shipped", r["body"])
        self.assertIn("USD 4000/month", r["body"])

    def test_infeasible_lead_gets_no_proposal(self):
        bl.import_enquiry(self.conn, {**ENQUIRY, "message": "We need a Python backend with a REST API, login and payments plus an admin dashboard. Deliver within 1 day.", "budget": ""})
        lead = bl.ranked(self.conn)[0]
        with mock.patch.object(bp, "_days_until", return_value=1):
            r = bp.propose(self.conn, lead["id"])
        self.assertFalse(r["created"])
        self.assertEqual(bl.get(self.conn, lead["id"])["status"], "rejected")

    def test_replies(self):
        lead = self.lead()
        p = bp.propose(self.conn, lead["id"])
        bp.record_reply(self.conn, p["proposal_id"], "No thanks", "declined")
        self.assertEqual(bl.get(self.conn, lead["id"])["status"], "lost")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM biz_communications WHERE direction='in'").fetchone()[0], 1)


# --------------------------------------------------------------------------------------------- projects
class ProjectTests(Base):
    def test_award_needs_acceptance_and_evidence(self):
        lead = self.lead()
        p = bp.propose(self.conn, lead["id"])
        with self.assertRaises(bc.BizError):
            bpj.award(self.conn, p["proposal_id"], "evidence")                       # not accepted yet
        bp.record_reply(self.conn, p["proposal_id"], "yes", "accepted")
        with self.assertRaises(bc.BizError):
            bpj.award(self.conn, p["proposal_id"], "")
        with self.assertRaises(bc.BizError):
            bpj.award(self.conn, p["proposal_id"], "ok", ai_permitted=True)          # AI permission needs its source
        a = bpj.award(self.conn, p["proposal_id"], "Client accepted by email")
        self.assertFalse(bpj.award(self.conn, p["proposal_id"], "again")["created"])
        self.assertGreaterEqual(self.conn.execute("SELECT COUNT(*) FROM biz_milestones WHERE project_id=?", (a["project_id"],)).fetchone()[0], 4)

    def test_full_delivery_through_isolated_execution_and_independent_qa(self):
        pid, out = self.built(delivery_channel="email")
        self.assertTrue(out["started"])
        self.assertEqual((out["qa"], out["status"]), ("PASS", "ready_to_deliver"))
        ws = Path(self.conn.execute("SELECT workspace FROM execution_jobs").fetchone()[0])
        self.assertEqual(ws.parent, self.execs)                                      # isolated workspace
        prompt = next(ws.joinpath("logs").glob("prompt-attempt-*.md")).read_text(encoding="utf-8")
        self.assertNotRegex(prompt.lower(), r"payment by milestones|\$400|budget")    # the deal terms never reach the worker
        h = bpj.handover(self.conn, pid)
        self.assertTrue(Path(h["zip"]).exists())
        md = (Path(h["path"]) / "HANDOVER.md").read_text(encoding="utf-8")
        self.assertIn("Independent QA: **PASS**", md)
        self.assertIn("[x]", md)
        bc.save_overrides({"autonomy": {"deliver_by_email": True}})
        m = Mailer()
        self.assertTrue(bpj.deliver(self.conn, pid, m)["delivered"])
        self.assertTrue(bpj.deliver(self.conn, pid, m)["already"])
        self.assertEqual(len(m.sent), 1)                                             # never delivered twice
        self.assertEqual(m.sent[0]["To"], "jane@example.org")

    def test_failing_qa_blocks_handover(self):
        pid, out = self.built(Runner(broken=True))
        self.assertEqual(out["qa"], "FAIL")
        with self.assertRaises(bc.BizError):
            bpj.handover(self.conn, pid)
        with self.assertRaises(bc.BizError):
            bpj.deliver(self.conn, pid)

    def test_no_ai_without_client_permission(self):
        lead, p, pid = self.won(ai_permitted=False, ai_source="")
        bpj.start(self.conn, pid, exec_dir=self.execs)
        runner = Runner()
        r = bpj.execute(self.conn, pid, runner, self.dbp, self.execs, probe=self.probe)
        self.assertFalse(r["started"])
        self.assertEqual(runner.calls, 0)

    def test_global_stop_halts_project_execution(self):
        lead, p, pid = self.won()
        bpj.start(self.conn, pid, exec_dir=self.execs)
        control.engage_stop(self.conn, "test")
        self.assertFalse(bpj.execute(self.conn, pid, Runner(), self.dbp, self.execs, probe=self.probe)["started"])

    def test_email_delivery_off_means_you_deliver(self):
        pid, _ = self.built(delivery_channel="email")
        r = bpj.deliver(self.conn, pid, Mailer())
        self.assertFalse(r["delivered"])
        self.assertTrue(bpj.deliver(self.conn, pid, evidence="Uploaded to the client's GitHub repository")["delivered"])

    def test_scope_control(self):
        lead, p, pid = self.won()
        before = bpj.view(self.conn, pid)["price"]
        ch = bpj.propose_change(self.conn, pid, "Also add a CSV export of readings")
        self.assertGreater(ch["extra_price"], 0)
        self.assertEqual(bpj.view(self.conn, pid)["price"], before)                  # nothing changes until approved
        with self.assertRaises(bc.BizError):
            bpj.decide_change(self.conn, ch["change_id"], True, "")
        bpj.decide_change(self.conn, ch["change_id"], True, "Client agreed by email")
        prj = bpj.view(self.conn, pid)
        self.assertEqual(prj["price"], before + ch["extra_price"])
        self.assertTrue(any("CSV export" in c["criterion"] for c in prj["acceptance"]))


# --------------------------------------------------------------------------------------------- finance and recurring revenue
class FinanceTests(Base):
    def deliver(self):
        pid, _ = self.built()
        bpj.deliver(self.conn, pid, evidence="Delivered on the platform")
        bpj.client_accepted(self.conn, pid, "Client approved", hours_logged=8)
        return pid

    def test_money_states_and_unified_ledger(self):
        pid = self.deliver()
        price = bpj.view(self.conn, pid)["price"]
        o = bf.overview(self.conn)
        self.assertEqual((o["collected_usd"], o["contracted_not_invoiced_usd"]), (0, price))      # accepted work is not money
        inv = bf.create_invoice(self.conn, pid)
        with self.assertRaises(bc.BizError):
            bf.create_invoice(self.conn, pid, 1)                                       # cannot invoice beyond the agreed price
        bf.mark_invoice_sent(self.conn, inv["id"], "PayPal invoice")
        self.assertEqual(bf.overview(self.conn)["invoiced_unpaid_usd"], price)
        with self.assertRaises(bc.BizError):
            bf.record_payment(self.conn, inv["id"], price, date.today().isoformat(), "paypal", "")
        half = round(price / 2, 2)
        self.assertEqual(bf.record_payment(self.conn, inv["id"], half, date.today().isoformat(), "paypal", "TX-1")["invoice_status"], "part_paid")
        with self.assertRaises(bc.BizError):
            bf.record_payment(self.conn, inv["id"], half, date.today().isoformat(), "paypal", "TX-1")         # same reference twice
        self.assertEqual(bf.record_payment(self.conn, inv["id"], price - half, date.today().isoformat(), "paypal", "TX-2")["invoice_status"], "paid")
        bf.add_cost(self.conn, "payment_fee", 10, project_id=pid)
        o = bf.overview(self.conn)
        self.assertEqual((o["collected_usd"], o["collected_freelance_usd"], o["net_operating_income_usd"]), (price, price, price - 10))
        self.assertEqual(o["by_service_line"][0]["service_line"], "software")
        self.assertEqual(o["effective_hourly_usd"], round(price / 8, 2))
        self.assertEqual(ledger.totals(self.conn)["money_received"]["by_currency"]["USD"], price)    # the same ledger as the microtask channel

    def test_microtask_payments_count_toward_the_combined_target(self):
        from microtask_monitor import workflow
        workflow.record_payment(self.conn, 50, "USD", date.today().isoformat(), platform_id="whop_workforce", method="paypal", reference="WHOP-1")
        o = bf.overview(self.conn)
        self.assertEqual((o["collected_usd"], o["collected_microtask_usd"], o["collected_freelance_usd"]), (50, 50, 0))
        self.assertEqual(o["target_progress"]["initial"]["percent"], 50.0)

    def test_gap_analysis_and_review(self):
        g = bf.gap_analysis(self.conn)
        self.assertEqual((g["gap"], g["collected"]), (500, 0))
        self.assertEqual(g["win_rate_basis"][:8], "planning")
        self.assertGreater(g["projects_needed"], 0)
        r = bf.monthly_review(self.conn)
        self.assertTrue(any("No proposal has been submitted" in n for n in r["next_month"]))

    def test_retainers_and_recurring_invoices(self):
        pid = self.deliver()
        self.assertEqual(len(bf.suggest_retainers(self.conn)), 1)
        self.assertEqual(bf.suggest_retainers(self.conn), [])                         # once per project
        rid = self.conn.execute("SELECT id FROM biz_retainers").fetchone()[0]
        with self.assertRaises(bc.BizError):
            bf.activate_retainer(self.conn, rid, "")
        bf.activate_retainer(self.conn, rid, "Client agreed to monthly maintenance")
        self.assertEqual(len(bf.bill_retainers(self.conn, "2026-12")), 1)
        self.assertEqual(bf.bill_retainers(self.conn, "2026-12"), [])                 # once per month
        self.assertEqual(bf.overview(self.conn)["recurring_monthly_usd"], 150)


# --------------------------------------------------------------------------------------------- cycle, restart, api, site, dashboard
class CycleTests(Base):
    def test_tick_is_idempotent_across_restarts(self):
        (Path(self.tmp.name) / "inbox").mkdir()
        (Path(self.tmp.name) / "inbox" / "e.eml").write_bytes(
            b"From: Sam Client <sam@example.net>\r\nSubject: Project enquiry: n8n automation\r\nContent-Type: text/plain\r\n\r\n"
            b"We need an n8n workflow that copies new Typeform answers to Airtable and sends a Slack alert. Must handle failures. Budget $300.\r\n")
        http = lambda u: b"<rss><channel></channel></rss>" if u.endswith("rss") else json.dumps({"submitted": []}).encode()   # noqa: E731
        first = be.tick(self.conn, http)
        self.assertEqual(first["inbound"]["messages"], 1)
        self.assertEqual(len([p for p in first["proposals"] if p["created"]]), 1)
        second = be.tick(self.conn, http)                                            # e.g. after a restart
        self.assertEqual(second["inbound"]["messages"], 0)
        self.assertFalse(any(p["created"] for p in second["proposals"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM biz_proposals").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM biz_proposals").fetchone()[0], "prepared")   # never sent by itself

    def test_offline_skips_discovery(self):
        self.assertIn("offline", be.tick(self.conn, online=False)["discover"])

    def test_enquiry_reader_is_read_only(self):
        class Fake:
            calls = []
            def login(self, u, p): self.calls.append("login")
            def select(self, box, readonly=False): self.calls.append(("select", readonly)); return "OK", [b"1"]
            def search(self, *a): return "OK", [b"1"]
            def fetch(self, n, what): self.calls.append(what); return "OK", [(b"x", b"raw")]
            def logout(self): self.calls.append("logout")
            def __getattr__(self, name): raise AssertionError(f"IMAP {name} must never be called")
        f = Fake()
        self.assertEqual(be.enquiry_reader("a@b.c", "pw", connect=lambda: f)(), [b"raw"])
        self.assertIn(("select", True), f.calls)
        self.assertIn("(BODY.PEEK[])", f.calls)

    def test_scheduler_has_the_business_job(self):
        from microtask_monitor import scheduler
        self.assertIn("freelance_business", [j.name for j in scheduler.Scheduler(self.dbp).default_jobs()])


class ApiSiteDashboardTests(Base):
    def call(self, method, path, body=None):
        from microtask_monitor import api as api_module
        a = api_module.Api(self.dbp)
        return a.handle(method, path, {}, body or {})

    def test_api_flow(self):
        s, r = self.call("POST", "/api/business/enquiry", ENQUIRY)
        self.assertEqual(s, 200, r)
        s, r = self.call("GET", "/api/business")
        self.assertEqual((s, len(r["leads"])), (200, 1))
        lid = r["leads"][0]["id"]
        s, r = self.call("POST", f"/api/business/leads/{lid}/propose")
        self.assertTrue(r["created"])
        s, r = self.call("POST", f"/api/business/proposals/{r['proposal_id']}/sent", {"evidence": ""})
        self.assertEqual(s, 400)
        s, r = self.call("GET", "/api/business/review")
        self.assertEqual(s, 200)
        s, r = self.call("POST", "/api/business/nope")
        self.assertEqual(s, 400)

    def test_website_is_built_honestly(self):
        info = bport.build_site()
        html = Path(info["path"]).read_text(encoding="utf-8")
        self.assertIn("my own projects, not client engagements", html)
        self.assertIn("owner@example.com", html)
        self.assertNotIn("<blockquote>", html)                                       # no testimonials until you record genuine ones
        self.assertEqual(info["testimonials"], 0)
        bc.save_overrides({"testimonials": [{"client": "Jane", "text": "Delivered early", "evidence": "Freelancer.com review #9"}]})
        self.assertIn("Delivered early", Path(bport.build_site()["path"]).read_text(encoding="utf-8"))

    def test_dashboard_has_the_business_tab(self):
        dash = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
        for needle in ('data-tab="business"', "/api/business", "Business overview", "Lead acquisition", "Financial management", "Automation health", "Monthly targets"):
            self.assertIn(needle, dash)
        self.assertNotIn("<script src=", dash.lower())


class PreservationTests(Base):
    def test_migration_is_additive_and_the_25_platforms_remain(self):
        from microtask_monitor import policy
        self.assertEqual(db.user_version(self.conn), 12)
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"assignments", "payments", "opportunities", "outreach_leads", "biz_leads", "biz_projects", "biz_invoices", "biz_audit"} <= tables)
        self.assertEqual(len(policy.load_capabilities()), 25)

    def test_business_modules_stay_within_their_bounds(self):
        for name in ("biz_config", "biz_proposals", "biz_projects", "biz_finance", "biz_portfolio", "biz_engine", "biz_api"):
            src = (ROOT / "microtask_monitor" / f"{name}.py").read_text(encoding="utf-8")
            for banned in ("urllib.request", "import requests", "selenium", "playwright", "webdriver", "Cookies", ".store(", "expunge", "fiverr.com/", "upwork.com/"):
                self.assertNotIn(banned, src, f"{name} must not contain {banned}")
        leads = (ROOT / "microtask_monitor" / "biz_leads.py").read_text(encoding="utf-8")
        self.assertNotIn("import selenium", leads)
        self.assertIn('startswith("https://")', leads)


if __name__ == "__main__":
    unittest.main()
