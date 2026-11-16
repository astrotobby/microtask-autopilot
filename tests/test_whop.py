"""Whop Workforce adapter.

MOCK TESTS ONLY: every HTTP call here goes to ``FakeWhop`` (shapes copied from Whop's API reference). No test contacts
Whop, and none proves the live integration. The real-API check is a separate, manual step (authorize in the dashboard,
then press "Sync from Whop"); its result is shown in the Whop tab as an API-verified connection.
"""
import json
import os
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import assignments as asg, connections as cx, db, policy, secret_store as ss, whop
from tests.test_api import ApiBase

ACCESS = "eyJhbGciOiJSUzI1NiJ9.ACCESSTOKENpayloadpayloadpayload.sigsigsigsig"
REFRESH = "whop_rt_REFRESHTOKEN_abcdefghijklmnopqrstuvwxyz0123456789"
ANDROID = {"devices": {"phone": "Samsung A05 (Android)", "ios_available": False}}
IPHONE = {"devices": {"phone": "iPhone 13", "ios_available": True}}
LONG = "Record a 30-60 second vertical clip of the stream's best moment, add captions, export at 1080x1920 and upload it with the campaign hashtag. One clip per person."


def bounty(n, **kw):
    b = {"id": f"bnty_T{n:04d}", "title": f"Clip the Tuesday livestream {n}", "description": LONG, "status": "open", "currency": "usd",
         "gross_reward_amount": 25, "net_reward_amount": 17.5, "accepted_submissions_limit": 5, "accepted_submissions_per_user_limit": 1,
         "accepted_submissions_count": 2, "viewer_accepted_submissions_count": 0, "spots_remaining": 3, "business_goal_type": "clipping",
         "accepted_deliverable_types": ["content_url", "media"], "capture_spec": None, "min_total_verified_duration_seconds": None,
         "allowed_country_codes": ["US", "KE"], "submissions_closed_at": None, "cancel_requested_at": None, "scheduled_publish_at": None,
         "experience_id": "exp_1", "discussion_post_id": "post_1", "discussion_feed_id": "feed_1", "discussion_experience_id": "exp_1",
         "hosting_account": {"id": "biz_1", "title": "Acme Studio", "route": "acme-studio"}, "poster": {"id": "user_1", "username": "acme"},
         "created_at": "2026-11-01T00:00:00.000Z", "updated_at": "2026-11-08T00:00:00.000Z"}
    b.update(kw)
    return b


class FakeWhop:
    """Stands in for api.whop.com: token endpoint plus cursor-paginated lists. Records every call."""

    def __init__(self, bounties=None, submissions=None, page=2, scope_ok=True, token_ok=True):
        self.bounties, self.submissions, self.page, self.scope_ok, self.token_ok = bounties or [], submissions or [], page, scope_ok, token_ok
        self.calls, self.valid, self.refreshes = [], {ACCESS}, 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/oauth/token":
            if not self.token_ok:
                return 400, {"error": "invalid_grant", "error_description": "Authorization code has expired"}
            if body["grant_type"] == "refresh_token":
                self.refreshes += 1
                new = f"{ACCESS}.r{self.refreshes}"
                self.valid = {new}
                return 200, {"access_token": new, "refresh_token": f"{REFRESH}_{self.refreshes}", "expires_in": 3600, "token_type": "Bearer"}
            return 200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600, "token_type": "Bearer"}
        if headers.get("Authorization", "").removeprefix("Bearer ") not in self.valid:
            return 401, {"error": "invalid_token"}
        if not self.scope_ok:
            return 403, {"error": "insufficient_scope"}
        rows = self.bounties if u.path.endswith("/bounties") else self.submissions
        if u.path.endswith("/bounties"):
            rows = [b for b in rows if b["status"] == q.get("status", ["open"])[0]]
            c = q.get("country", [None])[0]
            if c:
                rows = [b for b in rows if not b["allowed_country_codes"] or c in b["allowed_country_codes"]]
        start = int(q.get("after", ["0"])[0])
        size = min(int(q.get("first", ["20"])[0]), self.page)
        chunk = rows[start:start + size]
        return 200, {"data": chunk, "page_info": {"end_cursor": str(start + len(chunk)), "has_next_page": start + size < len(rows)}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ctx = db.session(Path(self.tmp.name) / "a.sqlite3")
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.store = ss.MemoryStore()
        whop._pending.clear()

    def authorize(self, http):
        whop.set_client_id(self.conn, "app_TESTCLIENT123")
        start = whop.begin_oauth(self.conn)
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start["authorize_url"]).query)["state"][0]
        return whop.complete_oauth(self.conn, self.store, "CODE123", state, http=http)


