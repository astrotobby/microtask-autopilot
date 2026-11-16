"""Controlled submission, kept completely separate from execution.

Automatic submission happens only through a registered ``SubmissionAdapter`` for a platform whose record carries
explicit, dated evidence that automated submission is permitted (level 5), and only after all eleven pre-submission
checks pass. ``REGISTRY`` is empty: today no registered platform is verified for automated submission, so every
package is handed to you with the exact reasons, and you submit it yourself and record the confirmation.

Duplicate protection: the idempotency key is derived from the platform, the task and the package's SHA-256. An attempt
row is committed BEFORE anything is sent. A timeout or crash leaves the attempt 'unknown', and an unknown attempt is
never re-sent blindly: it must be reconciled (the platform's own lookup, or your confirmation with a reference).
A successful upload is recorded as SUBMITTED, never as accepted.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from microtask_monitor import assignments as asg, connections as cx, control, db, execution as ex, lifecycle, opportunity_common as oc, permissions, policy, qa_gate
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import assignments as asg, connections as cx, control, db, execution as ex, lifecycle, opportunity_common as oc, permissions, policy, qa_gate
    from secret_store import redact

STATUSES = ("pending", "confirmed", "unknown", "failed")
_SECRET_KEY = re.compile(r"pass(word|wd)?$|cookie|otp|token|api_?key|secret|session|credential|authorization", re.I)


def scrub(value: Any) -> Any:
    """A platform's answer is stored as evidence, but never any credential-like field it may contain."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _SECRET_KEY.search(str(k)) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value[:200]]
    return redact(value) if isinstance(value, str) else value
PENDING_STALE_SECONDS = 600


class SubmissionError(ValueError):
    pass


class SubmissionTimeout(RuntimeError):
    """The request may or may not have reached the platform. Never retried automatically."""


class SubmissionRejected(RuntimeError):
    """The platform answered definitively that nothing was recorded (for example a validation error)."""


