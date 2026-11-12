"""Applications, tasks, evidence and earnings: persistent records with human-approval gates.

This module never contacts a platform. It records what YOU do. Key rules enforced here:
  * an application can be marked submitted only after YOU approved a draft;
  * a draft for a Kenya-unsupported or expired opportunity cannot be created;
  * a task submission needs your explicit confirmation;
  * advertised/expected pay can never become 'received' - only a recorded payment can.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import assistant, db, guidance, intelligence as ig, packets, policy
except ImportError:  # pragma: no cover
    import assistant, db, guidance, intelligence as ig, packets, policy

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "data" / "evidence"
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
FOLLOWUP_DAYS = 7
TASK_FLOW = {"accepted": {"in_progress", "completed"}, "in_progress": {"completed", "accepted"},
             "completed": {"submitted", "in_progress"}, "submitted": {"approved", "rejected"},
             "approved": {"paid"}, "rejected": set(), "paid": set()}


class WorkflowError(ValueError):
    """A request that breaks a rule (not a bug): shown to the user as a 4xx message."""


class Blocked(WorkflowError):
    pass


def _one(conn, sql: str, args: tuple = ()) -> dict[str, Any] | None:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def _require(row, what: str):
    if row is None:
        raise WorkflowError(f"{what} not found")
    return row


def _day(value: str | None, field: str) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        raise WorkflowError(f"{field} must be a date (YYYY-MM-DD)")


# ---------------------------------------------------------------- applications -----------------------
def create_application(conn, opportunity_url: str, provider=None, profile: dict | None = None) -> dict[str, Any]:
    opp = _require(_one(conn, "SELECT * FROM opportunities WHERE url=?", (ig.canonical_url(opportunity_url),)), "Opportunity")
    policy.require(opp.get("platform_id") or "", "assist_application")
    if opp["eligibility_class"] in (ig.KENYA_UNSUPPORTED, ig.EXPIRED):
        raise Blocked(f"Not applying: {opp['eligibility_reason'] or opp['eligibility_class']}")
    open_app = _one(conn, "SELECT id FROM applications WHERE opportunity_url=? AND status NOT IN ('rejected','withdrawn')", (opp["url"],))
    if open_app:
        raise WorkflowError(f"An application already exists for this opportunity (#{open_app['id']})")
    now = db.now_iso()
    draft = assistant.draft_application(opp | json.loads(opp["raw_json"] or "{}") | {"eligibility_class": opp["eligibility_class"]},
                                        profile if profile is not None else ig.load_profile(), provider)
    with db.tx(conn):
        cur = conn.execute("INSERT INTO applications(opportunity_url,platform_id,title,apply_url,status,created_at,updated_at) "
                           "VALUES(?,?,?,?, 'draft', ?, ?)", (opp["url"], opp["platform_id"], opp["title"], opp["apply_url"] or opp["url"], now, now))
        app_id = cur.lastrowid
        _insert_draft(conn, app_id, draft, 1, now)
        db.audit(conn, "application.create", "application", app_id, {"opportunity": opp["url"]})
    return get_application(conn, app_id)


def _insert_draft(conn, app_id: int, draft: dict[str, Any], version: int, now: str) -> int:
    cur = conn.execute("INSERT INTO application_drafts(application_id,version,summary,body,missing_info_json,eligibility_note,provider,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (app_id, version, draft["summary"], draft["body"],
                                                   json.dumps(draft["missing_info"]), draft["eligibility_note"], draft["provider"], now))
    return cur.lastrowid


def regenerate_draft(conn, app_id: int, provider=None) -> dict[str, Any]:
    app = _require(_one(conn, "SELECT * FROM applications WHERE id=?", (app_id,)), "Application")
    if app["status"] not in ("draft", "approved"):
        raise WorkflowError("Only draft or approved applications can get a new draft")
    opp = _require(_one(conn, "SELECT * FROM opportunities WHERE url=?", (app["opportunity_url"],)), "Opportunity")
    draft = assistant.draft_application(opp | json.loads(opp["raw_json"] or "{}") | {"eligibility_class": opp["eligibility_class"]},
                                        ig.load_profile(), provider)
    version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM application_drafts WHERE application_id=?", (app_id,)).fetchone()[0]
    with db.tx(conn):
        _insert_draft(conn, app_id, draft, version, db.now_iso())
        conn.execute("UPDATE applications SET status='draft', updated_at=? WHERE id=?", (db.now_iso(), app_id))
        db.audit(conn, "draft.regenerate", "application", app_id, {"version": version})
    return get_application(conn, app_id)


def edit_draft(conn, draft_id: int, body: str) -> dict[str, Any]:
    d = _require(_one(conn, "SELECT * FROM application_drafts WHERE id=?", (draft_id,)), "Draft")
    if not body or not body.strip():
        raise WorkflowError("Draft text cannot be empty")
    with db.tx(conn):
        conn.execute("UPDATE application_drafts SET body=?, approved=0, approved_at=NULL, provider=? WHERE id=?",
                     (body[:20000], "user-edited", draft_id))
        conn.execute("UPDATE applications SET status='draft', updated_at=? WHERE id=? AND status='approved'", (db.now_iso(), d["application_id"]))
        db.audit(conn, "draft.edit", "draft", draft_id)
    return get_application(conn, d["application_id"])


def approve_draft(conn, draft_id: int, approved_by_user: bool) -> dict[str, Any]:
    if approved_by_user is not True:
        raise WorkflowError("Approval must be given explicitly by you (approved_by_user=true)")
    d = _require(_one(conn, "SELECT * FROM application_drafts WHERE id=?", (draft_id,)), "Draft")
    now = db.now_iso()
    with db.tx(conn):
        conn.execute("UPDATE application_drafts SET approved=0, approved_at=NULL WHERE application_id=?", (d["application_id"],))
        conn.execute("UPDATE application_drafts SET approved=1, approved_at=? WHERE id=?", (now, draft_id))
        conn.execute("UPDATE applications SET status='approved', updated_at=? WHERE id=? AND status IN ('draft','approved')",
                     (now, d["application_id"]))
        db.audit(conn, "draft.approve", "draft", draft_id, actor="user")
    return get_application(conn, d["application_id"])


def mark_submitted(conn, app_id: int, submitted_at: str | None = None, confirmed_by_user: bool = False) -> dict[str, Any]:
    """Record that YOU submitted the application on the platform. Nothing is submitted by this program."""
    if confirmed_by_user is not True:
        raise WorkflowError("Confirm that you submitted this application yourself (confirmed_by_user=true)")
    app = _require(_one(conn, "SELECT * FROM applications WHERE id=?", (app_id,)), "Application")
    if not conn.execute("SELECT 1 FROM application_drafts WHERE application_id=? AND approved=1", (app_id,)).fetchone():
        raise WorkflowError("Approve a draft first: applications need your explicit approval before submission")
    try:
        packet = packets.build_packet(conn, app_id)
    except packets.PacketError as exc:
        raise WorkflowError(str(exc)) from exc
    if not packet["ready_to_apply"]:
        blockers = "; ".join(packet["blockers"][:3])
        more = "" if len(packet["blockers"]) <= 3 else f" (+{len(packet['blockers']) - 3} more)"
        raise WorkflowError(f"Application packet is not ready: resolve its blockers before submission: {blockers}{more}")
    policy.require(app["platform_id"] or "", "record_manual_submission")
    when = _day(submitted_at or date.today().isoformat(), "submitted_at")
    with db.tx(conn):
        follow = (date.fromisoformat(when) + timedelta(days=FOLLOWUP_DAYS)).isoformat()
        conn.execute("UPDATE applications SET status='submitted', submitted_at=?, next_followup_at=?, updated_at=? WHERE id=?", (when, follow, db.now_iso(), app_id))
        conn.execute("INSERT INTO application_events(application_id,at,event_date,kind,detail) VALUES(?,?,?,?,?)",
                     (app_id, db.now_iso(), when, "submitted", "Submitted by you on the platform"))
        db.audit(conn, "application.submitted_manually", "application", app_id, {"date": when})
    return get_application(conn, app_id)


def record_response(conn, app_id: int, status: str, text: str = "", response_at: str | None = None) -> dict[str, Any]:
    if status not in ("interview", "accepted", "rejected", "withdrawn"):
        raise WorkflowError("status must be interview, accepted, rejected or withdrawn")
    app = _require(_one(conn, "SELECT * FROM applications WHERE id=?", (app_id,)), "Application")
    if app["status"] == "draft":
        raise WorkflowError("This application was never marked as submitted")
    with db.tx(conn):
        conn.execute("UPDATE applications SET status=?, response_text=?, response_at=?, updated_at=? WHERE id=?",
                     (status, text[:4000], _day(response_at or date.today().isoformat(), "response_at"), db.now_iso(), app_id))
        conn.execute("INSERT INTO application_events(application_id,at,event_date,kind,detail) VALUES(?,?,?,?,?)",
                     (app_id, db.now_iso(), _day(response_at or date.today().isoformat(), "response_at"), "response", f"{status}: {text[:300]}"))
        db.audit(conn, "application.response", "application", app_id, {"status": status})
    return get_application(conn, app_id)


def _when(value: str | None, field: str) -> str:
    """A date or datetime (YYYY-MM-DD or YYYY-MM-DDTHH:MM), validated; returns the original text."""
    try:
        datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise WorkflowError(f"{field} must be a date or date-time such as 2026-11-20 or 2026-11-20T14:00")
    return str(value)


def log_event(conn, app_id: int, kind: str, detail: str = "", event_date: str | None = None,
              next_followup_at: str | None = None, decision: str | None = None) -> dict[str, Any]:
    """Follow-up tracking for a submitted application: follow-ups sent, interviews, decisions, notes."""
    if kind not in ("followup_sent", "interview_scheduled", "interview_done", "decision", "note"):
        raise WorkflowError("kind must be followup_sent, interview_scheduled, interview_done, decision or note (use submit/response for those)")
    app = _require(_one(conn, "SELECT * FROM applications WHERE id=?", (app_id,)), "Application")
    if app["status"] in ("draft", "approved") and kind != "note":
        raise WorkflowError("This application has not been marked as submitted yet")
    when = _when(event_date or date.today().isoformat(), "event_date")
    now = db.now_iso()
    with db.tx(conn):
        if kind == "followup_sent":
            nxt = _when(next_followup_at, "next_followup_at") if next_followup_at else (date.fromisoformat(when[:10]) + timedelta(days=FOLLOWUP_DAYS)).isoformat()
            conn.execute("UPDATE applications SET next_followup_at=?, updated_at=? WHERE id=?", (nxt, now, app_id))
        elif kind == "interview_scheduled":
            conn.execute("UPDATE applications SET interview_at=?, status='interview', next_followup_at=NULL, updated_at=? WHERE id=?", (when, now, app_id))
        elif kind == "interview_done":
            nxt = _when(next_followup_at, "next_followup_at") if next_followup_at else (date.fromisoformat(when[:10]) + timedelta(days=FOLLOWUP_DAYS)).isoformat()
            conn.execute("UPDATE applications SET next_followup_at=?, updated_at=? WHERE id=?", (nxt, now, app_id))
        elif kind == "decision":
            if decision not in ("accepted", "rejected", "withdrawn"):
                raise WorkflowError("decision must be accepted, rejected or withdrawn")
            conn.execute("UPDATE applications SET status=?, decision=?, decision_at=?, next_followup_at=NULL, updated_at=? WHERE id=?",
                         (decision, decision, when[:10], now, app_id))
            detail = f"{decision}. {detail}".strip()
        conn.execute("INSERT INTO application_events(application_id,at,event_date,kind,detail) VALUES(?,?,?,?,?)", (app_id, now, when, kind, detail[:500]))
        db.audit(conn, f"application.{kind}", "application", app_id, {"date": when})
    return get_application(conn, app_id)


def get_application(conn, app_id: int) -> dict[str, Any]:
    app = _require(_one(conn, "SELECT * FROM applications WHERE id=?", (app_id,)), "Application")
    app["events"] = db.rows(conn.execute("SELECT id,at,event_date,kind,detail FROM application_events WHERE application_id=? ORDER BY id DESC", (app_id,)))
    app["drafts"] = []
    for d in db.rows(conn.execute("SELECT * FROM application_drafts WHERE application_id=? ORDER BY version DESC", (app_id,))):
        d["missing_info"] = json.loads(d.pop("missing_info_json") or "[]")
        d["approved"] = bool(d["approved"])
        app["drafts"].append(d)
    return app


def list_applications(conn) -> list[dict[str, Any]]:
    return db.rows(conn.execute("SELECT * FROM applications ORDER BY updated_at DESC"))


# ---------------------------------------------------------------- tasks ------------------------------
def create_task(conn, title: str, instructions: str = "", application_id: int | None = None,
                opportunity_url: str | None = None, platform_id: str | None = None, due_at: str | None = None) -> dict[str, Any]:
    if not title or not title.strip():
        raise WorkflowError("A task needs a title")
    if application_id is not None:
        app = _require(_one(conn, "SELECT * FROM applications WHERE id=?", (application_id,)), "Application")
        opportunity_url, platform_id = opportunity_url or app["opportunity_url"], platform_id or app["platform_id"]
    task_guidance = assistant.simplify_instructions(instructions)
    pg = guidance.for_platform(platform_id)
    task_guidance["platform"] = {"platform_id": platform_id, "label": pg["label"], "verified": pg["verified"], "verified_on": pg["verified_on"],
                                 "sources": pg["sources"], "common_mistakes": pg["common_mistakes"], "payout": pg["payout"]}
    now = db.now_iso()
    with db.tx(conn):
        cur = conn.execute("INSERT INTO tasks(application_id,opportunity_url,platform_id,title,instructions,simplified,guidance_json,"
                           "status,created_at,updated_at,due_at) VALUES(?,?,?,?,?,?,?, 'accepted', ?,?,?)",
                           (application_id, opportunity_url, platform_id, title.strip()[:200], instructions[:20000],
                            task_guidance["summary"], json.dumps(task_guidance, ensure_ascii=False), now, now, due_at))
        tid = cur.lastrowid
        rows = assistant.generate_checklist(task_guidance) + guidance.task_checklist(platform_id)      # task text + platform QC checklist
        for pos, it in enumerate(rows, 1):
            conn.execute("INSERT INTO task_checklist_items(task_id,position,text,kind) VALUES(?,?,?,?)", (tid, pos, it["text"][:500], it["kind"]))
        db.audit(conn, "task.create", "task", tid)
    return get_task(conn, tid)


def set_task_status(conn, task_id: int, status: str) -> dict[str, Any]:
    t = _require(_one(conn, "SELECT * FROM tasks WHERE id=?", (task_id,)), "Task")
    if status == "submitted":
        raise WorkflowError("Use the submission record (with your confirmation) to mark a task submitted")
    if status != t["status"] and status not in TASK_FLOW.get(t["status"], set()):
        raise WorkflowError(f"A {t['status']} task cannot become {status}")
    with db.tx(conn):
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, db.now_iso(), task_id))
        db.audit(conn, "task.status", "task", task_id, {"from": t["status"], "to": status})
    return get_task(conn, task_id)


def toggle_item(conn, item_id: int, done: bool) -> dict[str, Any]:
    it = _require(_one(conn, "SELECT * FROM task_checklist_items WHERE id=?", (item_id,)), "Checklist item")
    with db.tx(conn):
        conn.execute("UPDATE task_checklist_items SET done=?, done_at=? WHERE id=?", (1 if done else 0, db.now_iso() if done else None, item_id))
    return get_task(conn, it["task_id"])


def start_session(conn, task_id: int) -> dict[str, Any]:
    _require(_one(conn, "SELECT id FROM tasks WHERE id=?", (task_id,)), "Task")
    if conn.execute("SELECT 1 FROM work_sessions WHERE task_id=? AND ended_at IS NULL", (task_id,)).fetchone():
        raise WorkflowError("A work session is already running for this task")
    with db.tx(conn):
        conn.execute("INSERT INTO work_sessions(task_id,started_at) VALUES(?,?)", (task_id, db.now_iso()))
        conn.execute("UPDATE tasks SET status='in_progress', updated_at=? WHERE id=? AND status='accepted'", (db.now_iso(), task_id))
    return get_task(conn, task_id)


def stop_session(conn, task_id: int, notes: str = "") -> dict[str, Any]:
    s = _require(_one(conn, "SELECT * FROM work_sessions WHERE task_id=? AND ended_at IS NULL", (task_id,)), "Running work session")
    started = datetime.fromisoformat(s["started_at"])
    ended = datetime.fromisoformat(db.now_iso())
    with db.tx(conn):
        conn.execute("UPDATE work_sessions SET ended_at=?, minutes=?, notes=? WHERE id=?",
                     (ended.isoformat(), round((ended - started).total_seconds() / 60, 2), notes[:1000], s["id"]))
    return get_task(conn, task_id)


def _safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "evidence").name).strip("._") or "evidence"
    return base[:80]


def add_evidence(conn, task_id: int, kind: str = "note", description: str = "", file_name: str | None = None,
                 content: bytes | None = None, evidence_dir: Path = EVIDENCE_DIR) -> dict[str, Any]:
    _require(_one(conn, "SELECT id FROM tasks WHERE id=?", (task_id,)), "Task")
    if not description.strip() and content is None:
        raise WorkflowError("Evidence needs a description or a file")
    path = digest = size = None
    if content is not None:
        if len(content) > MAX_EVIDENCE_BYTES:
            raise WorkflowError("Evidence files are limited to 10 MB")
        folder = Path(evidence_dir) / f"task-{task_id}"
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        target = folder / f"{digest[:10]}-{_safe_name(file_name or 'evidence')}"
        target.write_bytes(content)
        path, size = str(target), len(content)
    with db.tx(conn):
        cur = conn.execute("INSERT INTO evidence(task_id,kind,description,file_name,file_path,sha256,size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (task_id, kind[:30], description[:2000], _safe_name(file_name) if file_name else None, path, digest, size, db.now_iso()))
        db.audit(conn, "evidence.add", "task", task_id, {"evidence_id": cur.lastrowid, "sha256": digest})
    return get_task(conn, task_id)


def record_submission(conn, task_id: int, confirmed_by_user: bool, platform_reference: str = "", notes: str = "",
                      submitted_at: str | None = None) -> dict[str, Any]:
    """Record that YOU submitted the work. Never claims or fabricates anything."""
    if confirmed_by_user is not True:
        raise WorkflowError("Confirm that you submitted this work yourself (confirmed_by_user=true)")
    t = _require(_one(conn, "SELECT * FROM tasks WHERE id=?", (task_id,)), "Task")
    if t["status"] not in ("completed", "in_progress", "accepted"):
        raise WorkflowError(f"A {t['status']} task cannot be submitted")
    policy.require(t["platform_id"] or "", "record_manual_submission")
    when = _day(submitted_at or date.today().isoformat(), "submitted_at")
    with db.tx(conn):
        conn.execute("INSERT INTO submissions(task_id,submitted_at,method,status,platform_reference,notes) VALUES(?,?, 'manual','submitted',?,?)",
                     (task_id, when, platform_reference[:200], notes[:1000]))
        conn.execute("UPDATE tasks SET status='submitted', updated_at=? WHERE id=?", (db.now_iso(), task_id))
        db.audit(conn, "task.submitted_manually", "task", task_id, {"date": when})
    return get_task(conn, task_id)


def get_task(conn, task_id: int) -> dict[str, Any]:
    t = _require(_one(conn, "SELECT * FROM tasks WHERE id=?", (task_id,)), "Task")
    t["guidance"] = json.loads(t.pop("guidance_json") or "{}")
    t["checklist"] = db.rows(conn.execute("SELECT * FROM task_checklist_items WHERE task_id=? ORDER BY position", (task_id,)))
    t["sessions"] = db.rows(conn.execute("SELECT * FROM work_sessions WHERE task_id=? ORDER BY id DESC", (task_id,)))
    t["evidence"] = db.rows(conn.execute("SELECT id,kind,description,file_name,sha256,size_bytes,created_at FROM evidence WHERE task_id=? ORDER BY id DESC", (task_id,)))
    t["submissions"] = db.rows(conn.execute("SELECT * FROM submissions WHERE task_id=? ORDER BY id DESC", (task_id,)))
    t["open_items"] = sum(1 for i in t["checklist"] if not i["done"])
    t["minutes_worked"] = round(sum(s["minutes"] or 0 for s in t["sessions"]), 2)
    return t


def list_tasks(conn) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT id FROM tasks ORDER BY updated_at DESC").fetchall():
        t = get_task(conn, r["id"])
        t.pop("instructions", None)
        out.append(t)
    return out


# ---------------------------------------------------------------- earnings ---------------------------
def add_earning(conn, amount: float, currency: str, status: str = "expected", platform_id: str | None = None,
                task_id: int | None = None, description: str = "", expected_date: str | None = None) -> dict[str, Any]:
    """Expected/pending money only. Advertised pay can never be stored as received."""
    if status not in ("expected", "pending"):
        raise WorkflowError("Earnings are recorded as 'expected' or 'pending'; money becomes 'received' only via a recorded payment")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise WorkflowError("amount must be a number")
    if not (amount >= 0) or amount != amount:
        raise WorkflowError("amount must be zero or more")
    currency = (currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise WorkflowError("currency must be a 3-letter code such as USD or KES")
    now = db.now_iso()
    with db.tx(conn):
        cur = conn.execute("INSERT INTO earnings(task_id,platform_id,description,amount,currency,status,expected_date,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (task_id, platform_id, description[:300], amount, currency, status,
                                                         _day(expected_date, "expected_date") if expected_date else None, now, now))
        db.audit(conn, "earning.add", "earning", cur.lastrowid, {"status": status, "amount": amount, "currency": currency})
    return _one(conn, "SELECT * FROM earnings WHERE id=?", (cur.lastrowid,))


def record_payment(conn, amount: float, currency: str, received_date: str, earning_id: int | None = None,
                   platform_id: str | None = None, method: str = "", reference: str = "") -> dict[str, Any]:
    """Money you actually received. A received date on or before today is mandatory."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise WorkflowError("amount must be a number")
    if not amount > 0:
        raise WorkflowError("A payment must be more than zero")
    currency = (currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise WorkflowError("currency must be a 3-letter code such as USD or KES")
    day = _day(received_date, "received_date")
    if day > date.today().isoformat():
        raise WorkflowError("A payment cannot be received in the future")
    earning = None
    if earning_id is not None:
        earning = _require(_one(conn, "SELECT * FROM earnings WHERE id=?", (earning_id,)), "Earning")
        if earning["currency"] != currency:
            raise WorkflowError("Payment currency must match the earning's currency")
        platform_id = platform_id or earning["platform_id"]
    with db.tx(conn):
        cur = conn.execute("INSERT INTO payments(earning_id,platform_id,amount,currency,received_date,method,reference,created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (earning_id, platform_id, amount, currency, day, method[:60], reference[:120], db.now_iso()))
        if earning:
            paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE earning_id=?", (earning_id,)).fetchone()[0]
            if paid + 1e-9 >= earning["amount"]:
                conn.execute("UPDATE earnings SET status='received', updated_at=? WHERE id=?", (db.now_iso(), earning_id))
        db.audit(conn, "payment.record", "payment", cur.lastrowid, {"amount": amount, "currency": currency})
    return _one(conn, "SELECT * FROM payments WHERE id=?", (cur.lastrowid,))


def earnings_summary(conn) -> dict[str, Any]:
    """Per currency: expected (advertised/not yet worked), pending (work done, unpaid), received (recorded payments)."""
    out: dict[str, dict[str, float]] = {}

    def bucket(cur: str) -> dict[str, float]:
        return out.setdefault(cur, {"expected": 0.0, "pending": 0.0, "received": 0.0})

    paid_by_earning = {r["earning_id"]: r["paid"] for r in conn.execute(
        "SELECT earning_id, SUM(amount) paid FROM payments WHERE earning_id IS NOT NULL GROUP BY earning_id")}
    for e in conn.execute("SELECT id,currency,status,amount FROM earnings WHERE status IN ('expected','pending')"):
        remaining = max(0.0, e["amount"] - paid_by_earning.get(e["id"], 0.0))   # partly paid: only the rest is outstanding
        bucket(e["currency"])[e["status"]] += remaining
    for r in conn.execute("SELECT currency, SUM(amount) a FROM payments GROUP BY currency"):
        bucket(r["currency"])["received"] += r["a"]
    return {"by_currency": {k: {m: round(v, 2) for m, v in b.items()} for k, b in out.items()},
            "note": "Expected = advertised/not yet worked. Pending = work done, unpaid. Received = payments you recorded."}


def list_earnings(conn) -> dict[str, Any]:
    return {"earnings": db.rows(conn.execute("SELECT * FROM earnings ORDER BY id DESC")),
            "payments": db.rows(conn.execute("SELECT * FROM payments ORDER BY received_date DESC, id DESC")),
            "summary": earnings_summary(conn)}
