"""Synchronize the newest successful public GitHub Actions report into local SQLite.

No GitHub token is required because the configured repository is public.
Only report artifacts from successful runs on the configured branch are consumed.
"""
from __future__ import annotations

import argparse
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
MAX_ARCHIVE_BYTES = 15 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
LOG = logging.getLogger("microtask_autopilot.sync")


def api_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Microtask-Autopilot-Local-Agent/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(2_000_000)
    except urllib.error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", "replace")
        raise SyncStageError("github-api", f"{exc.reason} for {url} {body}".strip(),
                             exc.code) from exc
    except urllib.error.URLError as exc:
        raise SyncStageError("network", f"Could not reach api.github.com: {exc.reason}") from exc
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("GitHub API returned an unexpected response")
    return data


class SyncStageError(RuntimeError):
    """A sync failure tagged with the stage that failed and any HTTP status."""

    def __init__(self, stage: str, message: str, status: int | None = None):
        self.stage = stage
        self.status = status
        prefix = f"[{stage}]" + (f" HTTP {status}" if status is not None else "")
        super().__init__(f"{prefix}: {message}")


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


def fetch_published_report(run: dict[str, Any], artifact: dict[str, Any],
                           url: str = RAW_REPORT_URL, opener=urllib.request.urlopen,
                           ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch the unauthenticated report copy the workflow publishes to report-data."""
    request = urllib.request.Request(url, headers={
        "User-Agent": "Microtask-Autopilot-Local-Agent/1.0"})
    try:
        with opener(request, timeout=30) as response:
            payload = response.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SyncStageError("published-report", f"{exc.reason} for {url}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise SyncStageError("network", f"Could not reach raw.githubusercontent.com: {exc.reason}") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("Published report is too large")
    report = json.loads(payload.decode("utf-8"))
    validate_report(report)
    metadata = {
        "run_id": run["id"],
        "run_url": run.get("html_url", ""),
        "run_created_at": run.get("created_at", ""),
        "artifact_id": artifact["id"],
        "artifact_created_at": artifact.get("created_at", ""),
        "artifact_expires_at": artifact.get("expires_at", ""),
        "report_file": f"{REPORT_DATA_BRANCH}/latest.json",
    }
    return report, metadata


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
            return fetch_published_report(run, artifact)
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


def init_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                report_key TEXT PRIMARY KEY,
                checked_at_utc TEXT NOT NULL,
                run_id TEXT NOT NULL,
                run_url TEXT,
                artifact_id TEXT NOT NULL,
                imported_at_utc TEXT NOT NULL,
                listing_count INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source TEXT,
                summary TEXT,
                priority TEXT,
                relevance_score INTEGER,
                category TEXT,
                kenya_eligibility TEXT,
                published TEXT,
                first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                report_checked_at_utc TEXT NOT NULL,
                raw_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_opportunities_priority
            ON opportunities(priority, relevance_score DESC)
        """)
        conn.commit()


def import_report(report: dict[str, Any], metadata: dict[str, Any],
                  db_path: Path = DB_PATH, latest_path: Path = LATEST_REPORT) -> dict[str, int]:
    validate_report(report)
    init_db(db_path)
    checked = report["checked_at_utc"]
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(metadata["run_id"])
    artifact_id = str(metadata["artifact_id"])
    inserted = updated = 0
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_payload = {"report": report, "sync": metadata, "synced_at_utc": now}
    latest_path.write_text(json.dumps(latest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with closing(sqlite3.connect(db_path)) as conn, conn:
        existing = conn.execute(
            "SELECT 1 FROM reports WHERE report_key = ?", (f"{run_id}:{artifact_id}",)
        ).fetchone()
        if existing:
            return {"inserted": 0, "updated": 0, "already_imported": 1,
                    "total_items": len(report["items"])}
        for item in report["items"]:
            url = item["url"].split("#", 1)[0].rstrip("/")
            row = conn.execute(
                "SELECT first_seen_utc FROM opportunities WHERE url = ?", (url,)
            ).fetchone()
            first_seen = row[0] if row else now
            if row:
                updated += 1
            else:
                inserted += 1
            conn.execute("""
                INSERT INTO opportunities (
                    url,title,source,summary,priority,relevance_score,category,
                    kenya_eligibility,published,first_seen_utc,last_seen_utc,
                    report_checked_at_utc,raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title, source=excluded.source,
                    summary=excluded.summary, priority=excluded.priority,
                    relevance_score=excluded.relevance_score,
                    category=excluded.category,
                    kenya_eligibility=excluded.kenya_eligibility,
                    published=excluded.published,
                    last_seen_utc=excluded.last_seen_utc,
                    report_checked_at_utc=excluded.report_checked_at_utc,
                    raw_json=excluded.raw_json
            """, (
                url, item.get("title", "Untitled"), item.get("source", ""),
                item.get("summary", ""), item.get("priority", ""),
                int(item.get("relevance_score", 0)), item.get("category", ""),
                item.get("kenya_eligibility", ""), item.get("published", ""),
                first_seen, now, checked,
                json.dumps(item, ensure_ascii=False),
            ))
        conn.execute("""
            INSERT INTO reports(report_key,checked_at_utc,run_id,run_url,
                                 artifact_id,imported_at_utc,listing_count)
            VALUES(?,?,?,?,?,?,?)
        """, (f"{run_id}:{artifact_id}", checked, run_id, metadata.get("run_url", ""),
              artifact_id, now, len(report["items"])))
        conn.execute("""
            INSERT INTO sync_state(key,value,updated_at) VALUES('last_sync',?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
        """, (json.dumps(latest_payload, ensure_ascii=False), now))
        conn.commit()
    return {"inserted": inserted, "updated": updated, "already_imported": 0,
            "total_items": len(report["items"])}


def sync_once() -> dict[str, Any]:
    report, metadata = get_latest_report()
    result = import_report(report, metadata)
    LOG.info(
        "Sync complete: %s new, %s updated, %s already imported; %s total listings; run %s",
        result["inserted"], result["updated"], result["already_imported"],
        result["total_items"], metadata["run_id"],
    )
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
