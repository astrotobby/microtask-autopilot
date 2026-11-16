"""Real assignments and their evidence-backed lifecycle.

    discovered -> qualified -> ready -> accepted -> executing -> qa -> ready_for_delivery -> submitted
               -> awaiting_review -> work_accepted | rejected -> payment_pending -> paid

Rules enforced here (not in the dashboard):
* An assignment only exists for a real listing: it needs a platform with a capability record and an external id.
* Stages move forward one step at a time. Every move is stored in ``assignment_events`` with the evidence that justifies it.
* Evidence is checked against the database, not taken on trust: an "executing" move needs a real execution job that
  started, "qa" needs executed QA rows, "ready_for_delivery" needs a packaged job, "payment_pending" needs an earnings
  row and "paid" needs a recorded payment. Steps that only a person or the platform can attest (taking the task,
  submitting, the reviewer's decision) need either a platform record id or your explicit confirmation with a reference.
* A QA pass is never platform acceptance, a generated file is never a submission, and a quoted reward is never income.
* Repeating a move that already happened is a no-op, so retries and re-syncs cannot duplicate work or events.

This module never contacts a platform.
"""
from __future__ import annotations

import json
from typing import Any

try:
    from microtask_monitor import db, execution as ex, policy, workflow as wf
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import db, execution as ex, policy, workflow as wf
    from secret_store import redact

ORDER = ("discovered", "qualified", "ready", "accepted", "executing", "qa", "ready_for_delivery", "submitted",
         "awaiting_review", "work_accepted", "payment_pending", "paid")
TERMINAL = ("rejected", "paid", "cancelled")
LABELS = {"discovered": "Discovered", "qualified": "Qualified", "ready": "Ready", "accepted": "Accepted (taken by you)",
          "executing": "Executing", "qa": "Quality assurance", "ready_for_delivery": "Ready for delivery", "submitted": "Submitted",
          "awaiting_review": "Awaiting review", "work_accepted": "Work accepted by the platform", "rejected": "Rejected",
          "payment_pending": "Payment pending", "paid": "Paid", "cancelled": "Cancelled"}
# Which kinds of evidence can justify entering each stage.
EVIDENCE = {
    "discovered": ("listing",),
    "qualified": ("eligibility_check",),
    "ready": ("requirements_check",),
    "accepted": ("platform_record", "user_confirmation"),
    "executing": ("execution_job", "human_work"),
    "qa": ("execution_job", "human_work"),
    "ready_for_delivery": ("execution_job", "human_work"),
    "submitted": ("platform_record", "user_confirmation"),
    "awaiting_review": ("platform_record", "user_confirmation"),
    "work_accepted": ("platform_record", "user_confirmation"),
    "rejected": ("platform_record", "user_confirmation"),
    "payment_pending": ("earning",),
    "paid": ("payment",),
    "cancelled": ("user_confirmation", "platform_record"),
}
ORIGINS = ("whop_bounty", "application", "manual", "discovery", "direct")
DIRECT_LANE = "direct_client"          # your own client's work (see permissions.py); not one of the registered platforms


class AssignmentError(ValueError):
    pass


