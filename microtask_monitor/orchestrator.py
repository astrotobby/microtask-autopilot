"""The task orchestrator: intake, ranking, claiming and dispatch of permitted work to the specialist workers.

One pass (``tick``):
1. intake   - new assignments get their eligibility checked and are classified (task_intel);
2. recover  - jobs and claims left behind by a crash are marked interrupted / released (never silently re-run);
3. rank     - READY assignments are ordered by their score;
4. authorize - each candidate must pass the global stop, your settings, local resource limits and the permission engine;
5. claim    - an atomic database claim, so two workers can never take the same assignment;
6. dispatch - the execution engine runs the right specialist in the job's isolated workspace, then independent QA.

The orchestrator never takes a task on a platform, never signs in and never submits. Work it may run today is the
direct-client lane (your own client's work, supplied by you) and any platform whose record carries explicit, dated
evidence for automated execution. Everything else stays with you and is shown with the reason.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import assignments as asg, control, db, execution as ex, lifecycle, permissions, qa_gate, task_intel, workers
except ImportError:  # pragma: no cover
    import assignments as asg, control, db, execution as ex, lifecycle, permissions, qa_gate, task_intel, workers

MAX_ATTEMPTS = 2
AUTHORIZATION_REF = "automation settings"
_threads: dict[int, threading.Thread] = {}


def owner() -> str:
    return f"orchestrator:pid:{os.getpid()}"


# ------------------------------------------------------------------------------------------- staged inputs
def staging_dir(aid: int, exec_dir: Path | None = None) -> Path:
    return Path(exec_dir or ex.EXEC_DIR) / "staging" / f"assignment-{int(aid)}"


def stage_inputs(conn, aid: int, files: dict[str, bytes], exec_dir: Path | None = None) -> list[str]:
    """Keep the input files you supplied for a task until its job workspace exists. Names are sanitised; nothing leaves the folder."""
    folder = staging_dir(aid, exec_dir)
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for name, content in list(files.items())[:10]:
        if len(content) > ex.MAX_INPUT_BYTES:
            raise ex.ExecutionError(f"Input file {name} is larger than 10 MB")
        safe = (re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "input")[:100]
        (folder / safe).write_bytes(content)
        saved.append(safe)
    if "spec.json" in saved:
        a = asg.get(conn, aid)
        import json
        with db.tx(conn):
            conn.execute("UPDATE assignments SET requirements_json=? WHERE id=?", (json.dumps({**a["requirements"], "deterministic_spec": True}), aid))
    return saved


def _staged(aid: int, exec_dir: Path | None) -> dict[str, bytes]:
    folder = staging_dir(aid, exec_dir)
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*")) if p.is_file()} if folder.exists() else {}


# ------------------------------------------------------------------------------------------- intake and ranking
def intake(conn) -> dict[str, int]:
    """Bring every new assignment as far as the recorded facts allow: eligibility, then classification."""
    checked = classified = 0
    for r in conn.execute("SELECT id FROM assignments WHERE stage='discovered' AND eligibility_checked_at IS NULL AND hold IS NULL").fetchall():
        lifecycle.check_eligibility(conn, r["id"])
        checked += 1
    for r in conn.execute("""SELECT a.id FROM assignments a LEFT JOIN task_intel t ON t.assignment_id=a.id
                             WHERE t.assignment_id IS NULL AND a.stage IN ('discovered','qualified','ready','accepted')""").fetchall():
        lifecycle.classify(conn, r["id"])
        classified += 1
    return {"eligibility_checked": checked, "classified": classified, "expired": lifecycle.expire_due(conn)}


def ranked_queue(conn, limit: int = 50) -> list[dict[str, Any]]:
    """Executable work, best first. Only assignments that are ready (or already taken by you) and not held or claimed."""
    now = db.now_iso()
    rows = conn.execute("""SELECT a.id FROM assignments a JOIN task_intel t ON t.assignment_id=a.id
        WHERE a.stage IN ('ready','accepted') AND a.hold IS NULL AND a.execution_job_id IS NULL
          AND (a.claimed_by IS NULL OR a.claim_until < ?) AND t.automation_class IN (?,?,?)
        ORDER BY t.score DESC, a.id ASC LIMIT ?""", (now, *task_intel.EXECUTABLE, limit)).fetchall()
    return [lifecycle.view(conn, r["id"]) for r in rows]


def minutes_used_today(conn, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    day = now.date().isoformat()
    total = 0.0
    for r in conn.execute("SELECT started_at, finished_at, updated_at, status FROM execution_jobs WHERE started_at >= ?", (day,)):
        try:
            end = datetime.fromisoformat(r["finished_at"] or (r["updated_at"] if r["status"] != "running" else now.isoformat()))
            total += max(0.0, (end - datetime.fromisoformat(r["started_at"])).total_seconds() / 60)
        except (TypeError, ValueError):
            continue
    return total


def started_today(conn) -> int:
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return conn.execute("SELECT COUNT(*) FROM execution_jobs WHERE started_at >= ?", (day,)).fetchone()[0]


def running_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM execution_jobs WHERE status='running'").fetchone()[0]


def authorize(conn, a: dict[str, Any], settings: dict[str, Any] | None = None, probe: dict[str, float] | None = None) -> dict[str, Any]:
    """Every gate between a ready assignment and an automatic start. All reasons are returned, not just the first."""
    s = settings or control.settings(conn)
    intel = a.get("intel") or task_intel.load(conn, a["id"]) or {}
    reasons: list[str] = []
    ok, why = control.may_run(conn)
    if not ok:
        reasons.append(why)
    res = control.resources(conn, probe=probe)
    reasons += res["problems"]
    cls, category = intel.get("automation_class"), intel.get("category")
    if cls not in task_intel.EXECUTABLE:
        reasons.append(f"Classified {cls or 'not yet'}: not executable by the engine")
    if category not in s["permitted_categories"]:
        reasons.append(f"'{category}' is not on your permitted task categories")
    if category in s["excluded_task_types"]:
        reasons.append(f"'{category}' is on your excluded task types")
    eco = intel.get("economics") or {}
    value, currency = eco.get("advertised"), eco.get("currency")
    if value is not None:
        if currency != "USD":
            reasons.append(f"The reward is in {currency or 'an unstated currency'}: it cannot be compared with your USD limit, so it needs your decision")
        elif value > s["max_task_value_usd"]:
            reasons.append(f"The reward (USD {value}) is above your maximum task value (USD {s['max_task_value_usd']})")
    hourly = eco.get("hourly_comparable_usd")
    if s["min_hourly_rate_usd"] > 0 and (hourly is None or hourly < s["min_hourly_rate_usd"]):
        reasons.append(f"The expected hourly rate ({'unknown' if hourly is None else hourly}) is below your minimum (USD {s['min_hourly_rate_usd']})")
    if s.get("min_reward_usd", 0) > 0 and currency == "USD" and value is not None and value < s["min_reward_usd"]:
        reasons.append(f"The reward (USD {value}) is below your minimum reward (USD {s['min_reward_usd']})")
    if started_today(conn) >= s.get("max_daily_tasks", 10**6):
        reasons.append(f"{s['max_daily_tasks']} task(s) already started today (your daily task limit)")
    if minutes_used_today(conn) + float(eco.get("estimated_minutes") or 0) > s["max_daily_execution_minutes"]:
        reasons.append("Today's execution time limit would be exceeded")
    if running_count(conn) >= s["max_concurrency"]:
        reasons.append(f"{s['max_concurrency']} job(s) are already running (your concurrency limit)")
    needs_ai = cls != "DETERMINISTIC_LOCAL_PROCESSING"
    if needs_ai and not s["ai_runs_preauthorized"]:
        reasons.append("AI-based runs are not pre-authorized in your settings")
    decision = permissions.decide(conn, a["platform_id"], "task_execution", task={"title": a["title"], "instructions": a["instructions"], "ai_use": a["ai_use"], "needs_ai": needs_ai}, settings=s)
    reasons += [r for r in decision["reasons"] if r not in reasons]
    if a["stage"] == "ready" and a["platform_id"] != permissions.DIRECT_LANE:
        reasons.append("The task has not been taken on the platform yet: take it yourself, then record it")
    return {"allowed": not reasons, "reasons": reasons, "permission": decision, "resources": res}


def claim(conn, aid: int, who: str | None = None, ttl_seconds: int = 3600) -> bool:
    """Atomic claim. Returns False when another worker holds a live claim, so no assignment is ever executed twice."""
    now = datetime.now(timezone.utc)
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = conn.execute("""UPDATE assignments SET claimed_by=?, claim_until=? WHERE id=? AND execution_job_id IS NULL AND hold IS NULL
                            AND (claimed_by IS NULL OR claim_until < ?)""",
                         (who or owner(), (now + timedelta(seconds=ttl_seconds)).isoformat(), aid, now.isoformat())).rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n == 1


def release(conn, aid: int) -> None:
    with db.tx(conn):
        conn.execute("UPDATE assignments SET claimed_by=NULL, claim_until=NULL WHERE id=?", (aid,))


# ------------------------------------------------------------------------------------------- dispatch
def dispatch(conn, aid: int, runner: Any = None, db_path: Path | str | None = None, exec_dir: Path | None = None,
             settings: dict[str, Any] | None = None, probe: dict[str, float] | None = None) -> dict[str, Any]:
    """Authorize, claim and run one assignment to its QA verdict (synchronously). Returns the outcome with every reason."""
    s = settings or control.settings(conn)
    a = lifecycle.view(conn, aid)
    auth = authorize(conn, a, s, probe)
    if not auth["allowed"]:
        return {"assignment_id": aid, "started": False, "reasons": auth["reasons"]}
    if not claim(conn, aid, ttl_seconds=int(s["job_timeout_seconds"]) + 600):
        return {"assignment_id": aid, "started": False, "reasons": ["Another worker has already claimed this assignment"]}
    try:
        if a["stage"] == "ready":                               # direct-client lane only (authorize() refuses platforms here)
            asg.advance(conn, aid, "accepted", "user_confirmation", "direct_client:added_by_you", "Your own client's task, added by you", True, actor="user")
        cls = a["intel"]["automation_class"]
        work_type = task_intel.WORKER_FOR.get(a["intel"]["category"])
        a = asg.start_execution(conn, aid, work_type, exec_dir, _staged(aid, exec_dir))
        job_id = a["execution_job_id"]
        os.environ["AUTOPILOT_EXEC_TIMEOUT"] = str(int(s["job_timeout_seconds"]))
        with db.tx(conn):
            ex.event(conn, job_id, "authorization", f"Started by the orchestrator under your standing authorization ({AUTHORIZATION_REF}); class {cls}")
        ex.start(conn, job_id, approve_ai_run=bool(s["ai_runs_preauthorized"]) and cls != "DETERMINISTIC_LOCAL_PROCESSING", runner=runner,
                 db_path=db_path or db.DB_PATH, background=False)
        return finish(conn, aid, s)
    except (ex.ExecutionError, asg.AssignmentError, workers.WorkerError) as exc:
        lifecycle.set_hold(conn, aid, "failed", str(exc)[:300], f"orchestrator:{aid}")
        return {"assignment_id": aid, "started": False, "reasons": [str(exc)]}
    finally:
        release(conn, aid)


def finish(conn, aid: int, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """After a job stops: sync the assignment from the job's real state, compute the QA verdict, and package clean work if you allowed that."""
    s = settings or control.settings(conn)
    a = asg.sync_from_job(conn, aid)
    job = ex.get_job(conn, a["execution_job_id"])
    out: dict[str, Any] = {"assignment_id": aid, "job_id": job["id"], "started": True, "job_status": job["status"], "reasons": []}
    if job["status"] in ("awaiting_review", "qa_failed", "ready_for_submission"):
        report = qa_gate.verdict(conn, job["id"])
        out["qa"] = report["status"]
        if job["status"] == "awaiting_review" and report["status"] == qa_gate.PASS and s["auto_approve_clean_qa"]:
            ex.approve_under_authorization(conn, job["id"], AUTHORIZATION_REF)
            qa_gate.verdict(conn, job["id"])                         # re-run so the report also covers the package
            out["job_status"] = "ready_for_submission"
        elif report["status"] == qa_gate.MANUAL:
            out["reasons"].append("QA needs your own review: " + report["summary"])
        elif report["status"] == qa_gate.FAIL:
            if job["attempts"] >= MAX_ATTEMPTS:
                lifecycle.set_hold(conn, aid, "failed", f"QA failed after {job['attempts']} attempts: {report['summary']}"[:300], f"execution_job:{job['id']}")
            out["reasons"].append(report["summary"])
    elif job["status"] == "failed":
        lifecycle.set_hold(conn, aid, "failed", (job.get("status_reason") or "The worker failed")[:300], f"execution_job:{job['id']}")
        out["reasons"].append(job.get("status_reason") or "The worker failed")
    elif job["status"] == "paused":
        lifecycle.set_hold(conn, aid, "paused", "The job was paused", f"execution_job:{job['id']}")
    asg.sync_from_job(conn, aid)
    out["state"] = lifecycle.current(conn, aid)
    return out


