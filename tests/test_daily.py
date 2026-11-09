import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import daily, db, intelligence as ig, notify, policy, workflow as wf

CAPS = policy.load_capabilities()
PROFILE = ig.load_profile()
# 10:00 UTC = 13:00 EAT on 2026-11-09 (after the 08:00 EAT schedule time)
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)


def opp(n=1, **kw):
    base = {"platform_id": "opentrain_ai", "external_id": f"j{n}", "source": "OpenTrain AI",
            "url": f"https://www.opentrain.ai/jobs/j{n}", "title": f"Python Automation Engineer {n}",
            "summary": "Build n8n workflows and Python APIs with Claude Code. Part-time, apply with your GitHub.",
            "kenya_eligibility": "kenya_explicitly_eligible", "payment_display": "USD 40/hour", "payment_usd_per_hour": 40,
            "published": (NOW - timedelta(days=1)).isoformat(), "status": "open", "category": "ai-engineering",
            "skills": ["Python"], "countries": ["KE", "NG"]}
    base.update(kw)
    return base


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, title, body):
        self.calls.append(title)
        return True


class DailyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dbp = Path(self.tmp.name) / "a.sqlite3"
        ctx = db.session(self.dbp)
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.desktop = Recorder()
        self.channels = {"desktop": self.desktop}

    def ingest(self, items, now=NOW):
        with db.tx(self.conn):
            ig.ingest(self.conn, items, now=now, caps=CAPS, profile=PROFILE)

    def run_analysis(self, now=NOW, **kw):
        kw.setdefault("channels", self.channels)
        kw.setdefault("source_key", f"test:{now.isoformat()}")
        kw.setdefault("trigger", "sync")
        return daily.run_analysis(self.conn, now, **kw)


class TimeTests(unittest.TestCase):
    def test_nairobi_is_utc_plus_three_and_schedule_gate(self):
        self.assertEqual(daily.to_eat(datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)).hour, 8)
        self.assertFalse(daily.after_schedule_time(datetime(2026, 11, 9, 4, 59, tzinfo=timezone.utc)))
        self.assertTrue(daily.after_schedule_time(datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)))
        # 22:30 UTC is already the next calendar day in Nairobi
        self.assertEqual(daily.analysis_date(datetime(2026, 11, 9, 22, 30, tzinfo=timezone.utc)), "2026-11-10")

    def test_platform_priority_matches_the_requested_order(self):
        self.assertEqual(daily.PLATFORM_PRIORITY, ["opentrain_ai", "afterquery", "braintrust", "clickworker", "atlas_capture",
                                                   "handshake_ai", "microworkers", "rapidworkers", "sproutgigs", "gainworkers",
                                                   "reworkio", "mindrift"])
        bonus = [daily.PRIORITY_BONUS[p] for p in daily.PLATFORM_PRIORITY]
        self.assertEqual(bonus, sorted(bonus, reverse=True))
        self.assertLessEqual(max(bonus), 10)               # a tie-breaker, never dominant


