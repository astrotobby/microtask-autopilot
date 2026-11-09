"""The daily action list: the few most important things YOU should do next, in priority order.

Built from your own records (applications, packets, reminders, tasks, today's shortlist, profile gaps) and saved per
Nairobi day in SQLite so ticks persist. Nothing here performs any action: each item tells you what to do and where.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from microtask_monitor import daily, db, policy, profile_answers as pa, reminders as rem
except ImportError:  # pragma: no cover
    import daily, db, policy, profile_answers as pa, reminders as rem

MAX_ACTIONS = 12
# Platforms with no permitted automated discovery that are worth a manual look, in the user's research order.
MANUAL_CHECK_ORDER = ["afterquery", "braintrust", "clickworker", "handshake_ai", "microworkers", "rapidworkers", "sproutgigs", "gainworkers", "reworkio"]
MANUAL_CHECKS_PER_DAY = 2


def _action(key: str, title: str, detail: str, kind: str, priority: int, minutes: int, target: str, url: str | None = None, **ref: Any) -> dict[str, Any]:
    return {"key": key, "title": title, "detail": detail, "kind": kind, "priority": priority, "est_minutes": minutes,
            "target": target, "url": url, "ref": ref}


def build(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    """Compute today's candidate actions (pure read; nothing is saved here)."""
    now = now or datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    pa.ensure_defaults(conn)
    missing = pa.missing_required(conn)
    if missing:
        items.append(_action("profile", "Fill in your profile answers",
                             "Application packets cannot be ready until you add: " + "; ".join(m["question"] for m in missing[:5]) +
                             ". Only write things that are true. Never enter passwords or ID numbers.", "profile", 96, 10, "applications"))
    for r in rem.due_reminders(conn, now):
        if r["kind"] == "profile":                 # already covered by the single "profile" action above
            continue
        base = {"deadline": 92, "interview": 90, "followup": 78, "assessment": 74, "draft_review": 70, "task": 88, "decision": 86}.get(r["kind"], 60)
        items.append(_action(f"reminder:{r['id']}", r["title"], r["detail"] or "", "reminder:" + r["kind"], base, 10,
                             "tasks" if r["ref_type"] == "task" else "applications", r["url"], reminder_id=r["id"], app_id=r["ref_id"] if r["ref_type"] == "application" else None))
    latest = daily.load_shortlist(conn)
    top_rank = {e["url"]: i for i, e in enumerate(latest.get("top5", []), 1)}
    for a in conn.execute("SELECT * FROM applications WHERE status IN ('draft','approved')"):
        rank = top_rank.get(a["opportunity_url"])
        has_approved = conn.execute("SELECT 1 FROM application_drafts WHERE application_id=? AND approved=1", (a["id"],)).fetchone()
        if has_approved:
            items.append(_action(f"apply:{a['id']}", f"Apply on the platform: {a['title']}",
                                 "Open the packet, copy the approved draft, apply in your own account, then record the date here.", "apply", 85 - (rank or 5), 25,
                                 "applications", a["apply_url"], app_id=a["id"]))
        elif rank:
            items.append(_action(f"review:{a['id']}", f"Review and approve the draft: {a['title']}",
                                 f"Today's #{rank} opportunity. Open the packet, check every claim is true, fill placeholders, then approve (approval sends nothing).", "review", 82 - rank, 12,
                                 "applications", a["apply_url"], app_id=a["id"]))
    for a in conn.execute("SELECT a.* FROM applications a WHERE a.status='accepted' AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.application_id=a.id)"):
        items.append(_action(f"startwork:{a['id']}", f"Set up the work for: {a['title']}", "Open the platform's project instructions, then create a task workspace here for guidance and checklists.",
                             "task", 84, 15, "tasks", a["apply_url"], app_id=a["id"]))
    for t in conn.execute("SELECT * FROM tasks WHERE status IN ('accepted','in_progress') ORDER BY COALESCE(due_at,'9999')"):
        soon = t["due_at"] and (datetime.fromisoformat(t["due_at"][:10] + "T00:00:00+03:00") - now) <= timedelta(days=2)
        items.append(_action(f"task:{t['id']}", f"Work on: {t['title']}", "Follow the checklist, then run the quality checks before you submit it yourself.", "task", 89 if soon else 72, 45, "tasks", task_id=t["id"]))
    for t in conn.execute("SELECT * FROM tasks WHERE status='submitted' AND updated_at<?", ((now - timedelta(days=7)).isoformat(),)):
        items.append(_action(f"paycheck:{t['id']}", f"Check whether you were paid: {t['title']}",
                             "Submitted over a week ago. If money arrived, record it under Earnings; if not, check the platform's payment status.", "payment", 62, 5, "earnings", task_id=t["id"]))
    fresh = [e for e in latest.get("top5", []) if e.get("review_status") == "new"]
    if fresh:
        items.append(_action("review-top5", f"Review today's top five ({len(fresh)} not yet reviewed)",
                             "Open each original link, confirm Kenya eligibility and requirements on the platform, and mark each reviewed, shortlisted or rejected.", "review", 78, 15, "daily"))
    caps = policy.load_capabilities()
    candidates = [p for p in MANUAL_CHECK_ORDER if caps.get(p, {}).get("country_eligibility", {}).get("kenya") != "unsupported"]
    if candidates:
        start = (now.date().toordinal()) % len(candidates)
        for i in range(MANUAL_CHECKS_PER_DAY):
            pid = candidates[(start + i) % len(candidates)]
            c = caps.get(pid, {})
            verified = bool(c.get("last_policy_verified"))
            items.append(_action(f"manualcheck:{pid}", f"Check {c.get('name') or pid.replace('_', ' ').title()} yourself for suitable roles",
                                 "No permitted automated discovery here: look for roles matching your skills and note any worth adding. " +
                                 ("Terms reviewed " + c["last_policy_verified"] + "." if verified else "Its worker terms are UNVERIFIED: read them first."),
                                 "manual-check", 40 - i, 15, "platforms", (c.get("policy_urls") or [None])[0], platform_id=pid))
    return items


