"""Central platform permission engine: capability levels 0-5, evidence records and the single gate for automated actions.

Autonomy is earned per platform AND per action, from recorded evidence; it is never assumed from having a session,
from a platform's name or from a task looking easy. ``decide`` is the only function the discovery engine, the
orchestrator and the submission engine ask. It fails closed: unknown platform, unknown action, missing or stale
evidence, a suspended record, a hard restriction or the global stop all deny.

Levels (what the program itself may do on a platform):
    0  Disabled                         nothing automated touches the platform
    1  Read-only discovery              permitted public information or documented public API
    2  Authorized account monitoring    documented API with the owner's authorization, read-only
    3  Task preparation                 inspect instructions obtained through 1/2, classify, prepare deliverables locally
    4  Authorized task execution        the platform's terms explicitly permit automated/AI-assisted completion
    5  Authorized automatic submission  the platform's terms explicitly permit automated submission, and you pre-authorized it

A platform at level 0 can still be used by hand: records you import yourself are classified and tracked locally.
This module never contacts a platform.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from datetime import date, datetime, timezone
from typing import Any

try:
    from microtask_monitor import db, policy
except ImportError:  # pragma: no cover
    import db, policy

ROOT = Path(__file__).resolve().parents[1]

LEVELS = {0: "Disabled for automation (manual use)", 1: "Read-only discovery", 2: "Authorized account monitoring", 3: "Task preparation",
          4: "Authorized task execution", 5: "Authorized automatic submission"}
ACTIONS = {"public_discovery": 1, "policy_monitor": 1, "account_monitoring": 2, "task_preparation": 3,
           "task_execution": 4, "auto_submission": 5}
RESULTS = ("permitted", "prohibited", "human_required", "unverified")
MAX_EVIDENCE_AGE_DAYS = 120          # a review older than this no longer authorizes anything above level 0
DIRECT_LANE = "direct_client"        # work for your own client, supplied by you. Not a platform: never counted in the registry of 25.

# Never automated on any platform, whatever a capability record or a setting says. (code, label, pattern, class)
HARD_RESTRICTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("captcha", "CAPTCHA solving or bypass", r"\bcaptcha|recaptcha|hcaptcha\b", "NOT_FEASIBLE"),
    ("mfa", "MFA / one-time code handling", r"\b(otp|one[- ]time (?:code|password)|2fa|two[- ]factor|verification code)\b", "HUMAN_REQUIRED"),
    ("kyc", "KYC or identity verification", r"\b(kyc|identity verification|verify your identity|government[- ]issued id|passport photo|selfie with (?:your )?id|id card)\b", "HUMAN_REQUIRED"),
    ("geo", "Geo-restriction circumvention", r"\b(vpn|proxy|proxies|change your (?:ip|location)|spoof (?:your )?location|residential ip)\b", "NOT_FEASIBLE"),
    ("fake_accounts", "Creating accounts", r"\b(create|make|register|open|sign[- ]?up(?: for)?)\b[^.\n]{0,40}\b(accounts?|gmail|email address(?:es)?|profiles?)\b", "NOT_FEASIBLE"),
    ("fake_engagement", "Engagement on request (likes, follows, comments, views)", r"\b(like|follow|subscribe|upvote|retweet|repost|share|comment on|watch|view|clap|star)\b[^.\n]{0,60}\b(video|channel|page|post|tweet|profile|account|repo|reel|tiktok|youtube|instagram|facebook|twitter|x\.com|telegram)\b", "NOT_FEASIBLE"),
    ("fake_reviews", "Reviews or ratings on request", r"\b(write|leave|post|give|submit)\b[^.\n]{0,30}\b(reviews?|ratings?|testimonials?|5[- ]?stars?)\b", "NOT_FEASIBLE"),
    ("referral", "Referral manipulation", r"\b(referr?al (?:link|code)|refer (?:\d+|a|your) friends?|invite (?:\d+ )?(?:friends|people)|sign up (?:using|with|through) (?:my|this|the) link)\b", "NOT_FEASIBLE"),
    ("recording", "A recording of you (voice, face, screen with narration)", r"\b(record (?:yourself|your (?:voice|face|screen))|voice recording|read (?:the|these) (?:sentences|phrases|prompts) aloud|selfie|film yourself|video of (?:yourself|you)|webcam|speak into)\b", "HUMAN_REQUIRED"),
    ("opinion_survey", "A survey about your own opinions or life", r"\b(survey|questionnaire|poll|screener|your (?:own )?opinions?|about yourself|demographic questions)\b", "HUMAN_REQUIRED"),
    ("usability_test", "A usability test of your own experience", r"\b(usability test|user test(?:ing)?|think[- ]aloud|speak your thoughts|unmoderated test|moderated (?:session|interview)|user interview)\b", "HUMAN_REQUIRED"),
    ("physical", "A physical-world action", r"\b(visit (?:a|the|your nearest) (?:store|shop|branch|location)|in[- ]person|mystery shopp|take (?:a )?photos? of (?:a|the|your)|walk (?:around|through)|drive to|go to (?:a|the) (?:store|shop|location)|head[- ]mount|wear the)\b", "HUMAN_REQUIRED"),
    ("financial", "A withdrawal, transfer, deposit or purchase", r"\b(withdraw(?:al)?|transfer (?:money|funds)|send (?:money|crypto|usdt)|deposit|make a purchase|buy (?:a|the|this)|pay (?:a|the) fee|top[- ]?up)\b", "NOT_FEASIBLE"),
    ("attestation", "A personal attestation or legal declaration", r"\b(i (?:hereby )?(?:certify|declare|attest|swear)|under penalty of perjury|sign (?:the|this|an?) (?:nda|contract|agreement|declaration|affidavit))\b", "HUMAN_REQUIRED"),
)
_HARD = tuple((code, label, re.compile(rx, re.I), cls) for code, label, rx, cls in HARD_RESTRICTIONS)


def hard_restrictions(text: str) -> list[dict[str, str]]:
    """Which never-automated things a task asks for. Matched on the task's own words; the quote is kept as evidence."""
    out = []
    for code, label, rx, cls in _HARD:
        m = rx.search(text or "")
        if m:
            out.append({"code": code, "label": label, "class": cls, "evidence": (text[max(0, m.start() - 40):m.end() + 40]).strip()[:160]})
    return out


