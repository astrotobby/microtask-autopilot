"""The assignment state machine in the engine's vocabulary, on top of the evidence-checked stages in assignments.py.

    DISCOVERED -> ELIGIBILITY_CHECKED -> QUALIFIED -> AUTOMATION_CLASSIFIED -> READY_FOR_EXECUTION -> EXECUTING
      -> QUALITY_ASSURANCE -> READY_FOR_SUBMISSION -> SUBMITTED -> AWAITING_REVIEW -> ACCEPTED | REJECTED
      -> PAYMENT_PENDING -> PAID
    Side states: HUMAN_REQUIRED, POLICY_BLOCKED, EXPIRED, CANCELLED, FAILED, PAUSED, INTERRUPTED

The stored ``stage`` (assignments.py) still moves one step at a time with database-checked evidence, so nothing can
jump from DISCOVERED to PAID. This module adds the two analysis checkpoints (eligibility checked, automation
classified) and the side states as a ``hold``. Every change writes an ``assignment_events`` row with its evidence
and a timestamp. Nothing here contacts a platform.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    from microtask_monitor import assignments as asg, db, opportunity_common as oc, permissions, policy, task_intel
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import assignments as asg, db, opportunity_common as oc, permissions, policy, task_intel
    from secret_store import redact

STATES = ("DISCOVERED", "ELIGIBILITY_CHECKED", "QUALIFIED", "AUTOMATION_CLASSIFIED", "READY_FOR_EXECUTION", "EXECUTING", "QUALITY_ASSURANCE",
          "READY_FOR_SUBMISSION", "SUBMITTED", "AWAITING_REVIEW", "ACCEPTED", "REJECTED", "PAYMENT_PENDING", "PAID",
          "HUMAN_REQUIRED", "POLICY_BLOCKED", "EXPIRED", "CANCELLED", "FAILED", "PAUSED", "INTERRUPTED")
HOLDS = ("human_required", "policy_blocked", "expired", "failed", "paused", "interrupted")
_STAGE_STATE = {"ready": "READY_FOR_EXECUTION", "accepted": "READY_FOR_EXECUTION", "executing": "EXECUTING", "qa": "QUALITY_ASSURANCE",
                "ready_for_delivery": "READY_FOR_SUBMISSION", "submitted": "SUBMITTED", "awaiting_review": "AWAITING_REVIEW",
                "work_accepted": "ACCEPTED", "rejected": "REJECTED", "payment_pending": "PAYMENT_PENDING", "paid": "PAID", "cancelled": "CANCELLED"}
# The main line, in order. A state can only be entered from the one before it.
MAIN_LINE = STATES[:11] + ("PAYMENT_PENDING", "PAID")
NEXT = {s: (MAIN_LINE[i + 1],) for i, s in enumerate(MAIN_LINE[:-1])}
NEXT["AWAITING_REVIEW"] = ("ACCEPTED", "REJECTED")
NEXT["SUBMITTED"] = ("AWAITING_REVIEW", "REJECTED")


class LifecycleError(ValueError):
    pass


def state(a: dict[str, Any], classified: bool | None = None) -> str:
    """The engine state of an assignment row (``classified`` = a task_intel row exists)."""
    if a.get("stage") in ("cancelled", "paid", "rejected"):
        return _STAGE_STATE[a["stage"]]
    if a.get("hold"):
        return a["hold"].upper()
    stage = a["stage"]
    if stage == "discovered":
        return "ELIGIBILITY_CHECKED" if a.get("eligibility_checked_at") else "DISCOVERED"
    if stage == "qualified":
        return "AUTOMATION_CLASSIFIED" if classified else "QUALIFIED"
    return _STAGE_STATE[stage]


# The milestone's seventeen public states, derived from the engine state (one source of truth, no second state column).
SPEC_STATES = ("DISCOVERED", "ELIGIBILITY_CHECKED", "POLICY_CHECKED", "RANKED", "SELECTED", "PREPARING", "EXECUTING", "QA_PENDING", "QA_PASSED",
               "SUBMISSION_READY", "SUBMITTED", "UNDER_REVIEW", "APPROVED", "REJECTED", "PAID", "FAILED", "BLOCKED")
_SPEC_FOR = {"DISCOVERED": "DISCOVERED", "ELIGIBILITY_CHECKED": "ELIGIBILITY_CHECKED", "QUALIFIED": "POLICY_CHECKED", "AUTOMATION_CLASSIFIED": "RANKED",
             "READY_FOR_EXECUTION": "SELECTED", "EXECUTING": "EXECUTING", "QUALITY_ASSURANCE": "QA_PENDING", "READY_FOR_SUBMISSION": "SUBMISSION_READY",
             "SUBMITTED": "SUBMITTED", "AWAITING_REVIEW": "UNDER_REVIEW", "ACCEPTED": "APPROVED", "PAYMENT_PENDING": "APPROVED", "REJECTED": "REJECTED",
             "PAID": "PAID", "FAILED": "FAILED", "EXPIRED": "FAILED", "CANCELLED": "FAILED", "INTERRUPTED": "FAILED",
             "HUMAN_REQUIRED": "BLOCKED", "POLICY_BLOCKED": "BLOCKED", "PAUSED": "BLOCKED"}


def spec_state(a: dict[str, Any], job_status: str | None = None) -> str:
    """The public state of an assignment view. ``job_status`` (its execution job) separates PREPARING from EXECUTING and QA_PASSED from QA_PENDING."""
    st = a.get("state") or state(a, bool(a.get("intel")))
    if st == "DISCOVERED" and a.get("intel"):
        return "POLICY_CHECKED"
    if st == "EXECUTING" and job_status not in ("running",):
        return "PREPARING"
    if st == "QUALITY_ASSURANCE" and job_status in ("awaiting_review", "approved", "ready_for_submission"):
        return "QA_PASSED"
    return _SPEC_FOR.get(st, "BLOCKED")


def can_transition(frm: str, to: str) -> bool:
    if to in ("CANCELLED", "HUMAN_REQUIRED", "POLICY_BLOCKED", "EXPIRED", "FAILED", "PAUSED", "INTERRUPTED"):
        return frm not in ("PAID", "REJECTED", "CANCELLED")
    return to in NEXT.get(frm, ())


def _row(conn, aid: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
    if not r:
        raise LifecycleError("Assignment not found")
    return dict(r)


def current(conn, aid: int) -> str:
    return state(_row(conn, aid), bool(conn.execute("SELECT 1 FROM task_intel WHERE assignment_id=?", (aid,)).fetchone()))


def _event(conn, aid: int, frm: str, to: str, kind: str, ref: str, detail: str, actor: str = "system") -> None:
    conn.execute("INSERT INTO assignment_events(assignment_id,at,from_stage,to_stage,evidence_kind,evidence_ref,detail,actor) VALUES(?,?,?,?,?,?,?,?)",
                 (aid, db.now_iso(), frm, to, kind, redact(ref)[:300], redact(detail or "")[:1000], actor))


# ------------------------------------------------------------------------------------------- eligibility and payment
def eligibility(a: dict[str, Any], caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Kenya eligibility from recorded facts only: the task's own country list, the listing's verified class, or the platform's policy.

    Being able to open a listing proves nothing, so anything else is 'unverified' and waits for your confirmation.
    """
    import json
    caps = policy.load_capabilities() if caps is None else caps
    req = a.get("requirements")
    if not isinstance(req, dict):
        try:
            req = json.loads(a.get("requirements_json") or "{}")
        except ValueError:
            req = {}
    pid = a["platform_id"]
    if pid == permissions.DIRECT_LANE:
        return {"status": "eligible", "basis": "Your own client's work: no platform country rule applies", "evidence": "direct_client"}
    platform = (caps.get(pid, {}).get("country_eligibility") or {})
    if platform.get("kenya") == "unsupported":
        return {"status": "ineligible", "basis": "The platform lists Kenya as unsupported", "evidence": f"capability_record:{pid}"}
    countries = req.get("allowed_countries")
    if isinstance(countries, list):
        codes = [str(c).upper() for c in countries]
        if "KE" in codes:
            return {"status": "eligible", "basis": "Kenya (KE) is in the task's allowed countries", "evidence": "task:allowed_countries"}
        if codes:
            return {"status": "ineligible", "basis": "Kenya is not in the task's allowed countries (" + ", ".join(codes[:8]) + ")", "evidence": "task:allowed_countries"}
        if req.get("empty_means_worldwide"):
            return {"status": "eligible", "basis": "No country restriction (documented by the platform as worldwide)", "evidence": "task:allowed_countries=[]"}
    cls = req.get("eligibility_class") or ""
    if cls in ("KENYA_CONFIRMED", "WORLDWIDE_CONFIRMED"):
        return {"status": "eligible", "basis": f"The listing was verified as {cls}", "evidence": f"listing:{cls}"}
    if cls in ("KENYA_UNSUPPORTED", "EXPIRED"):
        return {"status": "ineligible", "basis": f"The listing is {cls}", "evidence": f"listing:{cls}"}
    if str(req.get("kenya") or "").lower() in ("eligible", "listed", "worldwide"):
        return {"status": "eligible", "basis": f"The platform's own data marks Kenya as {req['kenya']}", "evidence": "task:kenya"}
    return {"status": "unverified", "basis": "Nothing on record states that Kenya is accepted for this task", "evidence": f"capability_record:{pid}:{platform.get('kenya', 'unverified')}"}


