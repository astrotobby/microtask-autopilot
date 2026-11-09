"""One-click application packets.

A packet bundles everything you need to apply in one place: the opportunity and its evidence, the tailored
draft (plus a version with your real profile answers filled in), relevant skills, reusable profile answers and
platform-specific instructions. It never contacts a platform and never submits anything: you copy it and
apply yourself. Placeholders are only ever filled from answers YOU wrote; anything missing is a blocker.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from microtask_monitor import assistant, db, guidance, intelligence as ig, policy, profile_answers as pa
except ImportError:  # pragma: no cover
    import assistant, db, guidance, intelligence as ig, policy, profile_answers as pa

FOLLOWUP_DAYS = 7
PLACEHOLDER_RX = re.compile(r"\[[^\[\]]{3,200}\]")
# Placeholders produced by assistant.draft_application, and the profile answers that may fill them.
FILLERS = {
    "[Your name]": ("full_name",),
    "[Contact details]": ("contact_email",),
    "[Your availability: hours per week and start date.]": ("availability",),
    "[Add one or two sentences about a real, relevant project you completed, with a link.]": ("experience_summary", "portfolio_links"),
}


class PacketError(ValueError):
    pass


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) and value else (value or default)
    except ValueError:
        return default


def fill_draft(body: str, answers: dict[str, str]) -> tuple[str, list[str]]:
    """Fill known placeholders from the user's own answers. Returns (text, placeholders still unresolved)."""
    text = body
    for placeholder, keys in FILLERS.items():
        parts = [answers.get(k, "").strip() for k in keys if answers.get(k, "").strip()]
        if placeholder in text and parts:
            text = text.replace(placeholder, " ".join(parts))
    return text, sorted(set(PLACEHOLDER_RX.findall(text)))


