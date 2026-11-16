"""The earnings ledger: seven separate totals that are never mixed, and Kenya payout-route tracking.

    1 potential opportunities   advertised rewards of tasks not yet taken        (an advertisement, not money)
    2 tasks accepted            rewards of tasks you have taken
    3 work completed            rewards of tasks whose deliverables are packaged
    4 deliverables submitted    rewards of submissions the platform has on record
    5 work accepted             rewards of work the platform accepted
    6 payment pending           earnings entries owed for accepted work
    7 money actually received   payments you recorded with a reference

Only row 7 is income. Nothing moves to a later row by inference: approval is not payment, and an upload is not
acceptance. Amounts stay in their own currency (no exchange rate is invented). This module never moves money:
withdrawals, transfers and purchases are yours alone.
"""
from __future__ import annotations

from typing import Any

try:
    from microtask_monitor import db, permissions, policy, task_intel, workflow as wf
except ImportError:  # pragma: no cover
    import db, permissions, policy, task_intel, workflow as wf

METHODS = ("paypal", "payoneer", "bank_transfer", "mpesa", "other")
AVAILABILITY = ("documented", "unverified", "not_available")
_ROWS = (("potential_opportunities", ("discovered", "qualified", "ready")),
         ("tasks_accepted", ("accepted", "executing", "qa")),
         ("work_completed", ("ready_for_delivery",)),
         ("deliverables_submitted", ("submitted", "awaiting_review")),
         ("work_accepted", ("work_accepted",)))


class LedgerError(ValueError):
    pass


def _sum(conn, stages: tuple[str, ...]) -> dict[str, Any]:
    marks = ",".join("?" * len(stages))
    count = conn.execute(f"SELECT COUNT(*) FROM assignments WHERE stage IN ({marks}) AND COALESCE(hold,'') NOT IN ('expired','policy_blocked')", stages).fetchone()[0]
    by = {r["currency"] or "unstated": round(r["total"] or 0, 2) for r in conn.execute(
        f"""SELECT currency, SUM(COALESCE(reward_net, reward_gross, 0)) total FROM assignments
            WHERE stage IN ({marks}) AND COALESCE(hold,'') NOT IN ('expired','policy_blocked') AND COALESCE(reward_net, reward_gross) IS NOT NULL GROUP BY currency""", stages)}
    return {"count": count, "advertised_by_currency": by}


def totals(conn) -> dict[str, Any]:
    out: dict[str, Any] = {name: _sum(conn, stages) for name, stages in _ROWS}
    expected: dict[str, float] = {}
    for r in conn.execute("""SELECT a.currency, t.economics_json FROM task_intel t JOIN assignments a ON a.id=t.assignment_id
                             WHERE a.stage IN ('discovered','qualified','ready','accepted','executing','qa','ready_for_delivery','submitted','awaiting_review') AND a.hold IS NULL"""):
        import json
        v = (json.loads(r["economics_json"]) or {}).get("risk_adjusted_value")
        if v:
            expected[r["currency"] or "unstated"] = round(expected.get(r["currency"] or "unstated", 0) + v, 2)
    out["expected_compensation"] = {"by_currency": expected, "note": "Advertised rewards discounted by acceptance and payout history. A ranking estimate, not income."}
    money = wf.earnings_summary(conn)["by_currency"]
    out["payment_pending"] = {"count": conn.execute("SELECT COUNT(*) FROM earnings WHERE status='pending'").fetchone()[0],
                              "by_currency": {c: v["pending"] for c, v in money.items() if v["pending"]}}
    out["money_received"] = {"count": conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0],
                             "by_currency": {c: v["received"] for c, v in money.items() if v["received"]}}
    out["note"] = "Only 'money_received' is income: payments you recorded with a reference. Every other row is advertised or owed, not earned."
    return out