class RegistrationTests(unittest.TestCase):
    def test_whop_is_platform_thirteen_with_read_only_api_access(self):
        caps = policy.load_capabilities()
        self.assertEqual(len(caps), 25)
        self.assertEqual(list(caps)[-1], "whop_workforce")
        rec = caps["whop_workforce"]
        self.assertEqual((rec["access_class"], rec["permitted_automation"], rec["evidence_submission"]), ("official_api_permitted", ["api_read"], "human_only"))
        self.assertTrue(policy.check_action("whop_workforce", "api_read").allowed)

    def test_everything_beyond_reading_is_denied_even_when_you_authorize_it(self):
        for action in ("public_discovery", "authenticated_inspect", "initiate_task", "browser_task_execution", "submit_via_workflow", "auto_submit_application",
                       "auto_accept_task", "auto_submit_evidence", "auto_payment", "scrape_logged_in", "probe_private_endpoints"):
            self.assertFalse(policy.check_action("whop_workforce", action, user_authorized=True).allowed, action)

    def test_adapter_only_talks_to_the_documented_api_host(self):
        for url in ("https://whop.com/whop-workforce/", "http://api.whop.com/api/v1/bounties", "https://api.whop.com.evil.example/x"):
            with self.assertRaises(whop.WhopError):
                whop.http_json("GET", url, {})
        src = Path(whop.__file__).read_text(encoding="utf-8")
        for banned in ("selenium", "playwright", "webdriver", "subprocess", "bounty:submission:create", "/submit"):
            self.assertNotIn(banned, src)


