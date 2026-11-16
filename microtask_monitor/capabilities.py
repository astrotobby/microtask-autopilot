"""Per-platform capability profiles: eleven independently stated capabilities, three states, evidence for each.

    allowed      recorded evidence permits it (or it is purely local work on your own PC)
    prohibited   the platform's terms (or a recorded prohibition) forbid it
    unverified   nothing on record permits it: treated exactly like prohibited (fail closed)

The states are computed from the reviewed capability records and capability_evidence.json through permissions.derive,
so this module adds no second source of truth: it is the view of the permission engine in the milestone's vocabulary,
plus ``check``, the four-part gate every adapter operation calls.

Automatic submission keeps the existing restriction: every platform whose record says submission is done by a person
reports ``automatic_submission: prohibited`` with ``submission_mode: human_required`` until a platform AND the specific
assignment are verified to permit it.
"""
from __future__ import annotations

from typing import Any

try:
    from microtask_monitor import control, permissions, policy
except ImportError:  # pragma: no cover
    import control, permissions, policy

CAPABILITIES = ("public_discovery", "email_notification_monitoring", "authenticated_monitoring", "task_reading", "task_classification",
                "task_preparation", "ai_execution", "quality_assurance", "proof_preparation", "automatic_submission", "earnings_monitoring")
STATES = ("allowed", "prohibited", "unverified")
LOCAL = frozenset({"task_classification", "task_preparation", "quality_assurance", "email_notification_monitoring"})
ACTION_FOR = {"public_discovery": "public_discovery", "authenticated_monitoring": "account_monitoring", "ai_execution": "task_execution",
              "automatic_submission": "auto_submission"}
WHY_LOCAL = {"task_classification": "Runs on your PC on task text you or a permitted source supplied",
             "task_preparation": "Runs on your PC; nothing is sent to the platform",
             "quality_assurance": "Runs on your PC on the produced files",
             "email_notification_monitoring": "Reads notification emails in your own mailbox (an exported .eml folder or your read-only inbox connection)"}


def _state(result: str) -> str:
    return {"permitted": "allowed", "prohibited": "prohibited"}.get(result, "unverified")


