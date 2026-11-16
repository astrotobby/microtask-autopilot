"""Owner lane phase 1: the Shorts service fulfils paid Whop orders by itself.

Simulated Whop API and simulated media tools only; no network, no real account, no Whisper, no downloads.
One test renders a real tiny video with ffmpeg (skipped when ffmpeg is missing) to prove the QC checks execute on files.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import clip_worker as cw, connect, control, db, ledger, secret_store as ss, seller as sl, shorts_service as svc

BIZ, PROD, OWNER = "biz_TEST000001", "prod_TEST00001", "user_OWNER0001"
TRIAL, GROWTH, TEST_PLAN = "plan_TRIAL00001", "plan_GROWTH0001", "plan_TEST000001"
SENT = ("Why do most podcasts fail to grow on short video? Because the first three seconds never give a reason to stay. "
        "Here is the simple rule we use with every guest on the show. Start with the result, not the backstory. "
        "When you lead with the outcome the audience leans in and they watch the whole clip to the end. "
        "That is the difference between a clip that gets ten views and one that gets ten thousand views. ")


def words_for(seconds: float, offset: float = 0.0) -> list[cw.W]:
    toks, out, t = (SENT * 40).split(), [], 0.0
    for tok in toks:
        if t >= seconds:
            break
        out.append(cw.W(offset + t, offset + t + 0.35, tok))
        t += 0.4
    return out


def payment(pid, variant=TRIAL, user="user_BUYER0001", sub="succeeded", total="29.00", net="25.10"):
    return {"id": pid, "status": "paid", "substatus": sub, "variant_id": variant, "currency": "usd", "paid_at": "2026-11-16T10:00:00Z",
            "total": {"amount": total}, "amount_after_fees": {"amount": net}, "user": {"id": user, "username": "showhost"},
            "line_items": [{"product_id": PROD, "variant_id": variant}]}


class FakeWhop:
    def __init__(self):
        self.payments, self.messages, self.sent, self.files, self.calls = [], [], [], {}, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        assert url.startswith("https://api.whop.com/api/v1/")
        assert headers.get("Api-Version-Date") == sl.API_VERSION
        path = urlparse(url).path.replace("/api/v1", "")
        if method == "GET" and path == "/payments":
            q = parse_qs(urlparse(url).query)
            assert q["account_id"] == [BIZ] and q["product_ids"] == [PROD] and q["status"] == ["paid"]
            return 200, {"data": list(self.payments), "page_info": {"has_next_page": False}}
        if method == "POST" and path == "/channels":
            assert body["kind"] == "support" and body["account_id"] == BIZ
            return 200, {"id": "feed_SUPPORT1"}
        if method == "GET" and path == "/messages":
            return 200, {"data": list(reversed(self.messages))}
        if method == "POST" and path == "/messages":
            key = headers.get("Idempotency-Key")
            assert key, "every send carries an Idempotency-Key"
            if not any(s["key"] == key for s in self.sent):
                self.sent.append({"key": key, **body})
            return 200, {"id": "post_x", "channel_id": body["channel_id"]}
        if method == "POST" and path == "/files":
            fid = f"file_{len(self.files) + 1}"
            self.files[fid] = body
            return 200, {"id": fid, "upload_status": "pending", "upload_url": "https://upload.whop.com/put/" + fid, "upload_headers": {"Content-Type": "video/mp4"}}
        if method == "GET" and path.startswith("/files/"):
            return 200, {"id": path.split("/")[-1], "upload_status": "ready", "url": "https://assets.whop.com/x.mp4"}
        return 404, {"message": "unexpected " + method + " " + path}

    def buyer_says(self, text, minutes_after=1):
        self.messages.append({"id": f"post_{len(self.messages) + 1}", "author": {"id": "user_BUYER0001"}, "content": text, "message_type": "regular",
                              "created_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes_after)).isoformat()})


class FakeMedia:
    """Writes small placeholder files; probe/transcribe answer from what was 'rendered'."""

    def __init__(self, fail_qc=0):
        self.rendered, self.section_calls, self.fail_qc = {}, [], fail_qc

    def captions(self, url, work):
        return words_for(600)

    def download_section(self, url, start, end, out):
        self.section_calls.append((start, end))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"section")
        return out

    def download_drive(self, url, work):
        work.mkdir(parents=True, exist_ok=True)
        (work / "ep.mp4").write_bytes(b"full")
        return work / "ep.mp4"

    def transcribe(self, path):
        path = Path(path)
        if str(path) in self.rendered:
            a, b, words = self.rendered[str(path)]
            return [cw.W(w.start - a, w.end - a, w.text) for w in words if w.end > a and w.start < b]
        return words_for(600 if path.name == "ep.mp4" else 70)

    def render(self, src, words, start, end, out, hook):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(f"clip {start:.2f}-{end:.2f} {hook}".encode())
        self.rendered[str(out)] = (start, end, words)
        return out

    def probe(self, path):
        a, b, _ = self.rendered[str(path)]
        if self.fail_qc:
            self.fail_qc -= 1
            return {"ok": True, "video": True, "audio": True, "width": 1080, "height": 1920, "duration": 75.0}   # too long: QC fails
        return {"ok": True, "video": True, "audio": True, "width": 1080, "height": 1920, "duration": b - a}

    def frame_luma(self, path, t):
        return 110.0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ctx = db.session(Path(self.tmp.name) / "a.sqlite3")
        self.conn = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.cfg = {"account_id": BIZ, "product_id": PROD, "owner_user_id": OWNER, "start_after": "2026-11-01T00:00:00Z",
                    "plans": {TRIAL: {"label": "Trial - 3 clips", "clips": 3, "per_link": 3}, GROWTH: {"label": "Growth", "clips": 30, "per_link": 8}},
                    "test_plans": {TEST_PLAN: {"label": "Test order", "clips": 1, "per_link": 1}},
                    "clipper_dir": "unused", "work_dir": str(Path(self.tmp.name) / "work"), "min_clip_seconds": 25, "max_clip_seconds": 50}
        self.whop, self.media = FakeWhop(), FakeMedia()
        self.puts = []
        self.seller = sl.Seller(BIZ, http=self.whop, put=lambda url, headers, path: self.puts.append((url, Path(path).name)) or 200, key="test-key-not-real")
        control.update_settings(self.conn, {"owner_shorts_enabled": True})

    def run_pass(self, **kw):
        return svc.run(self.conn, self.seller, self.media, self.cfg, **kw)

    def order(self):
        return dict(self.conn.execute("SELECT * FROM owner_orders ORDER BY id DESC LIMIT 1").fetchone())


class ConfigTests(Base):
    def test_config_must_name_business_product_plans_and_clipper(self):
        p = Path(self.tmp.name) / "c.json"
        p.write_text(json.dumps({**self.cfg, "plans": {}}), encoding="utf-8")
        with self.assertRaises(svc.ServiceError):
            svc.load_config(p)
        p.write_text(json.dumps({**self.cfg, "plans": {"bad": {"clips": 3, "per_link": 3}}}), encoding="utf-8")
        with self.assertRaises(svc.ServiceError):
            svc.load_config(p)
        p.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.assertEqual(svc.load_config(p)["product_id"], PROD)
        with self.assertRaises(svc.ServiceError):
            svc.load_config(Path(self.tmp.name) / "missing.json")


class IntakeTests(Base):
    def test_a_paid_order_is_recorded_once_and_booked_once_after_fees(self):
        self.whop.payments = [payment("pay_A1")]
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM owner_orders").fetchone()[0], 1)
        pays = self.conn.execute("SELECT amount, currency, reference, method FROM payments").fetchall()
        self.assertEqual([tuple(p) for p in pays], [(25.10, "USD", "pay_A1", "Whop sale (in your Whop balance)")])
        self.assertEqual(ledger.totals(self.conn)["money_received"]["by_currency"], {"USD": 25.1})

    def test_test_orders_and_own_purchases_are_never_income(self):
        self.whop.payments = [payment("pay_T1", variant=TEST_PLAN, total="1.00", net="0.56"), payment("pay_T2", user=OWNER)]
        self.run_pass()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT SUM(test_order) FROM owner_orders").fetchone()[0], 2)

    def test_unknown_plan_and_refunds_escalate_and_stop(self):
        self.whop.payments = [payment("pay_U1", variant="plan_UNKNOWN00")]
        self.run_pass()
        self.assertEqual(self.order()["state"], "needs_you")
        self.assertEqual(self.whop.sent, [])
        self.whop.payments = [payment("pay_R1")]
        self.run_pass()
        self.assertEqual(self.order()["state"], "awaiting_footage")
        self.whop.payments = [payment("pay_R1", sub="refunded")]
        self.run_pass()
        self.assertEqual(self.order()["state"], "needs_you")
        self.assertIn("refunded", self.order()["state_reason"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications WHERE title LIKE 'Shorts order needs you%'").fetchone()[0], 2)

    def test_nothing_runs_when_switched_off_or_stopped(self):
        self.whop.payments = [payment("pay_S1")]
        control.update_settings(self.conn, {"owner_shorts_enabled": False})
        self.assertIn("switched off", self.run_pass()["skipped"])
        control.update_settings(self.conn, {"owner_shorts_enabled": True})
        control.engage_stop(self.conn, "test")
        self.assertIn("global stop", self.run_pass()["skipped"])
        self.assertEqual(self.whop.calls, [])


class FulfilmentTests(Base):
    def test_trial_order_runs_end_to_end_without_anyone(self):
        self.whop.payments = [payment("pay_E1")]
        self.run_pass()
        self.assertEqual(self.order()["state"], "awaiting_footage")
        self.assertEqual(len(self.whop.sent), 1)
        self.assertIn("episode", self.whop.sent[0]["content"])
        self.run_pass()                                                     # no link yet: nothing new is sent
        self.assertEqual(len(self.whop.sent), 1)
        self.whop.buyer_says("Here it is https://www.youtube.com/watch?v=abcDEF12345 thanks!")
        out = self.run_pass()
        o = self.order()
        self.assertEqual((o["state"], o["clips_delivered"]), ("delivered", 3), out)
        self.assertEqual(len(self.media.section_calls), 3)                   # only sections were downloaded, never the episode
        clips = self.conn.execute("SELECT status, file_id, qc_json FROM owner_clips").fetchall()
        self.assertTrue(all(c["status"] == "ready" and c["file_id"] for c in clips))
        for c in clips:
            names = {q["name"] for q in json.loads(c["qc_json"])}
            self.assertEqual(names, {"plays", "duration", "hook_start", "finished_end", "captions_match", "frames", "english"})
        delivery = self.whop.sent[-1]
        self.assertEqual(len(delivery["attachments"]), 3)
        self.assertIn("completes your order", delivery["content"])
        self.assertEqual(len(self.puts), 3)
        self.assertTrue(all(f["visibility"] == "public" for f in self.whop.files.values()))
        before = (len(self.whop.sent), len(self.puts))
        self.run_pass()
        self.run_pass()
        self.assertEqual((len(self.whop.sent), len(self.puts)), before)      # nothing is ever sent or uploaded twice

    def test_growth_plan_delivers_in_batches_and_asks_for_the_next_episode(self):
        self.whop.payments = [payment("pay_G1", variant=GROWTH, total="397.00", net="350.00")]
        self.run_pass()
        self.whop.buyer_says("https://youtu.be/abcDEF12345")
        self.run_pass()
        o = self.order()
        self.assertEqual((o["state"], o["clips_delivered"], o["batch"]), ("awaiting_footage", 8, 1))
        self.assertIn("22 clips left", self.whop.sent[-1]["content"])
        self.whop.buyer_says("https://youtu.be/abcDEF12345", minutes_after=5)        # an older-looking message is not reused...
        self.run_pass()
        self.assertEqual(self.order()["clips_delivered"], 16)
        moments = self.conn.execute("SELECT start, end FROM owner_clips ORDER BY start").fetchall()
        for x, y in zip(moments, moments[1:]):
            self.assertLessEqual(x["end"], y["start"] + 0.01)                        # ...and the same episode never yields overlapping clips

    def test_google_drive_links_work_too(self):
        self.whop.payments = [payment("pay_D1")]
        self.run_pass()
        self.whop.buyer_says("drive: https://drive.google.com/file/d/1AbC/view?usp=sharing")
        self.run_pass()
        self.assertEqual(self.order()["state"], "delivered")
        self.assertEqual(self.media.section_calls, [])

    def test_a_clip_failing_qc_twice_is_never_shipped(self):
        self.media = FakeMedia(fail_qc=99)
        self.whop.payments = [payment("pay_Q1")]
        self.run_pass()
        self.whop.buyer_says("https://youtu.be/abcDEF12345")
        self.run_pass()
        o = self.order()
        self.assertEqual(o["state"], "needs_you")
        self.assertIn("failed QC twice", o["state_reason"])
        self.assertFalse(any(s.get("attachments") for s in self.whop.sent))
        self.assertEqual(self.puts, [])

    def test_a_clip_failing_once_is_recut_and_then_delivered(self):
        self.media = FakeMedia(fail_qc=1)
        self.whop.payments = [payment("pay_Q2")]
        self.run_pass()
        self.whop.buyer_says("https://youtu.be/abcDEF12345")
        self.run_pass()
        self.assertEqual(self.order()["state"], "delivered")
        self.assertEqual(self.conn.execute("SELECT MAX(attempts) FROM owner_clips").fetchone()[0], 2)

    def test_one_reminder_after_a_day_and_never_more(self):
        self.whop.payments = [payment("pay_M1")]
        self.run_pass()
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        self.run_pass(now=later)
        self.run_pass(now=later + timedelta(days=3))
        texts = [s["content"] for s in self.whop.sent]
        self.assertEqual(len(texts), 2)
        self.assertIn("checking in", texts[1])

    def test_non_english_or_unusable_footage_goes_to_you(self):
        class Swahili(FakeMedia):
            def captions(self, url, work):
                return [cw.W(i * 0.4, i * 0.4 + 0.3, w) for i, w in enumerate(("habari yako leo ni siku nzuri sana kwa kazi " * 200).split())]
        self.media = Swahili()
        self.whop.payments = [payment("pay_N1")]
        self.run_pass()
        self.whop.buyer_says("https://youtu.be/abcDEF12345")
        self.run_pass()
        self.assertEqual(self.order()["state"], "needs_you")
        self.assertIn("English", self.order()["state_reason"])
        self.whop.payments.append(payment("pay_N2", user="user_BUYER0001"))
        self.whop.buyer_says("no link here, just saying hi")
        self.assertIsNone(cw.find_link("no link here, just saying hi"))


class SellerTests(unittest.TestCase):
    def test_only_whop_hosts_and_a_stored_key(self):
        with self.assertRaises(sl.SellerError):
            sl.http_json("GET", "https://evil.example/api/v1/payments", {})
        with self.assertRaises(sl.SellerError):
            sl.http_put("https://evil.example/upload", {}, Path(__file__))
        with self.assertRaises(sl.SellerError):
            sl.Seller("not_a_biz")
        with self.assertRaises(sl.NotConfigured):
            sl.Seller(BIZ, store=ss.MemoryStore(), http=lambda *a: (200, {}))._token()
        refused = sl.Seller(BIZ, http=lambda *a: (403, {"message": "missing scope"}), key="k")
        with self.assertRaises(sl.NotConfigured):
            refused.paid_payments([PROD])

    def test_connect_stores_the_owner_key_in_the_secret_store_only(self):
        store, out = ss.MemoryStore(), []
        rc = connect.main(["set-key", "owner_whop", "api_key"], store=store, ask=lambda prompt: "apik_secret_value_123", out=out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(store.get("owner_whop", "api_key"), "apik_secret_value_123")
        self.assertNotIn("apik_secret_value_123", " ".join(out))


class ClipWorkerTests(unittest.TestCase):
    def test_moments_are_whole_sentences_of_the_right_length_and_never_overlap(self):
        m = cw.pick_moments(words_for(600), 8, 25, 50)
        self.assertEqual(len(m), 8)
        for a, b, text in m:
            self.assertTrue(20 <= b - a <= 55, (a, b))
            self.assertRegex(text.strip(), r"[.?!]$")
        for x, y in zip(m, m[1:]):
            self.assertLessEqual(x[1], y[0])
        self.assertEqual(cw.pick_moments(words_for(600), 3, 25, 50, exclude=[(0, 600)]), [])

    def test_copy_uses_only_the_clips_own_words(self):
        c = cw.copy_for(SENT)
        self.assertTrue(c["hook"] and len(c["hook"].split()) <= 7)
        self.assertIn("podcast", c["hashtags"])
        self.assertTrue(set(c["caption"].lower().split()) - {"…"} <= set(SENT.lower().replace("?", " ").replace(".", " ").split()) | set(c["caption"].lower().split()))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_qc_checks_execute_on_a_real_video_file(self):
        with tempfile.TemporaryDirectory() as d:
            clip = Path(d) / "c.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30", "-f", "lavfi", "-i", "sine=frequency=440",
                            "-t", "26", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True)
            words = words_for(26)

            class RealProbe(cw.Media):
                def __init__(self):
                    pass

                def transcribe(self, path):
                    return words
            rows = {r["name"]: r for r in cw.qc(RealProbe(), clip, words, 0.0, 26.0)}
            self.assertEqual(rows["plays"]["status"], "pass", rows["plays"])
            self.assertEqual(rows["duration"]["status"], "pass", rows["duration"])
            self.assertEqual(rows["frames"]["status"], "pass", rows["frames"])
            self.assertTrue(all(r["executed"] for r in rows.values()))


class LedgerMethodsTests(Base):
    def test_owned_payout_methods_need_your_confirmation(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.set_owned_methods(self.conn, ["paypal"], False)
        with self.assertRaises(ledger.LedgerError):
            ledger.set_owned_methods(self.conn, ["gift_cards"], True)
        self.assertEqual(ledger.set_owned_methods(self.conn, ["paypal", "mpesa", "paypal"], True), ["mpesa", "paypal"])
        self.assertEqual(ledger.view(self.conn)["owned_methods"]["methods"], ["mpesa", "paypal"])


if __name__ == "__main__":
    unittest.main()