def entries(conn, limit: int = 300) -> list[dict[str, Any]]:
    """One ledger line per assignment, with each amount in its own column so nothing advertised can read as paid."""
    rows = db.rows(conn.execute("""SELECT a.id AS assignment_id, a.platform_id, a.title, a.stage, a.hold, a.currency, a.reward_gross, a.reward_net, a.submission_reference,
        e.amount AS pending_amount, e.status AS earning_status, p.amount AS received_amount, p.received_date AS payment_date, p.reference AS payment_reference, p.method AS payment_method
        FROM assignments a LEFT JOIN earnings e ON e.id=a.earning_id LEFT JOIN payments p ON p.id=a.payment_id ORDER BY a.updated_at DESC LIMIT ?""", (limit,)))
    for r in rows:
        adv = r["reward_net"] if r["reward_net"] is not None else r["reward_gross"]
        intel = task_intel.load(conn, r["assignment_id"])
        after = lambda *s: r["stage"] in s                                              # noqa: E731
        r.update(advertised=adv, expected=(intel or {}).get("economics", {}).get("risk_adjusted_value"),
                 submitted=adv if after("submitted", "awaiting_review", "work_accepted", "payment_pending", "paid") else None,
                 accepted=adv if after("work_accepted", "payment_pending", "paid") else None,
                 pending=r["pending_amount"] if r["earning_status"] == "pending" else None, received=r["received_amount"])
        del r["pending_amount"], r["received_amount"]
        r.update(_outcome(conn, r["assignment_id"]))
        r["estimated_minutes"] = (intel or {}).get("economics", {}).get("estimated_minutes")
    return rows


def _outcome(conn, aid: int) -> dict[str, Any]:
    """Measured facts for one ledger line: real execution time, and when/why the platform approved or rejected it (from recorded events)."""
    from datetime import datetime
    mins = 0.0
    for j in conn.execute("SELECT e.started_at, e.finished_at FROM execution_jobs e JOIN assignments a ON a.execution_job_id=e.id WHERE a.id=?", (aid,)):
        try:
            mins += max(0.0, (datetime.fromisoformat(j["finished_at"]) - datetime.fromisoformat(j["started_at"])).total_seconds() / 60)
        except (TypeError, ValueError):
            pass
    ev = {r["to_stage"]: r for r in conn.execute("SELECT to_stage, at, detail FROM assignment_events WHERE assignment_id=? AND to_stage IN ('work_accepted','rejected') ORDER BY id", (aid,))}
    ok, no = ev.get("work_accepted"), ev.get("rejected")
    return {"actual_minutes": round(mins, 1) if mins else None, "approved_at": ok["at"] if ok else None,
            "rejected_at": no["at"] if no else None, "rejection_reason": no["detail"] if no else None}


def earnings_overview(conn, today: str | None = None) -> dict[str, Any]:
    """Section E of the dashboard: received money only counts as earned; estimates are labelled as estimates."""
    from datetime import date
    today = today or date.today().isoformat()
    received = {r["currency"]: round(r["s"], 2) for r in conn.execute("SELECT currency, SUM(amount) s FROM payments GROUP BY currency")}
    received_today = {r["currency"]: round(r["s"], 2) for r in conn.execute("SELECT currency, SUM(amount) s FROM payments WHERE received_date=? GROUP BY currency", (today,))}
    est_today: dict[str, float] = {}
    import json
    for r in conn.execute("""SELECT a.currency, t.economics_json FROM assignments a JOIN task_intel t ON t.assignment_id=a.id
                             WHERE substr(a.updated_at,1,10)=? AND a.stage IN ('ready_for_delivery','submitted','awaiting_review','work_accepted','payment_pending')""", (today,)):
        v = (json.loads(r["economics_json"]) or {}).get("risk_adjusted_value")
        if v:
            est_today[r["currency"] or "unstated"] = round(est_today.get(r["currency"] or "unstated", 0) + v, 2)
    minutes = sum((x["actual_minutes"] or 0) for x in (_outcome(conn, r["id"]) for r in conn.execute("SELECT id FROM assignments WHERE stage='paid'")))
    usd = received.get("USD")
    t = totals(conn)
    return {"received_by_currency": received, "received_today_by_currency": received_today, "estimated_today_by_currency": est_today,
            "pending_by_currency": t["payment_pending"]["by_currency"], "approved": t["work_accepted"], "submitted": t["deliverables_submitted"],
            "paid_tasks": conn.execute("SELECT COUNT(*) FROM assignments WHERE stage='paid'").fetchone()[0],
            "usd_per_hour_of_execution": round(usd / (minutes / 60), 2) if usd and minutes else None,
            "note": "Received = payments you recorded with a reference. 'Estimated today' is work packaged or submitted today, discounted by acceptance history: not income."}


def by_platform(conn) -> list[dict[str, Any]]:
    """Confirmed (received) money per platform and currency, from recorded payments only."""
    return db.rows(conn.execute("SELECT COALESCE(platform_id,'unassigned') AS platform_id, currency, ROUND(SUM(amount),2) AS received, COUNT(*) AS payments, MAX(received_date) AS last_payment "
                                "FROM payments GROUP BY platform_id, currency ORDER BY received DESC"))


