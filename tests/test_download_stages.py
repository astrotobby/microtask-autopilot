import os
os.environ.setdefault("AUTOPILOT_NO_SLEEP", "1")
os.environ.setdefault("AUTOPILOT_NO_NOTIFY", "1")
os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")
import io
import unittest
import urllib.error
from email.message import Message

from microtask_monitor.sync_reports import SyncStageError, download_archive


def http_error(code, location=None, body=b""):
    headers = Message()
    if location:
        headers["Location"] = location
    return urllib.error.HTTPError("https://api.github.com/x", code, "status", headers, io.BytesIO(body))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DownloadStageTests(unittest.TestCase):
    URL = "https://api.github.com/repos/o/r/actions/artifacts/1/zip"

    def test_redirect_is_followed_without_github_headers(self):
        seen = {}

        def opener(req, timeout):
            raise http_error(302, "https://blob.example.net/a.zip?sig=x")

        def fetch_blob(req, timeout):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower() for k in req.headers}
            return FakeResponse(b"PK-zip-bytes")

        data = download_archive(self.URL, opener=opener, fetch_blob=fetch_blob)
        self.assertEqual(data, b"PK-zip-bytes")
        self.assertEqual(seen["url"], "https://blob.example.net/a.zip?sig=x")
        self.assertNotIn("authorization", seen["headers"])
        self.assertNotIn("accept", seen["headers"])

    def test_401_is_reported_as_github_authentication_requirement(self):
        def opener(req, timeout):
            raise http_error(401, body=b'{"message":"You must have the actions scope"}')

        with self.assertRaises(SyncStageError) as ctx:
            download_archive(self.URL, opener=opener)
        self.assertEqual(ctx.exception.stage, "github-artifact-api")
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("requires authentication", str(ctx.exception))

    def test_storage_host_failure_is_reported_as_redirect_stage(self):
        def opener(req, timeout):
            raise http_error(302, "https://blob.example.net/a.zip")

        def fetch_blob(req, timeout):
            raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")

        with self.assertRaises(SyncStageError) as ctx:
            download_archive(self.URL, opener=opener, fetch_blob=fetch_blob)
        self.assertEqual(ctx.exception.stage, "artifact-redirect")
        self.assertIn("blob.example.net", str(ctx.exception))

    def test_non_https_redirect_is_rejected(self):
        def opener(req, timeout):
            raise http_error(302, "http://blob.example.net/a.zip")

        with self.assertRaises(SyncStageError) as ctx:
            download_archive(self.URL, opener=opener)
        self.assertEqual(ctx.exception.stage, "github-artifact-api")

    def test_network_failure_before_github_is_reported_as_network(self):
        def opener(req, timeout):
            raise urllib.error.URLError("getaddrinfo failed")

        with self.assertRaises(SyncStageError) as ctx:
            download_archive(self.URL, opener=opener)
        self.assertEqual(ctx.exception.stage, "network")


if __name__ == "__main__":
    unittest.main()