def persist_today(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    """Save today's list (Nairobi date). Existing ticks are preserved; items that no longer apply are removed."""
    now = now or datetime.now(timezone.utc)
    day = daily.analysis_date(now)
    built = build(conn, now)
    keys = {a["key"] for a in built}
    stamp = now.isoformat()
    with db.tx(conn):
        for a in built:
            conn.execute("""INSERT INTO action_items(action_date,key,title,detail,kind,priority,est_minutes,target,url,ref_json,status,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)
                            ON CONFLICT(action_date,key) DO UPDATE SET title=excluded.title, detail=excluded.detail, priority=excluded.priority,
                              est_minutes=excluded.est_minutes, target=excluded.target, url=excluded.url, ref_json=excluded.ref_json, updated_at=excluded.updated_at
                            WHERE action_items.status='open'""",
                         (day, a["key"], a["title"][:200], a["detail"][:600], a["kind"], a["priority"], a["est_minutes"], a["target"], a["url"],
                          json.dumps(a["ref"]), stamp, stamp))
        for r in conn.execute("SELECT id,key FROM action_items WHERE action_date=? AND status='open'", (day,)).fetchall():
            if r["key"] not in keys:
                conn.execute("DELETE FROM action_items WHERE id=?", (r["id"],))
    return list_today(conn, now)


def list_today(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    day = daily.analysis_date(now)
    rows = db.rows(conn.execute("SELECT * FROM action_items WHERE action_date=? ORDER BY (status<>'open'), priority DESC, est_minutes, id LIMIT ?", (day, MAX_ACTIONS * 2)))
    for r in rows:
        r["ref"] = json.loads(r.pop("ref_json") or "{}")
    open_rows = [r for r in rows if r["status"] == "open"][:MAX_ACTIONS]
    return open_rows + [r for r in rows if r["status"] != "open"]


def set_status(conn, action_id: int, status: str) -> dict[str, Any]:
    if status not in ("open", "done", "skipped"):
        raise ValueError("status must be open, done or skipped")
    row = conn.execute("SELECT * FROM action_items WHERE id=?", (action_id,)).fetchone()
    if not row:
        raise LookupError("Action not found")
    with db.tx(conn):
        conn.execute("UPDATE action_items SET status=?, updated_at=? WHERE id=?", (status, db.now_iso(), action_id))
        if row["key"].startswith("reminder:") and status in ("done", "skipped"):          # ticking a reminder action closes the reminder
            rid = int(row["key"].split(":", 1)[1])
            conn.execute("UPDATE reminders SET status=?, done_at=? WHERE id=?", ("done" if status == "done" else "dismissed", db.now_iso(), rid))
    return dict(conn.execute("SELECT * FROM action_items WHERE id=?", (action_id,)).fetchone())


def summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    open_items = [i for i in items if i["status"] == "open"]
    return {"open": len(open_items), "done": sum(1 for i in items if i["status"] == "done"),
            "estimated_minutes": sum(i["est_minutes"] or 0 for i in open_items)}