class OAuthTests(Base):
    def test_authorize_url_requests_only_the_read_scope_with_pkce(self):
        with self.assertRaises(whop.NotAuthorized) as e:
            whop.begin_oauth(self.conn)
        self.assertIn("Developer Dashboard", str(e.exception))
        with self.assertRaises(whop.WhopError):
            whop.set_client_id(self.conn, "not-an-app-id")
        whop.set_client_id(self.conn, "app_TESTCLIENT123")
        out = whop.begin_oauth(self.conn)
        url = urllib.parse.urlparse(out["authorize_url"])
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "api.whop.com", "/oauth/authorize"))
        self.assertEqual((q["scope"], q["code_challenge_method"], q["response_type"], q["redirect_uri"]),
                         ("bounty:basic:read", "S256", "code", "http://localhost:8765/api/whop/oauth/callback"))
        self.assertGreaterEqual(len(q["state"]), 20)
        self.assertGreaterEqual(len(q["code_challenge"]), 40)

    def test_tokens_go_to_the_secret_store_and_nowhere_else(self):
        http = FakeWhop([bounty(1)])
        st = self.authorize(http)
        self.assertEqual((st["status"], st["verified_by"], st["scopes"]), ("connected_verified", "api", ["bounty:basic:read"]))
        self.assertEqual(ss.get_long(self.store, "whop_workforce", "access"), ACCESS)
        exchange = http.calls[0][3]
        self.assertEqual((exchange["grant_type"], exchange["code"], exchange["client_id"]), ("authorization_code", "CODE123", "app_TESTCLIENT123"))
        self.assertGreaterEqual(len(exchange["code_verifier"]), 43)
        self.conn.execute("PRAGMA wal_checkpoint(FULL)")
        raw = (Path(self.tmp.name) / "a.sqlite3").read_bytes()
        for secret in (ACCESS, REFRESH, "CODE123", exchange["code_verifier"]):
            self.assertNotIn(secret.encode(), raw)
        view = json.dumps(whop.view(self.conn, self.store))
        for secret in (ACCESS, REFRESH):
            self.assertNotIn(secret, view)

    def test_state_must_match_and_a_failed_exchange_connects_nothing(self):
        whop.set_client_id(self.conn, "app_TESTCLIENT123")
        whop.begin_oauth(self.conn)
        with self.assertRaises(whop.NotAuthorized):
            whop.complete_oauth(self.conn, self.store, "CODE", "forged-state", http=FakeWhop())
        with self.assertRaises(whop.NotAuthorized):
            whop.complete_oauth(self.conn, self.store, "", "", http=FakeWhop(), error="access_denied")
        start = whop.begin_oauth(self.conn)
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start["authorize_url"]).query)["state"][0]
        with self.assertRaises(whop.NotAuthorized):
            whop.complete_oauth(self.conn, self.store, "CODE", state, http=FakeWhop(token_ok=False))
        self.assertEqual(self.store.names(), [])
        self.assertEqual(cx.status(self.conn, "whop_workforce")["status"], "not_connected")
        with self.assertRaises(whop.NotAuthorized):                         # a state is single-use
            whop.complete_oauth(self.conn, self.store, "CODE", state, http=FakeWhop())

    def test_missing_read_scope_is_reported_and_never_marked_connected(self):
        with self.assertRaises(whop.NotAuthorized) as e:
            self.authorize(FakeWhop(scope_ok=False))
        self.assertIn("bounty:basic:read", str(e.exception))
        self.assertNotEqual(cx.status(self.conn, "whop_workforce")["status"], "connected_verified")

    def test_expired_access_token_is_refreshed_and_rotated(self):
        http = FakeWhop([bounty(1)])
        self.authorize(http)
        meta = whop.oauth_meta(self.conn)
        db.set_state(self.conn, "whop_oauth", {**meta, "expires_at": 0})
        whop.sync(self.conn, self.store, http)
        self.assertEqual(http.refreshes, 1)
        self.assertEqual(ss.get_long(self.store, "whop_workforce", "refresh"), f"{REFRESH}_1")
        http.valid = set()                                                  # token revoked server-side mid-session: one retry via refresh
        whop.sync(self.conn, self.store, http)
        self.assertEqual(http.refreshes, 2)

    def test_revoked_authorization_disconnects_and_asks_for_you(self):
        http = FakeWhop([bounty(1)])
        self.authorize(http)
        db.set_state(self.conn, "whop_oauth", {**whop.oauth_meta(self.conn), "expires_at": 0})
        http.token_ok = False
        with self.assertRaises(whop.NotAuthorized):
            whop.sync(self.conn, self.store, http)
        st = cx.status(self.conn, "whop_workforce")
        self.assertEqual((st["status"], st["verified_by"]), ("needs_user_action", None))
        self.assertEqual(self.store.names(), [])
        self.assertFalse(whop.oauth_status(self.conn, self.store)["authorized"])
        self.authorize(FakeWhop([bounty(1)]))                               # recovery: authorizing again restores it
        self.assertEqual(cx.status(self.conn, "whop_workforce")["status"], "connected_verified")

    def test_without_authorization_nothing_is_fetched(self):
        http = FakeWhop([bounty(1)])
        with self.assertRaises(whop.NotAuthorized):
            whop.sync(self.conn, self.store, http)
        self.assertEqual(http.calls, [])

    def test_long_tokens_are_split_for_credential_manager(self):
        big = "x" * 4321
        ss.put_long(self.store, "whop_workforce", "access", big)
        self.assertEqual(len(self.store.names()), 5)
        self.assertEqual(ss.get_long(self.store, "whop_workforce", "access"), big)
        ss.put_long(self.store, "whop_workforce", "access", "short")
        self.assertEqual((ss.get_long(self.store, "whop_workforce", "access"), len(self.store.names())), ("short", 1))