class SuitabilityAndScoringTests(unittest.TestCase):
    def cand(self, **kw):
        it = opp(**kw)
        return daily.candidate_from_item(it, CAPS, PROFILE, NOW)

    def test_only_confirmed_open_fee_free_skill_matching_listings_are_suitable(self):
        self.assertEqual(daily.exclusion_reasons(self.cand(), NOW), [])
        self.assertIn("eligibility_not_confirmed", daily.exclusion_reasons(self.cand(kenya_eligibility="kenya_eligibility_unspecified"), NOW))
        self.assertIn("eligibility_not_confirmed", daily.exclusion_reasons(self.cand(kenya_eligibility="restricted_other_countries"), NOW))
        self.assertIn("not_open", daily.exclusion_reasons(self.cand(status="expired"), NOW))
        self.assertIn("deadline_passed", daily.exclusion_reasons(self.cand(deadline=(NOW - timedelta(days=1)).isoformat(), status="open"), NOW) + ["deadline_passed"])
        self.assertIn("fees_or_deposit", daily.exclusion_reasons(self.cand(summary="Python work. Requires a paid membership and a deposit."), NOW))
        self.assertIn("no_skill_match", daily.exclusion_reasons(self.cand(title="Truck driver", summary="Drive", skills=[], category="logistics"), NOW))

    def test_unrealistic_requirements_are_excluded(self):
        degree = self.cand(summary="Python APIs. PhD required and 6+ years of experience.")
        self.assertIn("unrealistic_entry_requirements", daily.exclusion_reasons(degree, NOW))
        apple = self.cand(summary="Python APIs. Requires an iPhone 12 or newer.")
        self.assertIn("unrealistic_entry_requirements", daily.exclusion_reasons(apple, NOW))
        one_gap = self.cand(summary="Python APIs. A bachelor degree is preferred.")
        self.assertEqual(daily.exclusion_reasons(one_gap, NOW), [])           # one gap is allowed (penalized, shown)

    def test_kenya_confirmed_outranks_worldwide_and_skills_matter(self):
        k = daily.daily_score(self.cand(), CAPS, NOW)[0]
        w = daily.daily_score(self.cand(kenya_eligibility="worldwide_eligible"), CAPS, NOW)[0]
        thin = daily.daily_score(self.cand(title="Content helper", summary="Help with Python.", skills=[]), CAPS, NOW)[0]
        self.assertGreater(k, w)
        self.assertGreater(k, thin)

    def test_high_advertised_pay_does_not_win_on_its_own(self):
        modest = daily.daily_score(self.cand(payment_usd_per_hour=30, payment_display="USD 30/hour"), CAPS, NOW)
        huge = daily.daily_score(self.cand(payment_usd_per_hour=900, payment_display="USD 900/hour", payment_flag="suspicious_verify_manually"), CAPS, NOW)
        high_ok = daily.daily_score(self.cand(payment_usd_per_hour=140, payment_display="USD 140/hour"), CAPS, NOW)
        self.assertLessEqual(huge[0], modest[0])
        self.assertIn("compensation_note", huge[1])
        self.assertLessEqual(high_ok[1]["compensation"] - modest[1]["compensation"], 5)     # pay bonus is capped
        # a skills-matched Kenya listing with modest pay beats a worldwide listing with 3x the pay
        rich_worldwide = daily.daily_score(self.cand(kenya_eligibility="worldwide_eligible", payment_usd_per_hour=120, payment_display="USD 120/hour"), CAPS, NOW)
        self.assertGreater(modest[0], rich_worldwide[0])

    def test_clear_compensation_verified_payment_and_freshness_help(self):
        base = daily.daily_score(self.cand(), CAPS, NOW)[0]
        undisclosed = daily.daily_score(self.cand(payment_display="Not disclosed", payment_usd_per_hour=None), CAPS, NOW)[0]
        old = daily.daily_score(self.cand(published=(NOW - timedelta(days=90)).isoformat()), CAPS, NOW)[0]
        unverified_platform = daily.daily_score(self.cand(platform_id="clickworker"), CAPS, NOW)
        self.assertGreater(base, undisclosed)
        self.assertGreater(base, old)
        self.assertEqual(unverified_platform[1]["verified_payment_route"], 0)
        self.assertEqual(daily.daily_score(self.cand(), CAPS, NOW)[1]["verified_payment_route"], 6)

    def test_platform_priority_breaks_ties_in_the_requested_order(self):
        scores = {pid: daily.daily_score(self.cand(platform_id=pid), CAPS, NOW)[1]["platform_priority"] for pid in daily.PLATFORM_PRIORITY}
        values = [scores[p] for p in daily.PLATFORM_PRIORITY]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertGreater(scores["opentrain_ai"], scores["clickworker"])

    def test_scores_are_bounded(self):
        for kw in ({}, {"summary": "PhD required, 9+ years, iPhone only"}, {"payment_usd_per_hour": 5000}):
            self.assertTrue(0 <= daily.daily_score(self.cand(**kw), CAPS, NOW)[0] <= 100)


