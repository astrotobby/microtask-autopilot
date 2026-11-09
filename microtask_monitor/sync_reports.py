"""Synchronize the newest successful public GitHub Actions report into local SQLite.

No GitHub token is required because the configured repository is public.
Only report artifacts from successful runs on the configured branch are consumed.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import os
import sys
import zlib
from contextlib import closing
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:      # allow `python microtask_monitor/sync_reports.py`
    sys.path.insert(0, str(ROOT))
from microtask_monitor import db as _db, intelligence as _ig, reliability as _rel  # noqa: E402
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "autopilot.sqlite3"
LATEST_REPORT = DATA_DIR / "latest_report.json"
API = "https://api.github.com"
OWNER = "astrotobby"
REPO = "microtask-autopilot"
BRANCH = "main"
WORKFLOW = "hourly-opportunity-monitor.yml"
ARTIFACT_NAME = "hourly-opportunity-report"
REPORT_DATA_BRANCH = "report-data"
RAW_REPORT_URL = (
    f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{REPORT_DATA_BRANCH}/latest.json"
)
RAW_BASE = f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{REPORT_DATA_BRANCH}"
MANIFEST_URL = RAW_BASE + "/latest.manifest.json"
GZ_URL = RAW_BASE + "/latest.json.gz"
DOWNLOAD_DIR = DATA_DIR / "downloads"
MAX_ARCHIVE_BYTES = 15 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
LOG = logging.getLogger("microtask_autopilot.sync")


def _api_json_once(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Microtask-Autopilot-Local-Agent/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", "replace")
        try:
            retry_after = float(exc.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            retry_after = None
        raise SyncStageError("github-api", f"{exc.reason} for {url} {body}".strip(),
                             exc.code, retry_after=retry_after) from exc
    except (http.client.HTTPException, OSError) as exc:      # URLError, timeouts, resets, DNS, TLS
        raise SyncStageError("network", f"Could not reach api.github.com: {getattr(exc, 'reason', exc)}") from exc
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("GitHub API returned an unexpected response")
    return data


class SyncStageError(RuntimeError):
    """A sync failure tagged with the stage that failed and any HTTP status."""

    def __init__(self, stage: str, message: str, status: int | None = None, retry_after: float | None = None):
        self.stage = stage
        self.status = status
        self.retry_after = retry_after
        prefix = f"[{stage}]" + (f" HTTP {status}" if status is not None else "")
        super().__init__(f"{prefix}: {message}")


def is_retryable(exc: BaseException) -> bool:
    """Network trouble, rate limits and 5xx are retried; auth/not-found/validation errors are not."""
    return isinstance(exc, SyncStageError) and (
        exc.stage in ("network", "published-report") and exc.status in (None, 429, 500, 502, 503, 504)
        or exc.status in (429, 500, 502, 503, 504))


def api_json(url: str) -> dict[str, Any]:
    return _rel.with_retries(lambda: _api_json_once(url), attempts=4, retry_if=is_retryable,
                             retry_after=lambda e: getattr(e, "retry_after", None))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(_NoRedirect)
    return opener.open(request, timeout=timeout)


def download_archive(url: str, opener=_open_no_redirect,
                     fetch_blob=urllib.request.urlopen) -> bytes:
    """Download an artifact ZIP in two explicit stages.

    Stage "github-artifact-api": GitHub must answer 302 with a Location header.
    A 401/403 here means GitHub requires authentication for the download.
    Stage "artifact-redirect": the signed storage URL is fetched without any
    GitHub headers. A failure here is the storage host or the local network.
    """
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "Microtask-Autopilot-Local-Agent/1.0"},
    )
    location = ""
    try:
        with opener(request, timeout=60) as response:
            # Some clients/proxies follow redirects themselves; accept a direct body.
            data = response.read(MAX_ARCHIVE_BYTES + 1)
            return _check_size(data)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            location = exc.headers.get("Location", "")
        else:
            body = exc.read(500).decode("utf-8", "replace")
            hint = (" GitHub requires authentication to download this artifact."
                    if exc.code in (401, 403) else "")
            raise SyncStageError("github-artifact-api", f"{exc.reason}.{hint} {body}".strip(),
                                 exc.code) from exc
    except urllib.error.URLError as exc:
        raise SyncStageError("network", f"Could not reach api.github.com: {exc.reason}") from exc
    if not location.startswith("https://"):
        raise SyncStageError("github-artifact-api", "Redirect had no HTTPS Location header")

    host = urllib.parse.urlsplit(location).hostname or "unknown host"
    blob_request = urllib.request.Request(location, headers={
        "User-Agent": "Microtask-Autopilot-Local-Agent/1.0"})
    try:
        with fetch_blob(blob_request, timeout=60) as response:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SyncStageError("artifact-redirect",
                             f"Storage host {host} refused the download: {exc.reason}",
                             exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SyncStageError("artifact-redirect",
                             f"Could not reach storage host {host}: {reason}") from exc
    return _check_size(data)


def _check_size(data: bytes) -> bytes:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Artifact archive exceeds the configured size limit")
    return data


def _published_metadata(run: dict[str, Any], artifact: dict[str, Any], report_file: str) -> dict[str, Any]:
    return {
        "run_id": run["id"], "run_url": run.get("html_url", ""), "run_created_at": run.get("created_at", ""),
        "artifact_id": artifact["id"], "artifact_created_at": artifact.get("created_at", ""),
        "artifact_expires_at": artifact.get("expires_at", ""), "report_file": report_file,
    }


def _fetch_published_once(url: str, opener) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": "Microtask-Autopilot-Local-Agent/1.0", "Accept-Encoding": "gzip"})
    try:
        with opener(request, timeout=180) as response:
            payload = response.read(MAX_JSON_BYTES + 1)
            declared = response.headers.get("Content-Length")
            # http.client returns short data silently when a connection drops mid-body; detect it explicitly.
            if declared and declared.isdigit() and len(payload) < int(declared):
                raise http.client.IncompleteRead(payload, int(declared) - len(payload))
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                payload = gzip.GzipFile(fileobj=BytesIO(payload)).read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SyncStageError("published-report", f"{exc.reason} for {url}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise SyncStageError("network", f"Could not reach raw.githubusercontent.com: {exc.reason}") from exc
    except (http.client.HTTPException, EOFError, gzip.BadGzipFile, zlib.error, OSError) as exc:
        # Truncated or corrupted transfer on a flaky connection: report it, retry.
        raise SyncStageError("published-report", f"Incomplete download ({type(exc).__name__}: {exc})") from exc
    return payload


def fetch_published_report(run: dict[str, Any], artifact: dict[str, Any],
                           url: str = RAW_REPORT_URL, opener=urllib.request.urlopen,
                           ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch the unauthenticated whole-JSON report copy the workflow publishes to report-data."""
    payload = _rel.with_retries(lambda: _fetch_published_once(url, opener), attempts=3, retry_if=is_retryable)
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("Published report is too large")
    report = json.loads(payload.decode("utf-8"))
    validate_report(report)
    return report, _published_metadata(run, artifact, f"{REPORT_DATA_BRANCH}/latest.json")


