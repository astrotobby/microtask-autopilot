"""SQLite schema, versioned migrations and small helpers for Microtask Autopilot.

Migrations are append-only and run inside a transaction. Existing databases (created by the
earlier sync-only versions) are adopted at version 1 and upgraded in place; a file backup is
taken first, so existing records are never destroyed.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "autopilot.sqlite3"

APPLICATION_STATUSES = ("draft", "approved", "submitted", "interview", "accepted", "rejected", "withdrawn")
TASK_STATUSES = ("accepted", "in_progress", "completed", "submitted", "approved", "rejected", "paid")
EARNING_STATUSES = ("expected", "pending", "received", "cancelled")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _q(values: tuple[str, ...]) -> str:
    return ",".join("'%s'" % v for v in values)


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _m1_baseline(conn: sqlite3.Connection) -> None:
    """Tables created by the original sync service (adopted unchanged)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS sync_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reports (
        report_key TEXT PRIMARY KEY, checked_at_utc TEXT NOT NULL, run_id TEXT NOT NULL, run_url TEXT,
        artifact_id TEXT NOT NULL, imported_at_utc TEXT NOT NULL, listing_count INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS opportunities (
        url TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT, summary TEXT, priority TEXT,
        relevance_score INTEGER, category TEXT, kenya_eligibility TEXT, published TEXT,
        first_seen_utc TEXT NOT NULL, last_seen_utc TEXT NOT NULL, report_checked_at_utc TEXT NOT NULL,
        raw_json TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_priority ON opportunities(priority, relevance_score DESC)")


def _m2_intelligence_and_work(conn: sqlite3.Connection) -> None:
    for column, decl in (
        ("platform_id", "TEXT"), ("external_id", "TEXT"), ("eligibility_class", "TEXT"),
        ("eligibility_confidence", "TEXT"), ("eligibility_reason", "TEXT"), ("status", "TEXT DEFAULT 'open'"),
        ("deadline", "TEXT"), ("payment_text", "TEXT"), ("payment_currency", "TEXT"),
        ("payment_amount", "REAL"), ("payment_usd_per_hour", "REAL"), ("skills_json", "TEXT"),
        ("requirements_json", "TEXT"), ("equipment", "TEXT"), ("effort", "TEXT"),
        ("score_breakdown_json", "TEXT"), ("content_hash", "TEXT"), ("last_verified_utc", "TEXT"),
        ("expired_utc", "TEXT"), ("duplicate_of", "TEXT"), ("apply_url", "TEXT")):
        _add_column(conn, "opportunities", column, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opp_class ON opportunities(eligibility_class, relevance_score DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opp_platform ON opportunities(platform_id, status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS opportunity_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, changed_at TEXT NOT NULL,
        field TEXT NOT NULL, old_value TEXT, new_value TEXT)""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, opportunity_url TEXT, platform_id TEXT, title TEXT NOT NULL,
        apply_url TEXT, status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ({_q(APPLICATION_STATUSES)})),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT, response_at TEXT,
        response_text TEXT, notes TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS application_drafts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        version INTEGER NOT NULL, summary TEXT, body TEXT NOT NULL, missing_info_json TEXT, eligibility_note TEXT,
        provider TEXT, created_at TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, approved_at TEXT)""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
        opportunity_url TEXT, platform_id TEXT, title TEXT NOT NULL, instructions TEXT, simplified TEXT,
        guidance_json TEXT, status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ({_q(TASK_STATUSES)})),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, due_at TEXT, notes TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_checklist_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'step',
        done INTEGER NOT NULL DEFAULT 0, done_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS work_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        started_at TEXT NOT NULL, ended_at TEXT, minutes REAL, notes TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'note', description TEXT, file_name TEXT, file_path TEXT, sha256 TEXT,
        size_bytes INTEGER, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        submitted_at TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'manual', status TEXT NOT NULL DEFAULT 'submitted',
        platform_reference TEXT, notes TEXT)""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        platform_id TEXT, description TEXT, amount REAL NOT NULL CHECK (amount >= 0), currency TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'expected' CHECK (status IN ({_q(EARNING_STATUSES)})),
        expected_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, notes TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, earning_id INTEGER REFERENCES earnings(id) ON DELETE SET NULL,
        platform_id TEXT, amount REAL NOT NULL CHECK (amount > 0), currency TEXT NOT NULL,
        received_date TEXT NOT NULL, method TEXT, reference TEXT, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info', title TEXT NOT NULL, body TEXT, url TEXT,
        created_at TEXT NOT NULL, read_at TEXT, channels_json TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS platform_capabilities (
        platform_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, verified_on TEXT, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
        entity TEXT, entity_id TEXT, detail_json TEXT)""")


REVIEW_STATUSES = ("new", "reviewed", "shortlisted", "applied", "rejected", "accepted")


def _m3_daily_pipeline(conn: sqlite3.Connection) -> None:
    """Daily earning-opportunity analyses, their persisted shortlists, and your review status per opportunity."""
    conn.execute("""CREATE TABLE IF NOT EXISTS analysis_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL UNIQUE, analysis_date TEXT NOT NULL,
        trigger TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
        status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed')),
        stats_json TEXT, coverage_json TEXT, error TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_done ON analysis_runs(status, completed_at DESC)")
    conn.execute("""CREATE TABLE IF NOT EXISTS shortlist_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
        url TEXT NOT NULL, rank INTEGER, daily_score INTEGER NOT NULL, platform_id TEXT, eligibility_class TEXT,
        in_top5 INTEGER NOT NULL DEFAULT 0, in_shortlist INTEGER NOT NULL DEFAULT 0, in_kenya INTEGER NOT NULL DEFAULT 0,
        in_worldwide INTEGER NOT NULL DEFAULT 0, is_new INTEGER NOT NULL DEFAULT 0, snapshot_json TEXT NOT NULL,
        UNIQUE(analysis_id, url))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shortlist_analysis ON shortlist_entries(analysis_id, rank)")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS opportunity_review (
        url TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({_q(REVIEW_STATUSES)})),
        notes TEXT, updated_at TEXT NOT NULL)""")