class FilteringTests(unittest.TestCase):
    def decide(self, profile=ANDROID, **kw):
        return whop.evaluate(bounty(1, **kw), os_name=whop.device_os(profile))

    def test_kenya_must_be_listed_and_an_empty_list_is_worldwide(self):
        self.assertEqual(self.decide()["kenya"], "listed")
        self.assertEqual(self.decide(allowed_country_codes=[])["kenya"], "worldwide")
        self.assertEqual(self.decide(allowed_country_codes=[])["decision"], "executable")
        out = self.decide(allowed_country_codes=["US", "MX"])
        self.assertEqual((out["decision"], out["kenya"]), ("excluded", "not_listed"))
        self.assertIn("Kenya", out["reasons"][0])

    def test_unavailable_bounties_are_excluded(self):
        for status in ("closed", "completed", "canceled", "scheduled", ""):
            self.assertEqual(self.decide(status=status)["decision"], "excluded", status)
        self.assertEqual(self.decide(scheduled_publish_at="2999-01-01T00:00:00Z")["decision"], "excluded")
        self.assertEqual(self.decide(submissions_closed_at="2026-11-09T00:00:00Z")["decision"], "excluded")

    def test_no_remaining_slots_or_used_allowance_is_excluded(self):
        self.assertEqual(self.decide(spots_remaining=0)["decision"], "excluded")
        self.assertEqual(self.decide(viewer_accepted_submissions_count=1)["decision"], "excluded")
        self.assertEqual(self.decide(viewer_accepted_submissions_count=1, accepted_submissions_per_user_limit=2)["decision"], "executable")

    def test_reward_is_reported_as_quoted_with_its_currency_and_fee(self):
        r = self.decide(currency="kes", gross_reward_amount=1000, net_reward_amount=700)["reward"]
        self.assertEqual((r["currency"], r["gross"], r["net"], r["fee"]), ("KES", 1000, 700, 300.0))
        self.assertIn("low_value", self.decide(gross_reward_amount=0.15, net_reward_amount=0.1)["flags"])
        missing = self.decide(net_reward_amount=None)
        self.assertEqual((missing["decision"], missing["reward"]["fee"]), ("review", None))

    def test_iphone_only_capture_is_incompatible_with_the_android_phone(self):
        capture = dict(business_goal_type="data_capture", accepted_deliverable_types=["data_capture"], min_total_verified_duration_seconds=3600,
                       description="Record one continuous take with an iPhone 11 or newer on a head mount, hands visible, using the latest Whop iOS app. " * 2)
        out = self.decide(**capture)
        self.assertEqual((out["decision"], out["hardware_ok"]), ("excluded", False))
        self.assertIn("Android", out["reasons"][-1])
        self.assertIn("head mount", out["hardware_note"])
        on_iphone = self.decide(profile=IPHONE, **capture)
        self.assertEqual((on_iphone["hardware_ok"], on_iphone["decision"]), (True, "human_only"))    # compatible, still recorded by a person
        self.assertEqual(self.decide(description=LONG + " Requires the iOS app and an iPhone.")["decision"], "excluded")

    def test_one_incompatible_type_does_not_reject_the_others(self):
        self.assertEqual(self.decide()["decision"], "executable")
        self.assertEqual(self.decide(business_goal_type="ugc_content")["decision"], "executable")

    def test_deliverable_and_goal_routing(self):
        expect = {"clipping": ("content", "assisted"), "ugc_content": ("content", "assisted"), "owned_account_growth": ("content", "assisted"),
                  "post_engagement": (None, "human_only"), "local_activation": (None, "human_only")}
        for goal, (worker, mode) in expect.items():
            r = self.decide(business_goal_type=goal, title="Campaign task")["routing"]
            self.assertEqual((r["worker"], r["mode"], r["goal"]), (worker, mode, goal))
        other = self.decide(business_goal_type="other", title="Build a Python script", description="Write a Python script with unit tests for the backend API function and refactor the code. " * 2)["routing"]
        self.assertEqual((other["worker"], other["mode"]), ("software", "assisted"))
        self.assertEqual(self.decide(business_goal_type=None, title="Do it", description="Please do something helpful with the attached material for me, thanks. " * 3)["decision"], "review")
        self.assertEqual(self.decide()["routing"]["deliverables"], ["content_url", "media"])

    def test_engagement_is_never_automated(self):
        for title in ("Like, Share & Follow IG", "Get paid to follow us on X", "Post our code in 3 Reddit threads"):
            out = self.decide(business_goal_type=None, title=title)
            self.assertEqual((out["decision"], out["routing"]["worker"]), ("human_only", None), title)
        self.assertIn("never automated", self.decide(business_goal_type="post_engagement")["routing"]["note"])

    def test_suspicious_result_dependent_and_vague_tasks_go_to_review(self):
        self.assertIn("suspicious", self.decide(description=LONG + " You must pay a $10 deposit to unlock the task.")["flags"])
        self.assertEqual(self.decide(business_goal_type="other", title="Generate 1 Paid Sale via Affiliate Link")["decision"], "review")
        self.assertIn("poorly_specified", self.decide(description="Do the thing.")["flags"])
        self.assertEqual(self.decide(title="Comment on our post (US Only)", business_goal_type="post_engagement")["decision"], "review")

    def test_ai_rules_in_a_bounty_are_detected(self):
        r = self.decide(description=LONG + " Do not use AI tools or AI-generated content.")["routing"]
        self.assertEqual(r["ai_rule"], "prohibited")