def trend(conn, months: int = 12) -> list[dict[str, Any]]:
    """Received money per month and currency."""
    return db.rows(conn.execute("SELECT substr(received_date,1,7) AS month, currency, ROUND(SUM(amount),2) AS received, COUNT(*) AS payments FROM payments "
                                "GROUP BY month, currency ORDER BY month DESC LIMIT ?", (months * 4,)))


# ------------------------------------------------------------------------------------------- payout routes (Kenya)
def set_route(conn, platform_id: str, method: str, account_verified: bool, evidence: str, confirmed_by_user: bool) -> dict[str, Any]:
    """Record what YOUR account actually offers. Account-level compatibility is only ever set by you, with where you saw it."""
    if platform_id not in policy.load_capabilities():
        raise LedgerError(f"Unknown platform '{platform_id}'")
    if method not in METHODS:
        raise LedgerError("Unknown payout method")
    if confirmed_by_user is not True or not (evidence or "").strip():
        raise LedgerError("Confirm it yourself (confirmed_by_user=true) and say where your account shows this payout method")
    general = _general(policy.load_capabilities()[platform_id])
    with db.tx(conn):
        conn.execute("""INSERT INTO payout_routes(platform_id,method,general_availability,account_verified,evidence,updated_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(platform_id,method) DO UPDATE SET account_verified=excluded.account_verified, evidence=excluded.evidence, updated_at=excluded.updated_at""",
                     (platform_id, method, general, 1 if account_verified else 0, evidence.strip()[:300], db.now_iso()))
        db.audit(conn, "ledger.payout_route", "platform", platform_id, {"method": method, "account_verified": bool(account_verified)})
    return {"platform_id": platform_id, "method": method, "account_verified": bool(account_verified)}


def _general(record: dict[str, Any]) -> str:
    status = (record.get("payment_verification") or {}).get("status", "unverified")
    return status if status in AVAILABILITY else "unverified"


def payout_compatibility(conn) -> list[dict[str, Any]]:
    """Per platform: what the platform documents in general, and separately what you verified in your own account."""
    caps = policy.load_capabilities()
    mine: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM payout_routes ORDER BY platform_id, method"):
        mine.setdefault(r["platform_id"], []).append({"method": r["method"], "account_verified": bool(r["account_verified"]), "evidence": r["evidence"], "updated_at": r["updated_at"]})
    out = []
    for pid, rec in caps.items():
        pay = rec.get("payment_verification") or {}
        note = (pay.get("note") or "").lower()
        mentioned = [m for m, words in (("paypal", ("paypal",)), ("payoneer", ("payoneer",)), ("bank_transfer", ("bank", "wire", "ach")), ("mpesa", ("m-pesa", "mpesa")))
                     if any(w in note for w in words)]
        routes = mine.get(pid, [])
        out.append({"platform_id": pid, "name": rec.get("name", pid), "general_availability": _general(rec), "general_note": pay.get("note"),
                    "methods_mentioned": mentioned, "account_routes": routes, "account_verified": any(r["account_verified"] for r in routes),
                    "kenya": (rec.get("country_eligibility") or {}).get("kenya", "unverified")})
    return out


OWNED_METHODS = ("paypal", "payoneer", "mpesa", "airtel_money", "card", "crypto", "stripe", "bank_transfer")


def set_owned_methods(conn, methods: Any, confirmed_by_user: bool) -> list[str]:
    """The payout methods YOU have accounts for. This says nothing about which ones a given platform pays out to."""
    if confirmed_by_user is not True:
        raise LedgerError("Only you can record which payout methods you have (confirmed_by_user=true)")
    if not isinstance(methods, list) or any(m not in OWNED_METHODS for m in methods):
        raise LedgerError("Methods must be a list from: " + ", ".join(OWNED_METHODS))
    clean = sorted(set(methods))
    with db.tx(conn):
        db.set_state(conn, "payout_methods_owned", {"methods": clean, "recorded_at": db.now_iso()})
        db.audit(conn, "ledger.owned_methods", "settings", "payout", {"methods": clean})
    return clean


def owned_methods(conn) -> dict[str, Any]:
    return db.get_state(conn, "payout_methods_owned", {}) or {"methods": []}


def view(conn) -> dict[str, Any]:
    return {"totals": totals(conn), "entries": entries(conn), "by_platform": by_platform(conn), "trend": trend(conn),
            "payout_compatibility": payout_compatibility(conn), "methods": list(METHODS), "owned_methods": owned_methods(conn)}