def payment_conditions(a: dict[str, Any], caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Whether the reward and the payout route are clear. A missing amount or an upfront cost is never waved through."""
    caps = policy.load_capabilities() if caps is None else caps
    pay = (caps.get(a["platform_id"], {}).get("payment_verification") or {})
    costs = oc.find_cost_flags(f"{a.get('title') or ''}\n{a.get('instructions') or ''}")
    amount = a.get("reward_net") if a.get("reward_net") is not None else a.get("reward_gross")
    problems = []
    if costs:
        problems.append("Possible upfront cost: " + ", ".join(costs[:3]))
    if amount is None:
        problems.append("No reward amount is stated")
    route = "your own arrangement with the client" if a["platform_id"] == permissions.DIRECT_LANE else pay.get("status", "unverified")
    return {"clear": not problems, "amount": amount, "currency": a.get("currency"), "payout_route": route, "payout_note": pay.get("note"),
            "upfront_cost": bool(costs), "problems": problems}


def check_eligibility(conn, aid: int) -> dict[str, Any]:
    """DISCOVERED -> ELIGIBILITY_CHECKED, and on to QUALIFIED only when eligibility is positively established."""
    a = _row(conn, aid)
    if a["stage"] != "discovered" or a.get("hold") in ("policy_blocked", "expired"):
        return view(conn, aid)
    result = eligibility(a)
    with db.tx(conn):
        if not a.get("eligibility_checked_at"):
            conn.execute("UPDATE assignments SET eligibility_checked_at=?, updated_at=? WHERE id=?", (db.now_iso(), db.now_iso(), aid))
            _event(conn, aid, "DISCOVERED", "ELIGIBILITY_CHECKED", "eligibility_check", result["evidence"], f"{result['status']}: {result['basis']}")
    if result["status"] == "ineligible":
        set_hold(conn, aid, "policy_blocked", result["basis"], result["evidence"])
    elif result["status"] == "eligible":
        asg.qualify(conn, aid, True, [result["basis"]], result["evidence"])
    return view(conn, aid)


def confirm_eligibility(conn, aid: int, confirmed_by_user: bool, reference: str) -> dict[str, Any]:
    """You checked the country rule yourself in your account. Needs your confirmation and where you saw it."""
    a = _row(conn, aid)
    if confirmed_by_user is not True or not (reference or "").strip():
        raise LifecycleError("Confirm it yourself (confirmed_by_user=true) and say where the platform shows Kenya is accepted")
    if a["stage"] != "discovered":
        return view(conn, aid)
    if eligibility(a)["status"] == "ineligible":
        raise LifecycleError("The recorded facts say Kenya is not accepted for this task: it cannot be confirmed")
    with db.tx(conn):
        if not a.get("eligibility_checked_at"):
            conn.execute("UPDATE assignments SET eligibility_checked_at=? WHERE id=?", (db.now_iso(), aid))
            _event(conn, aid, "DISCOVERED", "ELIGIBILITY_CHECKED", "user_confirmation", reference, "Checked by you", "user")
    asg.qualify(conn, aid, True, ["Kenya eligibility confirmed by you: " + reference[:200]], "user:" + reference[:200])
    return view(conn, aid)


# ------------------------------------------------------------------------------------------- classification
def classify(conn, aid: int, caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """QUALIFIED -> AUTOMATION_CLASSIFIED (and READY_FOR_EXECUTION when it is genuinely executable). Re-running refreshes the analysis."""
    a = asg.get(conn, aid)
    result = task_intel.analyse(conn, {**a, "eligibility_checked_at": a.get("eligibility_checked_at")}, caps)
    first = not conn.execute("SELECT 1 FROM task_intel WHERE assignment_id=?", (aid,)).fetchone()
    with db.tx(conn):
        task_intel.store(conn, result)
        if first and a["stage"] == "qualified":
            _event(conn, aid, "QUALIFIED", "AUTOMATION_CLASSIFIED", "automation_classification", f"task_intel:{aid}",
                   f"{result['automation_class']} ({result['category']}): " + "; ".join(result["reasons"])[:700])
    cls = result["automation_class"]
    if a["stage"] in ("discovered", "qualified", "ready"):
        if cls == "HUMAN_REQUIRED":
            set_hold(conn, aid, "human_required", "; ".join(result["reasons"])[:300], f"task_intel:{aid}")
        elif cls in ("PLATFORM_AUTOMATION_PROHIBITED", "AI_PROHIBITED", "NOT_FEASIBLE"):
            set_hold(conn, aid, "policy_blocked", "; ".join(result["reasons"])[:300], f"task_intel:{aid}")
        elif a.get("hold") in ("human_required", "policy_blocked") and cls in task_intel.EXECUTABLE:
            clear_hold(conn, aid, "Re-classified as " + cls, f"task_intel:{aid}")
    a = _row(conn, aid)
    if cls in task_intel.EXECUTABLE and a["stage"] == "qualified" and not a.get("hold"):
        if cls == "DETERMINISTIC_LOCAL_PROCESSING" and a["ai_use"] == "unknown":
            asg.set_ai_use(conn, aid, "prohibited", "Deterministic specification: no AI is used")
        try:
            asg.mark_ready(conn, aid)
        except asg.AssignmentError:
            pass
    return view(conn, aid)


# ------------------------------------------------------------------------------------------- side states
def set_hold(conn, aid: int, hold: str, reason: str, evidence_ref: str, actor: str = "system") -> dict[str, Any]:
    if hold not in HOLDS:
        raise LifecycleError(f"Unknown side state '{hold}'")
    a = _row(conn, aid)
    frm = current(conn, aid)
    if not can_transition(frm, hold.upper()):
        raise LifecycleError(f"A {frm.lower().replace('_', ' ')} assignment cannot become {hold.replace('_', ' ')}")
    if a.get("hold") == hold:
        return view(conn, aid)
    if not (evidence_ref or "").strip():
        raise LifecycleError("A state change needs evidence")
    with db.tx(conn):
        conn.execute("UPDATE assignments SET hold=?, hold_reason=?, paused_from=?, claimed_by=NULL, claim_until=NULL, updated_at=? WHERE id=?",
                     (hold, redact(reason)[:300], frm, db.now_iso(), aid))
        _event(conn, aid, frm, hold.upper(), "hold", evidence_ref, reason, actor)
    return view(conn, aid)


def clear_hold(conn, aid: int, reason: str, evidence_ref: str, actor: str = "system") -> dict[str, Any]:
    a = _row(conn, aid)
    if not a.get("hold"):
        return view(conn, aid)
    if a["hold"] == "expired":
        raise LifecycleError("An expired assignment cannot be reopened")
    with db.tx(conn):
        conn.execute("UPDATE assignments SET hold=NULL, hold_reason=NULL, paused_from=NULL, updated_at=? WHERE id=?", (db.now_iso(), aid))
        _event(conn, aid, a["hold"].upper(), a.get("paused_from") or "RESUMED", "hold_cleared", evidence_ref or "resume", reason, actor)
    return view(conn, aid)


def pause(conn, aid: int, confirmed_by_user: bool = True) -> dict[str, Any]:
    return set_hold(conn, aid, "paused", "Paused by you", "user:pause", "user")


def resume(conn, aid: int) -> dict[str, Any]:
    a = _row(conn, aid)
    if a.get("hold") not in ("paused", "interrupted", "failed"):
        raise LifecycleError("Only a paused, interrupted or failed assignment can be resumed")
    return clear_hold(conn, aid, "Resumed by you", "user:resume", "user")


def expire_due(conn, now: datetime | None = None) -> int:
    """Deadline passed before submission: EXPIRED. Submitted work is never expired by the clock."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for r in conn.execute("SELECT id, deadline FROM assignments WHERE deadline IS NOT NULL AND stage IN ('discovered','qualified','ready','accepted','executing','qa','ready_for_delivery') AND COALESCE(hold,'')<>'expired'").fetchall():
        when = oc.parse_time(r["deadline"])
        if when and when < now:
            set_hold(conn, r["id"], "expired", f"The deadline ({r['deadline']}) has passed", f"deadline:{r['deadline']}")
            n += 1
    return n


def view(conn, aid: int) -> dict[str, Any]:
    a = asg.get(conn, aid)
    intel = task_intel.load(conn, aid)
    a["state"] = state(a, bool(intel))
    a["intel"] = intel
    return a


def list_view(conn, limit: int = 500) -> list[dict[str, Any]]:
    return [view(conn, r["id"]) for r in conn.execute("SELECT id FROM assignments ORDER BY updated_at DESC LIMIT ?", (limit,))]