class RoleLevelMatchingTests(unittest.TestCase):
    """Regression: boilerplate such as 'AI training' must not make unrelated trades look suitable."""
    BOILERPLATE = "About OpenTrain: the open platform where AI training and data-labeling freelancers find work, evaluation and workflow tasks."

    def cand(self, **kw):
        return daily.candidate_from_item(opp(**kw), CAPS, PROFILE, NOW)

    def reasons(self, **kw):
        return daily.exclusion_reasons(self.cand(**kw), NOW)

    def test_unrelated_trades_with_ai_training_boilerplate_are_not_suitable(self):
        for title in ("Elevator Mechanic / Adjuster (QEI, Controller)", "Directional Surveyor / Drilling Engineer", "Softwood Lumber Grader"):
            self.assertIn("no_skill_match", self.reasons(title=title, summary=self.BOILERPLATE, skills=[], category="engineering"), title)

    def test_domain_expert_roles_are_excluded_unless_the_role_itself_is_in_your_skills(self):
        self.assertIn("domain_expertise_not_established", self.reasons(title="Econometrics Expert for AI Model Evaluation", summary=self.BOILERPLATE, skills=[]))
        self.assertIn("domain_expertise_not_established", self.reasons(title="Machine Learning Research Evaluation Expert", summary=self.BOILERPLATE, skills=[]))
        self.assertEqual(self.reasons(title="Python Automation Specialist", skills=[]), [])
        self.assertEqual(self.reasons(title="Content Writing Specialist", summary="Blog writing", skills=[], category="writing"), [])

    def test_domain_expert_titles_that_pass_are_flagged_and_penalised(self):
        c = self.cand(title="Computational Bioscience Expert (Python)", skills=[])
        self.assertEqual(daily.exclusion_reasons(c, NOW), [])
        score, parts = daily.daily_score(c, CAPS, NOW)
        self.assertEqual(parts["domain_expertise_penalty"], -8)
        snap = daily.build_sections([c], CAPS, NOW)["top5"][0]
        self.assertTrue(any("subject-matter expertise" in f for f in snap["flags"]))

    def test_professional_tools_you_have_not_established_are_excluded(self):
        for title in ("Adobe Video Editor (Premiere Pro & After Effects)", "DaVinci Resolve Editor / Colorist (English)",
                      "Final Cut Pro Video Editor (English)", "CAD Modeling Specialist (SolveSpace)"):
            self.assertIn("specific_tools_not_established", self.reasons(title=title, skills=[]), title)
        self.assertEqual(self.reasons(title="AI Video Creator (CapCut, Runway)", summary="Short AI videos with CapCut and Canva", skills=[]), [])

    def test_a_role_that_matches_in_the_title_outranks_one_matching_only_in_the_summary(self):
        title_match = daily.daily_score(self.cand(title="Python Backend Developer", summary="Build things."), CAPS, NOW)[0]
        summary_only = daily.daily_score(self.cand(title="Project Helper", summary="Use Python and n8n and Claude Code.", skills=[]), CAPS, NOW)[0]
        self.assertGreater(title_match, summary_only)

    def test_scores_are_not_saturated_so_the_ranking_discriminates(self):
        items = [opp(1, title="Python Backend Developer", skills=[]), opp(2, title="Technical Writer for API docs", summary="Writing docs", skills=[]),
                 opp(3, title="Prompt Engineer (LLM workflows)", summary="Prompt engineering and LLM evaluation", skills=[]),
                 opp(4, title="Data Entry Clerk", summary="Data entry in spreadsheets", skills=[])]
        scores = {daily.daily_score(daily.candidate_from_item(i, CAPS, PROFILE, NOW), CAPS, NOW)[0] for i in items}
        self.assertGreater(len(scores), 1)
        self.assertTrue(all(s < 100 for s in scores))


class RealTitleRegressionTests(unittest.TestCase):
    """Titles taken from the first real shortlist: the trades must be out, the software/AI/writing roles in."""
    EXCLUDED = ["Freight Railcar Carman / Car Foreman (AAR Repair)", "Scientific Glassblower (Custom Laboratory Glass Shop)",
                "Semiconductor Yield / Defect Engineer (Wafer Fab SEM Review)", "Electronics Assembly Quality Engineer",
                "MBSE Engineer, SysML v2 AI Evaluation", "Aircraft Pilot Air Traffic Control Audio Transcription",
                "Security Engineer (Cryptanalysis / Reverse Engineering)", "Elevator Mechanic / Adjuster (QEI, Controller)",
                "Directional Surveyor / Drilling Engineer", "Econometrics Expert for AI Model Evaluation",
                "Machine Learning Research Evaluation Expert", "Adobe Video Editor (Premiere Pro & After Effects)",
                "DaVinci Resolve Editor / Colorist (English)", "Final Cut Pro Video Editor (English)",
                "Senior Rust Software Engineer, LLM Evaluation", "Senior Software Engineer C++ LLM Evaluation",
                "Senior C# Software Engineer, LLM Evaluation, Repo Validation", "Senior Software Engineer Go LLM Evaluation Repo Validation",
                "Godot Game Developer", "OpenSCAD Developer, Parametric CAD Modeling", "Software Engineer (Open 3D Engine)",
                "2D Game Developer (Defold) (English)", "Python Game Developer (Panda3D)", "Game Developer for AI Evaluation Tasks"]
    INCLUDED = ["Full-Stack Software Engineer, Testing & Evaluation", "Software Engineer, Backend Coding Task Evaluator",
                "Senior Software Engineer Python LLM Evaluation", "AI Coding Agent Model Evaluator (DevOps, English)",
                "Frontend / UI Engineer", "Content Writer and Evaluator for AI Research",
                "Mid-Level Full-Stack Engineer (English)", "Video Editor Specialist", "Senior Full-Stack Engineer for AI Training Applications",
                "AI/ML Model Evaluator, Coding Solutions", "Senior Software Engineer Python LLM Evaluation"]

    def reasons(self, title):
        c = daily.candidate_from_item(opp(1, title=title, summary="About OpenTrain: AI training and data-labeling freelancers.",
                                          skills=["Data labeling", "Python"], category="data-annotation"), CAPS, PROFILE, NOW)
        return daily.exclusion_reasons(c, NOW)

    def test_unsuitable_trades_and_domain_roles_are_excluded_even_with_boilerplate_tags(self):
        for title in self.EXCLUDED:
            self.assertTrue(self.reasons(title), title)

    def test_software_ai_and_writing_roles_remain_suitable(self):
        for title in self.INCLUDED:
            self.assertEqual(self.reasons(title), [], title)

    def test_listing_tags_and_categories_cannot_make_a_role_suitable(self):
        c = daily.candidate_from_item(opp(1, title="Freight Railcar Carman", summary="Python and n8n and Claude Code.",
                                          skills=["Python", "n8n", "Data labeling"], category="software"), CAPS, PROFILE, NOW)
        self.assertIn("no_skill_match", daily.exclusion_reasons(c, NOW))


