"""Persistent reminders: deadlines, assessments, follow-ups, interviews, stale drafts, tasks and profile gaps.

Reminders are generated from your own records (opportunity deadlines, application status and dates). They are
stored in SQLite with a unique fingerprint, so a reminder is created once, survives restarts, and a reminder you
marked done or dismissed never comes back. Notifications for due reminders go through the deduplicated notify layer.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from microtask_monitor import db, notify, opportunity_common as oc, profile_answers as pa
except ImportError:  # pragma: no cover
    import db, notify, opportunity_common as oc, profile_answers as pa

DEADLINE_MILESTONES = (7, 3, 1)         # days before the deadline
DRAFT_REVIEW_AFTER_DAYS = 2
MAX_NOTIFIED_PER_CYCLE = 5
ASSESSMENT_LABELS = {"assessment", "interview", "skills test", "background check", "identity verification", "KYC/identity check"}


def _fp(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) and value else (value or default)
    except ValueError:
        return default


def _dt(value: str | None) -> datetime | None:
    return oc.parse_time(value) if value and "T" in value else (oc.parse_time(value + "T09:00:00+03:00") if value else None)


def _add(conn, now: datetime, kind: str, key: str, title: str, detail: str, due: datetime, ref_type: str, ref_id: Any, url: str | None) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO reminders(fingerprint,kind,title,detail,due_at,ref_type,ref_id,url,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (_fp(kind, ref_type, ref_id, key), kind, title[:200], detail[:500], due.isoformat(), ref_type, str(ref_id), url, now.isoformat()))
    return cur.rowcount > 0


def generate(conn, now: datetime | None = None) -> dict[str, int]:
    """Create any reminders that are now warranted. Idempotent; safe to call after every sync or analysis."""
    now = now or datetime.now(timezone.utc)
    made = {k: 0 for k in ("deadline", "assessment", "followup", "interview", "draft_review", "task", "profile", "decision")}
    apps = [dict(r) for r in conn.execute("SELECT * FROM applications WHERE status NOT IN ('rejected','withdrawn')")]
    for a in apps:
        opp = conn.execute("SELECT * FROM opportunities WHERE url=?", (a["opportunity_url"],)).fetchone() if a["opportunity_url"] else None
        opp = dict(opp) if opp else {}
        link = a.get("apply_url") or opp.get("apply_url") or a.get("opportunity_url")
        req = _loads(opp.get("requirements_json"), {})
        assessments = sorted(set(req.get("application_requirements") or []) & ASSESSMENT_LABELS)
        deadline = oc.parse_time(opp.get("deadline"))
        pre_apply = a["status"] in ("draft", "approved")
        if pre_apply and deadline and deadline > now:
            for n in DEADLINE_MILESTONES:
                due = deadline - timedelta(days=n)
                if due + timedelta(days=1) >= now:        # skip milestones that are already stale
                    made["deadline"] += _add(conn, now, "deadline", f"{n}d", f"Deadline within {n} day{'s' if n > 1 else ''}: {a['title']}",
                                             f"Apply before {deadline.date().isoformat()}. Review the packet, apply yourself, then record it.", due, "application", a["id"], link)
        if assessments and pre_apply:
            made["assessment"] += _add(conn, now, "assessment", "prep", f"Plan time for {', '.join(assessments)}: {a['title']}",
                                       "The listing mentions it. Read the platform guidance in the packet and set aside uninterrupted time.", now, "application", a["id"], link)
        if pre_apply and not conn.execute("SELECT 1 FROM application_drafts WHERE application_id=? AND approved=1", (a["id"],)).fetchone():
            created = _dt(a["created_at"]) or now
            if now - created >= timedelta(days=DRAFT_REVIEW_AFTER_DAYS):
                made["draft_review"] += _add(conn, now, "draft_review", "stale", f"Review the draft: {a['title']}",
                                             "Prepared a while ago and still waiting for your review and approval.", created + timedelta(days=DRAFT_REVIEW_AFTER_DAYS), "application", a["id"], link)
        if a["status"] == "submitted":
            if a.get("next_followup_at"):
                due = _dt(a["next_followup_at"])
                if due:
                    made["followup"] += _add(conn, now, "followup", str(a["next_followup_at"])[:10], f"Check status / follow up: {a['title']}",
                                             "No response recorded yet. Check the platform and your email, then record what you find.", due, "application", a["id"], link)
            if assessments and a.get("submitted_at"):
                due = _dt(a["submitted_at"]) + timedelta(days=1)
                made["assessment"] += _add(conn, now, "assessment", "invite", f"Look for an assessment/interview invite: {a['title']}",
                                           f"The listing mentions: {', '.join(assessments)}.", due, "application", a["id"], link)
        if a["status"] == "interview":
            if a.get("interview_at"):
                iv = _dt(a["interview_at"])
                if iv:
                    made["interview"] += _add(conn, now, "interview", "eve", f"Interview tomorrow: {a['title']}", "Re-read the listing, your profile answers and the platform's interview notes.", iv - timedelta(days=1), "application", a["id"], link)
                    made["interview"] += _add(conn, now, "interview", "day", f"Interview today: {a['title']}", "Quiet room, working microphone, stable connection, no VPN.", iv - timedelta(hours=2), "application", a["id"], link)
            else:
                made["interview"] += _add(conn, now, "interview", "schedule", f"Confirm the interview time: {a['title']}", "Record the date once it is agreed.", now, "application", a["id"], link)
            if a.get("next_followup_at") and _dt(a["next_followup_at"]):
                made["followup"] += _add(conn, now, "followup", str(a["next_followup_at"])[:10], f"Follow up after interview: {a['title']}", "Check for a decision and record it.", _dt(a["next_followup_at"]), "application", a["id"], link)
        if a["status"] == "accepted":
            made["decision"] += _add(conn, now, "decision", "start", f"Accepted: start the work for {a['title']}", "Open the platform's project instructions and create a task workspace here.", now, "application", a["id"], link)
    for t in conn.execute("SELECT * FROM tasks WHERE status IN ('accepted','in_progress','completed') AND due_at IS NOT NULL"):
        due = _dt(t["due_at"])
        if due:
            made["task"] += _add(conn, now, "task", str(t["due_at"])[:10], f"Task due soon: {t['title']}", "Finish the checklist and quality checks, then submit it yourself.", due - timedelta(days=1), "task", t["id"], None)
    missing = pa.missing_required(conn)
    if missing:
        made["profile"] += _add(conn, now, "profile", ",".join(sorted(m["key"] for m in missing)), "Complete your profile answers",
                                "Missing: " + "; ".join(m["question"] for m in missing[:6]) + ". Application packets cannot be ready without them.", now, "profile", "answers", None)
    resolve_stale(conn, now)
    return made


def resolve_stale(conn, now: datetime) -> int:
    """Dismiss pending reminders whose application has moved on (so you never see an outdated nag)."""
    n = 0
    for r in conn.execute("SELECT id,kind,ref_type,ref_id FROM reminders WHERE status='pending' AND ref_type='application'").fetchall():
        status = (conn.execute("SELECT status FROM applications WHERE id=?", (r["ref_id"],)).fetchone() or [None])[0]
        stale = (status is None or status in ("rejected", "withdrawn")
                 or (r["kind"] in ("deadline", "draft_review") and status not in ("draft", "approved"))
                 or (r["kind"] == "followup" and status not in ("submitted", "interview"))
                 or (r["kind"] == "interview" and status != "interview"))
        if stale:
            conn.execute("UPDATE reminders SET status='dismissed', done_at=? WHERE id=?", (now.isoformat(), r["id"]))
            n += 1
    for r in conn.execute("SELECT id FROM reminders WHERE status='pending' AND kind='profile'").fetchall():
        if not pa.missing_required(conn):
            conn.execute("UPDATE reminders SET status='done', done_at=? WHERE id=?", (now.isoformat(), r["id"]))
            n += 1
    return n


def list_reminders(conn, now: datetime | None = None, upcoming_days: int = 7, include_closed: bool = False) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    horizon = (now + timedelta(days=upcoming_days)).isoformat()
    sql = "SELECT * FROM reminders WHERE " + ("1=1" if include_closed else "status='pending' AND due_at<=?") + " ORDER BY due_at"
    rows = db.rows(conn.execute(sql, () if include_closed else (horizon,)))
    for r in rows:
        r["is_due"] = r["status"] == "pending" and r["due_at"] <= now.isoformat()
    return rows


def due_reminders(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    return [r for r in list_reminders(conn, now, 0) if r["is_due"]]


def mark(conn, reminder_id: int, status: str) -> dict[str, Any]:
    if status not in ("done", "dismissed", "pending"):
        raise ValueError("status must be done, dismissed or pending")
    row = conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
    if not row:
        raise LookupError("Reminder not found")
    with db.tx(conn):
        conn.execute("UPDATE reminders SET status=?, done_at=? WHERE id=?", (status, db.now_iso() if status != "pending" else None, reminder_id))
        db.audit(conn, "reminder." + status, "reminder", reminder_id)
    return dict(conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone())


def notify_due(conn, now: datetime | None = None, settings=None, channels=None) -> list[dict[str, Any]]:
    """Notify (once each, a few per cycle) about reminders that have become due."""
    now = now or datetime.now(timezone.utc)
    made = []
    pending = [r for r in due_reminders(conn, now) if not r.get("notified_at")]
    pending.sort(key=lambda r: ({"deadline": 0, "interview": 1, "followup": 2}.get(r["kind"], 3), r["due_at"]))
    for r in pending[:MAX_NOTIFIED_PER_CYCLE]:
        urgent = r["kind"] in ("deadline", "interview")
        with db.tx(conn):
            res = notify.create(conn, "reminder", r["title"], r["detail"] or "", r["url"], "urgent" if urgent else "important", key=r["fingerprint"],
                                settings=settings, channels=channels)
            conn.execute("UPDATE reminders SET notified_at=? WHERE id=?", (now.isoformat(), r["id"]))
        if res:
            made.append(res)
    return made