EVENT_KINDS = ("submitted", "response", "interview_scheduled", "interview_done", "decision", "followup_sent", "note")
REMINDER_KINDS = ("deadline", "assessment", "followup", "interview", "draft_review", "task", "profile", "decision")
REMINDER_STATUSES = ("pending", "done", "dismissed")
ACTION_STATUSES = ("open", "done", "skipped")


def _m4_first_earning_workflow(conn: sqlite3.Connection) -> None:
    """Follow-up tracking, application packets, reminders, daily action items and reusable profile answers."""
    for column, decl in (("next_followup_at", "TEXT"), ("interview_at", "TEXT"), ("decision", "TEXT"), ("decision_at", "TEXT")):
        _add_column(conn, "applications", column, decl)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS application_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        at TEXT NOT NULL, event_date TEXT, kind TEXT NOT NULL CHECK (kind IN ({_q(EVENT_KINDS)})), detail TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_app ON application_events(application_id, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS application_packets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        version INTEGER NOT NULL, generated_at TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0,
        content_json TEXT NOT NULL, text_export TEXT NOT NULL, UNIQUE(application_id, version))""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ({_q(REMINDER_KINDS)})), title TEXT NOT NULL, detail TEXT, due_at TEXT NOT NULL,
        ref_type TEXT, ref_id TEXT, url TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ({_q(REMINDER_STATUSES)})),
        created_at TEXT NOT NULL, done_at TEXT, notified_at TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at)")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS action_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, action_date TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL, detail TEXT,
        kind TEXT NOT NULL, priority INTEGER NOT NULL, est_minutes INTEGER, target TEXT, url TEXT, ref_json TEXT,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ({_q(ACTION_STATUSES)})), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(action_date, key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS profile_answers (
        key TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT, category TEXT, required INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'template', position INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")


EXEC_STATUSES = ("created", "blocked", "ready", "running", "paused", "interrupted", "qa_failed", "awaiting_review", "approved",
                 "ready_for_submission", "submitted", "accepted", "rejected", "failed", "cancelled")
AI_POLICIES = ("unknown", "allowed", "prohibited")


def _m5_execution_engine(conn: sqlite3.Connection) -> None:
    """Execution jobs, their progress events, files and independent QA results."""
    conn.execute(f"""CREATE TABLE IF NOT EXISTS execution_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL, platform_id TEXT, opportunity_url TEXT,
        title TEXT NOT NULL, instructions TEXT NOT NULL, instructions_source TEXT NOT NULL DEFAULT 'user_provided',
        work_type TEXT NOT NULL, worker TEXT NOT NULL, classification_json TEXT,
        status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ({_q(EXEC_STATUSES)})), status_reason TEXT,
        ai_policy TEXT NOT NULL DEFAULT 'unknown' CHECK (ai_policy IN ({_q(AI_POLICIES)})), ai_source TEXT, ai_detected TEXT,
        ai_run_approved_at TEXT, runner TEXT, attempts INTEGER NOT NULL DEFAULT 0, workspace TEXT NOT NULL, deadline TEXT,
        pause_requested INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
        manual_ack_at TEXT, approved_at TEXT, submitted_at TEXT, submission_reference TEXT, outcome_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_status ON execution_jobs(status, updated_at DESC)")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES execution_jobs(id) ON DELETE CASCADE,
        at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_events ON execution_events(job_id, id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES execution_jobs(id) ON DELETE CASCADE,
        role TEXT NOT NULL, rel_path TEXT NOT NULL, sha256 TEXT, size_bytes INTEGER, created_at TEXT NOT NULL,
        UNIQUE(job_id, role, rel_path))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_qa (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES execution_jobs(id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL, check_name TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pass','fail','manual')),
        executed INTEGER NOT NULL DEFAULT 0, detail TEXT, created_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exec_qa ON execution_qa(job_id, attempt)")


MIGRATIONS = [(1, _m1_baseline), (2, _m2_intelligence_and_work), (3, _m3_daily_pipeline), (4, _m4_first_earning_workflow), (5, _m5_execution_engine)]
LATEST_VERSION = MIGRATIONS[-1][0]


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, db_path: Path | None = None) -> int:
    """Upgrade to the latest schema. Backs up an existing populated file before changing it."""
    current = user_version(conn)
    if current >= LATEST_VERSION:
        return current
    has_data = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
    if db_path and has_data and Path(db_path).exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(db_path, f"{db_path}.bak-v{current}-{stamp}")
    for version, apply in MIGRATIONS:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            apply(conn)
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return user_version(conn)


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    sync = os.environ.get("AUTOPILOT_SQLITE_SYNCHRONOUS", "").upper()   # opt-in, e.g. for tests
    if sync in ("OFF", "NORMAL", "FULL"):
        conn.execute(f"PRAGMA synchronous = {sync}")
    try:
        migrate(conn, path)
    except BaseException:
        conn.close()          # never leave the file locked after a failed migration
        raise
    return conn


@contextmanager
def session(path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    """Connection that is always closed; use ``with tx(conn):`` for atomic writes."""
    with closing(connect(path)) as conn:
        yield conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def audit(conn: sqlite3.Connection, action: str, entity: str = "", entity_id: Any = "",
          detail: dict | None = None, actor: str = "user") -> None:
    conn.execute("INSERT INTO audit_log(at,actor,action,entity,entity_id,detail_json) VALUES(?,?,?,?,?,?)",
                 (now_iso(), actor, action, entity, str(entity_id), json.dumps(detail or {}, ensure_ascii=False)))


def rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return row[0]


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("""INSERT INTO sync_state(key,value,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                 (key, json.dumps(value, ensure_ascii=False), now_iso()))