def fetch_manifest_report(run: dict[str, Any], artifact: dict[str, Any], manifest_url: str = MANIFEST_URL,
                          gz_url: str = GZ_URL, opener=urllib.request.urlopen, download_dir: Path = DOWNLOAD_DIR,
                          ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resumable, integrity-checked download: small manifest first, then the gzip with HTTP Range resume.

    Partial progress survives timeouts and restarts. The size and SHA-256 of both the compressed and the
    decompressed report must match the manifest, otherwise the partial file is discarded and nothing is imported.
    """
    manifest_raw = _rel.with_retries(lambda: _fetch_published_once(manifest_url, opener), attempts=3, retry_if=is_retryable)
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
        gz_size, gz_sha, json_sha = int(manifest["gz_size"]), str(manifest["gz_sha256"]), str(manifest["sha256"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SyncStageError("integrity", f"Report manifest is invalid: {exc}") from exc
    if gz_size <= 0 or gz_size > MAX_ARCHIVE_BYTES:
        raise SyncStageError("integrity", "Report manifest declares an unreasonable size")
    part = Path(download_dir) / f"latest-{gz_sha[:12]}.json.gz.part"
    for stale in Path(download_dir).glob("latest-*.part") if Path(download_dir).exists() else []:
        if stale != part:
            stale.unlink(missing_ok=True)             # leftovers from older reports
    try:
        blob = _rel.ranged_download(gz_url, part, expected_size=gz_size, opener=opener)
    except _rel.DownloadIncomplete as exc:
        raise SyncStageError("published-report", f"Incomplete download: {exc} (progress kept; will resume)") from exc
    except urllib.error.HTTPError as exc:
        raise SyncStageError("published-report", f"{exc.reason} for {gz_url}", exc.code) from exc
    if _rel.sha256_hex(blob) != gz_sha:
        part.unlink(missing_ok=True)
        raise SyncStageError("integrity", "Downloaded report failed its checksum (the CDN may still be updating); will retry")
    try:
        payload = gzip.GzipFile(fileobj=BytesIO(blob)).read(MAX_JSON_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        part.unlink(missing_ok=True)
        raise SyncStageError("integrity", f"Report archive is corrupt: {exc}") from exc
    if len(payload) > MAX_JSON_BYTES or _rel.sha256_hex(payload) != json_sha:
        part.unlink(missing_ok=True)
        raise SyncStageError("integrity", "Decompressed report does not match the manifest checksum")
    report = json.loads(payload.decode("utf-8"))
    validate_report(report)
    part.unlink(missing_ok=True)
    return report, _published_metadata(run, artifact, f"{REPORT_DATA_BRANCH}/latest.json.gz")


def fetch_best_published_report(run: dict[str, Any], artifact: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefer the resumable manifest path; fall back to the whole-JSON copy only if no manifest is published yet."""
    try:
        return fetch_manifest_report(run, artifact)
    except SyncStageError as exc:
        if exc.status == 404:
            LOG.info("No report manifest published yet; using the whole-JSON copy")
            return fetch_published_report(run, artifact)
        raise


def get_latest_report() -> tuple[dict[str, Any], dict[str, Any]]:
    runs_url = (
        f"{API}/repos/{OWNER}/{REPO}/actions/workflows/{WORKFLOW}/runs"
        f"?branch={BRANCH}&status=completed&per_page=10"
    )
    runs = api_json(runs_url).get("workflow_runs", [])
    successful = [
        run for run in runs
        if run.get("conclusion") == "success"
        and run.get("status") == "completed"
        and run.get("head_branch") == BRANCH
    ]
    if not successful:
        raise RuntimeError("No successful workflow run found on main yet")

    successful.sort(key=lambda run: run.get("created_at", ""), reverse=True)
    for run in successful:
        artifacts_url = (
            f"{API}/repos/{OWNER}/{REPO}/actions/runs/{run['id']}/artifacts"
            f"?name={ARTIFACT_NAME}&per_page=10"
        )
        artifacts = api_json(artifacts_url).get("artifacts", [])
        usable = [
            item for item in artifacts
            if item.get("name") == ARTIFACT_NAME and not item.get("expired")
        ]
        if not usable:
            continue
        artifact = max(usable, key=lambda item: item.get("created_at", ""))
        try:
            archive = download_archive(
                f"{API}/repos/{OWNER}/{REPO}/actions/artifacts/{artifact['id']}/zip"
            )
        except SyncStageError as exc:
            if exc.status not in (401, 403):
                raise
            # GitHub requires authentication to download artifacts, even for public
            # repositories. Fall back to the workflow-published copy on report-data.
            LOG.info("Artifact download needs authentication (HTTP %s); "
                     "using the public %s branch copy instead", exc.status,
                     REPORT_DATA_BRANCH)
            return fetch_best_published_report(run, artifact)
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            candidates = [
                info for info in bundle.infolist()
                if info.filename.lower().endswith(".json")
                and not info.is_dir()
                and info.file_size <= MAX_JSON_BYTES
            ]
            if not candidates:
                raise ValueError("Artifact contains no valid JSON report")
            # Each workflow artifact normally contains one report JSON.
            info = max(candidates, key=lambda entry: entry.filename)
            report = json.loads(bundle.read(info).decode("utf-8"))
        validate_report(report)
        metadata = {
            "run_id": run["id"],
            "run_url": run.get("html_url", ""),
            "run_created_at": run.get("created_at", ""),
            "artifact_id": artifact["id"],
            "artifact_created_at": artifact.get("created_at", ""),
            "artifact_expires_at": artifact.get("expires_at", ""),
            "report_file": info.filename,
        }
        return report, metadata
    raise RuntimeError("No unexpired report artifact found on the latest successful runs")


def validate_report(report: Any) -> None:
    if not isinstance(report, dict) or not isinstance(report.get("items"), list):
        raise ValueError("Downloaded JSON is not a Microtask Autopilot report")
    if not isinstance(report.get("checked_at_utc"), str):
        raise ValueError("Report is missing checked_at_utc")
    if len(report["items"]) > 100_000:
        raise ValueError("Report contains too many items")
    for item in report["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("Report contains an invalid listing")
        if not item["url"].startswith("https://"):
            raise ValueError("Report contains a non-HTTPS listing URL")


def init_db(db_path: Path | None = None) -> None:
    """Create or upgrade the database (versioned migrations; existing records are preserved)."""
    with closing(_db.connect(db_path or DB_PATH)):
        pass


def import_report(report: dict[str, Any], metadata: dict[str, Any],
                  db_path: Path | None = None, latest_path: Path | None = None) -> dict[str, int]:
    """Validate, then import atomically. A failed or invalid report never touches existing data."""
    db_path, latest_path = db_path or DB_PATH, latest_path or LATEST_REPORT    # resolved at call time
    validate_report(report)                              # raises before anything is written
    checked = report["checked_at_utc"]
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    run_id, artifact_id = str(metadata["run_id"]), str(metadata["artifact_id"])
    report_key = f"{run_id}:{artifact_id}"
    encoded = json.dumps({"report": report, "sync": metadata, "synced_at_utc": now}, ensure_ascii=False, indent=2).encode("utf-8")
    with closing(_db.connect(db_path)) as conn:
        if conn.execute("SELECT 1 FROM reports WHERE report_key = ?", (report_key,)).fetchone():
            if not Path(latest_path).exists():            # heal a missing file without re-importing
                _rel.atomic_write_bytes(latest_path, encoded)
            return {"inserted": 0, "updated": 0, "already_imported": 1, "total_items": len(report["items"])}
        # Only trust "vanished" for platforms whose scan completed (OpenTrain reports open_listings when ok).
        complete = {pid for pid, h in (report.get("integrations") or {}).items()
                    if isinstance(h, dict) and h.get("status") == "ok" and "open_listings" in h}
        items = [dict(i, report_checked_at_utc=checked) for i in report["items"]]
        with _db.tx(conn):                               # all-or-nothing
            stats = _ig.ingest(conn, items, now=now_dt, complete_platforms=complete)
            conn.execute("""INSERT INTO reports(report_key,checked_at_utc,run_id,run_url,artifact_id,imported_at_utc,listing_count)
                            VALUES(?,?,?,?,?,?,?)""", (report_key, checked, run_id, metadata.get("run_url", ""), artifact_id, now, len(items)))
            _db.set_state(conn, "integrations", report.get("integrations", {}))
            _db.set_state(conn, "eligibility_monitors", report.get("eligibility_monitors", {}))
            _db.set_state(conn, "last_report", {"checked_at_utc": checked, "run_id": run_id, "run_url": metadata.get("run_url", ""),
                                                "synced_at_utc": now, "listing_count": len(items), "report_file": metadata.get("report_file")})
            _db.audit(conn, "report.import", "report", report_key, {"items": len(items), **stats}, actor="system")
    _rel.atomic_write_bytes(latest_path, encoded)         # written only after the DB commit; replace is atomic
    return {"inserted": stats["new"], "updated": stats["updated"], "already_imported": 0, "total_items": len(items),
            "changed": stats["changed"], "expired": stats["expired"], "duplicates": stats["duplicates"]}


def _record_sync(ok: bool, error: BaseException | None = None, result: dict[str, Any] | None = None,
                 db_path: Path | None = None) -> None:
    """Persist sync health. Failures here must never mask the real sync outcome."""
    try:
        with closing(_db.connect(db_path or DB_PATH)) as conn, _db.tx(conn):
            health = _db.get_state(conn, "sync_health", {}) or {}
            now = _db.now_iso()
            health["last_attempt_utc"] = now
            if ok:
                health.update(last_success_utc=now, consecutive_failures=0, last_error=None, last_error_stage=None,
                              failure_streak_started=None, last_result=result or {})
            else:
                if not health.get("consecutive_failures"):
                    health["failure_streak_started"] = now
                health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
                health["last_error"] = str(error)[:500]
                health["last_error_stage"] = getattr(error, "stage", type(error).__name__)
                health["last_failure_utc"] = now
            _db.set_state(conn, "sync_health", health)
    except Exception:                                    # noqa: BLE001
        LOG.exception("Could not record sync health")


def sync_once(notify_after: bool = True) -> dict[str, Any]:
    try:
        report, metadata = get_latest_report()
        result = import_report(report, metadata)
    except Exception as exc:
        _record_sync(False, exc)                         # a failed download is NEVER recorded as success
        raise
    _record_sync(True, result=result)
    LOG.info(
        "Sync complete: %s new, %s updated, %s already imported; %s total listings; run %s",
        result["inserted"], result["updated"], result["already_imported"],
        result["total_items"], metadata["run_id"],
    )
    if notify_after and os.environ.get("AUTOPILOT_NO_NOTIFY") != "1":
        try:
            from microtask_monitor import notify
            with closing(_db.connect(DB_PATH)) as conn, _db.tx(conn):
                notify.generate(conn)
        except Exception:                                # noqa: BLE001
            LOG.exception("Notification generation failed")
    return {**result, **metadata, "checked_at_utc": report["checked_at_utc"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync public GitHub Actions opportunity reports")
    parser.add_argument("--loop", action="store_true", help="Repeat checks until stopped")
    parser.add_argument("--interval", type=int, default=900, help="Loop interval in seconds (minimum 300)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    interval = max(300, args.interval)
    while True:
        ok = False
        try:
            sync_once()
            ok = True
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError,
                zipfile.BadZipFile, json.JSONDecodeError) as exc:
            LOG.warning("Sync did not complete: %s", exc)
        except Exception:
            LOG.exception("Unexpected sync error")
        if not args.loop:
            raise SystemExit(0 if ok else 1)
        time.sleep(interval)


if __name__ == "__main__":
    main()