def build_packet(conn, application_id: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    app = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
    if not app:
        raise PacketError("Application not found")
    app = dict(app)
    opp = conn.execute("SELECT * FROM opportunities WHERE url=?", (app["opportunity_url"],)).fetchone() if app["opportunity_url"] else None
    opp = dict(opp) if opp else {}
    draft = conn.execute("SELECT * FROM application_drafts WHERE application_id=? ORDER BY approved DESC, version DESC LIMIT 1", (application_id,)).fetchone()
    draft = dict(draft) if draft else None
    pa.ensure_defaults(conn)
    answers = pa.as_map(conn)
    answer_rows = pa.list_answers(conn)
    pid = app.get("platform_id")
    caps = policy.load_capabilities()
    pv = (caps.get(pid or "", {}).get("payment_verification") or {})
    breakdown = _loads(opp.get("score_breakdown_json"), {})
    req = _loads(opp.get("requirements_json"), {})
    raw = _loads(opp.get("raw_json"), {})
    skills = assistant.relevant_skills(opp | raw, ig.load_profile()) if opp else []
    g = guidance.for_platform(pid)
    assessments = sorted(set(req.get("application_requirements") or []) & {"assessment", "interview", "skills test", "background check", "identity verification", "KYC/identity check"})
    filled, unresolved = fill_draft(draft["body"], answers) if draft else ("", [])

    blockers: list[str] = []
    cls = opp.get("eligibility_class")
    if opp and cls in (ig.KENYA_UNSUPPORTED, ig.EXPIRED):
        blockers.append(f"Do not apply: this opportunity is {cls}.")
    if opp and cls not in (ig.KENYA_CONFIRMED, ig.WORLDWIDE_CONFIRMED, ig.KENYA_UNSUPPORTED, ig.EXPIRED, None):
        blockers.append("Kenya eligibility is not confirmed: verify it on the original page first.")
    for a in pa.missing_required(conn):
        blockers.append(f"Fill in your profile answer: {a['question']}")
    if unresolved:
        blockers.append("The draft still has placeholders you must complete: " + "; ".join(unresolved))
    if draft is None:
        blockers.append("There is no draft yet.")
    elif not draft["approved"]:
        blockers.append("Approve the draft (approval never sends anything).")
    if (opp.get("deadline") and ig.oc.parse_time(opp.get("deadline")) and ig.oc.parse_time(opp["deadline"]) < now):
        blockers.append("The deadline has passed.")

    packet = {
        "generated_at": now.isoformat(),
        "application": {k: app.get(k) for k in ("id", "title", "status", "apply_url", "created_at", "submitted_at", "next_followup_at", "interview_at")},
        "opportunity": {
            "title": opp.get("title") or app["title"], "platform_id": pid, "source": opp.get("source"), "url": app["opportunity_url"],
            "apply_url": app.get("apply_url") or opp.get("apply_url") or app["opportunity_url"],
            "compensation": opp.get("payment_text") or "Not disclosed",
            "payment_method": pv.get("note") if pv.get("status") == "documented" else "not verified",
            "eligibility": {"class": cls, "confidence": opp.get("eligibility_confidence"), "reason": opp.get("eligibility_reason")},
            "deadline": opp.get("deadline"), "last_verified": opp.get("last_verified_utc"), "score": opp.get("relevance_score"),
            "requirements": req, "assessments": assessments, "summary": (opp.get("summary") or "")[:600],
            "qualification_gaps": breakdown.get("qualification_gaps") or [],
        },
        "skills": skills, "draft": ({"id": draft["id"], "version": draft["version"], "approved": bool(draft["approved"]), "body": draft["body"],
                                      "missing_info": _loads(draft.get("missing_info_json"), [])} if draft else None),
        "draft_filled": filled, "unresolved_placeholders": unresolved,
        "profile_answers": [{"key": a["key"], "question": a["question"], "answer": a["answer"] or "", "required": a["required"], "needs_input": a["needs_input"]}
                            for a in answer_rows],
        "instructions": {"label": g["label"], "verified": g["verified"], "verified_on": g["verified_on"], "sources": g["sources"],
                         "steps": g["apply_steps"], "assessments": g["assessments"], "after_applying": g["after_applying"],
                         "common_mistakes": g["common_mistakes"]},
        "follow_up_plan": [f"After you submit, record it here; a follow-up check is scheduled {FOLLOWUP_DAYS} days later.",
                           "Record any response, interview date and decision so reminders stay accurate."],
        "blockers": blockers, "ready_to_apply": not blockers,
    }
    return packet


def text_export(p: dict[str, Any]) -> str:
    """Plain-text version for pasting into notes or a chat. Contains only what is in the packet."""
    o, a = p["opportunity"], p["application"]
    L = [f"APPLICATION PACKET: {o['title']}", "=" * 60, f"Platform: {o.get('source') or o.get('platform_id')}", f"Original link: {o['apply_url']}",
         f"Compensation: {o['compensation']}  (payment method: {o['payment_method']})",
         f"Eligibility: {o['eligibility']['class']} ({o['eligibility'].get('confidence')}): {o['eligibility'].get('reason')}",
         f"Deadline: {o['deadline'] or 'none stated'}   Last verified: {o['last_verified'] or 'unknown'}   Score: {o['score']}",
         f"Skills matched: {', '.join(p['skills']) or 'none'}", f"Assessments / interviews mentioned: {', '.join(o['assessments']) or 'none'}", ""]
    if o["qualification_gaps"]:
        L += ["CONFIRM BEFORE APPLYING (not established in your profile):"] + [f"  - {g}" for g in o["qualification_gaps"]] + [""]
    L += ["STATUS: " + ("READY TO APPLY" if p["ready_to_apply"] else "NOT READY"), ""]
    if p["blockers"]:
        L += ["BLOCKERS:"] + [f"  - {b}" for b in p["blockers"]] + [""]
    L += ["--- DRAFT (with your profile answers filled in) ---", p["draft_filled"] or "(no draft)", "",
          "--- YOUR PROFILE ANSWERS ---"]
    for ans in p["profile_answers"]:
        if ans["answer"]:
            L.append(f"{ans['question']}: {ans['answer']}")
    L += ["", f"--- HOW TO APPLY ({p['instructions']['label']}) ---"] + [f"{i}. {s}" for i, s in enumerate(p["instructions"]["steps"], 1)]
    if p["instructions"]["assessments"]:
        L += ["", "Assessments / interviews:"] + [f"  - {s}" for s in p["instructions"]["assessments"]]
    L += ["", "After applying:"] + [f"  - {s}" for s in p["instructions"]["after_applying"]]
    L += ["", "Common mistakes:"] + [f"  - {s}" for s in p["instructions"]["common_mistakes"]]
    L += ["", "Nothing here was submitted for you. Apply yourself, then record the date in the dashboard."]
    return "\n".join(L)


def save_packet(conn, application_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Build and persist a new packet version (snapshot). Safe to call repeatedly: each call adds a version."""
    now = now or datetime.now(timezone.utc)
    p = build_packet(conn, application_id, now)
    text = text_export(p)
    with db.tx(conn):
        version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM application_packets WHERE application_id=?", (application_id,)).fetchone()[0]
        cur = conn.execute("INSERT INTO application_packets(application_id,version,generated_at,ready,content_json,text_export) VALUES(?,?,?,?,?,?)",
                           (application_id, version, now.isoformat(), 1 if p["ready_to_apply"] else 0, json.dumps(p, ensure_ascii=False), text))
        db.audit(conn, "packet.generate", "application", application_id, {"version": version, "ready": p["ready_to_apply"]}, actor="system")
    return {**p, "packet_id": cur.lastrowid, "version": version, "text": text}


def latest_packet(conn, application_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM application_packets WHERE application_id=? ORDER BY version DESC LIMIT 1", (application_id,)).fetchone()
    if not row:
        return None
    return {**json.loads(row["content_json"]), "packet_id": row["id"], "version": row["version"], "text": row["text_export"]}
