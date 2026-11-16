"""Owner lane phase 2: outreach. Simulated Gmail, simulated YouTube API, simulated media; nothing is sent anywhere."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import connect, control, db, outreach as ot, secret_store as ss, youtube_leads as yl
from tests.test_owner_shorts import FakeMedia

TEMPLATES = """# Templates

## 1. First email
Subject: I made a Short from "{episode_title_short}"

Hi {first_name_or_team},

Your episodes average about {episodes_median} views, your Shorts about {shorts_median}.
Here is one I cut: {clip_link} ({clip_seconds} seconds). Trial: {trial_link}

## 2. Follow-up
Subject: Re: I made a Short from "{episode_title_short}"

Hi {first_name_or_team}, did the clip land? {trial_link}
"""


class FakeMailer:
    def __init__(self):
        self.sent, self.replies, self.bounces, self.sent_folder, self.fail = [], {}, set(), set(), False

    def send(self, msg):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append(msg)

    def messages_from(self, email, since):
        return self.replies.get(email, [])

    def bounced(self, email, since):
        return email in self.bounces

    def already_emailed(self, email):
        return email in self.sent_folder


class FakeSeller:
    def __init__(self):
        self.uploads = []

    def upload(self, path, idem):
        self.uploads.append(idem)
        return {"id": f"file_{len(self.uploads)}"}

    def file(self, fid):
        return {"upload_status": "ready", "url": f"https://assets.whop.com/{fid}.mp4"}


def video(vid, seconds_, views, desc=""):
    m, s = divmod(seconds_, 60)
    return {"id": vid, "contentDetails": {"duration": f"PT{m}M{s}S"}, "statistics": {"viewCount": str(views)},
            "snippet": {"title": f"Episode {vid}: the rule that changed everything", "description": desc}}


def channel(cid, country="US", desc="For business inquiries: hello@testshowpod.com", name="The Test Show"):
    return {"id": cid, "snippet": {"title": name, "country": country, "description": desc}, "statistics": {"subscriberCount": "50000"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}}}


class FakeYouTube:
    def __init__(self, channels, videos):
        self._channels, self._videos, self.units = channels, videos, 0

    def search_channels(self, q, region=None, max_results=25):
        self.units += 100
        return list(self._channels)

    def channels(self, ids):
        return [self._channels[i] for i in ids if i in self._channels]

    def recent_videos(self, uploads, n=50):
        return self._videos.get(uploads[2:], [])


def good_videos(gap_views=(100000, 2000)):
    ep, sh = gap_views
    return [video(f"epVideo000{i}", 3600, ep) for i in range(5)] + [video(f"shVideo000{i}", 40, sh) for i in range(5)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ctx = db.session(Path(self.tmp.name) / "a.sqlite3")
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.tpl = Path(self.tmp.name) / "t.md"
        self.tpl.write_text(TEMPLATES, encoding="utf-8")
        self.cfg = {"from_address": "me@gmail.com", "sender_name": "Tobias", "postal_address": "P.O. Box 123-00100, Nairobi, Kenya",
                    "trial_link": "https://whop.com/checkout/plan_TRIAL", "templates_path": str(self.tpl), "allowed_countries": ["US", "CA", "AU", "NZ"],
                    "queries": ["business podcast full episode"], "work_dir": str(Path(self.tmp.name) / "w"), "daily_cap": 2}
        self.mailer, self.seller, self.media = FakeMailer(), FakeSeller(), FakeMedia()
        self.yt = FakeYouTube({"UC1": channel("UC1"), "UC2": channel("UC2", name="Second Show", desc="Booking and sponsor enquiries: team@secondshowpod.com"),
                               "UC3": channel("UC3", name="Third Show", desc="Business inquiries: biz@thirdshowpod.com")},
                              {"UC1": good_videos(), "UC2": good_videos(), "UC3": good_videos()})
        control.update_settings(self.conn, {"owner_outreach_enabled": True})
        ot.approve_templates(self.conn, TEMPLATES, True)

    def run_pass(self, **kw):
        return ot.run(self.conn, self.cfg, self.mailer, self.yt, self.media, self.seller, **kw)

    def lead(self, name):
        return dict(self.conn.execute("SELECT * FROM outreach_leads WHERE name=?", (name,)).fetchone())


class QualifyTests(unittest.TestCase):
    def test_gap_country_email_and_no_solicitation_rules(self):
        ok = yl.assess(channel("A"), good_videos(), ["US"])
        self.assertTrue(ok["ok"], ok)
        self.assertEqual((ok["email"], ok["gap"]), ("hello@testshowpod.com", 50.0))
        self.assertIn("outside", yl.assess(channel("B", country="GB"), good_videos(), ["US"])["reason"])
        self.assertIn("not stated", yl.assess(channel("C", country=""), good_videos(), ["US"])["reason"])
        self.assertIn("fine", yl.assess(channel("D"), good_videos((3000, 2000)), ["US"])["reason"])
        self.assertIn("business email", yl.assess(channel("E", desc="Thanks for watching"), good_videos(), ["US"])["reason"])
        self.assertIn("unsolicited", yl.assess(channel("F", desc="No unsolicited pitches please. Business: a@bshowpod.com"), good_videos(), ["US"])["reason"])
        self.assertIn("Shorts", yl.assess(channel("G"), [video(f"e{i}", 3600, 9) for i in range(5)], ["US"])["reason"])

    def test_durations_and_only_the_official_api_host(self):
        self.assertEqual((yl.seconds("PT1H2M3S"), yl.seconds("PT45S"), yl.seconds("P1DT1S")), (3723, 45, 86401))
        with self.assertRaises(yl.YouTubeError):
            yl.http_get("https://www.youtube.com/@someone/about")
        with self.assertRaises(yl.YouTubeError):
            yl.YouTube(store=ss.MemoryStore())._k()


class TemplateTests(Base):
    def test_templates_must_be_approved_and_any_edit_needs_approval_again(self):
        self.tpl.write_text(TEMPLATES + "\nP.S. new line\n", encoding="utf-8")
        self.assertIn("not approved", self.run_pass()["skipped"])
        self.assertEqual(self.mailer.sent, [])
        with self.assertRaises(ot.OutreachError):
            ot.approve_templates(self.conn, TEMPLATES, False)
        with self.assertRaises(ot.OutreachError):
            ot.parse_templates("## 1. First email\nSubject: hi {secret_field}\nbody\n## 2. Follow-up\nSubject: x\ny")

    def test_every_email_carries_the_postal_address_and_an_opt_out(self):
        lead = {"name": "Show", "episodes_median": 100000, "shorts_median": 2000, "episode_title": "A very long episode title that keeps going and going past sixty characters",
                "clip_url": "https://assets.whop.com/x.mp4", "clip_seconds": 41.6}
        subject, body = ot.render(ot.parse_templates(TEMPLATES)["first"], lead, self.cfg)
        self.assertIn("P.O. Box 123-00100", body)
        self.assertIn('Reply "no thanks"', body)
        self.assertIn("100,000", body)
        self.assertIn("42 seconds", body)
        self.assertLess(len(subject), 90)
        with self.assertRaises(ot.OutreachError):
            ot.render(ot.parse_templates(TEMPLATES)["first"], lead, {**self.cfg, "postal_address": ""})
        with self.assertRaises(ot.OutreachError):
            ot.render(ot.parse_templates(TEMPLATES)["first"], {**lead, "clip_url": None}, self.cfg)

    def test_missing_setup_and_off_switch_and_global_stop_send_nothing(self):
        self.assertIn("incomplete", ot.run(self.conn, {**self.cfg, "postal_address": ""}, self.mailer)["skipped"])
        control.update_settings(self.conn, {"owner_outreach_enabled": False})
        self.assertIn("switched off", self.run_pass()["skipped"])
        control.update_settings(self.conn, {"owner_outreach_enabled": True})
        control.engage_stop(self.conn, "t")
        self.assertIn("global stop", self.run_pass()["skipped"])
        self.assertEqual(self.mailer.sent, [])


class FlowTests(Base):
    def test_discover_sample_and_send_within_the_daily_cap(self):
        out = self.run_pass()
        self.assertEqual(out["discovery"]["added"], 3)
        self.assertEqual(len(self.mailer.sent), 2)                         # daily cap 2, three leads
        msg = self.mailer.sent[0]
        self.assertIn("List-Unsubscribe", msg)
        self.assertIn("https://assets.whop.com/", msg.get_content())
        self.assertEqual(self.run_pass()["sent"], 0)                       # same day: cap reached
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        self.run_pass(now=tomorrow)
        self.assertEqual(len(self.mailer.sent), 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outreach_messages WHERE kind='first'").fetchone()[0], 3)

    def test_one_followup_after_four_days_then_never_again(self):
        self.cfg["daily_cap"] = 1
        self.run_pass()
        self.assertEqual(len(self.mailer.sent), 1)
        later = datetime.now(timezone.utc) + timedelta(days=5)
        self.yt._channels = {}
        self.run_pass(now=later)
        follow = [m for m in self.mailer.sent if m["Subject"].startswith("Re:")]
        self.assertEqual(len(follow), 1)
        self.assertTrue(follow[0]["In-Reply-To"])
        self.run_pass(now=later + timedelta(days=10))
        self.assertEqual(len([m for m in self.mailer.sent if m["To"] == follow[0]["To"]]), 2)

    def test_any_reply_stops_and_no_thanks_blocks_forever(self):
        self.run_pass()
        a, b = [m["To"] for m in self.mailer.sent]
        self.mailer.replies = {a: ["Hey, this is great, how does it work?"], b: ["No thanks.\n\nOn Mon..."]}
        later = datetime.now(timezone.utc) + timedelta(days=5)
        self.run_pass(now=later)
        self.assertEqual(len([m for m in self.mailer.sent if m["Subject"].startswith("Re:")]), 0)
        statuses = {r["email"]: r["status"] for r in self.conn.execute("SELECT email,status FROM outreach_leads")}
        self.assertEqual((statuses[a], statuses[b]), ("replied", "opted_out"))
        self.assertTrue(ot.suppressed(self.conn, b))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications WHERE title LIKE '%replied to your Shorts email'").fetchone()[0], 1)
        ot.add_lead(self.conn, {"channel_id": "UC_OTHER", "name": "Other channel same person", "email": b, "episode_id": "e1",
                                "episodes_median": 1, "shorts_median": 1}, "test", "sample_ready")
        self.conn.execute("UPDATE outreach_leads SET clip_url='https://assets.whop.com/z.mp4' WHERE channel_id='UC_OTHER'")
        self.run_pass(now=later + timedelta(days=1))
        self.assertFalse(any(m["To"] == b for m in self.mailer.sent[2:]))

    def test_bounces_and_previously_emailed_addresses_are_never_mailed(self):
        self.mailer.sent_folder = {"hello@testshowpod.com"}
        self.run_pass()
        self.assertNotIn("hello@testshowpod.com", [m["To"] for m in self.mailer.sent])
        self.assertEqual(self.lead("The Test Show")["status"], "manual_hold")
        bounced = self.mailer.sent[0]["To"]
        self.mailer.bounces = {bounced}
        self.run_pass(now=datetime.now(timezone.utc) + timedelta(days=5))
        self.assertTrue(ot.suppressed(self.conn, bounced))
        self.assertFalse(any(m["To"] == bounced and m["Subject"].startswith("Re:") for m in self.mailer.sent))

    def test_a_send_failure_is_never_retried_into_a_duplicate(self):
        self.mailer.fail = True
        with self.assertRaises(ot.OutreachError):
            self.run_pass()
        self.mailer.fail = False
        self.run_pass()
        firsts = [m["To"] for m in self.mailer.sent]
        self.assertEqual(len(firsts), len(set(firsts)))
        failed = self.conn.execute("SELECT COUNT(*) FROM outreach_messages WHERE status='failed'").fetchone()[0]
        self.assertEqual(failed, 1)

    def test_legacy_leads_are_held_or_enriched_never_double_contacted(self):
        legacy = Path(self.tmp.name) / "legacy.json"
        legacy.write_text(json.dumps([
            {"name": "Drafted Show", "channel_id": "UC9", "channel": "https://www.youtube.com/channel/UC9", "email": "x@drafted.example.com", "status": "drafted"},
            {"name": "New Show", "channel_id": "UC1", "channel": "https://www.youtube.com/channel/UC1", "email": "hello@testshowpod.com", "status": "new"},
            {"name": "Bad", "channel_id": "UC8", "email": "y@bad.example.com", "status": "excluded"}]), encoding="utf-8")
        self.assertEqual(ot.import_legacy(self.conn, legacy), {"added": 1, "held": 1, "skipped": 1})
        self.run_pass()
        self.assertEqual(self.lead("Drafted Show")["status"], "manual_hold")
        self.assertNotIn("x@drafted.example.com", [m["To"] for m in self.mailer.sent])
        self.assertEqual(self.lead("New Show")["country"], "US")             # enriched from the API before any email
        with self.assertRaises(ot.OutreachError):
            ot.release(self.conn, self.lead("Drafted Show")["id"], False)
        ot.release(self.conn, self.lead("Drafted Show")["id"], True)
        self.assertEqual(self.lead("Drafted Show")["status"], "new")

    def test_samples_that_fail_qc_are_never_sent(self):
        self.media = FakeMedia(fail_qc=99)
        self.run_pass()
        self.assertEqual(self.mailer.sent, [])
        self.assertEqual(self.seller.uploads, [])
        statuses = [r["status"] for r in self.conn.execute("SELECT status FROM outreach_leads")]
        self.assertEqual(statuses.count("skipped"), 2)                      # the two attempted today (daily cap 2) were skipped
        self.assertNotIn("sample_ready", statuses)


class RateLimitTests(Base):
    def test_youtube_rate_limit_pauses_samples_and_keeps_the_leads(self):
        class Limited(FakeMedia):
            calls = 0

            def captions(self, url, work):
                Limited.calls += 1
                raise RuntimeError("ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests")
        self.media = Limited()
        out = self.run_pass()
        self.assertIn("rate-limiting", out["samples_paused"])
        self.assertEqual(Limited.calls, 1)                               # stops at the first refusal, no hammering
        statuses = [r["status"] for r in self.conn.execute("SELECT status FROM outreach_leads")]
        self.assertNotIn("skipped", statuses)                            # nobody is dropped because YouTube said "later"
        self.run_pass()
        self.assertEqual(Limited.calls, 1)                               # still paused: no new attempt
        self.media = FakeMedia()
        later = datetime.now(timezone.utc) + timedelta(hours=7)
        self.run_pass(now=later)
        self.assertTrue(self.mailer.sent)


class ConnectTests(unittest.TestCase):
    def test_gmail_and_youtube_secrets_go_to_the_secret_store(self):
        store = ss.MemoryStore()
        for pid, name in (("owner_gmail", "app_password"), ("owner_youtube", "api_key")):
            out = []
            self.assertEqual(connect.main(["set-key", pid, name], store=store, ask=lambda p: "s3cret-value", out=out.append), 0)
            self.assertNotIn("s3cret-value", " ".join(out))
        self.assertEqual(store.get("owner_gmail", "app_password"), "s3cret-value")
        cleared, out = [], []
        rc = connect.main(["set-key-from-clipboard", "owner_gmail", "app_password"], store=store, out=out.append,
                          clipboard=lambda: "abcd efgh ijkl mnop", clear=lambda: cleared.append(True))
        self.assertEqual((rc, store.get("owner_gmail", "app_password"), cleared), (0, "abcdefghijklmnop", [True]))
        self.assertNotIn("abcd", " ".join(out))


if __name__ == "__main__":
    unittest.main()