def recover(conn) -> dict[str, int]:
    """After a restart: running jobs with no live worker become INTERRUPTED (resumable), and dead claims are released."""
    interrupted = ex.recover_interrupted(conn)
    marked = 0
    for r in conn.execute("""SELECT a.id, j.id AS job FROM assignments a JOIN execution_jobs j ON j.id=a.execution_job_id
                             WHERE j.status='interrupted' AND a.hold IS NULL AND a.stage IN ('accepted','executing','qa')""").fetchall():
        lifecycle.set_hold(conn, r["id"], "interrupted", "The program stopped while this job was running; resume to continue", f"execution_job:{r['job']}")
        marked += 1
    with db.tx(conn):
        released = conn.execute("UPDATE assignments SET claimed_by=NULL, claim_until=NULL WHERE claimed_by IS NOT NULL AND claim_until < ?", (db.now_iso(),)).rowcount
    return {"jobs_interrupted": interrupted, "assignments_marked": marked, "claims_released": released}


def tick(conn, runner: Any = None, db_path: Path | str | None = None, exec_dir: Path | None = None, background: bool = False,
         probe: dict[str, float] | None = None) -> dict[str, Any]:
    """One orchestrator pass. With ``background`` each dispatch runs in its own thread, bounded by your concurrency limit."""
    result: dict[str, Any] = {"intake": intake(conn), "recovery": recover(conn), "started": [], "skipped": []}
    ok, why = control.may_run(conn)
    if not ok:
        result["blocked"] = why
        return result
    s = control.settings(conn)
    if not s["auto_execute"]:
        result["blocked"] = "Automatic execution is switched off in your automation settings"
        return result
    for a in ranked_queue(conn):
        if running_count(conn) + sum(1 for t in _threads.values() if t.is_alive()) >= s["max_concurrency"]:
            break
        if background:
            auth = authorize(conn, a, s, probe)
            if not auth["allowed"]:
                result["skipped"].append({"assignment_id": a["id"], "reasons": auth["reasons"]})
                continue
            t = threading.Thread(target=_dispatch_thread, args=(str(db_path or db.DB_PATH), a["id"], runner, exec_dir), daemon=True, name=f"dispatch-{a['id']}")
            _threads[a["id"]] = t
            t.start()
            result["started"].append({"assignment_id": a["id"], "background": True})
        else:
            outcome = dispatch(conn, a["id"], runner, db_path, exec_dir, s, probe)
            (result["started"] if outcome["started"] else result["skipped"]).append(outcome)
    return result


