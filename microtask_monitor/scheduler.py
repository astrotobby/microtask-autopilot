"""The single controlled scheduler for this PC.

One loop owns every periodic job, so jobs never overlap or compete for the same record:

    report_sync       every 15 min   download the cloud scan's report (existing sync)
    discovery         hourly         permitted adapters only (discovery.py)
    daily_analysis    08:00 Nairobi (05:00 UTC), once per Nairobi day; a missed run is recovered after a restart
    maintenance       every 10 min   reminders and today's action list
    stale_check       every 6 h      stale listings and passed deadlines
    connection_health every 6 h      sessions due for re-confirmation (never signs in or reads a session)
    policy_review     daily          permission evidence re-synced; stale or changed records lose their automation
    orchestrator      every 2 min    intake, crash recovery, dispatch of permitted work
    submissions       every 5 min    fully authorized submissions only; unknown outcomes are never re-sent
    payment_monitor   hourly         read-only: pending payments that need you
    cleanup           daily          temporary execution files past your retention period

Each job takes a database lease first (only one process can hold it), runs under the global stop and pause rules,
backs off after failures and is stopped automatically after repeated failures until you re-enable it.
GitHub's own schedule for the cloud scan is best-effort; nothing here promises exact times while the PC is off.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from microtask_monitor import connections as cx, control, daily, db, discovery, execution as ex, notify, orchestrator, permissions, submission
except ImportError:  # pragma: no cover
    import connections as cx, control, daily, db, discovery, execution as ex, notify, orchestrator, permissions, submission

LOG = logging.getLogger("microtask_autopilot.scheduler")
TICK_SECONDS = 30
LEASE_SECONDS = 900
MAX_FAILURES = 5                 # consecutive failures before a job is stopped automatically
BACKOFF_CAP_SECONDS = 3600
ALWAYS = frozenset({"report_sync", "maintenance", "connection_health", "policy_review", "payment_monitor", "stale_check"})   # read-only upkeep keeps running while paused


@dataclass
class Job:
    name: str
    interval: int
    fn: Callable[["Scheduler", Any, datetime], Any]
    due: Callable[[Any, datetime], bool] | None = None      # custom due rule (the daily analysis)


def owner() -> str:
    return f"scheduler:pid:{os.getpid()}"


def backoff(failures: int, interval: int) -> int:
    """Seconds until the next try after ``failures`` consecutive failures (60 s, doubling, capped)."""
    return interval if failures <= 0 else int(min(max(interval, BACKOFF_CAP_SECONDS), 60 * (2 ** (failures - 1))))


class Scheduler:
    def __init__(self, db_path: Path | str | None = None, jobs: list[Job] | None = None, sync_fn: Callable[[], Any] | None = None,
                 store: Any = None, runner: Any = None, exec_dir: Path | None = None):
        self.db_path, self.sync_fn, self.store, self.runner, self.exec_dir = Path(db_path or db.DB_PATH), sync_fn, store, runner, exec_dir
        self.jobs = jobs if jobs is not None else self.default_jobs()
        self.heartbeat_at: str | None = None

    # ---------------------------------------------------------------- job bodies
    def default_jobs(self) -> list[Job]:
        return [Job("report_sync", 900, lambda s, c, n: s.sync_fn() if s.sync_fn else "no sync function in this process"),
                Job("maintenance", 600, _maintenance),
                Job("discovery", 3600, _discovery),
                Job("daily_analysis", 600, _daily, due=lambda c, n: daily.due_today(c, n)),
                Job("stale_check", 21600, lambda s, c, n: discovery.stale_check(c, n)),
                Job("connection_health", 21600, _connection_health),
                Job("policy_review", 86400, lambda s, c, n: permissions.sync(c)),
                Job("orchestrator", 120, lambda s, c, n: orchestrator.tick(c, s.runner, s.db_path, s.exec_dir, background=True)),
                Job("submissions", 300, lambda s, c, n: {"recovered": submission.recover(c, n), **submission.process_queue(c)}),
                Job("payment_monitor", 3600, _payment_monitor),
                Job("cleanup", 86400, lambda s, c, n: cleanup(c, n, s.exec_dir))]

    # ---------------------------------------------------------------- leases and state
    def _state(self, conn, name: str) -> dict[str, Any]:
        r = conn.execute("SELECT * FROM scheduler_jobs WHERE name=?", (name,)).fetchone()
        if not r:
            with db.tx(conn):
                conn.execute("INSERT OR IGNORE INTO scheduler_jobs(name) VALUES(?)", (name,))
            r = conn.execute("SELECT * FROM scheduler_jobs WHERE name=?", (name,)).fetchone()
        return dict(r)

    def acquire(self, conn, name: str, now: datetime) -> bool:
        """Take the job's lease atomically. False when another live process holds it: the same job never runs twice at once."""
        self._state(conn, name)
        conn.execute("BEGIN IMMEDIATE")
        try:
            n = conn.execute("UPDATE scheduler_jobs SET lease_owner=?, lease_until=?, last_started=? WHERE name=? AND (lease_owner IS NULL OR lease_until < ? OR lease_owner=?)",
                             (owner(), (now + timedelta(seconds=LEASE_SECONDS)).isoformat(), now.isoformat(), name, now.isoformat(), owner())).rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return n == 1

    def is_due(self, conn, job: Job, now: datetime) -> tuple[bool, str]:
        st = self._state(conn, job.name)
        if st["disabled_reason"]:
            return False, st["disabled_reason"]
        if job.name not in ALWAYS:
            ok, why = control.may_run(conn)
            if not ok:
                return False, why
        if st["next_due"] and st["next_due"] > now.isoformat():
            return False, "not due"
        if job.due is not None and not job.due(conn, now):
            return False, "not due"
        return True, ""

    def run_job(self, conn, job: Job, now: datetime) -> dict[str, Any]:
        """Run one job if it is due and the lease is free. Failures are recorded and backed off; they never propagate."""
        due, why = self.is_due(conn, job, now)
        if not due:
            return {"job": job.name, "ran": False, "reason": why}
        if not self.acquire(conn, job.name, now):
            return {"job": job.name, "ran": False, "reason": "another process holds this job's lease"}
        st = self._state(conn, job.name)
        try:
            result = job.fn(self, conn, now)
            with db.tx(conn):
                conn.execute("""UPDATE scheduler_jobs SET last_finished=?, last_status='ok', last_error=NULL, next_due=?, lease_owner=NULL, lease_until=NULL,
                                runs=runs+1, consecutive_failures=0 WHERE name=?""", (db.now_iso(), (now + timedelta(seconds=job.interval)).isoformat(), job.name))
            return {"job": job.name, "ran": True, "ok": True, "result": result}
        except Exception as exc:                                    # noqa: BLE001 - a failing job must not stop the scheduler
            fails = st["consecutive_failures"] + 1
            disabled = f"Stopped automatically after {fails} failures in a row: {str(exc)[:160]}" if fails >= MAX_FAILURES else None
            with db.tx(conn):
                conn.execute("""UPDATE scheduler_jobs SET last_finished=?, last_status='error', last_error=?, next_due=?, lease_owner=NULL, lease_until=NULL,
                                runs=runs+1, consecutive_failures=?, disabled_reason=? WHERE name=?""",
                             (db.now_iso(), str(exc)[:500], (now + timedelta(seconds=backoff(fails, job.interval))).isoformat(), fails, disabled, job.name))
                if disabled:
                    db.audit(conn, "scheduler.job_stopped", "scheduler_job", job.name, {"failures": fails}, actor="system")
                    notify.create(conn, "task_attention", f"Automation stopped: {job.name}", disabled, severity="urgent", key=f"scheduler_stopped:{job.name}:{now.date().isoformat()}")
            LOG.exception("Scheduled job %s failed (%s in a row)", job.name, fails)
            return {"job": job.name, "ran": True, "ok": False, "error": str(exc)[:300], "stopped": bool(disabled)}

    def run_pending(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """One scheduler pass: every due job, one after another (never in parallel with itself or each other)."""
        now = now or datetime.now(timezone.utc)
        out = []
        with closing(db.connect(self.db_path)) as conn:
            for job in self.jobs:
                out.append(self.run_job(conn, job, now))
            self.heartbeat_at = now.isoformat()
            with db.tx(conn):
                db.set_state(conn, "scheduler_heartbeat", {"at": self.heartbeat_at, "owner": owner()})
        return out

    def recover_after_restart(self) -> dict[str, Any]:
        """Called once at start: leases held by a dead process are released, interrupted jobs and unknown submissions are marked."""
        with closing(db.connect(self.db_path)) as conn:
            with db.tx(conn):
                released = conn.execute("UPDATE scheduler_jobs SET lease_owner=NULL, lease_until=NULL WHERE lease_owner IS NOT NULL AND lease_owner<>?", (owner(),)).rowcount
            return {"leases_released": released, **orchestrator.recover(conn), "submissions_marked_unknown": submission.recover(conn)}

    def loop(self, stop: threading.Event | None = None, tick: int = TICK_SECONDS) -> None:
        try:
            LOG.info("Scheduler recovery: %s", self.recover_after_restart())
        except Exception:                                           # noqa: BLE001
            LOG.exception("Scheduler recovery failed")
        while not (stop and stop.is_set()):
            try:
                self.run_pending()
            except Exception:                                       # noqa: BLE001 - e.g. the database is briefly locked
                LOG.exception("Scheduler pass failed")
            if stop:
                if stop.wait(tick):
                    return
            else:
                time.sleep(tick)


# ------------------------------------------------------------------------------------------- job functions
def _maintenance(s: Scheduler, conn, now: datetime) -> str:
    try:
        from microtask_monitor import actions, reminders
    except ImportError:  # pragma: no cover
        import actions, reminders
    with db.tx(conn):
        reminders.generate(conn, now)
    reminders.notify_due(conn, now)
    actions.persist_today(conn, now)
    return "ok"


def _discovery(s: Scheduler, conn, now: datetime) -> dict[str, Any]:
    result = discovery.run(conn, discovery.default_adapters(s.store), now)
    if result["changed"]:                                           # a meaningful change refreshes the ranking at once
        result["analysis"] = {k: v for k, v in daily.run_analysis(conn, now, "discovery", f"discovery:{now.isoformat()}").items() if k != "stats"}
    result["intake"] = orchestrator.intake(conn)
    return {"changed": result["changed"], "intake": result["intake"], "adapters": {k: v.get("status") for k, v in result["adapters"].items()}}


def _daily(s: Scheduler, conn, now: datetime) -> Any:
    out = daily.run_analysis(conn, now, "schedule", f"schedule:{daily.analysis_date(now)}")
    return {k: v for k, v in out.items() if k != "stats"}


def _connection_health(s: Scheduler, conn, now: datetime) -> dict[str, int]:
    """Read-only: which connections are due for your re-confirmation. Never opens a browser or inspects a session."""
    due = [c for c in cx.list_status(conn) if c["status"] in ("check_due", "needs_user_action")]
    for c in due:
        notify.create(conn, "task_attention", f"Check your {c['name']} connection", c["required_action"] or "", severity="important",
                      key=f"connection:{c['platform_id']}:{c['status']}:{now.date().isoformat()}")
    return {"needing_you": len(due)}


def _payment_monitor(s: Scheduler, conn, now: datetime) -> dict[str, int]:
    """Read-only: accepted work with no payment recorded yet. It never withdraws or transfers anything."""
    waiting = conn.execute("SELECT COUNT(*) FROM assignments WHERE stage IN ('work_accepted','payment_pending')").fetchone()[0]
    if waiting:
        notify.create(conn, "payment_update", f"{waiting} accepted assignment(s) await payment", "Withdraw yourself, then record the payout reference.",
                      severity="info", key=f"payments_waiting:{waiting}:{now.date().isoformat()}")
    return {"awaiting_payment": waiting}


def cleanup(conn, now: datetime | None = None, exec_dir: Path | None = None) -> dict[str, int]:
    """Remove bulky temporary files of finished jobs after your retention period. The package, QA report and logs are always kept."""
    now = now or datetime.now(timezone.utc)
    days = control.settings(conn)["retention_days"]
    cutoff = (now - timedelta(days=days)).isoformat()
    removed = 0
    for r in conn.execute("SELECT id, workspace FROM execution_jobs WHERE status IN ('submitted','accepted','rejected','cancelled') AND updated_at < ?", (cutoff,)).fetchall():
        ws = Path(r["workspace"] or "")
        base = Path(exec_dir or ex.EXEC_DIR).resolve()
        if not r["workspace"] or base not in ws.resolve().parents:          # never delete outside the executions folder
            continue
        for sub in ("input", "tests"):
            if (ws / sub).is_dir():
                shutil.rmtree(ws / sub, ignore_errors=True)
                removed += 1
    staging = Path(exec_dir or ex.EXEC_DIR) / "staging"
    if staging.is_dir():
        for folder in staging.iterdir():
            aid = folder.name.rsplit("-", 1)[-1]
            row = conn.execute("SELECT stage, updated_at FROM assignments WHERE id=?", (aid if aid.isdigit() else -1,)).fetchone()
            if folder.is_dir() and (row is None or (row["stage"] in ("paid", "rejected", "cancelled") and row["updated_at"] < cutoff)):
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
    return {"folders_removed": removed, "retention_days": days}


def enable(conn, name: str, confirmed_by_user: bool) -> dict[str, Any]:
    """Re-enable a job that was stopped after repeated failures. Only you can do this."""
    if confirmed_by_user is not True:
        raise control.ControlError("Only you can re-enable a stopped job (confirmed_by_user=true)")
    with db.tx(conn):
        n = conn.execute("UPDATE scheduler_jobs SET disabled_reason=NULL, consecutive_failures=0, next_due=NULL WHERE name=?", (name,)).rowcount
        db.audit(conn, "scheduler.job_enabled", "scheduler_job", name)
    if not n:
        raise control.ControlError(f"Unknown job '{name}'")
    return status(conn)


def status(conn) -> dict[str, Any]:
    beat = db.get_state(conn, "scheduler_heartbeat", {}) or {}
    alive = False
    if beat.get("at"):
        try:
            alive = (datetime.now(timezone.utc) - datetime.fromisoformat(beat["at"])).total_seconds() < TICK_SECONDS * 6
        except ValueError:
            pass
    return {"heartbeat": beat, "alive": alive, "jobs": db.rows(conn.execute("SELECT * FROM scheduler_jobs ORDER BY name")),
            "daily_target": "08:00 Africa/Nairobi (05:00 UTC); a missed run is recovered the next time this PC is on"}