def _https(url: Any) -> bool:
    return isinstance(url, str) and url.startswith("https://") and len(url) < 500


def _fresh(day: Any, today: date | None = None) -> bool:
    try:
        return ((today or date.today()) - date.fromisoformat(str(day)[:10])).days <= MAX_EVIDENCE_AGE_DAYS
    except (TypeError, ValueError):
        return False


def _entry(result: str, url: Any = None, clause: Any = None, scope: Any = None, verified_on: Any = None, why: str = "") -> dict[str, Any]:
    return {"result": result, "evidence_url": url if _https(url) else None, "clause": (str(clause)[:400] if clause else None),
            "scope": (str(scope)[:300] if scope else None), "verified_on": (str(verified_on)[:10] if verified_on else None), "why": why}


def _explicit(block: Any) -> dict[str, Any] | None:
    """An explicit, evidenced permission block in a capability record: {"status": "permitted", "evidence_url", "clause", "verified_on"}."""
    if isinstance(block, dict) and block.get("status") == "permitted":
        return block
    return None


def derive(record: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """The permission profile of one platform, computed from its reviewed capability record. Missing evidence never permits."""
    review, api, execution = record.get("review") or {}, record.get("documented_api") or {}, record.get("execution") or {}
    levels, permitted = set(record.get("levels") or []), set(record.get("permitted_automation") or [])
    prohibited = set(record.get("prohibited_automation") or [])
    verified = record.get("last_policy_verified") or review.get("reviewed_on")
    url = api.get("url") or next((u for u in (record.get("policy_urls") or []) if _https(u)), None)
    clause = review.get("automation_clause")
    disabled = None
    if (record.get("country_eligibility") or {}).get("kenya") == "unsupported":
        disabled = "Kenya is listed as unsupported by this platform"
    a: dict[str, dict[str, Any]] = {}

    public_ok = "PUBLIC_DISCOVERY_ALLOWED" in levels and "opportunity_discovery" not in prohibited
    a["public_discovery"] = _entry("permitted", url, clause, api.get("scope") or record.get("public_discovery"), verified) if public_ok else \
        _entry("prohibited" if "AUTOMATION_PROHIBITED" in levels else "unverified", why="No permitted public listing interface is recorded")
    a["policy_monitor"] = _entry("permitted", next((u for u in (record.get("policy_urls") or []) if _https(u)), None), clause or "policy_monitor_only",
                                 "Reads the published eligibility policy page only", verified) if "policy_monitor" in permitted else \
        _entry("unverified", why="No policy monitor is recorded")
    account_ok = "DOCUMENTED_API_ALLOWED" in levels and "api_read" in permitted and bool(record.get("api_account_access"))
    a["account_monitoring"] = _entry("permitted", url, clause, api.get("scope"), verified) if account_ok else \
        _entry("prohibited" if "AUTOMATION_PROHIBITED" in levels or "scrape_logged_in" in prohibited else "unverified",
               why="No documented API for reading your own account is recorded; a signed-in session is not permission")
    source_ok = a["public_discovery"]["result"] == "permitted" or a["account_monitoring"]["result"] == "permitted"
    a["task_preparation"] = _entry("permitted", url, clause, "Local classification and preparation of listings read through the permitted interface", verified) if source_ok else \
        _entry("human_required", why="Nothing is read automatically here: import the task details yourself and they are prepared locally")

    ex_block = _explicit(execution.get("automated_execution"))
    if ex_block:
        a["task_execution"] = _entry("permitted", ex_block.get("evidence_url"), ex_block.get("clause"), ex_block.get("scope"), ex_block.get("verified_on"))
    elif "AUTOMATION_PROHIBITED" in levels:                        # an explicit prohibition was read in the platform's terms
        a["task_execution"] = _entry("prohibited", url, clause, verified_on=verified, why="The terms read prohibit automated access or task completion")
    else:
        a["task_execution"] = _entry("unverified", why="No clause permitting automated or AI-assisted completion has been recorded")
    sub_block = _explicit(execution.get("automated_submission"))
    if sub_block and record.get("evidence_submission") == "automation_permitted":
        a["auto_submission"] = _entry("permitted", sub_block.get("evidence_url"), sub_block.get("clause"), sub_block.get("scope"), sub_block.get("verified_on"))
    elif record.get("evidence_submission") == "human_only" or "automated_evidence_submission" in prohibited:
        a["auto_submission"] = _entry("human_required", url, clause, verified_on=verified, why="Submission is done by you in your own account")
    else:
        a["auto_submission"] = _entry("unverified", why="No clause permitting automated submission has been recorded")

    # Explicit evidence recorded per capability (capability_evidence.json) can grant or remove a permission. Grants need a
    # source, a clause, a verification date and an expiry date in the future; a recorded prohibition always applies.
    evidence = _evidence_for(record.get("platform_id"), today)
    for action in ("public_discovery", "account_monitoring", "task_execution", "auto_submission"):     # order matters: submission last
        ev = evidence.get(action)
        if not ev:
            continue
        if ev["state"] == "prohibited":
            a[action] = _entry("prohibited", ev.get("source_url"), ev.get("notes") or "Recorded prohibition", ev.get("scope"), ev.get("verified_at"),
                               why=ev.get("notes") or "A prohibition is recorded for this capability")
        elif action == "auto_submission" and a["task_execution"]["result"] != "permitted":
            a[action] = {**a[action], "why": "Submission evidence exists, but automated execution is not permitted, so nothing can be submitted automatically"}
        else:
            a[action] = _entry("permitted", ev.get("source_url"), ev.get("clause"), ev.get("scope"), ev.get("verified_at"))
    if a["public_discovery"]["result"] == "permitted" or a["account_monitoring"]["result"] == "permitted":
        a["task_preparation"] = _entry("permitted", url or a["public_discovery"]["evidence_url"] or a["account_monitoring"]["evidence_url"],
                                       clause or a["public_discovery"]["clause"] or a["account_monitoring"]["clause"],
                                       "Local classification and preparation of listings read through the permitted interface",
                                       verified or a["public_discovery"]["verified_on"] or a["account_monitoring"]["verified_on"])
    # "If evidence is missing, block the action": a permitted result needs a source, a clause and a current review date.
    for name, e in a.items():
        if e["result"] != "permitted":
            continue
        missing = [k for k in ("evidence_url", "clause", "verified_on") if not e[k]]
        if missing:
            a[name] = {**e, "result": "unverified", "why": "Evidence incomplete: " + ", ".join(missing)}
        elif not _fresh(e["verified_on"], today):
            a[name] = {**e, "result": "unverified", "why": f"The review of {e['verified_on']} is older than {MAX_EVIDENCE_AGE_DAYS} days: re-verify the terms"}
        elif disabled and name != "policy_monitor":            # watching the published policy is how a country change is noticed
            a[name] = {**e, "result": "prohibited", "why": disabled}
    ok = lambda n: a[n]["result"] == "permitted"                                         # noqa: E731
    level = 0
    if ok("public_discovery") or ok("policy_monitor"):
        level = 1
    if ok("account_monitoring"):
        level = 2
    if ok("task_preparation"):
        level = 3
    if level >= 3 and ok("task_execution"):
        level = 4
    if level >= 4 and ok("auto_submission"):
        level = 5
    return {"platform_id": record.get("platform_id"), "name": record.get("name"), "level": level, "level_label": LEVELS[level],
            "disabled_reason": disabled, "actions": a, "manual_import": True, "reviewed_on": verified,
            "ai_use": (execution.get("ai_assistance") or {}).get("status", "unverified"),
            "record_hash": record_hash(record)}


EVIDENCE_PATH = ROOT / "microtask_monitor" / "capability_evidence.json"
EVIDENCE_SOURCES = ("platform_terms", "api_documentation", "assignment_instructions", "written_permission")
CAPABILITY_TO_ACTION = {"public_discovery": "public_discovery", "authenticated_monitoring": "account_monitoring",
                        "ai_execution": "task_execution", "automatic_submission": "auto_submission"}


def load_evidence(path: Path | None = None) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path or EVIDENCE_PATH).read_text(encoding="utf-8"))
        return [e for e in data.get("evidence", []) if isinstance(e, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def evidence_problems(e: dict[str, Any], today: date | None = None) -> list[str]:
    """Why a recorded grant does not count. A prohibition needs no evidence to apply."""
    today = today or date.today()
    if e.get("state") == "prohibited":
        return []
    out = []
    if e.get("state") != "allowed":
        out.append("state must be allowed or prohibited")
    if e.get("capability") not in CAPABILITY_TO_ACTION:
        out.append(f"capability must be one of {', '.join(CAPABILITY_TO_ACTION)}")
    if not _https(e.get("source_url")):
        out.append("source_url must be an https link to the platform's own terms, docs or written permission")
    if e.get("source_type") not in EVIDENCE_SOURCES:
        out.append(f"source_type must be one of {', '.join(EVIDENCE_SOURCES)}")
    if not str(e.get("clause") or "").strip():
        out.append("the exact clause or wording is required")
    try:
        verified = date.fromisoformat(str(e.get("verified_at"))[:10])
        if verified > today:
            out.append("verified_at is in the future")
    except ValueError:
        out.append("verified_at must be a date")
    try:
        if date.fromisoformat(str(e.get("expires_at"))[:10]) < today:
            out.append("the evidence has expired: re-verify")
    except ValueError:
        out.append("expires_at must be a date (evidence without an expiry is not accepted)")
    return out


def _evidence_for(platform_id: Any, today: date | None = None) -> dict[str, dict[str, Any]]:
    out = {}
    for e in load_evidence():
        if e.get("platform_id") != platform_id or e.get("capability") not in CAPABILITY_TO_ACTION:
            continue
        if evidence_problems(e, today):
            continue
        action = CAPABILITY_TO_ACTION[e["capability"]]
        if e.get("state") == "prohibited" or action not in out:
            out[action] = {**e, "action": action}
    return out


def direct_profile() -> dict[str, Any]:
    """The built-in lane for your own client's work. You are the authority for the work; there is no platform to submit to."""
    today = date.today().isoformat()
    mine = lambda scope: _entry("permitted", None, "user_authority", scope, today)      # noqa: E731
    a = {"public_discovery": _entry("human_required", why="You add these tasks yourself"),
         "policy_monitor": _entry("unverified", why="Not applicable"),
         "account_monitoring": _entry("human_required", why="There is no account to monitor"),
         "task_preparation": mine("Tasks and files you supplied"),
         "task_execution": mine("Your own client's work, with the AI-use rule you recorded for each task"),
         "auto_submission": _entry("human_required", why="You deliver the package to your client yourself")}
    return {"platform_id": DIRECT_LANE, "name": "Direct client work (yours)", "level": 4, "level_label": LEVELS[4], "disabled_reason": None,
            "actions": a, "manual_import": True, "reviewed_on": today, "ai_use": "task_specific", "record_hash": "user_authority"}


def record_hash(record: dict[str, Any]) -> str:
    """Fingerprint of the policy-relevant part of a capability record. A change without a new review date suspends automation."""
    keys = ("levels", "permitted_automation", "prohibited_automation", "application_submission", "evidence_submission", "documented_api",
            "api_account_access", "access_class", "policy_urls", "execution", "country_eligibility")
    return hashlib.sha256(json.dumps({k: record.get(k) for k in keys}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def profile(platform_id: str, caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if platform_id == DIRECT_LANE:
        return direct_profile()
    caps = policy.load_capabilities() if caps is None else caps
    rec = caps.get(platform_id)
    return derive(rec) if rec else None


def known(platform_id: str, caps: dict[str, dict[str, Any]] | None = None) -> bool:
    return platform_id == DIRECT_LANE or platform_id in (policy.load_capabilities() if caps is None else caps)


# ------------------------------------------------------------------------------------------- evidence store
def sync(conn, caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Mirror every platform/action permission result into SQLite with its evidence. Detects records that changed without re-review."""
    caps = policy.load_capabilities() if caps is None else caps
    now, suspended = db.now_iso(), []
    with db.tx(conn):
        for pid, rec in caps.items():
            prof = derive(rec)
            old = {r["action"]: dict(r) for r in conn.execute("SELECT * FROM permission_evidence WHERE platform_id=?", (pid,))}
            prior = next(iter(old.values()), None)
            reason = prior["suspended_reason"] if prior else None
            if prior and prior["record_hash"] and prior["record_hash"] != prof["record_hash"]:
                if str(prof["reviewed_on"] or "") > str(prior["last_reviewed"] or ""):
                    reason = None                                   # a newer dated review replaces the old evidence
                else:
                    reason = "The capability record changed without a new review date: automation is suspended until the terms are re-reviewed"
                    suspended.append(pid)
            for action, e in prof["actions"].items():
                conn.execute("""INSERT INTO permission_evidence(platform_id,action,result,evidence_url,clause,scope,verified_on,last_reviewed,source,record_hash,suspended_reason,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(platform_id,action) DO UPDATE SET result=excluded.result, evidence_url=excluded.evidence_url,
                    clause=excluded.clause, scope=excluded.scope, verified_on=excluded.verified_on, last_reviewed=excluded.last_reviewed, source=excluded.source,
                    record_hash=excluded.record_hash, suspended_reason=excluded.suspended_reason, updated_at=excluded.updated_at""",
                             (pid, action, e["result"], e["evidence_url"], e["clause"] or e["why"], e["scope"], e["verified_on"], prof["reviewed_on"],
                              "capability_record", prof["record_hash"], reason, now))
        if suspended:
            db.audit(conn, "permission.suspended", "platform", ",".join(suspended), {"reason": "record_changed_without_review"}, actor="system")
    return {"platforms": len(caps), "suspended": suspended}


def suspension(conn, platform_id: str) -> str | None:
    if conn is None or platform_id == DIRECT_LANE:
        return None
    r = conn.execute("SELECT suspended_reason FROM permission_evidence WHERE platform_id=? AND suspended_reason IS NOT NULL LIMIT 1", (platform_id,)).fetchone()
    return r[0] if r else None


def suspend(conn, platform_id: str, reason: str, actor: str = "system") -> None:
    """Stop every automated action on one platform (terms changed, a policy violation, repeated failures) until you re-review it."""
    if not known(platform_id) or platform_id == DIRECT_LANE:
        raise policy.PermissionDenied(f"Blocked: no capability record for '{platform_id}'")
    if not conn.execute("SELECT 1 FROM permission_evidence WHERE platform_id=?", (platform_id,)).fetchone():
        sync(conn)
    with db.tx(conn):
        conn.execute("UPDATE permission_evidence SET suspended_reason=?, updated_at=? WHERE platform_id=?", (str(reason)[:300], db.now_iso(), platform_id))
        db.audit(conn, "permission.suspend", "platform", platform_id, {"reason": str(reason)[:300]}, actor=actor)


def clear_suspension(conn, platform_id: str, reviewed_by_user: bool, note: str = "") -> None:
    """Only you can lift a suspension, after reading the platform's current terms yourself."""
    if reviewed_by_user is not True:
        raise policy.PermissionDenied("Blocked: lifting a suspension needs your own confirmation that you re-read the current terms (reviewed_by_user=true)")
    with db.tx(conn):
        conn.execute("UPDATE permission_evidence SET suspended_reason=NULL, last_reviewed=?, updated_at=? WHERE platform_id=?",
                     (date.today().isoformat(), db.now_iso(), platform_id))
        db.audit(conn, "permission.review", "platform", platform_id, {"note": str(note)[:300]})


# ------------------------------------------------------------------------------------------- the gate
def decide(conn, platform_id: str, action: str, task: dict[str, Any] | None = None, settings: dict[str, Any] | None = None,
           caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """May the program itself perform ``action`` on ``platform_id`` (for ``task``, when one is involved)?

    Returns {"allowed", "reasons", "result", "level", "required_level", "evidence"}. Every reason that blocks is listed.
    """
    reasons: list[str] = []
    out = {"platform_id": platform_id, "action": action, "allowed": False, "result": "unverified", "level": 0,
           "required_level": ACTIONS.get(action), "evidence": None, "reasons": reasons, "checked_at": datetime.now(timezone.utc).isoformat()}
    if action not in ACTIONS:
        reasons.append(f"unknown action '{action}' (fail closed)")
        return out
    prof = profile(platform_id, caps)
    if prof is None:
        reasons.append(f"no capability record for '{platform_id}' (fail closed)")
        return out
    entry = prof["actions"][action]
    out.update(result=entry["result"], level=prof["level"], evidence={k: entry[k] for k in ("evidence_url", "clause", "scope", "verified_on")})
    if conn is not None and (db.get_state(conn, "kill_switch", {}) or {}).get("engaged"):
        reasons.append("the global stop is engaged")
    if prof["disabled_reason"] and action != "policy_monitor":
        reasons.append(prof["disabled_reason"])
    held = suspension(conn, platform_id)
    if held:
        reasons.append(held)
    if entry["result"] != "permitted":
        reasons.append(f"{action} is {entry['result'].replace('_', ' ')} for {prof['name'] or platform_id}: {entry['why'] or 'no evidence recorded'}")
    if task is not None and action in ("task_execution", "auto_submission"):
        text = " ".join(str(task.get(k) or "") for k in ("title", "instructions"))
        for hit in hard_restrictions(text):
            reasons.append(f"never automated: {hit['label']} (\"{hit['evidence']}\")")
        ai = task.get("ai_use") or "unknown"
        if ai == "human_only":
            reasons.append("this task must be done by you personally")
        elif ai == "prohibited" and task.get("needs_ai", True):
            reasons.append("the task instructions prohibit AI or automation")
        elif ai == "unknown" and task.get("needs_ai", True):
            reasons.append("the AI-use rule for this task has not been established")
        if action == "auto_submission" and not (settings or {}).get("auto_submit_platforms", {}).get(platform_id):
            reasons.append("you have not pre-authorized automatic submission for this platform")
    if settings is not None and action == "task_execution" and not settings.get("auto_execute", False):
        reasons.append("automatic execution is switched off in your automation settings")
    out["allowed"] = not reasons
    return out


def require(conn, platform_id: str, action: str, **kw: Any) -> dict[str, Any]:
    d = decide(conn, platform_id, action, **kw)
    if not d["allowed"]:
        raise policy.PermissionDenied("Blocked: " + "; ".join(d["reasons"]))
    return d


def matrix(conn=None, caps: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One row per registered platform for the dashboard and the capability report."""
    caps = policy.load_capabilities() if caps is None else caps
    rows = []
    for pid, rec in caps.items():
        p = derive(rec)
        rows.append({"platform_id": pid, "name": p["name"], "level": p["level"], "level_label": p["level_label"], "disabled_reason": p["disabled_reason"],
                     "suspended_reason": suspension(conn, pid), "reviewed_on": p["reviewed_on"], "ai_use": p["ai_use"],
                     "official_api": bool((rec.get("documented_api") or {}).get("available")),
                     "actions": {k: {"result": v["result"], "evidence_url": v["evidence_url"], "clause": v["clause"], "why": v["why"], "verified_on": v["verified_on"]}
                                 for k, v in p["actions"].items()}})
    return rows