def _row(conn, aid: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
    if not r:
        raise AssignmentError("Assignment not found")
    return dict(r)


def create(conn, platform_id: str, external_id: str, title: str, origin: str, instructions: str = "", source_url: str = "",
           deadline: str | None = None, reward_gross: float | None = None, reward_net: float | None = None, currency: str | None = None,
           requirements: dict | None = None, application_id: int | None = None, ai_use: str = "unknown", ai_use_source: str = "",
           listing_ref: str = "") -> dict[str, Any]:
    """Register a real listing as a discovered assignment. The same (platform, external id) is never created twice."""
    if platform_id != DIRECT_LANE and platform_id not in policy.load_capabilities():
        raise AssignmentError(f"No capability record for '{platform_id}' (fail closed)")
    if (platform_id == DIRECT_LANE) != (origin == "direct"):
        raise AssignmentError("Direct client work uses the 'direct' origin, and only that lane does")
    if origin not in ORIGINS:
        raise AssignmentError("Unknown origin")
    external_id, title = str(external_id or "").strip(), str(title or "").strip()
    if not external_id or not title:
        raise AssignmentError("An assignment needs the platform's own identifier and a title: nothing is invented")
    if ai_use not in db.AI_USE:
        raise AssignmentError("Unknown AI-use status")
    existing = conn.execute("SELECT id FROM assignments WHERE platform_id=? AND external_id=?", (platform_id, external_id)).fetchone()
    if existing:
        return get(conn, existing["id"])
    now = db.now_iso()
    with db.tx(conn):
        cur = conn.execute("""INSERT INTO assignments(platform_id,external_id,origin,title,instructions,source_url,stage,ai_use,ai_use_source,deadline,
            reward_gross,reward_net,currency,requirements_json,application_id,created_at,updated_at) VALUES(?,?,?,?,?,?, 'discovered', ?,?,?,?,?,?,?,?,?,?)""",
                           (platform_id, external_id, origin, title[:200], (instructions or "")[:50000], source_url or None, ai_use, (ai_use_source or "")[:500],
                            deadline, reward_gross, reward_net, (currency or "").upper() or None, json.dumps(requirements or {}, ensure_ascii=False),
                            application_id, now, now))
        _event(conn, cur.lastrowid, None, "discovered", "listing", listing_ref or f"{platform_id}:{external_id}", "Listing recorded", "system")
        db.audit(conn, "assignment.create", "assignment", cur.lastrowid, {"platform": platform_id, "external_id": external_id}, actor="system")
    return get(conn, cur.lastrowid)


def _event(conn, aid: int, frm: str | None, to: str, kind: str, ref: str, detail: str, actor: str) -> None:
    conn.execute("INSERT INTO assignment_events(assignment_id,at,from_stage,to_stage,evidence_kind,evidence_ref,detail,actor) VALUES(?,?,?,?,?,?,?,?)",
                 (aid, db.now_iso(), frm, to, kind, redact(ref)[:300], redact(detail or "")[:1000], actor))


def _check_evidence(conn, a: dict[str, Any], to: str, kind: str, ref: str, confirmed_by_user: bool, actor: str | None = None) -> None:
    if kind not in EVIDENCE[to]:
        raise AssignmentError(f"'{LABELS[to]}' needs evidence of type {' or '.join(EVIDENCE[to])}, not '{kind}'")
    ref = str(ref or "").strip()
    if not ref:
        raise AssignmentError("Evidence needs a reference (a record id, confirmation number, link or date)")
    if kind == "user_confirmation" and confirmed_by_user is not True:
        raise AssignmentError("This step must be confirmed by you (confirmed_by_user=true)")
    if kind == "human_work":
        # Work you did personally (e.g. filming, posting). Only when no engine job exists, and only on your word or a platform record.
        if a["execution_job_id"]:
            raise AssignmentError("This assignment has an execution job: its stages come from that job, not from a statement")
        if confirmed_by_user is not True and actor != "platform":
            raise AssignmentError("Work done by you personally must be confirmed by you or shown by a platform record")
    if kind == "execution_job":
        job = conn.execute("SELECT * FROM execution_jobs WHERE id=?", (a["execution_job_id"] or 0,)).fetchone()
        if not job or ref != f"execution_job:{job['id']}":
            raise AssignmentError("No execution job is linked to this assignment")
        if to == "executing" and not job["attempts"]:
            raise AssignmentError("The execution job has not started: nothing has been worked on yet")
        if to == "qa" and not conn.execute("SELECT 1 FROM execution_qa WHERE job_id=? AND executed=1", (job["id"],)).fetchone():
            raise AssignmentError("No quality check has actually been executed for this job")
        if to == "ready_for_delivery":
            if job["status"] not in ("ready_for_submission", "submitted", "accepted", "rejected"):
                raise AssignmentError("The deliverables are not approved and packaged yet (QA must pass and you must approve them)")
            if not conn.execute("SELECT 1 FROM execution_files WHERE job_id=? AND role='package'", (job["id"],)).fetchone():
                raise AssignmentError("No delivery package exists for this job")
    if kind == "earning":
        e = conn.execute("SELECT * FROM earnings WHERE id=?", (a["earning_id"] or 0,)).fetchone()
        if not e or ref != f"earning:{e['id']}":
            raise AssignmentError("Payment pending needs a recorded earnings entry linked to this assignment")
    if kind == "payment":
        p = conn.execute("SELECT * FROM payments WHERE id=?", (a["payment_id"] or 0,)).fetchone()
        if not p or ref != f"payment:{p['id']}" or not p["amount"] > 0:
            raise AssignmentError("Paid needs a recorded payment you actually received, linked to this assignment")


def advance(conn, aid: int, to: str, evidence_kind: str, evidence_ref: str, detail: str = "", confirmed_by_user: bool = False,
            actor: str | None = None) -> dict[str, Any]:
    """Move one step forward with evidence. Re-stating a stage already reached is a harmless no-op."""
    a = _row(conn, aid)
    if to not in db.ASSIGNMENT_STAGES:
        raise AssignmentError(f"Unknown stage '{to}'")
    cur = a["stage"]
    if to == cur or (to in ORDER and cur in ORDER and ORDER.index(to) < ORDER.index(cur)):
        return get(conn, aid)                                             # already there (or beyond): never duplicate
    if cur in TERMINAL:
        raise AssignmentError(f"This assignment is {LABELS[cur].lower()}; it cannot change")
    if to == "cancelled":
        pass
    elif to == "rejected":
        if cur not in ("submitted", "awaiting_review"):
            raise AssignmentError("Only submitted work can be rejected by a platform")
    elif to not in ORDER or ORDER.index(to) != ORDER.index(cur) + 1:
        nxt = ORDER[ORDER.index(cur) + 1] if cur in ORDER and ORDER.index(cur) + 1 < len(ORDER) else None
        raise AssignmentError(f"Stages cannot be skipped: after '{LABELS[cur]}' comes '{LABELS.get(nxt, 'nothing')}', not '{LABELS[to]}'")
    _check_evidence(conn, a, to, evidence_kind, evidence_ref, confirmed_by_user, actor)
    with db.tx(conn):
        fields = {"stage": to, "stage_reason": redact(detail or "")[:300], "updated_at": db.now_iso()}
        if to in ("submitted", "awaiting_review") and not a["submission_reference"]:
            fields["submission_reference"] = redact(str(evidence_ref))[:200]
        conn.execute(f"UPDATE assignments SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), aid))
        _event(conn, aid, cur, to, evidence_kind, str(evidence_ref), detail, actor or ("user" if evidence_kind == "user_confirmation" else "system"))
        db.audit(conn, "assignment.advance", "assignment", aid, {"from": cur, "to": to, "evidence": evidence_kind}, actor=actor or "system")
    return get(conn, aid)


def advance_through(conn, aid: int, target: str, evidence_kind: str, evidence_ref: str, detail: str = "", confirmed_by_user: bool = False,
                    actor: str | None = None) -> dict[str, Any]:
    """Walk forward to ``target`` on one platform record or one confirmation of yours. Every step is still evidence-checked:
    the engine stages use the linked job when there is one, otherwise they are recorded as work you did personally."""
    a = _row(conn, aid)
    while a["stage"] in ORDER and target in ORDER and ORDER.index(a["stage"]) < ORDER.index(target):
        nxt = ORDER[ORDER.index(a["stage"]) + 1]
        if nxt in ("executing", "qa", "ready_for_delivery"):
            if a["execution_job_id"]:
                advance(conn, aid, nxt, "execution_job", f"execution_job:{a['execution_job_id']}", detail)
            else:
                advance(conn, aid, nxt, "human_work", evidence_ref, "Done by you personally (no engine job). " + detail, confirmed_by_user, actor)
        else:
            advance(conn, aid, nxt, evidence_kind, evidence_ref, detail, confirmed_by_user, actor)
        a = _row(conn, aid)
    return get(conn, aid)


def qualify(conn, aid: int, passed: bool, reasons: list[str], ref: str) -> dict[str, Any]:
    if not passed:
        return get(conn, aid)
    return advance(conn, aid, "qualified", "eligibility_check", ref, "; ".join(reasons)[:900])


def mark_ready(conn, aid: int) -> dict[str, Any]:
    """Ready = real instructions are on file and the AI-use rule is decided. Unknown AI rules keep it out of 'ready'."""
    a = _row(conn, aid)
    if len((a["instructions"] or "").strip()) < 20:
        raise AssignmentError("The real task instructions are not on file yet")
    if a["ai_use"] == "unknown":
        raise AssignmentError("Decide the AI-use rule first (allowed with its source, prohibited, or human-only)")
    return advance(conn, aid, "ready", "requirements_check", f"assignment:{aid}", f"Instructions on file; AI use: {a['ai_use']}")


def set_ai_use(conn, aid: int, ai_use: str, source: str = "") -> dict[str, Any]:
    a = _row(conn, aid)
    if ai_use not in db.AI_USE:
        raise AssignmentError("Unknown AI-use status")
    detected = ex.detect_ai_rules(a["instructions"] or "")
    if ai_use == "allowed":
        if detected["status"] == "prohibited":
            raise AssignmentError(f"The instructions appear to prohibit AI help (\"{detected['evidence']}\"): AI cannot be enabled")
        if not (source or "").strip():
            raise AssignmentError("State where the platform or task rules permit AI help")
    with db.tx(conn):
        conn.execute("UPDATE assignments SET ai_use=?, ai_use_source=?, updated_at=? WHERE id=?", (ai_use, (source or "")[:500], db.now_iso(), aid))
        db.audit(conn, "assignment.ai_use", "assignment", aid, {"ai_use": ai_use})
    return get(conn, aid)


def start_execution(conn, aid: int, work_type: str | None = None, exec_dir=None, inputs: dict[str, bytes] | None = None) -> dict[str, Any]:
    """Create the execution job for an accepted assignment (once). Running it still needs your go-ahead in the engine."""
    a = _row(conn, aid)
    if a["execution_job_id"]:
        return get(conn, aid)                                            # resumable: never a second job for the same assignment
    if a["stage"] != "accepted":
        raise AssignmentError("Work can only start on an assignment you have actually taken (stage 'Accepted')")
    if a["ai_use"] == "human_only":
        raise AssignmentError("This assignment must be done by you personally; the engine prepares nothing for it")
    job = ex.create_job(conn, a["title"], a["instructions"] or "", None if a["platform_id"] == DIRECT_LANE else a["platform_id"], a["application_id"], a["deadline"], work_type,
                        a["ai_use"] if a["ai_use"] in ("allowed", "prohibited") else None, a["ai_use_source"] or "", inputs, exec_dir,
                        "platform_api" if a["origin"] == "whop_bounty" else "user_provided")
    with db.tx(conn):
        conn.execute("UPDATE assignments SET execution_job_id=?, updated_at=? WHERE id=?", (job["id"], db.now_iso(), aid))
    return get(conn, aid)


def sync_from_job(conn, aid: int) -> dict[str, Any]:
    """Derive executing / qa / ready_for_delivery from what the linked execution job has really done."""
    a = _row(conn, aid)
    if not a["execution_job_id"] or a["stage"] not in ("accepted", "executing", "qa"):
        return get(conn, aid)
    ref = f"execution_job:{a['execution_job_id']}"
    for to, note in (("executing", "Execution job started"), ("qa", "Independent quality checks were executed"),
                     ("ready_for_delivery", "QA passed, you approved the deliverables and they were packaged")):
        try:
            advance(conn, aid, to, "execution_job", ref, note)
        except AssignmentError:
            break
    return get(conn, aid)


def record_payment_pending(conn, aid: int, amount: float, currency: str, description: str = "") -> dict[str, Any]:
    """Accepted work that is owed but not yet received. The amount must come from the platform record, not an estimate."""
    a = _row(conn, aid)
    if a["stage"] != "work_accepted":
        raise AssignmentError("Only work the platform has accepted can have a payment pending")
    if not a["earning_id"]:
        e = wf.add_earning(conn, amount, currency, "pending", a["platform_id"], None, description or f"{a['title']} ({a['external_id']})")
        with db.tx(conn):
            conn.execute("UPDATE assignments SET earning_id=? WHERE id=?", (e["id"], aid))
        a["earning_id"] = e["id"]
    return advance(conn, aid, "payment_pending", "earning", f"earning:{a['earning_id']}", "Reward owed for accepted work")


def record_paid(conn, aid: int, amount: float, currency: str, received_date: str, method: str = "", reference: str = "",
                confirmed_by_user: bool = False) -> dict[str, Any]:
    """Money you actually received (for example a completed withdrawal). Needs your confirmation and a payment reference."""
    a = _row(conn, aid)
    if confirmed_by_user is not True:
        raise AssignmentError("Only you can confirm that money was received (confirmed_by_user=true)")
    if a["stage"] != "payment_pending":
        raise AssignmentError("Only an assignment with a pending payment can be marked paid")
    if not (reference or "").strip():
        raise AssignmentError("Give the payout reference or transaction id shown by the platform or your bank/M-Pesa statement")
    if not a["payment_id"]:
        p = wf.record_payment(conn, amount, currency, received_date, a["earning_id"], a["platform_id"], method, reference)
        with db.tx(conn):
            conn.execute("UPDATE assignments SET payment_id=? WHERE id=?", (p["id"], aid))
        a["payment_id"] = p["id"]
    return advance(conn, aid, "paid", "payment", f"payment:{a['payment_id']}", f"Payment received ({reference[:60]})", actor="user")


# ------------------------------------------------------------------------------------------- reads
def get(conn, aid: int) -> dict[str, Any]:
    a = _row(conn, aid)
    a["requirements"] = json.loads(a.pop("requirements_json") or "{}")
    a["stage_label"] = LABELS[a["stage"]]
    a["history"] = db.rows(conn.execute("SELECT at,from_stage,to_stage,evidence_kind,evidence_ref,detail,actor FROM assignment_events WHERE assignment_id=? ORDER BY id", (aid,)))
    a["next_action"] = next_action(a)
    return a


def next_action(a: dict[str, Any]) -> str:
    return {
        "discovered": "Eligibility has not been confirmed for this listing.",
        "qualified": "Read the instructions and set the AI-use rule; then it becomes ready.",
        "ready": "Take the task yourself on the platform, then record it here with the platform's reference.",
        "accepted": "Start the work in the Execution Centre.",
        "executing": "Work is in progress; QA runs when the worker finishes.",
        "qa": "Review the QA results and files, then approve the deliverables.",
        "ready_for_delivery": "Submit the package yourself on the platform, then record the confirmation.",
        "submitted": "Waiting for the platform to show the submission as under review.",
        "awaiting_review": "Wait for the reviewer's decision.",
        "work_accepted": "Record the reward that is now owed (payment pending).",
        "payment_pending": "Withdraw yourself, then record the payout reference when the money arrives.",
        "paid": "Done: payment confirmed.", "rejected": "Read the reviewer's reason; nothing is owed.", "cancelled": "Nothing further.",
    }[a["stage"]]


def list_all(conn, platform_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    sql, args = "SELECT id FROM assignments", []
    if platform_id:
        sql += " WHERE platform_id=?"
        args.append(platform_id)
    return [get(conn, r["id"]) for r in conn.execute(sql + " ORDER BY updated_at DESC LIMIT ?", (*args, limit))]


def funnel(conn) -> dict[str, Any]:
    """The honest funnel. Each number counts a different real thing; none is derived from advertised pay."""
    def one(sql: str, *a: Any) -> int:
        return conn.execute(sql, a).fetchone()[0]
    stages = {r["stage"]: r["n"] for r in conn.execute("SELECT stage, COUNT(*) n FROM assignments GROUP BY stage")}
    reached = lambda *names: sum(stages.get(n, 0) for n in names)          # noqa: E731
    after = lambda stage: reached(*ORDER[ORDER.index(stage):])             # noqa: E731
    return {
        "discovered_opportunities": one("SELECT COUNT(*) FROM opportunities WHERE duplicate_of IS NULL AND status='open'") + one("SELECT COUNT(*) FROM whop_bounties WHERE status='open'"),
        "applications_submitted": one("SELECT COUNT(*) FROM applications WHERE submitted_at IS NOT NULL"),
        "assignments_qualified": after("qualified") + stages.get("rejected", 0),
        "assignments_accepted": after("accepted") + stages.get("rejected", 0),
        "work_performed": one("SELECT COUNT(*) FROM execution_jobs WHERE attempts>0"),
        "deliverables_completed": one("SELECT COUNT(*) FROM execution_jobs WHERE status IN ('ready_for_submission','submitted','accepted','rejected')"),
        "submissions_confirmed": after("submitted") + stages.get("rejected", 0),
        "work_accepted": after("work_accepted"),
        "payments_confirmed": one("SELECT COUNT(*) FROM payments"),
        "by_stage": stages,
        "money": wf.earnings_summary(conn),
        "note": "Advertised or quoted rewards are never counted as income. 'Payments confirmed' are payments you recorded with a reference.",
    }
