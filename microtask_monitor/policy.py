"""Platform capability records and hard enforcement of what automation is permitted.

Nothing in this project may contact or act on a platform without passing ``check_action``.
Unknown platforms, unknown actions and unreadable capability data all fail CLOSED (deny).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES_PATH = ROOT / "microtask_monitor" / "capabilities.json"

LEVELS = ("PUBLIC_DISCOVERY_ALLOWED", "DOCUMENTED_API_ALLOWED", "MANUAL_ONLY", "HUMAN_APPROVAL_REQUIRED",
          "AUTOMATION_PROHIBITED", "UNVERIFIED")

# Never permitted on any platform, whatever the capability record or user setting says.
ALWAYS_BLOCKED = frozenset({
    "auto_accept_task", "auto_complete_task", "auto_submit_evidence", "auto_accept_contract", "auto_payment",
    "bypass_captcha", "bypass_mfa", "bypass_kyc", "bypass_geo_restriction", "use_vpn_or_proxy",
    "scrape_logged_in", "probe_private_endpoints", "fabricate_evidence", "impersonate_user", "create_account",
})
ACCESS_CLASSES = ("official_api_permitted", "browser_automation_permitted", "human_assisted_only", "prohibited_or_unverified")
# Actions inside YOUR signed-in account. Each needs: access_class browser_automation_permitted, the action listed in the
# platform's permitted_automation (from its verified terms), your explicit authorization, and a live session (connections.py).
AUTHENTICATED = frozenset({"authenticated_inspect", "initiate_task", "browser_task_execution", "submit_via_workflow"})
# Local-only helpers that never contact a platform: always allowed.
LOCAL_ACTIONS = frozenset({"assist_application", "assist_task", "record_manual_submission", "track_status"})
# Allowed only when the capability record lists the matching level or permitted_automation entry.
GATED = {
    "public_discovery": ("level", "PUBLIC_DISCOVERY_ALLOWED"),
    "api_read": ("level", "DOCUMENTED_API_ALLOWED"),
    "policy_monitor": ("permitted", "policy_monitor"),
    "auto_submit_application": ("permitted", "application_submission"),
}


class PermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def load_capabilities(path: Path = CAPABILITIES_PATH) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {p["platform_id"]: p for p in data["platforms"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def check_action(platform_id: str, action: str, *, caps: dict[str, dict[str, Any]] | None = None,
                 user_authorized: bool = False) -> Decision:
    """Decide whether ``action`` may be performed for ``platform_id``."""
    if action in ALWAYS_BLOCKED:
        return Decision(False, f"'{action}' is never permitted on any platform")
    if action in LOCAL_ACTIONS:
        return Decision(True, "local-only action; no platform contact")
    if action == "autonomous_earning":
        try:
            from microtask_monitor import autonomy
        except ImportError:  # pragma: no cover
            import autonomy
        capset = load_capabilities() if caps is None else caps
        result = autonomy.evaluate(platform_id, capset.get(platform_id, {}))
        if not result["eligible"]:
            return Decision(False, f"autonomous earning is blocked for {platform_id}: {'; '.join(result['reasons'])}")
        if not user_authorized:
            return Decision(False, "autonomous earning needs explicit user authorization")
        return Decision(True, f"verified autonomous earning contract for {platform_id} and authorized by you")
    caps = load_capabilities() if caps is None else caps
    record = caps.get(platform_id)
    if record is None:
        return Decision(False, f"no capability record for '{platform_id}' (fail closed)")
    if action in AUTHENTICATED:
        access = record.get("access_class")
        if access not in ACCESS_CLASSES:
            return Decision(False, f"{platform_id} has no valid access classification (fail closed)")
        if access != "browser_automation_permitted":
            return Decision(False, f"{platform_id} is '{access}': account automation is not permitted")
        if "AUTOMATION_PROHIBITED" in set(record.get("levels", [])):
            return Decision(False, f"{platform_id} prohibits automation")
        if action not in record.get("permitted_automation", []):
            return Decision(False, f"'{action}' is not listed as permitted for {platform_id}")
        if not user_authorized:
            return Decision(False, "this account action needs your explicit authorization")
        return Decision(True, f"'{action}' permitted for {platform_id} and authorized by you")
    if action not in GATED:
        return Decision(False, f"unknown action '{action}' (fail closed)")
    levels = set(record.get("levels", []))
    prohibited = set(record.get("prohibited_automation", []))
    kind, needed = GATED[action]
    if action == "auto_submit_application":
        if "AUTOMATION_PROHIBITED" in levels or "automated_application_submission" in prohibited:
            return Decision(False, f"{platform_id} prohibits automated application submission")
        if record.get("application_submission") != "automation_permitted" or needed not in record.get("permitted_automation", []):
            return Decision(False, f"{platform_id} does not explicitly permit automated submission")
        if not user_authorized:
            return Decision(False, "automated submission requires your explicit authorization for this workflow")
        return Decision(True, "platform permits it and the user authorized it")
    if kind == "level":
        if needed not in levels:
            return Decision(False, f"{platform_id} lacks the {needed} capability")
        if action in prohibited or (action == "public_discovery" and "opportunity_discovery" in prohibited):
            return Decision(False, f"{action} is prohibited for {platform_id}")
        return Decision(True, f"{needed} recorded for {platform_id}")
    if needed in record.get("permitted_automation", []):
        return Decision(True, f"'{needed}' is explicitly permitted for {platform_id}")
    return Decision(False, f"'{needed}' is not permitted for {platform_id}")


PERMISSION_CLASSES = {1: "Official API automation permitted", 2: "Browser automation expressly permitted", 3: "Human-assisted execution only",
                      4: "Automation prohibited", 5: "Not verified"}


def permission_class(record: dict[str, Any]) -> dict[str, Any]:
    """The five-way classification, derived from the reviewed capability record (never from a platform's name)."""
    access, levels = record.get("access_class"), set(record.get("levels", []))
    if (record.get("country_eligibility") or {}).get("kenya") == "unsupported":
        n = 4
    elif access == "official_api_permitted":
        n = 1
    elif access == "browser_automation_permitted":
        n = 2
    elif "AUTOMATION_PROHIBITED" in levels:
        n = 4
    elif access == "human_assisted_only":
        n = 3
    else:
        n = 5
    return {"class": n, "label": PERMISSION_CLASSES[n]}


def registry(caps: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One evidence row per platform: classification, what must stay manual, sources and the date they were read."""
    caps = load_capabilities() if caps is None else caps
    out = []
    for pid, r in caps.items():
        execution, review = r.get("execution") or {}, r.get("review") or {}
        permitted = set(r.get("permitted_automation", []))
        out.append({"platform_id": pid, "name": r.get("name", pid), **permission_class(r),
                    "automated_discovery": bool({"public_discovery", "api_read"} & permitted) or "PUBLIC_DISCOVERY_ALLOWED" in r.get("levels", []),
                    "worker_api": bool((r.get("documented_api") or {}).get("available")), "api_scope": (r.get("documented_api") or {}).get("scope"),
                    "browser_automation": r.get("access_class") == "browser_automation_permitted",
                    "ai_assistance": (execution.get("ai_assistance") or {}).get("status", "unverified"),
                    "ai_note": (execution.get("ai_assistance") or {}).get("note"),
                    "auto_acceptance": False, "auto_submission": bool(execution.get("automated_submission")),
                    "kenya": (r.get("country_eligibility") or {}).get("kenya", "unverified"), "kenya_note": (r.get("country_eligibility") or {}).get("note"),
                    "payment": (r.get("payment_verification") or {}).get("status", "unverified"), "payment_note": (r.get("payment_verification") or {}).get("note"),
                    "manual_operations": execution.get("manual_steps") or ["Everything in your account: sign-in, taking tasks, doing the work, submitting, withdrawing."],
                    "sources": list(dict.fromkeys((r.get("policy_urls") or []) + (review.get("terms_read") or []) + (execution.get("sources") or []))),
                    "not_read": review.get("not_read_or_unavailable") or [], "checked_on": r.get("last_policy_verified"),
                    "automation_clause": review.get("automation_clause")})
    return out


def require(platform_id: str, action: str, **kw: Any) -> None:
    decision = check_action(platform_id, action, **kw)
    if not decision.allowed:
        raise PermissionDenied(f"Blocked: {decision.reason}")


def kenya_status(platform_id: str, caps: dict[str, dict[str, Any]] | None = None) -> str:
    caps = load_capabilities() if caps is None else caps
    return caps.get(platform_id, {}).get("country_eligibility", {}).get("kenya", "unverified")


def sync_to_db(conn, caps: dict[str, dict[str, Any]] | None = None) -> int:
    """Mirror capability records into SQLite so the dashboard reads them from the backend."""
    from microtask_monitor import db
    caps = load_capabilities() if caps is None else caps
    now = db.now_iso()
    for pid, record in caps.items():
        conn.execute("""INSERT INTO platform_capabilities(platform_id,record_json,verified_on,updated_at)
                        VALUES(?,?,?,?) ON CONFLICT(platform_id) DO UPDATE SET
                        record_json=excluded.record_json, verified_on=excluded.verified_on, updated_at=excluded.updated_at""",
                     (pid, json.dumps(record, ensure_ascii=False), record.get("last_policy_verified"), now))
    return len(caps)
