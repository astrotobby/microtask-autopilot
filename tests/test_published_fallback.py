import os
os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")
import gzip
import http.client
import io
import json
import unittest
from unittest import mock

from microtask_monitor import sync_reports as sr
import test_sync_reports


class _Resp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PublishedFallbackTests(unittest.TestCase):
    def sample(self):
        return test_sync_reports.SyncReportTests.sample(self)

    def test_fetch_published_report_validates_and_builds_metadata(self):
        body = json.dumps(self.sample()).encode()
        opener = lambda request, timeout: _Resp(body)
        run = {"id": 7, "html_url": "u", "created_at": "t"}
        report, meta = sr.fetch_published_report(run, {"id": 9}, opener=opener)
        self.assertEqual(report["items"][0]["title"], "AI evaluator")
        self.assertEqual((meta["run_id"], meta["artifact_id"]), (7, 9))

    def test_gzip_encoded_published_report_is_decoded(self):
        body = gzip.compress(json.dumps(self.sample()).encode())
        resp = _Resp(body)
        resp.headers = {"Content-Encoding": "gzip"}
        report, _ = sr.fetch_published_report({"id": 1}, {"id": 2},
                                              opener=lambda request, timeout: resp)
        self.assertEqual(report["items"][0]["title"], "AI evaluator")

    def test_truncated_transfer_is_a_clean_stage_error(self):
        def truncated(request, timeout):
            raise http.client.IncompleteRead(b"partial")
        with self.assertRaises(sr.SyncStageError) as ctx:
            sr.fetch_published_report({"id": 1}, {"id": 2}, opener=truncated)
        self.assertEqual(ctx.exception.stage, "published-report")

    def test_corrupt_gzip_is_a_clean_stage_error(self):
        def fresh(request, timeout):               # a real opener returns a new response on every attempt
            resp = _Resp(gzip.compress(b"x" * 100)[:20])
            resp.headers = {"Content-Encoding": "gzip"}
            return resp
        with self.assertRaises(sr.SyncStageError):
            sr.fetch_published_report({"id": 1}, {"id": 2}, opener=fresh)

    def test_401_on_artifact_download_falls_back_to_published_report(self):
        runs = {"workflow_runs": [{"id": 1, "status": "completed", "conclusion": "success",
                                   "head_branch": "main", "created_at": "2026-01-01"}]}
        arts = {"artifacts": [{"id": 5, "name": sr.ARTIFACT_NAME, "expired": False}]}
        denied = sr.SyncStageError("github-artifact-api", "Unauthorized", 401)
        with mock.patch.object(sr, "api_json", side_effect=[runs, arts]), \
             mock.patch.object(sr, "download_archive", side_effect=denied), \
             mock.patch.object(sr, "fetch_best_published_report",
                               return_value=("report", {"run_id": 1})) as fetch:
            self.assertEqual(sr.get_latest_report(), ("report", {"run_id": 1}))
            fetch.assert_called_once()

    def test_non_auth_artifact_errors_are_not_masked(self):
        runs = {"workflow_runs": [{"id": 1, "status": "completed", "conclusion": "success",
                                   "head_branch": "main", "created_at": "2026-01-01"}]}
        arts = {"artifacts": [{"id": 5, "name": sr.ARTIFACT_NAME, "expired": False}]}
        broken = sr.SyncStageError("network", "down")
        with mock.patch.object(sr, "api_json", side_effect=[runs, arts]), \
             mock.patch.object(sr, "download_archive", side_effect=broken):
            with self.assertRaises(sr.SyncStageError):
                sr.get_latest_report()


if __name__ == "__main__":
    unittest.main()