class SubmissionAdapter:
    """One platform's documented submission interface. Only written for a platform verified at level 5."""
    platform_id = ""
    destination = ""                 # the documented https endpoint the package is sent to

    def submit(self, package: Path, manifest: dict[str, Any], assignment: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        """Send once. Return the platform's response containing its own ``reference``. Raise SubmissionTimeout / SubmissionRejected."""
        raise NotImplementedError

    # Optional: def lookup(self, idempotency_key, assignment) -> dict | None   (the platform's record of an earlier attempt)


REGISTRY: dict[str, SubmissionAdapter] = {}      # deliberately empty: see the module docstring


def _package(conn, a: dict[str, Any]) -> dict[str, Any]:
    job = conn.execute("SELECT * FROM execution_jobs WHERE id=?", (a["execution_job_id"] or 0,)).fetchone()
    if not job:
        return {"job": None}
    paths = ex.workspace_paths(dict(job))
    archive, manifest_path = paths["package"] / f"deliverables-job-{job['id']}.zip", paths["package"] / "MANIFEST.json"
    info: dict[str, Any] = {"job": dict(job), "archive": archive, "manifest": None, "sha256": None}
    if archive.exists() and manifest_path.exists():
        try:
            info["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
            info["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        except (OSError, ValueError):
            pass
    return info


def idempotency_key(platform_id: str, external_id: str, package_sha256: str) -> str:
    return hashlib.sha256(f"{platform_id}|{external_id}|{package_sha256}".encode()).hexdigest()


def preflight(conn, aid: int, adapter: SubmissionAdapter | None = None, settings: dict[str, Any] | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """The eleven checks. Every one is evaluated and reported; automatic submission needs all of them."""
    now = now or datetime.now(timezone.utc)
    s = settings or control.settings(conn)
    a = lifecycle.view(conn, aid)
    pid = a["platform_id"]
    adapter = adapter if adapter is not None else REGISTRY.get(pid)
    pkg = _package(conn, a)
    job, manifest = pkg["job"], pkg.get("manifest")
    prof = permissions.profile(pid) or {"actions": {}, "name": pid, "level": 0}
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("correct_task", bool(job and manifest and manifest.get("job_id") == job["id"] and a["stage"] == "ready_for_delivery"),
        "The package belongs to this assignment's job" if job and manifest and manifest.get("job_id") == job["id"] and a["stage"] == "ready_for_delivery"
        else "There is no approved package for this assignment yet")
    add("correct_platform", bool(adapter and adapter.platform_id == pid and pid in policy.load_capabilities()),
        f"A submission interface for {prof['name']} is registered" if adapter and adapter.platform_id == pid else f"No automated submission interface exists for {prof['name']}")
    conn_state = cx.status(conn, pid)["status"] if pid in policy.load_capabilities() else "not_applicable"
    add("correct_account", conn_state == "connected_verified",
        "Your account connection was verified by the platform's API" if conn_state == "connected_verified"
        else f"The account connection is '{conn_state}': only an API-verified connection can be submitted through")
    elig = lifecycle.eligibility(a)
    add("eligibility", elig["status"] == "eligible" or (a["stage"] != "discovered" and elig["status"] != "ineligible"), elig["basis"])
    exe = (prof["actions"].get("task_execution") or {"result": "unverified", "why": ""})
    add("automation_permission", exe["result"] == "permitted", f"Automated completion is {exe['result'].replace('_', ' ')} for {prof['name']}" + (f": {exe.get('why')}" if exe.get("why") else ""))
    ai_used = bool(job and job["ai_run_approved_at"])
    add("ai_use_permission", (a["ai_use"] == "allowed") if ai_used else a["ai_use"] in ("allowed", "prohibited"),
        "AI was used and the task's rule permits it" if ai_used and a["ai_use"] == "allowed" else ("No AI was used" if not ai_used and a["ai_use"] in ("allowed", "prohibited") else f"AI-use rule is '{a['ai_use']}'"))
    decision = permissions.decide(conn, pid, "auto_submission", task={"title": a["title"], "instructions": a["instructions"], "ai_use": a["ai_use"], "needs_ai": ai_used}, settings=s)
    category = (a.get("intel") or {}).get("category")
    human_kind = category in control.HUMAN_ONLY_CATEGORIES
    add("submission_permission", decision["allowed"] and not human_kind,
        "Automated submission is verified and pre-authorized by you" if decision["allowed"] and not human_kind
        else "; ".join(decision["reasons"] + ([f"'{category}' work is submitted by the person who did it"] if human_kind else [])))
    deadline = oc.parse_time(a["deadline"]) if a.get("deadline") else None
    add("deadline", not deadline or deadline > now, "No deadline" if not deadline else (f"Due {a['deadline']}" if deadline > now else f"The deadline ({a['deadline']}) has passed"))
    deliverables_ok, detail = False, "No package"
    if manifest and pkg["archive"].exists():
        with zipfile.ZipFile(pkg["archive"]) as z:
            names, bad = set(z.namelist()), z.testzip()
        missing = [f["path"] for f in manifest.get("files", []) if "deliverables/" + f["path"] not in names]
        deliverables_ok = bool(manifest.get("files")) and not bad and not missing
        detail = f"{len(manifest.get('files', []))} deliverable(s) in the package" if deliverables_ok else ("The archive is corrupt" if bad else "Missing from the archive: " + ", ".join(missing[:5]) if missing else "The package is empty")
    add("required_deliverables", deliverables_ok, detail)
    qa_status, qa_summary = "none", "No QA result"
    if job:
        report = qa_gate.verdict(conn, job["id"], write_report=False)
        qa_status, qa_summary = report["status"], report["summary"]
    add("qa_result", qa_status == qa_gate.PASS, f"{qa_status}: {qa_summary}")
    api_host = urlparse((policy.load_capabilities().get(pid, {}).get("documented_api") or {}).get("url") or "").hostname or ""
    dest = urlparse(adapter.destination) if adapter else None
    dest_ok = bool(dest and dest.scheme == "https" and dest.hostname and api_host and (dest.hostname == api_host or dest.hostname.endswith("." + api_host.split(".", 1)[-1])))
    add("submission_destination", dest_ok, f"Destination {dest.hostname} is the platform's documented API host" if dest_ok else "No documented https submission destination is recorded for this platform")
    stopped = control.is_stopped(conn)
    return {"assignment_id": aid, "platform_id": pid, "ok": all(c["ok"] for c in checks) and not stopped, "checks": checks,
            "blocked_by": [c["name"] for c in checks if not c["ok"]] + (["global_stop"] if stopped else []),
            "package_sha256": pkg.get("sha256"), "manual_route": "Submit the package yourself in your own account, then record the platform's confirmation."}


def _attempt(conn, key: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM submission_attempts WHERE idempotency_key=?", (key,)).fetchone()
    return dict(r) if r else None


def _confirm(conn, attempt_id: int, aid: int, reference: str, response: Any, actor: str) -> None:
    a = asg.get(conn, aid)
    with db.tx(conn):
        conn.execute("UPDATE submission_attempts SET status='confirmed', platform_reference=?, response_json=?, error=NULL, updated_at=? WHERE id=?",
                     (redact(reference)[:200], json.dumps(scrub(response), ensure_ascii=False, default=str)[:8000], db.now_iso(), attempt_id))
        db.audit(conn, "submission.confirmed", "assignment", aid, {"attempt": attempt_id, "reference": redact(reference)[:80]}, actor=actor)
    asg.advance_through(conn, aid, "submitted", "platform_record" if actor != "user" else "user_confirmation", f"submission:{reference}"[:200],
                        "Submitted through the platform's documented interface" if actor != "user" else "Submission confirmed by you", confirmed_by_user=actor == "user",
                        actor="platform" if actor != "user" else None)
    if a["execution_job_id"]:
        job = conn.execute("SELECT status FROM execution_jobs WHERE id=?", (a["execution_job_id"],)).fetchone()
        if job and job["status"] == "ready_for_submission":
            with db.tx(conn):
                ex._set(conn, a["execution_job_id"], "submitted", "Submitted (recorded with the platform's reference)", submitted_at=db.now_iso()[:10], submission_reference=redact(reference)[:200])
                ex.event(conn, a["execution_job_id"], "submitted", f"Submission recorded (reference {redact(reference)[:60]})")


def submit(conn, aid: int, adapter: SubmissionAdapter | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Submit once, if and only if every check passes. Returns what happened; it never raises for a blocked submission."""
    pre = preflight(conn, aid, adapter, settings)
    if not pre["ok"]:
        return {"submitted": False, "manual_required": True, "blocked_by": pre["blocked_by"], "checks": pre["checks"], "manual_route": pre["manual_route"]}
    a = asg.get(conn, aid)
    adapter = adapter if adapter is not None else REGISTRY[a["platform_id"]]
    key = idempotency_key(a["platform_id"], a["external_id"], pre["package_sha256"])
    prior = _attempt(conn, key)
    if prior:
        if prior["status"] == "confirmed":
            return {"submitted": True, "duplicate_prevented": True, "reference": prior["platform_reference"], "attempt_id": prior["id"]}
        if prior["status"] in ("pending", "unknown"):
            return {"submitted": None, "needs_reconcile": True, "attempt_id": prior["id"],
                    "reason": "An earlier attempt has no confirmed outcome. It is not sent again until it is reconciled."}
    with db.tx(conn):                                           # committed before anything is sent
        if prior:
            conn.execute("UPDATE submission_attempts SET status='pending', error=NULL, updated_at=? WHERE id=?", (db.now_iso(), prior["id"]))
            attempt_id = prior["id"]
        else:
            attempt_id = conn.execute("""INSERT INTO submission_attempts(assignment_id,idempotency_key,platform_id,destination,package_sha256,status,created_at,updated_at)
                VALUES(?,?,?,?,?, 'pending', ?,?)""", (aid, key, a["platform_id"], adapter.destination, pre["package_sha256"], db.now_iso(), db.now_iso())).lastrowid
        db.audit(conn, "submission.attempt", "assignment", aid, {"attempt": attempt_id}, actor="system")
    pkg = _package(conn, a)
    try:
        response = adapter.submit(pkg["archive"], pkg["manifest"], a, key)
    except SubmissionRejected as exc:
        with db.tx(conn):
            conn.execute("UPDATE submission_attempts SET status='failed', error=?, updated_at=? WHERE id=?", (redact(str(exc))[:500], db.now_iso(), attempt_id))
        return {"submitted": False, "attempt_id": attempt_id, "reason": f"The platform refused the submission: {redact(str(exc))[:300]}"}
    except Exception as exc:                                    # noqa: BLE001 - timeout, network error, crash: outcome unknown
        with db.tx(conn):
            conn.execute("UPDATE submission_attempts SET status='unknown', error=?, updated_at=? WHERE id=?", (redact(str(exc))[:500], db.now_iso(), attempt_id))
        return {"submitted": None, "needs_reconcile": True, "attempt_id": attempt_id, "reason": "No answer was received. It will NOT be sent again until the outcome is known."}
    reference = str((response or {}).get("reference") or "").strip() if isinstance(response, dict) else ""
    if not reference:
        with db.tx(conn):
            conn.execute("UPDATE submission_attempts SET status='unknown', response_json=?, error=?, updated_at=? WHERE id=?",
                         (json.dumps(scrub(response), default=str)[:8000], "The platform's answer carried no submission reference", db.now_iso(), attempt_id))
        return {"submitted": None, "needs_reconcile": True, "attempt_id": attempt_id, "reason": "The answer had no submission reference, so nothing is recorded as submitted."}
    _confirm(conn, attempt_id, aid, reference, response, "platform")
    return {"submitted": True, "reference": reference, "attempt_id": attempt_id, "note": "Submitted. Acceptance and payment are separate and are not assumed."}


def reconcile(conn, attempt_id: int, adapter: SubmissionAdapter | None = None, confirmed_by_user: bool = False, reference: str = "",
              not_submitted: bool = False) -> dict[str, Any]:
    """Resolve an attempt whose outcome is unknown: ask the platform's own lookup, or record what you saw in your account."""
    row = conn.execute("SELECT * FROM submission_attempts WHERE id=?", (attempt_id,)).fetchone()
    if not row:
        raise SubmissionError("Submission attempt not found")
    if row["status"] not in ("unknown", "pending"):
        return dict(row)
    a = asg.get(conn, row["assignment_id"])
    adapter = adapter if adapter is not None else REGISTRY.get(row["platform_id"])
    found = adapter.lookup(row["idempotency_key"], a) if adapter is not None and hasattr(adapter, "lookup") else None
    if isinstance(found, dict) and found.get("reference"):
        _confirm(conn, attempt_id, a["id"], str(found["reference"]), found, "platform")
    elif confirmed_by_user is True and not_submitted:
        with db.tx(conn):
            conn.execute("UPDATE submission_attempts SET status='failed', error='You confirmed nothing was recorded by the platform', updated_at=? WHERE id=?", (db.now_iso(), attempt_id))
            db.audit(conn, "submission.reconciled_not_submitted", "assignment", a["id"], {"attempt": attempt_id})
    elif confirmed_by_user is True and (reference or "").strip():
        _confirm(conn, attempt_id, a["id"], reference.strip(), {"confirmed_by_user": True}, "user")
    else:
        raise SubmissionError("The outcome is still unknown. Check your account, then confirm with the platform's reference, or confirm that nothing was recorded.")
    return dict(conn.execute("SELECT * FROM submission_attempts WHERE id=?", (attempt_id,)).fetchone())


def recover(conn, now: datetime | None = None) -> int:
    """After a crash: an attempt still 'pending' was interrupted mid-send. Its outcome is unknown; it is never re-sent blindly."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for r in conn.execute("SELECT id, updated_at FROM submission_attempts WHERE status='pending'").fetchall():
        try:
            age = (now - datetime.fromisoformat(r["updated_at"])).total_seconds()
        except (TypeError, ValueError):
            age = PENDING_STALE_SECONDS + 1
        if age > PENDING_STALE_SECONDS:
            with db.tx(conn):
                conn.execute("UPDATE submission_attempts SET status='unknown', error='Interrupted before an answer was recorded', updated_at=? WHERE id=?", (db.now_iso(), r["id"]))
            n += 1
    return n


def process_queue(conn, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Submit whatever is fully authorized; leave everything else for you. Used by the scheduler."""
    out = {"submitted": [], "manual_required": [], "needs_reconcile": []}
    if control.is_stopped(conn):
        return {**out, "blocked": "The global stop is engaged"}
    for r in conn.execute("SELECT id FROM assignments WHERE stage='ready_for_delivery' AND hold IS NULL").fetchall():
        result = submit(conn, r["id"], settings=settings)
        key = "submitted" if result.get("submitted") else ("needs_reconcile" if result.get("needs_reconcile") else "manual_required")
        out[key].append({"assignment_id": r["id"], **{k: result[k] for k in ("blocked_by", "reference", "reason") if k in result}})
    return out


def manager(conn) -> dict[str, Any]:
    """Everything the Submission Manager shows, from real records only."""
    def items(*stages: str) -> list[dict[str, Any]]:
        rows = conn.execute(f"SELECT id FROM assignments WHERE stage IN ({','.join('?' * len(stages))}) ORDER BY updated_at DESC LIMIT 200", stages).fetchall()
        return [{k: a[k] for k in ("id", "title", "platform_id", "stage", "state", "submission_reference", "reward_net", "reward_gross", "currency", "deadline", "next_action")}
                for a in (lifecycle.view(conn, r["id"]) for r in rows)]
    ready = []
    for a in items("ready_for_delivery"):
        pre = preflight(conn, a["id"])
        ready.append({**a, "auto_submit": pre["ok"], "blocked_by": pre["blocked_by"], "checks": pre["checks"], "manual_route": pre["manual_route"]})
    attempts = db.rows(conn.execute("SELECT id,assignment_id,platform_id,destination,status,platform_reference,error,created_at,updated_at FROM submission_attempts ORDER BY id DESC LIMIT 200"))
    return {"ready_for_submission": ready, "automatically_submitted": [t for t in attempts if t["status"] == "confirmed"],
            "manual_submission_required": [r for r in ready if not r["auto_submit"]], "needs_reconcile": [t for t in attempts if t["status"] in ("unknown", "pending")],
            "awaiting_review": items("submitted", "awaiting_review"), "accepted": items("work_accepted", "payment_pending", "paid"), "rejected": items("rejected"),
            "registered_interfaces": sorted(REGISTRY),
            "note": "No registered platform is verified for automated submission today. A successful upload is never treated as acceptance."}
