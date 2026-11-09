import gzip
import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")

from microtask_monitor import db, reliability as rel, sync_reports as sr


class Fixture:
    """Real local HTTP server with Range support and failure injection."""

    def __init__(self, files, cut_first_after=None, ignore_range=False, fail_first_with=None):
        self.files, self.cut, self.ignore_range, self.fail_first = files, cut_first_after, ignore_range, fail_first_with
        self.requests, self.cut_done, self.failed = [], set(), set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = outer.files.get(self.path)
                outer.requests.append((self.path, self.headers.get("Range")))
                if data is None:
                    self.send_error(404)
                    return
                if outer.fail_first and self.path not in outer.failed:
                    outer.failed.add(self.path)
                    self.send_error(outer.fail_first)
                    return
                rng, start = self.headers.get("Range"), 0
                if rng and not outer.ignore_range:
                    start = int(rng.split("=")[1].rstrip("-"))
                    if start >= len(data):
                        self.send_error(416)
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                else:
                    self.send_response(200)
                body = data[start:]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if outer.cut and self.path not in outer.cut_done and start == 0:
                    outer.cut_done.add(self.path)
                    self.wfile.write(body[:outer.cut])      # connection then closes: client sees an incomplete read
                    return
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_report(n=3, checked="20261109T100000Z", **extra):
    items = [{"title": f"Job {i}", "url": f"https://example.com/jobs/{i}", "source": "Feed", "summary": "Python API work",
              "kenya_eligibility": "remote_scope_unclear_kenya_not_confirmed", "priority": "medium_review",
              "relevance_score": 30, "category": "ai_related_role", "published": "2026-11-08"} for i in range(n)]
    return {"checked_at_utc": checked, "items": items, **extra}


def publish_files(report):
    raw = json.dumps(report).encode()
    gz = gzip.compress(raw, 9)
    manifest = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "gz_sha256": hashlib.sha256(gz).hexdigest(), "gz_size": len(gz)}
    return {"/latest.json.gz": gz, "/latest.manifest.json": json.dumps(manifest).encode(), "/latest.json": raw}, manifest


class RetryTests(unittest.TestCase):
    def test_retries_then_succeeds_with_increasing_backoff(self):
        calls, waits = [], []
        def flaky():
            calls.append(1)
            if len(calls) < 4:
                raise ConnectionError("reset")
            return "ok"
        with mock.patch.object(rel, "sleep", side_effect=waits.append):
            self.assertEqual(rel.with_retries(flaky, attempts=5, base=1, cap=100), "ok")
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(waits), 3)
        self.assertTrue(waits[0] < waits[1] < waits[2])           # exponential growth

    def test_non_retryable_errors_are_raised_immediately(self):
        calls = []
        def bad():
            calls.append(1)
            raise ValueError("bad data")
        with self.assertRaises(ValueError):
            rel.with_retries(bad, attempts=5, retry_if=lambda e: not isinstance(e, ValueError))
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_the_last_attempt_and_honours_retry_after(self):
        waits = []
        def always():
            raise sr.SyncStageError("github-api", "rate limited", 429, retry_after=7)
        with mock.patch.object(rel, "sleep", side_effect=waits.append), self.assertRaises(sr.SyncStageError):
            rel.with_retries(always, attempts=3, retry_if=sr.is_retryable, retry_after=lambda e: e.retry_after)
        self.assertEqual(waits, [7, 7])

    def test_classification(self):
        for exc in (sr.SyncStageError("network", "x"), sr.SyncStageError("github-api", "x", 503),
                    sr.SyncStageError("github-api", "x", 429), sr.SyncStageError("published-report", "cut")):
            self.assertTrue(sr.is_retryable(exc), exc)
        for exc in (sr.SyncStageError("github-artifact-api", "x", 401), sr.SyncStageError("github-api", "x", 404),
                    sr.SyncStageError("integrity", "bad sum"), ValueError("x")):
            self.assertFalse(sr.is_retryable(exc), exc)

    def test_api_json_retries_transient_network_errors(self):
        attempts = []
        class Ok:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b'{"workflow_runs": []}'
        def flaky(request, timeout):
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionResetError("reset by peer")
            return Ok()
        with mock.patch("urllib.request.urlopen", side_effect=flaky):
            self.assertEqual(sr.api_json("https://api.github.com/x"), {"workflow_runs": []})
        self.assertEqual(len(attempts), 3)

    def test_api_json_does_not_retry_auth_failures(self):
        attempts = []
        def denied(request, timeout):
            attempts.append(1)
            raise urllib.error.HTTPError("https://api.github.com/x", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=denied), self.assertRaises(sr.SyncStageError) as ctx:
            sr.api_json("https://api.github.com/x")
        self.assertEqual((len(attempts), ctx.exception.status), (1, 401))