def profile(platform_id: str, caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """{"platform_id", "name", "capabilities": {cap: state}, "reasons": {cap: why}, "permission_evidence": {...}, "submission_mode"}"""
    caps = policy.load_capabilities() if caps is None else caps
    prof = permissions.profile(platform_id, caps)
    if prof is None:
        return None
    acts = prof["actions"]
    out: dict[str, str] = {}
    why: dict[str, str] = {}
    for cap, action in ACTION_FOR.items():
        e = acts[action]
        out[cap], why[cap] = _state(e["result"]), e.get("why") or (e.get("clause") or "")
    for cap in LOCAL:
        out[cap], why[cap] = "allowed", WHY_LOCAL[cap]
    if prof.get("disabled_reason"):
        for cap in ("email_notification_monitoring", "task_preparation"):
            out[cap], why[cap] = "prohibited", prof["disabled_reason"]
    read_ok = out["public_discovery"] == "allowed" or out["authenticated_monitoring"] == "allowed"
    out["task_reading"] = "allowed" if read_ok else ("prohibited" if out["public_discovery"] == out["authenticated_monitoring"] == "prohibited" else "unverified")
    why["task_reading"] = "Task details come through the permitted interface" if read_ok else "No permitted interface reads tasks; details are imported by you"
    out["proof_preparation"] = "allowed" if out["ai_execution"] == "allowed" else "unverified"
    why["proof_preparation"] = ("Evidence is prepared from work the engine genuinely produced" if out["ai_execution"] == "allowed"
                                else "Only work the engine is permitted to do can have proof prepared by it; nothing is ever fabricated")
    rec = caps.get(platform_id, {})
    earnings_api = out["authenticated_monitoring"] == "allowed" and bool(rec.get("api_account_access"))
    out["earnings_monitoring"] = "allowed" if earnings_api else "unverified"
    why["earnings_monitoring"] = "Read through the documented API with your authorization" if earnings_api else "Payments are recorded by you with a reference"
    sub = acts["auto_submission"]
    mode = "automatic" if sub["result"] == "permitted" else ("human_required" if sub["result"] in ("human_required", "unverified") else "prohibited")
    if out["automatic_submission"] != "allowed":
        out["automatic_submission"] = "prohibited" if mode != "automatic" else out["automatic_submission"]
    best = next((acts[a] for a in ("auto_submission", "task_execution", "account_monitoring", "public_discovery") if acts[a]["result"] == "permitted"), None)
    src = next((e for e in permissions.load_evidence() if e.get("platform_id") == platform_id and not permissions.evidence_problems(e)), None)
    evidence = {"source_url": (src or {}).get("source_url") or (best or {}).get("evidence_url"), "source_type": (src or {}).get("source_type") or ("capability_record" if best else None),
                "verified_at": (src or {}).get("verified_at") or (best or {}).get("verified_on") or prof.get("reviewed_on"),
                "expires_at": (src or {}).get("expires_at"), "notes": (src or {}).get("notes") or rec.get("access_note") or ""}
    return {"platform_id": platform_id, "name": prof["name"], "level": prof["level"], "capabilities": {c: out[c] for c in CAPABILITIES},
            "reasons": {c: why[c] for c in CAPABILITIES}, "permission_evidence": evidence, "submission_mode": mode,
            "disabled_reason": prof.get("disabled_reason")}


def all_profiles(caps: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    caps = policy.load_capabilities() if caps is None else caps
    return [profile(pid, caps) for pid in caps]


def check(conn, platform_id: str, capability: str, task: dict[str, Any] | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """The four questions, answered separately, for one operation:
    1. platform: does the platform permit this capability?           2. assignment: does this task's own text permit it?
    3. authorized: did you pre-authorize this kind of task?           4. limits: is it within your configured limits?
    Every answer must be yes. Unverified is no."""
    if capability not in CAPABILITIES:
        return {"allowed": False, "checks": {}, "reasons": [f"unknown capability '{capability}' (fail closed)"]}
    prof = profile(platform_id) if platform_id != permissions.DIRECT_LANE else {"capabilities": {**{c: "allowed" for c in CAPABILITIES}, "automatic_submission": "prohibited",
                                                                                                "authenticated_monitoring": "unverified", "public_discovery": "unverified"},
                                                                               "reasons": {c: "Your own client's work" for c in CAPABILITIES}}
    if prof is None:
        return {"allowed": False, "checks": {}, "reasons": [f"no capability record for '{platform_id}' (fail closed)"]}
    s = settings if settings is not None else (control.settings(conn) if conn is not None else dict(control.DEFAULTS))
    checks, reasons = {}, []
    state = prof["capabilities"][capability]
    checks["platform"] = state == "allowed"
    if not checks["platform"]:
        reasons.append(f"platform: {capability} is {state} ({prof['reasons'][capability]})")
    needs_task = capability in ("ai_execution", "proof_preparation", "automatic_submission")
    if needs_task and task is not None:
        action = "auto_submission" if capability == "automatic_submission" else "task_execution"
        d = permissions.decide(conn, platform_id, action, task={**task, "needs_ai": task.get("needs_ai", True)}, settings={**s, "auto_execute": True})
        task_reasons = [r for r in d["reasons"] if not r.startswith(f"{action} is ") and "switched off" not in r and "not pre-authorized automatic submission" not in r]
        checks["assignment"] = not task_reasons
        reasons += [f"assignment: {r}" for r in task_reasons]
    else:
        checks["assignment"] = not needs_task
        if needs_task:
            reasons.append("assignment: the specific task's rules are needed for this decision")
    cat = (task or {}).get("category")
    if capability == "ai_execution":
        checks["authorized"] = bool(s.get("auto_execute")) and (cat is None or cat in s.get("permitted_categories", []))
        if not checks["authorized"]:
            reasons.append("authorized: " + ("automatic execution is switched off" if not s.get("auto_execute") else f"'{cat}' is not a pre-authorized task category"))
    elif capability == "automatic_submission":
        checks["authorized"] = bool((s.get("auto_submit_platforms") or {}).get(platform_id))
        if not checks["authorized"]:
            reasons.append("authorized: you have not pre-authorized automatic submission for this platform")
    else:
        checks["authorized"] = True
    ok, why = control.may_run(conn) if conn is not None else (True, "")
    checks["limits"] = ok and not (cat and cat in s.get("excluded_task_types", []))
    if not checks["limits"]:
        reasons.append("limits: " + (why or f"'{cat}' is on your excluded task types"))
    return {"allowed": all(checks.values()), "checks": checks, "reasons": reasons, "state": state}