def _dispatch_thread(path: str, aid: int, runner: Any, exec_dir: Path | None) -> None:
    try:
        with db.session(path) as conn:
            dispatch(conn, aid, runner, path, exec_dir)
    except Exception as exc:                                    # noqa: BLE001 - last-resort safety net; recorded on the assignment
        with db.session(path) as conn:
            try:
                lifecycle.set_hold(conn, aid, "failed", f"Unexpected error: {exc}"[:300], f"orchestrator:{aid}")
            except lifecycle.LifecycleError:
                pass


def overview(conn) -> dict[str, Any]:
    """What the Execution Centre shows about the queue: each ready item with the exact reasons it will or will not start."""
    s = control.settings(conn)
    queue = [{"assignment_id": a["id"], "title": a["title"], "platform_id": a["platform_id"], "state": a["state"], "score": a["intel"]["score"],
              "category": a["intel"]["category"], "automation_class": a["intel"]["automation_class"], **{k: v for k, v in authorize(conn, a, s).items() if k in ("allowed", "reasons")}}
             for a in ranked_queue(conn, 25)]
    return {"queue": queue, "running": running_count(conn), "max_concurrency": s["max_concurrency"], "minutes_used_today": round(minutes_used_today(conn), 1),
            "max_daily_execution_minutes": s["max_daily_execution_minutes"], "active_dispatch_threads": sum(1 for t in _threads.values() if t.is_alive()),
            "auto_execute": s["auto_execute"]}
