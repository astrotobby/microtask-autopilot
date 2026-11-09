import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

os.environ.setdefault("AUTOPILOT_SQLITE_SYNCHRONOUS", "OFF")   # tests only: skip slow fsyncs

from microtask_monitor import db


def make_legacy_db(path: Path, count: int = 3):
    """A database exactly as the original sync-only versions created it (user_version 0)."""
    with closing(sqlite3.connect(path)) as conn, conn:
        db._m1_baseline(conn)
        for i in range(count):
            conn.execute("""INSERT INTO opportunities(url,title,source,summary,priority,relevance_score,category,
                kenya_eligibility,published,first_seen_utc,last_seen_utc,report_checked_at_utc,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (f"https://example.com/job/{i}", f"Legacy job {i}", "Feed", "summary", "high_review", 50 + i,
                          "ai_related_role", "kenya_mentioned_verify_requirements", "2026-11-01",
                          "2026-11-01T00:00:00+00:00", "2026-11-02T00:00:00+00:00", "20261102T000000Z",
                          json.dumps({"title": f"Legacy job {i}"})))
        conn.execute("INSERT INTO reports VALUES('1:2','20261102T000000Z','1','https://run','2','2026-11-02',3)")
        conn.execute("INSERT INTO sync_state VALUES('last_sync','{}','2026-11-02')")


class MigrationTests(unittest.TestCase):
    def test_fresh_database_is_created_at_latest_version_with_all_tables(self):
        with tempfile.TemporaryDirectory() as tmp, db.session(Path(tmp) / "a.sqlite3") as conn:
            self.assertEqual(db.user_version(conn), db.LATEST_VERSION)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for needed in ("opportunities", "applications", "application_drafts", "tasks", "task_checklist_items",
                           "work_sessions", "evidence", "submissions", "earnings", "payments", "notifications",
                           "platform_capabilities", "audit_log", "opportunity_changes", "reports", "sync_state"):
                self.assertIn(needed, tables)

    def test_legacy_database_is_upgraded_without_losing_any_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            make_legacy_db(path, 5)
            with db.session(path) as conn:
                self.assertEqual(db.user_version(conn), db.LATEST_VERSION)
                rows = conn.execute("SELECT url,title,relevance_score,kenya_eligibility,raw_json,first_seen_utc "
                                    "FROM opportunities ORDER BY url").fetchall()
                self.assertEqual(len(rows), 5)
                self.assertEqual(rows[2]["title"], "Legacy job 2")
                self.assertEqual(rows[2]["relevance_score"], 52)
                self.assertEqual(rows[2]["kenya_eligibility"], "kenya_mentioned_verify_requirements")
                self.assertEqual(json.loads(rows[2]["raw_json"])["title"], "Legacy job 2")
                self.assertEqual(rows[0]["first_seen_utc"], "2026-11-01T00:00:00+00:00")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT value FROM sync_state WHERE key='last_sync'").fetchone()[0], "{}")
                # New columns exist and default sensibly for old rows.
                self.assertIsNone(rows[0]["first_seen_utc"] and conn.execute(
                    "SELECT eligibility_class FROM opportunities LIMIT 1").fetchone()[0])
                self.assertEqual(conn.execute("SELECT status FROM opportunities LIMIT 1").fetchone()[0], "open")

    def test_upgrade_takes_a_file_backup_that_still_holds_the_old_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            make_legacy_db(path, 2)
            with db.session(path):
                pass
            backups = list(Path(tmp).glob("legacy.sqlite3.bak-v0-*"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as old:
                self.assertEqual(old.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 2)
                self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_migration_is_idempotent_and_does_not_back_up_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.sqlite3"
            make_legacy_db(path, 1)
            for _ in range(3):
                with db.session(path):
                    pass
            self.assertEqual(len(list(Path(tmp).glob("a.sqlite3.bak-*"))), 1)
            with db.session(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)

    def test_failed_migration_rolls_back_and_keeps_old_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.sqlite3"
            make_legacy_db(path, 1)
            original = db.MIGRATIONS
            def broken(conn):
                conn.execute("CREATE TABLE half_done(x)")
                raise RuntimeError("boom")
            try:
                db.MIGRATIONS = [(1, db._m1_baseline), (2, broken)]
                db.LATEST_VERSION = 2
                with self.assertRaises(RuntimeError):
                    db.connect(path)
            finally:
                db.MIGRATIONS = original
                db.LATEST_VERSION = original[-1][0]
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
                self.assertNotIn("half_done", tables)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)


class ConstraintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = db.session(Path(self.tmp.name) / "a.sqlite3")
        self.conn = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def test_invalid_statuses_and_negative_money_are_rejected(self):
        c = self.conn
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO applications(title,status,created_at,updated_at) VALUES('x','bogus','a','a')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO earnings(amount,currency,created_at,updated_at) VALUES(-1,'USD','a','a')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO earnings(amount,currency,status,created_at,updated_at) VALUES(1,'USD','bogus','a','a')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO payments(amount,currency,received_date,created_at) VALUES(0,'USD','2026-11-01','a')")

    def test_deleting_a_task_cascades_to_checklist_evidence_sessions(self):
        c = self.conn
        c.execute("INSERT INTO tasks(title,created_at,updated_at) VALUES('t','a','a')")
        tid = c.execute("SELECT id FROM tasks").fetchone()[0]
        c.execute("INSERT INTO task_checklist_items(task_id,position,text) VALUES(?,1,'x')", (tid,))
        c.execute("INSERT INTO evidence(task_id,created_at) VALUES(?, 'a')", (tid,))
        c.execute("INSERT INTO work_sessions(task_id,started_at) VALUES(?, 'a')", (tid,))
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        for table in ("task_checklist_items", "evidence", "work_sessions"):
            self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_notification_fingerprint_is_unique(self):
        c = self.conn
        c.execute("INSERT INTO notifications(fingerprint,type,title,created_at) VALUES('f','t','x','a')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO notifications(fingerprint,type,title,created_at) VALUES('f','t','y','b')")

    def test_tx_rolls_back_on_error_and_audit_is_recorded(self):
        c = self.conn
        with self.assertRaises(RuntimeError):
            with db.tx(c):
                c.execute("INSERT INTO tasks(title,created_at,updated_at) VALUES('t','a','a')")
                raise RuntimeError("fail")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        db.audit(c, "test", "task", 1, {"k": "v"})
        self.assertEqual(c.execute("SELECT action FROM audit_log").fetchone()[0], "test")

    def test_state_helpers_round_trip(self):
        db.set_state(self.conn, "k", {"a": 1})
        self.assertEqual(db.get_state(self.conn, "k"), {"a": 1})
        self.assertEqual(db.get_state(self.conn, "missing", "dflt"), "dflt")


if __name__ == "__main__":
    unittest.main()
