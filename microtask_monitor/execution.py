"""Execution engine: turn genuine, user-supplied work instructions into verified deliverables.

What this module does: classifies the work, enforces AI-use permission, creates an isolated workspace, runs a specialist
worker, runs independent QA, packages the result and tracks review, submission and outcome. State lives in SQLite
(``execution_jobs`` / ``execution_events`` / ``execution_files`` / ``execution_qa``); work products live in local files.

What it never does: contact a platform, accept work, submit anything, sign anything or move money. Instructions are only
ever provided by YOU (pasted or uploaded from your own account); submission is always recorded by you after you do it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from microtask_monitor import db, policy, workflow as wf
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import db, policy, workflow as wf
    from secret_store import redact

ROOT = Path(__file__).resolve().parents[1]
EXEC_DIR = ROOT / "data" / "executions"
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_BYTES = 200 * 1024
WORK_TYPES = ("software", "writing", "data", "evaluation", "content", "automation", "unknown")
ACTIVE = ("running",)
TERMINAL = ("submitted", "accepted", "rejected", "cancelled")


class ExecutionError(ValueError):
    pass


# ------------------------------------------------------------------------------------------- classification
_SIGNALS = {
    "software": r"\b(python|javascript|typescript|node\.?js|react|django|flask|fastapi|api|backend|frontend|full-?stack|function|script|unit tests?|refactor|bug|code|sql|html|css)\b",
    "writing": r"\b(article|blog|essay|write|rewrite|edit|proofread|copywrit|research report|summary|summarise|summarize|translate|words?)\b",
    "data": r"\b(csv|spreadsheet|excel|xlsx|clean|dedup|categori[sz]e|categori[sz]ation|label|tag|annotat|classif|transcrib|data entry|records?|rows?|columns?)\b",
    "evaluation": r"\b(evaluate|evaluation|rank|compare|rate|rating|rubric|grade|model responses?|chatbot|llm|preference)\b",
    "content": r"\b(video script|script|caption|subtitle|thumbnail|social media|reel|tiktok|voice-?over|storyboard|youtube)\b",
    "automation": r"\b(n8n|make\.com|zapier|workflow|webhook|integration|automation|automate)\b",
}
_AI_PROHIBITED = re.compile(
    r"(do not|don't|must not|never|no|without|not (?:allowed|permitted) to|prohibited from|forbidden to)\W+(?:\w+\W+){0,4}?"
    r"(?:ai|a\.i\.|chatgpt|gpt|llms?|large language models?|generative|copilot|claude|gemini|ai[- ](?:tools?|assist\w*|generated))\b|"
    r"\b(?:ai|chatgpt|llms?|generative ai)(?:[- ]generated)?\W+(?:\w+\W+){0,4}?(?:is|are)? ?(?:not allowed|not permitted|prohibited|forbidden|banned)\b|"
    r"\b(?:human[- ]written|written (?:entirely )?by (?:a )?human|your own (?:original )?work|no ai)\b", re.I)
_AI_ALLOWED_HINT = re.compile(r"\b(ai[- ]assist\w*|you may use ai|ai tools? (?:are|is) (?:allowed|permitted|welcome|encouraged)|using ai is (?:allowed|permitted))\b", re.I)


def classify(title: str, instructions: str, forced: str | None = None) -> dict[str, Any]:
    """Rule-based work-type classification from the real instruction text. Never guesses: low evidence gives ``unknown``."""
    if forced:
        if forced not in WORK_TYPES:
            raise ExecutionError(f"Unknown work type '{forced}'")
        return {"work_type": forced, "scores": {}, "reasons": ["chosen by you"], "confidence": "user"}
    text = f"{title}\n{instructions}".lower()
    scores = {k: len(re.findall(rx, text, re.I)) for k, rx in _SIGNALS.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    if top[1] < 2 or top[1] == second[1]:
        return {"work_type": "unknown", "scores": scores, "confidence": "low",
                "reasons": ["The instructions do not clearly point to one kind of work: choose the work type yourself."]}
    # Annotation/labeling is data work; "write code" beats generic writing words when both appear.
    return {"work_type": top[0], "scores": scores, "confidence": "high" if top[1] >= 2 * max(second[1], 1) else "medium",
            "reasons": [f"{top[1]} instruction terms match {top[0]} work"]}


def detect_ai_rules(text: str) -> dict[str, Any]:
    """Look for explicit AI-use rules in the instructions. Absence of a rule is NOT permission."""
    prohibited = _AI_PROHIBITED.search(text or "")
    allowed = _AI_ALLOWED_HINT.search(text or "")
    if prohibited:
        return {"status": "prohibited", "evidence": prohibited.group(0)[:160]}
    if allowed:
        return {"status": "hint_allowed", "evidence": allowed.group(0)[:160]}
    return {"status": "not_stated", "evidence": ""}


# ------------------------------------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job(conn, job_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise ExecutionError("Execution job not found")
    return dict(row)


def event(conn, job_id: int, kind: str, message: str) -> None:
    conn.execute("INSERT INTO execution_events(job_id,at,kind,message) VALUES(?,?,?,?)", (job_id, _now(), kind, redact(message)[:2000]))


def _set(conn, job_id: int, status: str | None = None, reason: str | None = None, **fields: Any) -> None:
    cols, vals = ["updated_at=?"], [_now()]
    if status:
        cols.append("status=?"), vals.append(status)
        cols.append("status_reason=?"), vals.append(reason)
    for k, v in fields.items():
        cols.append(f"{k}=?"), vals.append(v)
    conn.execute(f"UPDATE execution_jobs SET {', '.join(cols)} WHERE id=?", (*vals, job_id))


def workspace_paths(job: dict[str, Any]) -> dict[str, Path]:
    base = Path(job["workspace"])
    return {"base": base, "input": base / "input", "output": base / "output", "logs": base / "logs", "package": base / "package"}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def register_files(conn, job_id: int, role: str, folder: Path, base: Path) -> int:
    n = 0
    for p in sorted(folder.rglob("*")) if folder.exists() else []:
        if p.is_file():
            rel = p.relative_to(base).as_posix()
            conn.execute("""INSERT INTO execution_files(job_id,role,rel_path,sha256,size_bytes,created_at) VALUES(?,?,?,?,?,?)
                            ON CONFLICT(job_id,role,rel_path) DO UPDATE SET sha256=excluded.sha256, size_bytes=excluded.size_bytes""",
                         (job_id, role, rel, _sha(p), p.stat().st_size, _now()))
            n += 1
    return n


# ------------------------------------------------------------------------------------------- create / permissions
def create_job(conn, title: str, instructions: str, platform_id: str | None = None, application_id: int | None = None,
               deadline: str | None = None, work_type: str | None = None, ai_policy: str | None = None, ai_source: str = "",
               inputs: dict[str, bytes] | None = None, exec_dir: Path | None = None, instructions_source: str = "user_provided") -> dict[str, Any]:
    """Create a job from instructions YOU supplied. A linked task workspace is created too so evidence/submission stay unified."""
    if not (title or "").strip():
        raise ExecutionError("A job needs a title")
    if len((instructions or "").strip()) < 20:
        raise ExecutionError("Paste the real task instructions (at least a couple of sentences). Nothing is invented for you.")
    if instructions_source not in ("user_provided", "user_file"):
        raise ExecutionError("Instructions must come from you")
    if platform_id:
        if platform_id not in policy.load_capabilities():
            raise ExecutionError(f"No capability record for '{platform_id}' (fail closed): choose a tracked platform")
        policy.require(platform_id, "assist_task")
    cls = classify(title, instructions, work_type)
    from microtask_monitor import workers
    worker = workers.select(cls["work_type"])
    rules = detect_ai_rules(instructions)
    policy_value, reason = "unknown", None
    if rules["status"] == "prohibited":
        policy_value, reason = "prohibited", f"The instructions appear to prohibit AI help: \"{rules['evidence']}\""
    elif ai_policy == "allowed":
        if not (ai_source or "").strip():
            raise ExecutionError("To allow AI help, state where the platform/client rules say so (e.g. the project instructions page)")
        policy_value = "allowed"
    elif ai_policy == "prohibited":
        policy_value = "prohibited"
    base = (exec_dir or EXEC_DIR)
    now = _now()
    task = wf.create_task(conn, title, instructions, application_id, None, platform_id, deadline)
    with db.tx(conn):
        cur = conn.execute("""INSERT INTO execution_jobs(task_id,application_id,platform_id,opportunity_url,title,instructions,instructions_source,work_type,worker,
            classification_json,status,ai_policy,ai_source,ai_detected,workspace,deadline,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?, 'created', ?,?,?, '', ?,?,?)""",
                           (task["id"], application_id, platform_id, task.get("opportunity_url"), title.strip()[:200], instructions[:50000], instructions_source,
                            cls["work_type"], worker.name, json.dumps(cls), policy_value, (ai_source or "")[:500], json.dumps(rules), deadline, now, now))
        jid = cur.lastrowid
        ws = Path(base) / f"job-{jid}"
        for sub in ("input", "output", "logs", "package"):
            (ws / sub).mkdir(parents=True, exist_ok=True)
        conn.execute("UPDATE execution_jobs SET workspace=? WHERE id=?", (str(ws), jid))
        (ws / "INSTRUCTIONS.md").write_text(f"# {title.strip()}\n\n(Provided by the user; source: {instructions_source})\n\n{instructions}\n", encoding="utf-8")
        for name, content in (inputs or {}).items():
            if len(content) > MAX_INPUT_BYTES:
                raise ExecutionError(f"Input file {name} is larger than 10 MB")
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "input"
            (ws / "input" / safe[:100]).write_bytes(content)
        register_files(conn, jid, "input", ws / "input", ws)
        event(conn, jid, "created", f"Job created for {cls['work_type']} work ({cls['confidence']} confidence); worker: {worker.name}")
        db.audit(conn, "execution.create", "execution_job", jid, {"work_type": cls["work_type"], "platform": platform_id})
    return refresh_state(conn, jid)


def refresh_state(conn, job_id: int) -> dict[str, Any]:
    """Compute whether the job can run (permission/inputs) and set created/blocked/ready. Only touches pre-run states."""
    job = _job(conn, job_id)
    if job["status"] not in ("created", "blocked", "ready"):
        return get_job(conn, job_id)
    from microtask_monitor import workers
    worker = workers.select(job["work_type"])
    problem = worker.preflight(job, workspace_paths(job))
    if problem:
        status, reason = "blocked", problem
    else:
        status, reason = "ready", "Ready to run. Starting still needs your go-ahead."
    if status != job["status"] or reason != job["status_reason"]:
        with db.tx(conn):
            _set(conn, job_id, status, reason)
            event(conn, job_id, "state", f"{status}: {reason}")
    return get_job(conn, job_id)


def set_ai_permission(conn, job_id: int, status: str, source: str = "") -> dict[str, Any]:
    job = _job(conn, job_id)
    if status not in ("allowed", "prohibited", "unknown"):
        raise ExecutionError("AI policy must be allowed, prohibited or unknown")
    detected = json.loads(job["ai_detected"] or "{}")
    if status == "allowed":
        if detected.get("status") == "prohibited":
            raise ExecutionError(f"The instructions appear to prohibit AI help (\"{detected.get('evidence')}\"). Resolve that with the client first; AI cannot be enabled here.")
        if not (source or "").strip():
            raise ExecutionError("State where the platform/client rules permit AI help (a page name or quote)")
    if job["status"] in ("running", *TERMINAL):
        raise ExecutionError(f"Cannot change AI permission while the job is {job['status']}")
    with db.tx(conn):
        _set(conn, job_id, ai_policy=status, ai_source=(source or "")[:500], ai_run_approved_at=None)
        event(conn, job_id, "ai_permission", f"AI help set to {status}" + (f" (source: {source[:120]})" if source else ""))
        db.audit(conn, "execution.ai_permission", "execution_job", job_id, {"status": status})
    return refresh_state(conn, job_id)


# ------------------------------------------------------------------------------------------- running
_threads: dict[int, threading.Thread] = {}


STALE_SECONDS = 180          # a job run by ANOTHER process is presumed dead after this long without a heartbeat
HEARTBEAT_SECONDS = 20


def _owner() -> str:
    return f"pid:{os.getpid()}"


def should_stop_factory(db_path: Path | str, job_id: int) -> Callable[[], str | None]:
    last = {"beat": 0.0}

    def check() -> str | None:
        with db.session(db_path) as c:
            row = c.execute("SELECT pause_requested, cancel_requested FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
            if time.monotonic() - last["beat"] > HEARTBEAT_SECONDS:            # heartbeat so a restart can tell live from dead jobs
                last["beat"] = time.monotonic()
                c.execute("UPDATE execution_jobs SET updated_at=? WHERE id=? AND status='running'", (_now(), job_id))
        if row and row["cancel_requested"]:
            return "cancel"
        if row and row["pause_requested"]:
            return "pause"
        return None
    return check


def start(conn, job_id: int, approve_ai_run: bool = False, runner: Any = None, db_path: Path | str | None = None,
          background: bool = False) -> dict[str, Any]:
    """Run (or resume) the job. AI-based work needs allowed AI policy AND your explicit approval of this run."""
    from microtask_monitor import workers
    job = _job(conn, job_id)
    if job["status"] not in ("ready", "paused", "interrupted", "qa_failed", "blocked", "created"):
        raise ExecutionError(f"A {job['status']} job cannot be started")
    job = refresh_state(conn, job_id)
    if job["status"] == "blocked":
        raise ExecutionError(job["status_reason"] or "The job is blocked")
    worker = workers.select(job["work_type"])
    needs_ai = worker.requires_ai(job, workspace_paths(job))
    if needs_ai:
        if job["ai_policy"] != "allowed":
            raise ExecutionError("AI help is not confirmed as permitted for this work. Check the platform/client rules, then record the permission with its source.")
        if approve_ai_run is not True:
            raise ExecutionError("Confirm the run (approve_ai_run=true): it starts an AI coding/writing tool on your computer using your own subscription.")
    path = db_path or db.DB_PATH
    with db.tx(conn):
        _set(conn, job_id, "running", "Working", runner=_owner(), attempts=job["attempts"] + 1, started_at=job["started_at"] or _now(), pause_requested=0, cancel_requested=0,
             ai_run_approved_at=_now() if needs_ai else None, error=None)
        event(conn, job_id, "start", f"Attempt {job['attempts'] + 1} started" + (" with your approval for the AI run" if needs_ai else " (no AI involved)"))
    if background:
        t = threading.Thread(target=_run_in_thread, args=(str(path), job_id, runner), daemon=True, name=f"exec-{job_id}")
        _threads[job_id] = t
        t.start()
        return get_job(conn, job_id)
    run_pipeline(conn, job_id, runner, path)
    return get_job(conn, job_id)


def _run_in_thread(path: str, job_id: int, runner: Any) -> None:
    try:
        with db.session(path) as conn:
            run_pipeline(conn, job_id, runner, path)
    except Exception as exc:                                    # pragma: no cover - last-resort safety net
        with db.session(path) as conn:
            with db.tx(conn):
                _set(conn, job_id, "failed", "Unexpected error", error=str(exc)[:500])
                event(conn, job_id, "error", f"Unexpected error: {exc}")


def run_pipeline(conn, job_id: int, runner: Any, db_path: Path | str) -> None:
    """worker -> independent QA -> awaiting_review / qa_failed. Every step is logged; failures leave the job recoverable."""
    from microtask_monitor import qa, workers
    job = _job(conn, job_id)
    worker = workers.select(job["work_type"])
    paths = workspace_paths(job)
    stop = should_stop_factory(db_path, job_id)

    def log(msg: str) -> None:
        with db.tx(conn):
            event(conn, job_id, "progress", msg)

    try:
        outcome = worker.run(job, paths, runner=runner, log=log, should_stop=stop)
    except workers.Stopped as exc:
        with db.tx(conn):
            if str(exc) == "cancel":
                _set(conn, job_id, "cancelled", "Cancelled by you", finished_at=_now())
            else:
                _set(conn, job_id, "paused", "Paused by you; resume to continue", pause_requested=0)
            event(conn, job_id, "state", f"Stopped: {exc}")
        return
    except workers.WorkerError as exc:
        with db.tx(conn):
            _set(conn, job_id, "failed", str(exc)[:300], error=str(exc)[:1000])
            event(conn, job_id, "error", str(exc))
        return
    with db.tx(conn):
        register_files(conn, job_id, "output", paths["output"], paths["base"])
        register_files(conn, job_id, "log", paths["logs"], paths["base"])
        event(conn, job_id, "worker_done", outcome.get("summary", "Worker finished"))
        _set(conn, job_id, "running", "Running quality checks")
    results = qa.run_qa(_job(conn, job_id), paths, log)
    attempt = _job(conn, job_id)["attempts"]
    with db.tx(conn):
        conn.execute("DELETE FROM execution_qa WHERE job_id=? AND attempt=?", (job_id, attempt))
        for r in results:
            conn.execute("INSERT INTO execution_qa(job_id,attempt,check_name,status,executed,detail,created_at) VALUES(?,?,?,?,?,?,?)",
                         (job_id, attempt, r["name"], r["status"], 1 if r["executed"] else 0, r["detail"][:3000], _now()))
        failed = [r for r in results if r["status"] == "fail"]
        manual = [r for r in results if r["status"] == "manual"]
        feedback = workspace_paths(_job(conn, job_id))["logs"] / "qa_feedback.txt"
        if failed:
            feedback.write_text(chr(10).join(f"- {r['name']}: {r['detail']}" for r in failed), encoding="utf-8")
        elif feedback.exists():
            feedback.unlink()
        if failed:
            _set(conn, job_id, "qa_failed", f"{len(failed)} quality check(s) failed: " + "; ".join(r["name"] for r in failed)[:300])
            event(conn, job_id, "qa", f"QA failed: {', '.join(r['name'] for r in failed)}")
        else:
            _set(conn, job_id, "awaiting_review", f"QA passed ({len(results) - len(manual)} checks)" + (f"; {len(manual)} item(s) need your own review" if manual else ""), finished_at=_now())
            event(conn, job_id, "qa", "QA passed; waiting for your review")


def pause(conn, job_id: int) -> dict[str, Any]:
    job = _job(conn, job_id)
    if job["status"] != "running":
        raise ExecutionError("Only a running job can be paused")
    with db.tx(conn):
        _set(conn, job_id, pause_requested=1)
        event(conn, job_id, "control", "Pause requested")
    return get_job(conn, job_id)


def cancel(conn, job_id: int) -> dict[str, Any]:
    job = _job(conn, job_id)
    if job["status"] in TERMINAL or job["status"] == "cancelled":
        raise ExecutionError(f"A {job['status']} job cannot be cancelled")
    with db.tx(conn):
        if job["status"] == "running":
            _set(conn, job_id, cancel_requested=1)
        else:
            _set(conn, job_id, "cancelled", "Cancelled by you", finished_at=_now())
        event(conn, job_id, "control", "Cancel requested")
    return get_job(conn, job_id)


def recover_interrupted(conn) -> int:
    """After a restart no worker thread survives: mark such jobs interrupted so they can be resumed (never silently re-run)."""
    rows = conn.execute("SELECT id, runner, updated_at FROM execution_jobs WHERE status='running'").fetchall()
    marked = 0
    for r in rows:
        if r["id"] in _threads and _threads[r["id"]].is_alive():
            continue
        if r["runner"] and r["runner"] != _owner():                          # owned by another process: only if its heartbeat stopped
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(r["updated_at"])).total_seconds()
            except (TypeError, ValueError):
                age = STALE_SECONDS + 1
            if age <= STALE_SECONDS:
                continue
        marked += 1
        with db.tx(conn):
            _set(conn, r["id"], "interrupted", "The program stopped while this job was running; resume to continue")
            event(conn, r["id"], "recovery", "Marked interrupted after a restart; resume continues from the saved workspace")
    return marked


# ------------------------------------------------------------------------------------------- review, packaging, submission, outcome
def approve_review(conn, job_id: int, approved_by_user: bool, acknowledged_manual: bool = False) -> dict[str, Any]:
    job = _job(conn, job_id)
    if approved_by_user is not True:
        raise ExecutionError("Approval must come from you (approved_by_user=true)")
    if job["status"] != "awaiting_review":
        raise ExecutionError(f"Only work that passed QA and awaits review can be approved (status: {job['status']})")
    attempt = job["attempts"]
    latest = db.rows(conn.execute("SELECT * FROM execution_qa WHERE job_id=? AND attempt=?", (job_id, attempt)))
    if not latest or any(r["status"] == "fail" for r in latest):
        raise ExecutionError("Cannot approve: there is no passing QA result for the latest attempt")
    if any(r["status"] == "manual" for r in latest) and acknowledged_manual is not True:
        raise ExecutionError("Some checks need your own review. Read them, then approve with acknowledged_manual=true")
    with db.tx(conn):
        _set(conn, job_id, "approved", "Approved by you", approved_at=_now(), manual_ack_at=_now() if acknowledged_manual else None)
        event(conn, job_id, "approved", "You approved the deliverables")
        db.audit(conn, "execution.approve", "execution_job", job_id)
    return prepare_submission(conn, job_id)


def prepare_submission(conn, job_id: int) -> dict[str, Any]:
    """Zip the approved deliverables with a QA report and a submission guide. Nothing is sent anywhere."""
    job = _job(conn, job_id)
    if job["status"] not in ("approved", "ready_for_submission"):
        raise ExecutionError("Only approved work can be packaged")
    paths = workspace_paths(job)
    outputs = [p for p in sorted(paths["output"].rglob("*")) if p.is_file()]
    if not outputs:
        raise ExecutionError("There are no deliverables to package")
    qa_rows = db.rows(conn.execute("SELECT * FROM execution_qa WHERE job_id=? AND attempt=? ORDER BY id", (job_id, job["attempts"])))
    report = [f"QA REPORT - {job['title']}", f"Attempt {job['attempts']}", ""] + [f"[{r['status'].upper()}] {r['check_name']} ({'executed' if r['executed'] else 'not executed'}): {r['detail']}" for r in qa_rows]
    cap = policy.load_capabilities().get(job["platform_id"] or "", {})
    guide = [f"SUBMISSION GUIDE - {job['title']}", "",
             f"Platform: {cap.get('name') or job['platform_id'] or 'not specified'}",
             "Submission on this platform is done by YOU in your own account. This program does not log in or upload anything.", ""]
    for i, step in enumerate((cap.get("execution") or {}).get("manual_steps") or ["Open the task in your own account.", "Upload or paste the deliverables from this package.", "Confirm the platform's own checklist, then submit yourself."], 1):
        guide.append(f"{i}. {step}")
    guide += ["", "After you submit: record the date and reference in the Execution Centre. Acceptance and payment are tracked separately."]
    (paths["package"]).mkdir(parents=True, exist_ok=True)
    (paths["package"] / "QA_REPORT.txt").write_text("\n".join(report), encoding="utf-8")
    (paths["package"] / "SUBMISSION_GUIDE.txt").write_text("\n".join(guide), encoding="utf-8")
    manifest = {"job_id": job_id, "attempt": job["attempts"], "generated_at": _now(),
                "files": [{"path": p.relative_to(paths["output"]).as_posix(), "sha256": _sha(p), "bytes": p.stat().st_size} for p in outputs]}
    (paths["package"] / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = paths["package"] / f"deliverables-job-{job_id}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in outputs:
            z.write(p, "deliverables/" + p.relative_to(paths["output"]).as_posix())
        for name in ("QA_REPORT.txt", "SUBMISSION_GUIDE.txt", "MANIFEST.json"):
            z.write(paths["package"] / name, name)
    with db.tx(conn):
        register_files(conn, job_id, "package", paths["package"], paths["base"])
        _set(conn, job_id, "ready_for_submission", "Packaged. Submit it yourself, then record it here.")
        event(conn, job_id, "packaged", f"Deliverables packaged: {archive.name}")
    return get_job(conn, job_id)


def record_submitted(conn, job_id: int, confirmed_by_user: bool, reference: str = "", submitted_at: str | None = None) -> dict[str, Any]:
    job = _job(conn, job_id)
    if confirmed_by_user is not True:
        raise ExecutionError("Confirm that you submitted this work yourself (confirmed_by_user=true)")
    if job["status"] != "ready_for_submission":
        raise ExecutionError("Only packaged work that is ready for submission can be recorded as submitted")
    if job["task_id"]:
        wf.record_submission(conn, job["task_id"], True, reference, f"Execution job {job_id}", submitted_at)
    with db.tx(conn):
        _set(conn, job_id, "submitted", "Submitted by you", submitted_at=submitted_at or _now()[:10], submission_reference=(reference or "")[:200])
        event(conn, job_id, "submitted", "You recorded that you submitted the work" + (f" (reference {reference[:60]})" if reference else ""))
    return get_job(conn, job_id)


def record_outcome(conn, job_id: int, outcome: str) -> dict[str, Any]:
    job = _job(conn, job_id)
    if outcome not in ("accepted", "rejected"):
        raise ExecutionError("Outcome must be accepted or rejected")
    if job["status"] != "submitted":
        raise ExecutionError("Only submitted work can be marked accepted or rejected")
    with db.tx(conn):
        _set(conn, job_id, outcome, f"Recorded by you: {outcome}", outcome_at=_now())
        event(conn, job_id, "outcome", f"Platform outcome recorded by you: {outcome}")
    if job["task_id"]:
        try:
            wf.set_task_status(conn, job["task_id"], "approved" if outcome == "accepted" else "rejected")
        except wf.WorkflowError:
            pass
    return get_job(conn, job_id)


# ------------------------------------------------------------------------------------------- reads
def get_job(conn, job_id: int) -> dict[str, Any]:
    job = _job(conn, job_id)
    job["classification"] = json.loads(job.pop("classification_json") or "{}")
    job["ai_detected"] = json.loads(job["ai_detected"] or "{}")
    job["events"] = db.rows(conn.execute("SELECT at,kind,message FROM execution_events WHERE job_id=? ORDER BY id DESC LIMIT 200", (job_id,)))
    job["files"] = db.rows(conn.execute("SELECT id,role,rel_path,sha256,size_bytes FROM execution_files WHERE job_id=? ORDER BY role, rel_path", (job_id,)))
    job["qa"] = db.rows(conn.execute("SELECT check_name,status,executed,detail,attempt FROM execution_qa WHERE job_id=? AND attempt=(SELECT MAX(attempt) FROM execution_qa WHERE job_id=?) ORDER BY id", (job_id, job_id)))
    job["needs_manual_ack"] = any(q["status"] == "manual" for q in job["qa"])
    job["next_action"] = next_action(job)
    return job


def next_action(job: dict[str, Any]) -> str:
    s = job["status"]
    return {
        "created": "Check the work type and AI permission.",
        "blocked": job.get("status_reason") or "Resolve the blocker.",
        "ready": "Press Start (AI work needs your explicit approval of the run).",
        "running": "Wait, or pause/cancel.",
        "paused": "Resume when you are ready.",
        "interrupted": "Resume: the saved workspace is kept.",
        "qa_failed": "Read the failed checks, then resume to let the worker fix them or cancel.",
        "awaiting_review": "Inspect the files and QA results, then approve (or cancel).",
        "approved": "Packaging...",
        "ready_for_submission": "Submit the package yourself on the platform, then record it.",
        "submitted": "Wait for the platform's decision, then record accepted or rejected.",
        "accepted": "Record expected/received payment under Earnings when it is real.",
        "rejected": "Read any feedback; decide whether to retry or move on.",
        "failed": "Read the error in the log; resume or cancel.",
        "cancelled": "Nothing further.",
    }.get(s, "")


def list_jobs(conn) -> list[dict[str, Any]]:
    rows = db.rows(conn.execute("""SELECT id,title,platform_id,work_type,worker,status,status_reason,ai_policy,attempts,deadline,created_at,updated_at,
        (SELECT COUNT(*) FROM execution_qa q WHERE q.job_id=execution_jobs.id AND q.status='fail' AND q.attempt=execution_jobs.attempts) AS qa_failures
        FROM execution_jobs ORDER BY updated_at DESC"""))
    for r in rows:
        r["next_action"] = next_action(r)
    return rows


def summary(conn) -> dict[str, Any]:
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM execution_jobs GROUP BY status")}
    return {"by_status": counts, "total": sum(counts.values()), "awaiting_you": sum(counts.get(s, 0) for s in ("blocked", "ready", "awaiting_review", "ready_for_submission", "qa_failed", "interrupted", "paused")),
            "payments": wf.earnings_summary(conn)}


def read_file(conn, job_id: int, file_id: int) -> dict[str, Any]:
    job = _job(conn, job_id)
    row = conn.execute("SELECT * FROM execution_files WHERE id=? AND job_id=?", (file_id, job_id)).fetchone()
    if not row:
        raise ExecutionError("File not found")
    base = Path(job["workspace"]).resolve()
    target = (base / row["rel_path"]).resolve()
    if base not in target.parents or not target.is_file():          # never serve anything outside the job workspace
        raise ExecutionError("File not available")
    data = target.read_bytes()[:MAX_PREVIEW_BYTES]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"file": dict(row), "text": None, "note": "Binary file: open it from the folder shown below.", "path": str(target)}
    return {"file": dict(row), "text": text, "truncated": row["size_bytes"] > MAX_PREVIEW_BYTES, "path": str(target)}
