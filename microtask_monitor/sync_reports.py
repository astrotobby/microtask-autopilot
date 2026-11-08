"""Synchronize the newest successful public GitHub Actions report into local SQLite.

No GitHub token is required because the configured repository is public.
Only report artifacts from successful runs on the configured branch are consumed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
import urllib.error
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
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read(2_000_000)
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("GitHub API returned an unexpected response")
    return data


def download_archive(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "Microtask-Autopilot-Local-Agent/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Artifact archive exceeds the configured size limit")
    return data


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
        archive = download_archive(
            f"{API}/repos/{OWNER}/{REPO}/actions/artifacts/{artifact['id']}/zip"
        )
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
    with sqlite3.connect(db_path) as conn:
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

    with sqlite3.connect(db_path) as conn:
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
        try:
            sync_once()
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError,
                zipfile.BadZipFile, json.JSONDecodeError) as exc:
            LOG.warning("Sync did not complete: %s", exc)
        except Exception:
            LOG.exception("Unexpected sync error")
        if not args.loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