class SyncTests(Base):
    def setUp(self):
        super().setUp()
        self.bounties = [bounty(1), bounty(2, allowed_country_codes=[]), bounty(3, allowed_country_codes=["US"]), bounty(4, spots_remaining=0),
                         bounty(5, business_goal_type="post_engagement", title="Like our post"), bounty(6, status="closed"),
                         bounty(7, business_goal_type="data_capture", accepted_deliverable_types=["data_capture"], min_total_verified_duration_seconds=3600)]
        self.http = FakeWhop(self.bounties)
        self.authorize(self.http)
        self.http.calls.clear()

    def test_pagination_follows_cursors_and_sends_the_bearer_token(self):
        out = whop.sync(self.conn, self.store, self.http)
        gets = [c for c in self.http.calls if c[0] == "GET" and "/bounties?" in c[1]]
        self.assertEqual(len(gets), 3)                                      # 5 server-side matches for KE, 2 per page
        self.assertTrue(all(c[2]["Authorization"] == f"Bearer {ACCESS}" for c in gets))
        self.assertTrue(all("country=KE" in c[1] and "status=open" in c[1] for c in gets))
        self.assertEqual((out["complete"], out["bounties"]["received"]), (True, 5))
        self.assertEqual({k: out["bounties"][k] for k in ("executable", "human_only", "excluded")}, {"executable": 2, "human_only": 1, "excluded": 2})

    def test_only_executable_bounties_become_qualified_assignments(self):
        whop.sync(self.conn, self.store, self.http)
        rows = asg.list_all(self.conn, "whop_workforce")
        self.assertEqual(sorted(a["external_id"] for a in rows), ["bnty_T0001", "bnty_T0002"])
        a = rows[0]
        self.assertEqual((a["stage"], a["origin"], a["reward_net"], a["currency"], a["ai_use"]), ("qualified", "whop_bounty", 17.5, "USD", "unknown"))
        self.assertEqual(a["source_url"], "https://whop.com/acme-studio/")
        self.assertIn("whop_api:", a["history"][-1]["evidence_ref"])
        f = asg.funnel(self.conn)
        self.assertEqual((f["assignments_qualified"], f["assignments_accepted"], f["money"]["by_currency"]), (2, 0, {}))      # quoted rewards are not income

    def test_syncing_twice_duplicates_nothing(self):
        whop.sync(self.conn, self.store, self.http)
        first = [self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("whop_bounties", "assignments", "assignment_events", "notifications")]
        seen = self.conn.execute("SELECT first_seen_at FROM whop_bounties WHERE id='bnty_T0001'").fetchone()[0]
        out = whop.sync(self.conn, self.store, self.http)
        again = [self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("whop_bounties", "assignments", "assignment_events", "notifications")]
        self.assertEqual((first, out["bounties"]["new"], out["bounties"]["assignments_created"]), (again, 0, 0))
        self.assertEqual(self.conn.execute("SELECT first_seen_at FROM whop_bounties WHERE id='bnty_T0001'").fetchone()[0], seen)

    def test_a_bounty_that_disappears_becomes_unavailable(self):
        whop.sync(self.conn, self.store, self.http)
        self.http.bounties = [b for b in self.bounties if b["id"] != "bnty_T0002"]
        whop.sync(self.conn, self.store, self.http)
        row = self.conn.execute("SELECT status, decision FROM whop_bounties WHERE id='bnty_T0002'").fetchone()
        self.assertEqual((row["status"], row["decision"]), ("unavailable", "excluded"))

    def test_excluded_bounties_cannot_be_tracked_and_human_only_can(self):
        whop.upsert_bounties(self.conn, self.bounties, "oauth_api", profile=ANDROID)
        for bid in ("bnty_T0003", "bnty_T0004", "bnty_T0007"):
            with self.assertRaises(whop.WhopError):
                whop.track(self.conn, bid)
        a = whop.track(self.conn, "bnty_T0005")
        self.assertEqual((a["stage"], a["ai_use"]), ("qualified", "human_only"))
        with self.assertRaises(asg.AssignmentError):                        # nothing is prepared or executed for a human-only task
            asg.mark_ready(self.conn, a["id"]) and asg.start_execution(self.conn, a["id"])

    def test_opening_or_tracking_a_bounty_is_not_an_attempt(self):
        whop.sync(self.conn, self.store, self.http)
        a = asg.list_all(self.conn, "whop_workforce")[0]
        self.assertEqual(a["stage"], "qualified")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM whop_submissions").fetchone()[0], 0)
        self.assertFalse(any(c[0] == "POST" and "/bounty" in c[1] for c in self.http.calls))      # the adapter never writes to Whop

    def test_a_compatible_bounty_reaches_the_execution_engine(self):
        whop.sync(self.conn, self.store, self.http)
        a = next(x for x in asg.list_all(self.conn, "whop_workforce") if x["external_id"] == "bnty_T0001")
        asg.set_ai_use(self.conn, a["id"], "allowed", "Poster confirmed in the bounty discussion thread")
        asg.mark_ready(self.conn, a["id"])
        with self.assertRaises(asg.AssignmentError):
            asg.start_execution(self.conn, a["id"], exec_dir=Path(self.tmp.name) / "e")
        asg.advance(self.conn, a["id"], "accepted", "user_confirmation", "Started the attempt on Whop 10 Nov", confirmed_by_user=True)
        a = asg.start_execution(self.conn, a["id"], "content", Path(self.tmp.name) / "e")
        job = self.conn.execute("SELECT * FROM execution_jobs WHERE id=?", (a["execution_job_id"],)).fetchone()
        self.assertEqual((job["platform_id"], job["instructions_source"], job["work_type"], job["instructions"]), ("whop_workforce", "platform_api", "content", LONG))
        self.assertTrue((Path(job["workspace"]) / "INSTRUCTIONS.md").is_file())

    def submission(self, status, **kw):
        return {"id": "btys_S0001", "bounty_id": "bnty_T0001", "status": status, "claimed_at": "2026-11-10T09:00:00Z", "submitted_at": None,
                "resolved_at": None, "denial_reason": None, "deliverable_urls": ["https://www.tiktok.com/@me/video/1"], "content": "My clip", **kw}

    def test_review_results_follow_the_platform_record(self):
        whop.sync(self.conn, self.store, self.http)
        aid = next(x["id"] for x in asg.list_all(self.conn, "whop_workforce") if x["external_id"] == "bnty_T0001")
        self.http.submissions = [self.submission("in_progress")]
        whop.sync(self.conn, self.store, self.http)
        self.assertEqual(asg.get(self.conn, aid)["stage"], "accepted")
        self.http.submissions = [self.submission("submitted", submitted_at="2026-11-11T10:00:00Z")]
        whop.sync(self.conn, self.store, self.http)
        a = asg.get(self.conn, aid)
        self.assertEqual((a["stage"], a["submission_reference"]), ("awaiting_review", "whop_submission:btys_S0001"))
        self.http.submissions = [self.submission("approved", submitted_at="2026-11-11T10:00:00Z", resolved_at="2026-11-12T10:00:00Z")]
        whop.sync(self.conn, self.store, self.http)
        whop.sync(self.conn, self.store, self.http)
        a = asg.get(self.conn, aid)
        self.assertEqual(a["stage"], "work_accepted")
        self.assertEqual(len([h for h in a["history"] if h["to_stage"] == "work_accepted"]), 1)
        # approval credits a Whop balance; it is not recorded as money until you enter the real amount, and not as paid until a payout arrives
        self.assertEqual(asg.funnel(self.conn)["money"]["by_currency"], {})
        a = asg.record_payment_pending(self.conn, aid, 17.5, "USD")
        self.assertEqual(a["stage"], "payment_pending")
        a = asg.record_paid(self.conn, aid, 17.5, "USD", date.today().isoformat(), "Whop withdrawal", "WD-99812", True)
        self.assertEqual((a["stage"], asg.funnel(self.conn)["payments_confirmed"]), ("paid", 1))

    def test_a_denied_submission_is_rejected_with_its_reason(self):
        whop.sync(self.conn, self.store, self.http)
        self.http.submissions = [self.submission("denied", submitted_at="2026-11-11T10:00:00Z", resolved_at="2026-11-12T10:00:00Z", denial_reason="Wrong aspect ratio")]
        whop.sync(self.conn, self.store, self.http)
        a = next(x for x in asg.list_all(self.conn, "whop_workforce") if x["external_id"] == "bnty_T0001")
        self.assertEqual(a["stage"], "rejected")
        self.assertIn("Wrong aspect ratio", a["history"][-1]["detail"])
        with self.assertRaises(asg.AssignmentError):
            asg.record_payment_pending(self.conn, a["id"], 17.5, "USD")

    def test_connector_import_stores_bounties_but_never_claims_a_connection(self):
        out = whop.import_official_response(self.conn, {"data": self.bounties, "page_info": {"has_next_page": True}})
        self.assertEqual(out["received"], 7)
        with self.assertRaises(whop.WhopError):
            whop.upsert_bounties(self.conn, self.bounties, "scraped_page")
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        with db.session(Path(fresh.name) / "b.sqlite3") as conn:
            whop.import_official_response(conn, {"data": self.bounties})
            self.assertEqual(cx.status(conn, "whop_workforce")["status"], "not_connected")