class AtomicWriteTests(unittest.TestCase):
    def test_failed_write_leaves_the_previous_file_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "latest.json"
            rel.atomic_write_bytes(target, b"GOOD")
            with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                rel.atomic_write_bytes(target, b"NEW")
            self.assertEqual(target.read_bytes(), b"GOOD")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["latest.json"])     # no temp litter


class ResumableDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.blob = os.urandom(200_000)
        self.part = Path(self.tmp.name) / "f.part"

    def serve(self, **kw):
        fx = Fixture({"/f.bin": self.blob}, **kw)
        self.addCleanup(fx.close)
        return fx

    def test_interrupted_transfer_resumes_with_a_range_request(self):
        fx = self.serve(cut_first_after=70_000)
        data = rel.ranged_download(fx.url + "/f.bin", self.part, expected_size=len(self.blob))
        self.assertEqual(data, self.blob)
        ranges = [r for _, r in fx.requests]
        self.assertEqual(ranges[0], None)
        self.assertTrue(ranges[1].startswith("bytes=") and ranges[1] != "bytes=0-")

    def test_progress_survives_a_program_restart(self):
        self.part.write_bytes(self.blob[:50_000])                  # left by a previous interrupted run
        fx = self.serve()
        self.assertEqual(rel.ranged_download(fx.url + "/f.bin", self.part, expected_size=len(self.blob)), self.blob)
        self.assertEqual(fx.requests[0][1], "bytes=50000-")        # resumed, did not start over

    def test_server_that_ignores_range_still_produces_correct_data(self):
        self.part.write_bytes(self.blob[:50_000])
        fx = self.serve(ignore_range=True)
        self.assertEqual(rel.ranged_download(fx.url + "/f.bin", self.part, expected_size=len(self.blob)), self.blob)

    def test_oversized_or_wrong_size_partial_is_discarded_and_restarted(self):
        self.part.write_bytes(self.blob + b"extra")
        fx = self.serve()
        self.assertEqual(rel.ranged_download(fx.url + "/f.bin", self.part, expected_size=len(self.blob)), self.blob)

    def test_gives_up_with_a_clear_error_when_the_size_never_arrives(self):
        fx = self.serve()
        with self.assertRaises(rel.DownloadIncomplete):
            rel.ranged_download(fx.url + "/f.bin", self.part, expected_size=len(self.blob) + 10, attempts=2)

    def test_transient_server_errors_are_retried(self):
        fx = self.serve(fail_first_with=503)
        self.assertEqual(rel.ranged_download(fx.url + "/f.bin", self.part, expected_size=len(self.blob)), self.blob)

    def test_not_found_is_not_retried_forever(self):
        fx = self.serve()
        with self.assertRaises(urllib.error.HTTPError):
            rel.ranged_download(fx.url + "/missing", self.part, expected_size=10)