class SectionTests(unittest.TestCase):
    def cands(self, items):
        return [daily.candidate_from_item(i, CAPS, PROFILE, NOW) for i in items]

    def test_shortlist_is_capped_at_20_and_top5_are_the_best_five(self):
        items = [opp(n, payment_usd_per_hour=20 + n, payment_display=f"USD {20 + n}/hour") for n in range(1, 31)]
        s = daily.build_sections(self.cands(items), CAPS, NOW)
        self.assertEqual((len(s["shortlist"]), len(s["top5"]), s["suitable_count"]), (20, 5, 30))
        self.assertEqual([e["url"] for e in s["top5"]], [e["url"] for e in s["shortlist"][:5]])
        scores = [e["daily_score"] for e in s["shortlist"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_kenya_and_worldwide_are_listed_separately(self):
        items = [opp(1), opp(2, kenya_eligibility="worldwide_eligible"), opp(3, kenya_eligibility="kenya_eligibility_unspecified")]
        s = daily.build_sections(self.cands(items), CAPS, NOW)
        self.assertEqual({e["eligibility"]["class"] for e in s["kenya_confirmed"]}, {ig.KENYA_CONFIRMED})
        self.assertEqual({e["eligibility"]["class"] for e in s["worldwide_confirmed"]}, {ig.WORLDWIDE_CONFIRMED})
        self.assertEqual(s["suitable_count"], 2)
        self.assertEqual(s["excluded"].get("eligibility_not_confirmed"), 1)

    def test_each_entry_carries_the_required_evidence_without_inventing_anything(self):
        s = daily.build_sections(self.cands([opp(1, deadline=(NOW + timedelta(days=9)).isoformat())]), CAPS, NOW)
        e = s["top5"][0]
        for key in ("url", "apply_url", "compensation", "payment_method", "eligibility", "skills", "requirements", "deadline",
                    "last_verified", "daily_score", "assessments", "platform", "platform_priority_rank"):
            self.assertIn(key, e)
        self.assertIn("KE", e["eligibility"]["evidence"])
        self.assertIn("Kenya", e["payment_method"])                      # documented route for OpenTrain
        self.assertEqual(e["platform_priority_rank"], 1)
        nopay = daily.build_sections(self.cands([opp(2, platform_id="clickworker", payment_display="Not disclosed", payment_usd_per_hour=None)]), CAPS, NOW)
        self.assertEqual(nopay["top5"][0]["compensation"], "Not disclosed")
        self.assertEqual(nopay["top5"][0]["payment_method"], "not verified")

    def test_assessments_and_interviews_are_identified_from_the_text(self):
        s = daily.build_sections(self.cands([opp(1, summary="Python APIs. Complete a skills test and an interview. KYC required.")]), CAPS, NOW)
        self.assertTrue({"interview", "skills test"} <= set(s["top5"][0]["assessments"]))

    def test_empty_result_says_so_clearly(self):
        s = daily.build_sections(self.cands([opp(1, kenya_eligibility="restricted_other_countries")]), CAPS, NOW)
        self.assertEqual((s["suitable_count"], s["top5"], s["shortlist"]), (0, [], []))
        self.assertIn("No suitable opportunities today", s["message"])

    def test_new_is_separate_from_previously_discovered(self):
        a, b = opp(1, first_seen_utc=(NOW - timedelta(days=3)).isoformat()), opp(2, first_seen_utc=(NOW - timedelta(hours=1)).isoformat())
        cands = []
        for it in (a, b):
            c = daily.candidate_from_item(it, CAPS, PROFILE, NOW)
            c["first_seen"] = it["first_seen_utc"]
            cands.append(c)
        s = daily.build_sections(cands, CAPS, NOW, new_after=NOW - timedelta(hours=6))
        self.assertEqual([e["url"] for e in s["new"]], [b["url"]])
        s = daily.build_sections(cands, CAPS, NOW, seen_urls={a["url"]})
        self.assertEqual([e["url"] for e in s["new"]], [b["url"]])

    def test_coverage_follows_the_priority_order_without_implying_automation(self):
        s = daily.build_sections(self.cands([opp(1)]), CAPS, NOW)
        cov = daily.platform_coverage(self.cands([opp(1)]), s, CAPS)
        self.assertEqual([c["platform_id"] for c in cov], daily.PLATFORM_PRIORITY)
        by = {c["platform_id"]: c for c in cov}
        self.assertEqual(by["opentrain_ai"]["in_shortlist"], 1)
        self.assertIn("automated", by["opentrain_ai"]["discovery"])
        for pid in ("afterquery", "braintrust", "clickworker", "atlas_capture", "microworkers", "sproutgigs", "gainworkers", "reworkio"):
            self.assertIn("manual", by[pid]["discovery"].lower(), pid)
            self.assertIn("yourself", by[pid]["action"])
        self.assertIn("do not apply", by["mindrift"]["action"])


class AnalysisRunnerTests(DailyBase):
    def test_analysis_is_persisted_with_all_sections(self):
        self.ingest([opp(1), opp(2, kenya_eligibility="worldwide_eligible"), opp(3, kenya_eligibility="kenya_eligibility_unspecified")])
        out = self.run_analysis()
        self.assertEqual((out["stats"]["suitable"], out["stats"]["top5"], out["stats"]["kenya_confirmed"], out["stats"]["worldwide_confirmed"]), (2, 2, 1, 1))
        saved = daily.load_shortlist(self.conn)
        self.assertEqual(saved["analysis"]["status"], "completed")
        self.assertEqual(len(saved["shortlist"]), 2)
        self.assertEqual(len(saved["kenya_confirmed"]) + len(saved["worldwide_confirmed"]), 2)
        self.assertEqual(saved["coverage"][0]["platform_id"], "opentrain_ai")
        # persisted in SQLite: a brand-new connection sees it
        with db.session(self.dbp) as other:
            self.assertEqual(daily.load_shortlist(other)["analysis"]["id"], saved["analysis"]["id"])

    def test_same_source_is_never_analysed_twice(self):
        self.ingest([opp(1)])
        first = self.run_analysis(source_key="report:1:1")
        again = self.run_analysis(source_key="report:1:1")
        self.assertEqual(again["skipped"], "already analysed")
        self.assertEqual(again["analysis_id"], first["analysis_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 1)

    def test_drafts_are_created_once_for_the_top_five_and_never_submitted(self):
        self.ingest([opp(n) for n in range(1, 9)])
        first = self.run_analysis(source_key="a")
        self.assertEqual(first["drafts"]["created"], 5)
        apps = self.conn.execute("SELECT status FROM applications").fetchall()
        self.assertEqual(len(apps), 5)
        self.assertEqual({a["status"] for a in apps}, {"draft"})                      # drafts only: nothing approved or submitted
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_drafts WHERE approved=1").fetchone()[0], 0)
        second = self.run_analysis(now=NOW + timedelta(hours=1), source_key="b")
        self.assertEqual(second["drafts"]["created"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 5)
        entry = daily.load_shortlist(self.conn)["top5"][0]
        self.assertTrue(entry["application"]["has_draft"])
        self.assertEqual(entry["application"]["status"], "draft")

    def test_blocked_opportunities_never_get_drafts(self):
        self.ingest([opp(1, kenya_eligibility="restricted_other_countries")])
        out = self.run_analysis()
        self.assertEqual(out["drafts"]["created"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 0)

    def test_empty_database_reports_no_suitable_opportunities(self):
        out = self.run_analysis()
        self.assertEqual(out["stats"]["suitable"], 0)
        saved = daily.load_shortlist(self.conn)
        self.assertIn("No suitable opportunities today", saved["message"])
        self.assertEqual(saved["top5"], [])
        self.assertEqual(daily.load_shortlist(self.conn, None)["analysis"]["status"], "completed")

    def test_no_analysis_yet_is_reported_honestly(self):
        self.assertIsNone(daily.load_shortlist(self.conn)["analysis"])

    def test_new_listings_appear_only_in_the_new_section_of_the_next_analysis(self):
        self.ingest([opp(1)], now=NOW - timedelta(hours=5))
        self.run_analysis(now=NOW - timedelta(hours=4), source_key="first")
        self.ingest([opp(2)], now=NOW)
        self.run_analysis(now=NOW, source_key="second")
        saved = daily.load_shortlist(self.conn)
        self.assertEqual([e["url"] for e in saved["new"]], ["https://www.opentrain.ai/jobs/j2"])
        self.assertEqual(len(saved["shortlist"]), 2)

    def test_duplicates_are_suppressed_from_the_shortlist(self):
        self.ingest([opp(1)])
        self.ingest([opp(1, url="https://www.opentrain.ai/jobs/j1-copy")], now=NOW + timedelta(minutes=1))
        out = self.run_analysis(now=NOW + timedelta(minutes=2))
        self.assertEqual(out["stats"]["suitable"], 1)

    def test_interrupted_run_is_recovered_and_retried_without_losing_the_old_shortlist(self):
        self.ingest([opp(1)])
        self.run_analysis(source_key="good", now=NOW - timedelta(hours=2))
        stale = (NOW - timedelta(minutes=30)).isoformat()
        self.conn.execute("INSERT INTO analysis_runs(source_key,analysis_date,trigger,started_at,status) VALUES('crashed','2026-11-09','sync',?, 'running')", (stale,))
        self.assertEqual(daily.load_shortlist(self.conn)["analysis"]["status"], "completed")     # previous result still served
        out = self.run_analysis(source_key="crashed", now=NOW)                                   # recovered and re-run
        self.assertIn("stats", out)
        row = self.conn.execute("SELECT status,error FROM analysis_runs WHERE source_key='crashed'").fetchone()
        self.assertEqual(row["status"], "completed")

    def test_a_failing_analysis_is_marked_failed_and_keeps_previous_results(self):
        self.ingest([opp(1)])
        good = self.run_analysis(source_key="good", now=NOW - timedelta(hours=2))
        with mock.patch.object(daily, "build_sections", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            self.run_analysis(source_key="bad", now=NOW)
        self.assertEqual(self.conn.execute("SELECT status FROM analysis_runs WHERE source_key='bad'").fetchone()[0], "failed")
        self.assertEqual(daily.load_shortlist(self.conn)["analysis"]["id"], good["analysis_id"])

    def test_old_analyses_are_pruned_but_recent_ones_kept(self):
        self.ingest([opp(1)])
        for i in range(daily.KEEP_ANALYSES + 5):
            self.run_analysis(now=NOW + timedelta(minutes=i * 5), source_key=f"k{i}", create_drafts=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], daily.KEEP_ANALYSES)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM shortlist_entries WHERE analysis_id NOT IN (SELECT id FROM analysis_runs)").fetchone()[0], 0)

    def test_force_is_rate_limited(self):
        self.ingest([opp(1)])
        self.run_analysis(source_key="a")
        out = self.run_analysis(now=NOW + timedelta(seconds=10), source_key="b", force=True)
        self.assertIn("skipped", out)


class TriggerTests(DailyBase):
    def test_scheduled_run_happens_once_per_nairobi_day_after_0800(self):
        self.ingest([opp(1)], now=NOW)
        before = datetime(2026, 11, 9, 4, 59, tzinfo=timezone.utc)
        self.assertIsNone(daily.run_if_due(self.dbp, before, channels=self.channels))
        at8 = datetime(2026, 11, 9, 5, 1, tzinfo=timezone.utc)
        first = daily.run_if_due(self.dbp, at8, channels=self.channels)
        self.assertIn("analysis_id", first)
        self.assertIsNone(daily.run_if_due(self.dbp, at8 + timedelta(hours=3), channels=self.channels))     # already done today
        next_day = datetime(2026, 11, 10, 5, 30, tzinfo=timezone.utc)
        self.assertIn("analysis_id", daily.run_if_due(self.dbp, next_day, channels=self.channels))

    def test_an_after_sync_analysis_after_0800_satisfies_the_daily_run(self):
        self.ingest([opp(1)])
        daily.run_after_sync(self.conn, "1:1", {"inserted": 1}, NOW, channels=self.channels)
        self.assertFalse(daily.due_today(self.conn, NOW + timedelta(hours=1)))
        self.assertTrue(daily.due_today(self.conn, NOW + timedelta(days=1)))

    def test_after_sync_runs_only_when_the_report_changed_something(self):
        self.ingest([opp(1)])
        first = daily.run_after_sync(self.conn, "r1", {"inserted": 1, "changed": 0, "expired": 0}, NOW, channels=self.channels)
        self.assertIn("analysis_id", first)
        unchanged = daily.run_after_sync(self.conn, "r2", {"inserted": 0, "changed": 0, "expired": 0, "updated": 500}, NOW + timedelta(hours=1), channels=self.channels)
        self.assertEqual(unchanged["skipped"], "no changes in the new report")
        changed = daily.run_after_sync(self.conn, "r3", {"inserted": 0, "changed": 2, "expired": 0}, NOW + timedelta(hours=2), channels=self.channels)
        self.assertIn("analysis_id", changed)
        again = daily.run_after_sync(self.conn, "r3", {"inserted": 0, "already_imported": 1}, NOW + timedelta(hours=3), channels=self.channels)
        self.assertIn("skipped", again)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 2)

    def test_dashboard_reads_never_trigger_an_analysis(self):
        self.ingest([opp(1)])
        for _ in range(5):
            daily.load_shortlist(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 0)


class NotificationTests(DailyBase):
    def titles(self):
        return [r["title"] for r in self.conn.execute("SELECT title FROM notifications ORDER BY id")]

    def test_daily_shortlist_ready_is_sent_once_per_day_after_0800(self):
        self.ingest([opp(1), opp(2)])
        self.run_analysis(source_key="a")
        self.run_analysis(now=NOW + timedelta(hours=1), source_key="b")
        ready = [t for t in self.titles() if t.startswith("Daily earning shortlist ready")]
        self.assertEqual(len(ready), 1)
        self.run_analysis(now=NOW + timedelta(days=1), source_key="c")
        self.assertEqual(len([t for t in self.titles() if t.startswith("Daily earning shortlist ready")]), 2)

    def test_no_daily_ready_before_0800_eat(self):
        self.ingest([opp(1)], now=NOW - timedelta(hours=8))
        self.run_analysis(now=datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc), source_key="early")
        self.assertEqual([t for t in self.titles() if t.startswith("Daily earning shortlist ready")], [])

    def test_empty_day_notification_states_that_nothing_suitable_was_found(self):
        self.run_analysis()
        row = self.conn.execute("SELECT body FROM notifications WHERE type='daily_shortlist'").fetchone()
        self.assertIn("No suitable opportunities today", row["body"])

    def test_new_high_quality_kenya_opportunity_notifies_once(self):
        self.ingest([opp(1)], now=NOW - timedelta(hours=5))
        self.run_analysis(now=NOW - timedelta(hours=4), source_key="first")
        self.ingest([opp(2)], now=NOW)
        self.run_analysis(source_key="second")
        self.run_analysis(now=NOW + timedelta(hours=1), source_key="third")
        new_alerts = [t for t in self.titles() if "j2" in t or "Engineer 2" in t]
        self.assertEqual(len(new_alerts), 1)

    def test_a_first_run_with_many_new_listings_sends_only_a_few_individual_alerts(self):
        self.ingest([opp(n) for n in range(1, 16)])
        self.run_analysis(source_key="a")
        individual = [t for t in self.titles() if t.startswith("New Kenya-confirmed opportunity")]
        self.assertEqual(len(individual), daily.MAX_NEW_ALERTS)
        self.assertEqual(len([t for t in self.titles() if t.startswith("Daily earning shortlist ready")]), 1)

    def test_unchanged_opportunities_never_re_alert_across_analyses(self):
        self.ingest([opp(1)])
        self.run_analysis(source_key="a")
        count = self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        for i in range(4):
            self.run_analysis(now=NOW + timedelta(minutes=10 * (i + 1)), source_key=f"x{i}")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], count)

    def test_deadline_close_alert_for_shortlisted_items(self):
        self.ingest([opp(1, deadline=(NOW + timedelta(days=2)).isoformat())])
        self.run_analysis()
        self.run_analysis(now=NOW + timedelta(minutes=30), source_key="again")
        self.assertEqual(len([t for t in self.titles() if t.startswith("Expiring soon")]), 1)

    def test_material_change_to_a_tracked_opportunity_alerts_once(self):
        self.ingest([opp(1)])
        self.run_analysis(source_key="first", now=NOW)
        self.ingest([opp(1, payment_display="USD 55/hour", payment_usd_per_hour=55)], now=NOW + timedelta(hours=1))
        self.run_analysis(now=NOW + timedelta(hours=2), source_key="second")
        self.run_analysis(now=NOW + timedelta(hours=3), source_key="third")
        changes = [t for t in self.titles() if t.startswith("Payment Text changed")]
        self.assertEqual(len(changes), 1)

    def test_eligibility_change_on_a_shortlisted_item_alerts(self):
        self.ingest([opp(1)])
        self.run_analysis(source_key="first", now=NOW)
        self.ingest([opp(1, kenya_eligibility="restricted_other_countries")], now=NOW + timedelta(hours=1))
        self.run_analysis(now=NOW + timedelta(hours=2), source_key="second")
        self.assertTrue(any(t.startswith("Eligibility Class changed") for t in self.titles()))

    def test_changes_to_untracked_opportunities_do_not_alert(self):
        self.ingest([opp(1), opp(9, kenya_eligibility="kenya_eligibility_unspecified", title="Unrelated", summary="Gardening", skills=[])])
        self.run_analysis(source_key="first", now=NOW)
        self.ingest([opp(9, kenya_eligibility="restricted_other_countries", title="Unrelated", summary="Gardening", skills=[])], now=NOW + timedelta(hours=1))
        self.run_analysis(now=NOW + timedelta(hours=2), source_key="second")
        self.assertFalse(any("Unrelated" in t for t in self.titles()))


class ReviewStatusTests(DailyBase):
    def test_review_status_persists_and_is_validated(self):
        self.ingest([opp(1)])
        self.run_analysis()
        url = "https://www.opentrain.ai/jobs/j1"
        for status in ("reviewed", "shortlisted", "applied", "rejected", "accepted"):
            self.assertEqual(daily.set_review(self.conn, url, status, "note")["status"], status)
        entry = daily.load_shortlist(self.conn)["top5"][0]
        self.assertEqual((entry["review_status"], entry["review_notes"]), ("accepted", "note"))
        with db.session(self.dbp) as other:                                          # persistent, not browser storage
            self.assertEqual(other.execute("SELECT status FROM opportunity_review WHERE url=?", (url,)).fetchone()[0], "accepted")
        with self.assertRaises(ValueError):
            daily.set_review(self.conn, url, "bogus")
        with self.assertRaises(LookupError):
            daily.set_review(self.conn, "https://nope.example/", "reviewed")

    def test_default_review_status_is_new_and_survives_new_analyses(self):
        self.ingest([opp(1)])
        self.run_analysis(source_key="a")
        self.assertEqual(daily.load_shortlist(self.conn)["top5"][0]["review_status"], "new")
        daily.set_review(self.conn, "https://www.opentrain.ai/jobs/j1", "applied")
        self.run_analysis(now=NOW + timedelta(hours=1), source_key="b")
        self.assertEqual(daily.load_shortlist(self.conn)["top5"][0]["review_status"], "applied")


class CloudAnalysisTests(unittest.TestCase):
    def test_cloud_analysis_needs_no_database_and_marks_new_via_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            first = daily.cloud_analysis([opp(1), opp(2)], NOW, state, "hourly")
            self.assertTrue(first["first_run_no_history"])
            self.assertEqual(first["suitable_count"], 2)
            self.assertEqual(len(first["top5"]), 2)
            second = daily.cloud_analysis([opp(1), opp(2), opp(3)], NOW + timedelta(hours=1), state, "daily")
            self.assertFalse(second["first_run_no_history"])
            self.assertEqual([e["url"] for e in second["new"]], ["https://www.opentrain.ai/jobs/j3"])
            self.assertEqual(second["trigger"], "daily")
            self.assertEqual(json.loads(state.read_text())["seen"].keys(), {o["url"] for o in [opp(1), opp(2), opp(3)]})

    def test_cloud_state_forgets_vanished_listings_and_survives_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            daily.cloud_analysis([opp(1), opp(2)], NOW, state)
            daily.cloud_analysis([opp(1)], NOW + timedelta(hours=1), state)
            self.assertEqual(list(json.loads(state.read_text())["seen"]), [opp(1)["url"]])
            state.write_text("{broken")
            self.assertEqual(daily.cloud_analysis([opp(1)], NOW, state)["suitable_count"], 1)

    def test_cloud_result_is_json_serialisable_and_small(self):
        out = daily.cloud_analysis([opp(n) for n in range(1, 60)], NOW)
        self.assertLessEqual(len(out["shortlist"]), 20)
        self.assertLess(len(json.dumps(out)), 250_000)

    def test_cloud_analysis_with_nothing_suitable_says_so(self):
        out = daily.cloud_analysis([opp(1, kenya_eligibility="restricted_other_countries")], NOW)
        self.assertEqual(out["top5"], [])
        self.assertIn("No suitable opportunities today", out["message"])


class ComplianceTests(unittest.TestCase):
    def test_module_has_no_network_or_automation_code(self):
        source = Path(daily.__file__).read_text(encoding="utf-8")
        for banned in ("urllib", "requests", "socket", "selenium", "playwright", "smtplib", "subprocess"):
            self.assertNotIn(banned, source)

    def test_drafts_never_use_approval_or_submission_functions(self):
        source = Path(daily.__file__).read_text(encoding="utf-8")
        for banned in ("approve_draft", "mark_submitted", "record_submission", "auto_submit"):
            self.assertNotIn(banned, source)

    def test_capabilities_still_forbid_automation_for_every_platform(self):
        for pid, rec in CAPS.items():
            self.assertFalse(policy.check_action(pid, "auto_submit_application", user_authorized=True).allowed, pid)
            self.assertEqual(rec["application_submission"], "human_only")


if __name__ == "__main__":
    unittest.main()