class WhopApiTests(ApiBase):
    def setUp(self):
        super().setUp()
        api = self.server.RequestHandlerClass.api
        self.store, self.http = ss.MemoryStore(), FakeWhop([bounty(1), bounty(2, allowed_country_codes=["US"])])
        api.store, api.whop_http = self.store, self.http
        whop._pending.clear()

    def test_api_refuses_tokens_and_reports_the_missing_prerequisite(self):
        s, r = self.post("/api/whop/client", {"client_id": "app_TESTCLIENT123", "access_token": ACCESS})
        self.assertEqual(s, 400)
        s, r = self.get("/api/whop")
        self.assertEqual((s, r["oauth"]["authorized"], r["connection"]["status"]), (200, False, "not_connected"))
        self.assertIn("client id", r["oauth"]["missing_prerequisite"])
        self.assertEqual(self.post("/api/whop/authorize")[0], 409)
        self.assertEqual(self.post("/api/whop/sync")[0], 409)

    def test_full_authorization_and_sync_through_the_local_server(self):
        self.assertEqual(self.post("/api/whop/client", {"client_id": "app_TESTCLIENT123"})[0], 200)
        s, r = self.post("/api/whop/authorize")
        self.assertEqual(s, 200)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(r["authorize_url"]).query)
        self.assertEqual(q["redirect_uri"][0], f"http://localhost:{self.port}/api/whop/oauth/callback")
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", f"/api/whop/oauth/callback?code=CODE123&state={urllib.parse.quote(q['state'][0])}", headers={"Host": f"localhost:{self.port}"})
        resp = c.getresponse()
        body = resp.read()
        c.close()
        self.assertEqual((resp.status, resp.getheader("Location"), body), (303, "/?whop=connected#whop", b""))
        s, r = self.post("/api/whop/sync")
        self.assertEqual((s, r["bounties"]["executable"]), (200, 1))
        s, r = self.get("/api/whop")
        self.assertEqual((r["connection"]["status"], r["oauth"]["authorized"], len(r["bounties"])), ("connected_verified", True, 1))
        self.assertNotIn(ACCESS, json.dumps(r))
        self.assertEqual(self.get("/api/centre")[1]["connected"]["api_verified"], 1)
        s, r = self.post("/api/whop/disconnect")
        self.assertEqual((s, r["oauth"]["authorized"], self.store.names()), (200, False, []))

    def test_callback_is_rejected_from_other_hosts_and_with_a_forged_state(self):
        self.post("/api/whop/client", {"client_id": "app_TESTCLIENT123"})
        s, _ = self.call("GET", "/api/whop/oauth/callback?code=X&state=Y", host="evil.example")
        self.assertEqual(s, 403)
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", "/api/whop/oauth/callback?code=X&state=forged", headers={"Host": f"127.0.0.1:{self.port}"})
        resp = c.getresponse()
        resp.read()
        c.close()
        self.assertEqual(resp.getheader("Location"), "/?whop=error#whop")
        self.assertEqual(self.store.names(), [])

    def test_server_log_lines_never_contain_the_oauth_code(self):
        from microtask_monitor import local_server
        with self.assertLogs(local_server.LOG, level="INFO") as logs:
            self.call("GET", "/api/whop/oauth/callback?code=SECRETCODE987&state=zzz")
        joined = "\n".join(logs.output)
        self.assertNotIn("SECRETCODE987", joined)


if __name__ == "__main__":
    unittest.main()