class ManifestFetchTests(unittest.TestCase):
    RUN = {"id": 7, "html_url": "https://github.com/x/runs/7", "created_at": "2026-11-09T10:00:00Z"}
    ART = {"id": 9}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fetch(self, fx):
        return sr.fetch_manifest_report(self.RUN, self.ART, fx.url + "/latest.manifest.json", fx.url + "/latest.json.gz",
                                        download_dir=Path(self.tmp.name))

    def test_valid_report_downloads_and_verifies_after_an_interruption(self):
        files, _ = publish_files(make_report(40))
        fx = Fixture(files, cut_first_after=len(files["/latest.json.gz"]) // 3)
        self.addCleanup(fx.close)
        report, meta = self.fetch(fx)
        self.assertEqual(len(report["items"]), 40)
        self.assertEqual((meta["run_id"], meta["report_file"]), (7, "report-data/latest.json.gz"))
        self.assertEqual(list(Path(self.tmp.name).glob("*.part")), [])          # cleaned up on success

    def test_checksum_mismatch_is_rejected_and_the_partial_file_removed(self):
        files, manifest = publish_files(make_report(5))
        manifest["gz_sha256"] = "0" * 64
        files["/latest.manifest.json"] = json.dumps(manifest).encode()
        fx = Fixture(files)
        self.addCleanup(fx.close)
        with self.assertRaises(sr.SyncStageError) as ctx:
            self.fetch(fx)
        self.assertEqual(ctx.exception.stage, "integrity")
        self.assertEqual(list(Path(self.tmp.name).glob("*.part")), [])

    def test_decompressed_checksum_mismatch_is_rejected(self):
        files, manifest = publish_files(make_report(5))
        manifest["sha256"] = "f" * 64
        files["/latest.manifest.json"] = json.dumps(manifest).encode()
        fx = Fixture(files)
        self.addCleanup(fx.close)
        with self.assertRaises(sr.SyncStageError) as ctx:
            self.fetch(fx)
        self.assertEqual(ctx.exception.stage, "integrity")

    def test_invalid_manifest_and_unreasonable_size_are_rejected(self):
        for bad in (b"{not json", json.dumps({"gz_size": 1}).encode(),
                    json.dumps({"gz_size": 10**9, "gz_sha256": "a", "sha256": "b"}).encode()):
            fx = Fixture({"/latest.manifest.json": bad})
            self.addCleanup(fx.close)
            with self.assertRaises(sr.SyncStageError) as ctx:
                self.fetch(fx)
            self.assertEqual(ctx.exception.stage, "integrity")

    def test_invalid_report_content_is_rejected_after_integrity_passes(self):
        files, _ = publish_files({"checked_at_utc": "x", "items": [{"title": "t", "url": "http://insecure.example/"}]})
        fx = Fixture(files)
        self.addCleanup(fx.close)
        with self.assertRaises(ValueError):
            self.fetch(fx)

    def test_missing_manifest_falls_back_to_the_whole_json_copy(self):
        with mock.patch.object(sr, "fetch_manifest_report", side_effect=sr.SyncStageError("published-report", "Not Found", 404)), \
             mock.patch.object(sr, "fetch_published_report", return_value=("R", {"run_id": 1})) as legacy:
            self.assertEqual(sr.fetch_best_published_report(self.RUN, self.ART), ("R", {"run_id": 1}))
            legacy.assert_called_once()

    def test_other_manifest_failures_are_not_masked_by_the_fallback(self):
        with mock.patch.object(sr, "fetch_manifest_report", side_effect=sr.SyncStageError("integrity", "bad")), \
             mock.patch.object(sr, "fetch_published_report") as legacy:
            with self.assertRaises(sr.SyncStageError):
                sr.fetch_best_published_report(self.RUN, self.ART)
            legacy.assert_not_called()


class ImportSafetyTests(unittest.TestCase):
    META = {"run_id": "1", "artifact_id": "2", "run_url": "https://github.com/x/runs/1"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "a.sqlite3"
        self.latest = Path(self.tmp.name) / "latest_report.json"

    def count(self):
        with closing(db.connect(self.db)) as c:
            return c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

    def test_invalid_report_leaves_the_previous_report_and_data_untouched(self):
        sr.import_report(make_report(3), self.META, self.db, self.latest)
        before = self.latest.read_bytes()
        bad = {"checked_at_utc": "x", "items": [{"title": "t", "url": "http://insecure.example/"}]}
        with self.assertRaises(ValueError):
            sr.import_report(bad, {"run_id": "9", "artifact_id": "9"}, self.db, self.latest)
        self.assertEqual(self.latest.read_bytes(), before)
        self.assertEqual(self.count(), 3)

    def test_database_failure_rolls_back_everything_and_keeps_the_old_file(self):
        sr.import_report(make_report(3), self.META, self.db, self.latest)
        before = self.latest.read_bytes()
        with mock.patch.object(sr._ig, "ingest", side_effect=RuntimeError("disk error")), self.assertRaises(RuntimeError):
            sr.import_report(make_report(5, checked="20261110T000000Z"), {"run_id": "2", "artifact_id": "3"}, self.db, self.latest)
        self.assertEqual(self.latest.read_bytes(), before)           # file not replaced
        self.assertEqual(self.count(), 3)                            # transaction rolled back
        with closing(db.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)

    def test_success_is_idempotent_and_heals_a_missing_latest_file(self):
        first = sr.import_report(make_report(3), self.META, self.db, self.latest)
        self.assertEqual((first["inserted"], first["already_imported"]), (3, 0))
        self.latest.unlink()
        again = sr.import_report(make_report(3), self.META, self.db, self.latest)
        self.assertEqual((again["already_imported"], again["inserted"]), (1, 0))
        self.assertTrue(self.latest.exists())
        self.assertEqual(self.count(), 3)

    def test_integration_state_and_eligibility_monitors_are_persisted_for_the_dashboard(self):
        report = make_report(1, integrations={"opentrain_ai": {"status": "ok", "open_listings": 1}},
                             eligibility_monitors={"mindrift": {"needs_manual_review": False}})
        sr.import_report(report, self.META, self.db, self.latest)
        with closing(db.connect(self.db)) as c:
            self.assertEqual(db.get_state(c, "integrations")["opentrain_ai"]["status"], "ok")
            self.assertEqual(db.get_state(c, "last_report")["listing_count"], 1)

    def test_vanished_opentrain_listings_expire_only_after_a_complete_ok_scan(self):
        def ot_item(n):
            return {"title": f"OT {n}", "url": f"https://www.opentrain.ai/jobs/{n}", "platform_id": "opentrain_ai", "external_id": str(n),
                    "kenya_eligibility": "kenya_explicitly_eligible", "source": "OpenTrain AI"}
        sr.import_report({"checked_at_utc": "20261109T100000Z", "items": [ot_item(1), ot_item(2)]}, self.META, self.db, self.latest)
        partial = {"checked_at_utc": "20261110T000000Z", "items": [ot_item(1)],
                   "integrations": {"opentrain_ai": {"status": "partial", "open_listings": 1}}}
        sr.import_report(partial, {"run_id": "2", "artifact_id": "2"}, self.db, self.latest)
        with closing(db.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT status FROM opportunities WHERE url LIKE '%/2'").fetchone()[0], "open")
        complete = {"checked_at_utc": "20261111T000000Z", "items": [ot_item(1)],
                    "integrations": {"opentrain_ai": {"status": "ok", "open_listings": 1}}}
        result = sr.import_report(complete, {"run_id": "3", "artifact_id": "3"}, self.db, self.latest)
        self.assertEqual(result["expired"], 1)

    def test_existing_legacy_database_is_upgraded_in_place_by_a_sync(self):
        from test_db import make_legacy_db
        make_legacy_db(self.db, 4)
        sr.import_report(make_report(2), {"run_id": "50", "artifact_id": "60"}, self.db, self.latest)
        self.assertEqual(self.count(), 6)                           # 4 legacy + 2 new, nothing lost


class SyncHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "a.sqlite3"
        self.latest = Path(self.tmp.name) / "latest_report.json"
        self.patches = [mock.patch.object(sr, "DB_PATH", self.db), mock.patch.object(sr, "LATEST_REPORT", self.latest)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def health(self):
        with closing(db.connect(self.db)) as c:
            return db.get_state(c, "sync_health", {})

    def test_failed_download_is_never_recorded_as_success(self):
        with mock.patch.object(sr, "get_latest_report", side_effect=sr.SyncStageError("network", "timed out")):
            for _ in range(2):
                with self.assertRaises(sr.SyncStageError):
                    sr.sync_once(notify_after=False)
        h = self.health()
        self.assertEqual(h["consecutive_failures"], 2)
        self.assertNotIn("last_success_utc", h)
        self.assertEqual(h["last_error_stage"], "network")
        self.assertIn("timed out", h["last_error"])
        self.assertTrue(h["failure_streak_started"])

    def test_success_resets_the_failure_streak_and_records_the_timestamp(self):
        with mock.patch.object(sr, "get_latest_report", side_effect=sr.SyncStageError("network", "x")), self.assertRaises(sr.SyncStageError):
            sr.sync_once(notify_after=False)
        with mock.patch.object(sr, "get_latest_report", return_value=(make_report(2), {"run_id": "5", "artifact_id": "6"})):
            result = sr.sync_once(notify_after=False)
        h = self.health()
        self.assertEqual((h["consecutive_failures"], h["last_error"]), (0, None))
        self.assertTrue(h["last_success_utc"])
        self.assertEqual(result["inserted"], 2)

    def test_a_failure_after_data_exists_keeps_the_existing_data(self):
        with mock.patch.object(sr, "get_latest_report", return_value=(make_report(2), {"run_id": "5", "artifact_id": "6"})):
            sr.sync_once(notify_after=False)
        before = self.latest.read_bytes()
        with mock.patch.object(sr, "get_latest_report", side_effect=sr.SyncStageError("integrity", "bad checksum")), self.assertRaises(sr.SyncStageError):
            sr.sync_once(notify_after=False)
        self.assertEqual(self.latest.read_bytes(), before)
        with closing(db.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 2)
        self.assertEqual(self.health()["consecutive_failures"], 1)

    def test_notification_generation_failure_does_not_fail_the_sync(self):
        os.environ["AUTOPILOT_NO_NOTIFY"] = "0"
        self.addCleanup(os.environ.__setitem__, "AUTOPILOT_NO_NOTIFY", "1")
        with mock.patch.object(sr, "get_latest_report", return_value=(make_report(1), {"run_id": "5", "artifact_id": "6"})), \
             mock.patch("microtask_monitor.notify.generate", side_effect=RuntimeError("toast failed")):
            self.assertEqual(sr.sync_once()["inserted"], 1)


if __name__ == "__main__":
    unittest.main()
