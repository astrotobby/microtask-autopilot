"""Business finance: invoices, collected payments, costs, profitability, recurring revenue, the monthly review and the gap
to your targets.

Four money states are never mixed:
    pipeline     qualified leads and open proposals: potential work, not revenue
    contracted   awarded project value not yet invoiced: not collected
    invoiced     invoices sent and unpaid: requested, not received
    collected    payments you recorded with a date and reference: the only money that counts
Collected money is written to the existing ``payments`` table, so the microtask and freelance channels share one ledger, and
the monthly target counts both. This module never moves money: withdrawals and transfers stay with you.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

try:
    from microtask_monitor import biz_config as bc, biz_projects as bpj, db, workflow as wf
except ImportError:  # pragma: no cover
    import biz_config as bc, biz_projects as bpj, db, workflow as wf

COST_KINDS = ("platform_fee", "payment_fee", "tool", "operating", "refund")
# Every payment recorded against an invoice, however many part payments it took.
BIZ_PAY = ("(SELECT CAST(entity_id AS INTEGER) payment_id, json_extract(detail_json,'$.invoice') invoice_id FROM biz_audit WHERE action='payment.received')")


def _payment_platform(conn, project: dict[str, Any]) -> str:
    try:
        return bc.source(project.get("channel") or "manual").get("platform_id") or "direct_client"
    except bc.BizError:
        return "direct_client"


def create_invoice(conn, project_id: int, amount: float | None = None, kind: str = "one_time", due_days: int = 7, note: str = "") -> dict[str, Any]:
    prj = bpj._row(conn, project_id)
    if kind not in ("one_time", "deposit", "recurring"):
        raise bc.BizError("kind must be one_time, deposit or recurring")
    billed = conn.execute("SELECT COALESCE(SUM(amount),0) FROM biz_invoices WHERE project_id=? AND status!='void' AND kind!='recurring'", (project_id,)).fetchone()[0]
    if prj["pricing"] == "hourly" and amount is None:
        raise bc.BizError("Hourly work: give the amount (hours worked x rate)")
    amount = float(amount if amount is not None else prj["price"] - billed)
    if amount <= 0:
        raise bc.BizError("Nothing left to invoice on this project")
    if kind != "recurring" and prj["pricing"] != "hourly" and billed + amount > prj["price"] + 1e-6:
        raise bc.BizError(f"That would invoice more than the agreed price (USD {prj['price']:g}); approve a scope change first")
    year = date.today().year
    n = conn.execute("SELECT COUNT(*) FROM biz_invoices WHERE number LIKE ?", (f"INV-{year}-%",)).fetchone()[0] + 1
    number = f"INV-{year}-{n:04d}"
    today = date.today()
    with db.tx(conn):
        iid = conn.execute("""INSERT INTO biz_invoices(project_id,client_id,number,amount,currency,kind,status,issued_at,due_at,created_at)
                              VALUES(?,?,?,?,?,?,'draft',?,date(?, '+' || ? || ' days'),?)""",
                           (project_id, prj["client_id"], number, amount, prj["currency"], kind, today.isoformat(), today.isoformat(), int(due_days), db.now_iso())).lastrowid
        bc.audit(conn, "invoice.create", "invoice", iid, {"project": project_id, "amount": amount, "kind": kind, "note": note})
    return dict(conn.execute("SELECT * FROM biz_invoices WHERE id=?", (iid,)).fetchone())


def mark_invoice_sent(conn, invoice_id: int, evidence: str) -> None:
    if not bc.norm(evidence):
        raise bc.BizError("Say how the invoice was sent (platform invoice, email, payment link)")
    with db.tx(conn):
        n = conn.execute("UPDATE biz_invoices SET status='sent' WHERE id=? AND status='draft'", (invoice_id,)).rowcount
        if n:
            bc.audit(conn, "invoice.sent", "invoice", invoice_id, {"evidence": evidence}, actor="user")


def record_payment(conn, invoice_id: int, amount: float, received_date: str, method: str, reference: str) -> dict[str, Any]:
    """Money you actually received for an invoice: written to the unified payments ledger. A reference is mandatory."""
    inv = conn.execute("SELECT * FROM biz_invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        raise bc.BizError("Invoice not found")
    if inv["status"] in ("paid", "void"):
        raise bc.BizError(f"Invoice {inv['number']} is already {inv['status']}")
    if not bc.norm(reference):
        raise bc.BizError("A payment reference (transaction id) is required")
    if conn.execute("SELECT 1 FROM payments WHERE reference=? AND method=?", (reference[:120], method[:60])).fetchone():
        raise bc.BizError("A payment with this reference is already recorded")
    prj = bpj._row(conn, inv["project_id"]) if inv["project_id"] else {"channel": "manual"}
    pay = wf.record_payment(conn, amount, inv["currency"], received_date, platform_id=_payment_platform(conn, prj), method=method, reference=reference[:120])
    with db.tx(conn):
        bc.audit(conn, "payment.received", "payment", pay["id"], {"invoice": invoice_id, "amount": float(amount), "reference": reference}, actor="user")
        paid = conn.execute("SELECT COALESCE(SUM(p.amount),0) FROM payments p JOIN biz_audit a ON a.action='payment.received' AND a.entity_id=CAST(p.id AS TEXT) "
                            "WHERE json_extract(a.detail_json,'$.invoice')=?", (invoice_id,)).fetchone()[0]
        if paid + 1e-6 >= inv["amount"]:
            conn.execute("UPDATE biz_invoices SET status='paid', paid_at=?, payment_id=? WHERE id=?", (received_date, pay["id"], invoice_id))
        else:
            conn.execute("UPDATE biz_invoices SET status='part_paid', payment_id=? WHERE id=?", (pay["id"], invoice_id))
    return {"payment": pay, "invoice_status": "paid" if paid + 1e-6 >= inv["amount"] else "part_paid", "paid_so_far": round(paid, 2)}


def add_cost(conn, kind: str, amount: float, currency: str = "USD", project_id: int | None = None, note: str = "") -> int:
    if kind not in COST_KINDS:
        raise bc.BizError("kind must be one of " + ", ".join(COST_KINDS))
    if float(amount) < 0:
        raise bc.BizError("A cost cannot be negative")
    with db.tx(conn):
        cid = conn.execute("INSERT INTO biz_costs(project_id,kind,amount,currency,note,at) VALUES(?,?,?,?,?,?)",
                           (project_id, kind, float(amount), currency.upper(), note[:300], db.now_iso())).lastrowid
        bc.audit(conn, "cost.add", "cost", cid, {"kind": kind, "amount": amount, "project": project_id}, actor="user")
    return cid


# ------------------------------------------------------------------------------------------- recurring revenue
def suggest_retainers(conn) -> list[dict[str, Any]]:
    """After an accepted delivery, suggest a maintenance retainer where the work keeps needing care (never more than one per project)."""
    cfg = bc.load()
    pkg = next((p for p in cfg["packages"] if p.get("recurring")), {"from_usd": 150})
    out = []
    for p in conn.execute("SELECT * FROM biz_projects WHERE status='accepted'"):
        line = cfg["service_lines"].get(p["service_line"] or "", {})
        service = line.get("recurring")
        if not service:
            continue
        reason = f"{line.get('label', 'This work')} keeps needing updates and monitoring after delivery"
        with db.tx(conn):
            cur = conn.execute("""INSERT OR IGNORE INTO biz_retainers(client_id,project_id,service,monthly_amount,currency,status,reason,created_at)
                                  VALUES(?,?,?,?,?,'suggested',?,?)""", (p["client_id"], p["id"], service, float(pkg["from_usd"]), p["currency"], reason, db.now_iso()))
        if cur.rowcount:
            out.append({"project_id": p["id"], "service": service, "monthly_amount": pkg["from_usd"], "reason": reason})
    return out


def activate_retainer(conn, retainer_id: int, evidence: str, monthly_amount: float | None = None) -> None:
    if not bc.norm(evidence):
        raise bc.BizError("Record where the client agreed to the retainer")
    with db.tx(conn):
        conn.execute("UPDATE biz_retainers SET status='active', started_at=?, monthly_amount=COALESCE(?, monthly_amount) WHERE id=? AND status='suggested'",
                     (date.today().isoformat(), monthly_amount, retainer_id))
        bc.audit(conn, "retainer.active", "retainer", retainer_id, {"evidence": evidence}, actor="user")


def bill_retainers(conn, month: str | None = None) -> list[dict[str, Any]]:
    """One draft invoice per active retainer per month (idempotent)."""
    month = month or date.today().strftime("%Y-%m")
    out = []
    for r in conn.execute("SELECT * FROM biz_retainers WHERE status='active' AND project_id IS NOT NULL"):
        tag = f"retainer:{r['id']}:{month}"
        if conn.execute("SELECT 1 FROM biz_audit WHERE action='invoice.create' AND json_extract(detail_json,'$.note')=?", (tag,)).fetchone():
            continue
        out.append(create_invoice(conn, r["project_id"], r["monthly_amount"], "recurring", note=tag))
    return out


# ------------------------------------------------------------------------------------------- reporting
def overview(conn, month: str | None = None) -> dict[str, Any]:
    cfg = bc.load()
    month = month or date.today().strftime("%Y-%m")
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0          # noqa: E731
    biz_pay_ids = "SELECT CAST(entity_id AS INTEGER) FROM biz_audit WHERE action='payment.received'"
    collected_all = one("SELECT SUM(amount) FROM payments WHERE currency='USD' AND substr(received_date,1,7)=?", month)
    collected_biz = one(f"SELECT SUM(amount) FROM payments WHERE currency='USD' AND substr(received_date,1,7)=? AND id IN ({biz_pay_ids})", month)
    fees = one("SELECT SUM(amount) FROM biz_costs WHERE currency='USD' AND kind IN ('platform_fee','payment_fee') AND substr(at,1,7)=?", month)
    costs = one("SELECT SUM(amount) FROM biz_costs WHERE currency='USD' AND kind NOT IN ('platform_fee','payment_fee') AND substr(at,1,7)=?", month)
    invoiced_unpaid = one("SELECT SUM(amount) FROM biz_invoices WHERE status IN ('sent','part_paid','overdue') AND currency='USD'")
    contracted = one("""SELECT SUM(p.price - COALESCE((SELECT SUM(i.amount) FROM biz_invoices i WHERE i.project_id=p.id AND i.status!='void' AND i.kind!='recurring'),0))
                        FROM biz_projects p WHERE p.status NOT IN ('cancelled') AND p.currency='USD' AND p.pricing='fixed'""")
    pipeline = one("SELECT SUM(price) FROM biz_proposals WHERE status IN ('prepared','sent','replied') AND currency='USD'")
    mrr = one("SELECT SUM(monthly_amount) FROM biz_retainers WHERE status='active' AND currency='USD'")
    hours = one("SELECT SUM(hours_logged) FROM biz_projects WHERE status='accepted'")
    by_line = db.rows(conn.execute(f"""SELECT COALESCE(pr.service_line,'other') service_line, ROUND(SUM(p.amount),2) collected FROM payments p
        JOIN {BIZ_PAY} bp ON bp.payment_id=p.id JOIN biz_invoices i ON i.id=bp.invoice_id JOIN biz_projects pr ON pr.id=i.project_id WHERE p.currency='USD' GROUP BY pr.service_line ORDER BY collected DESC"""))
    by_channel = db.rows(conn.execute(f"""SELECT COALESCE(pr.channel,'other') channel, ROUND(SUM(p.amount),2) collected, COUNT(DISTINCT pr.id) projects FROM payments p
        JOIN {BIZ_PAY} bp ON bp.payment_id=p.id JOIN biz_invoices i ON i.id=bp.invoice_id JOIN biz_projects pr ON pr.id=i.project_id WHERE p.currency='USD' GROUP BY pr.channel ORDER BY collected DESC"""))
    funnel = {"leads": one("SELECT COUNT(*) FROM biz_leads"), "qualified": one("SELECT COUNT(*) FROM biz_leads WHERE status NOT IN ('new','rejected')"),
              "proposals_prepared": one("SELECT COUNT(*) FROM biz_proposals"), "proposals_sent": one("SELECT COUNT(*) FROM biz_proposals WHERE sent_at IS NOT NULL"),
              "responses": one("SELECT COUNT(*) FROM biz_proposals WHERE reply_at IS NOT NULL"), "won": one("SELECT COUNT(*) FROM biz_projects"),
              "active": one("SELECT COUNT(*) FROM biz_projects WHERE status IN ('awarded','in_progress','qa','ready_to_deliver')"),
              "delivered": one("SELECT COUNT(*) FROM biz_projects WHERE status IN ('delivered','accepted')"),
              "cancelled": one("SELECT COUNT(*) FROM biz_projects WHERE status IN ('cancelled','disputed')"),
              "active_clients": one("SELECT COUNT(DISTINCT client_id) FROM biz_projects WHERE status NOT IN ('cancelled')")}
    sent = funnel["proposals_sent"]
    funnel["win_rate"] = round(funnel["won"] / sent, 3) if sent else None
    targets = cfg["targets"]
    gross, net = round(collected_all, 2), round(collected_all - fees - costs, 2)
    return {"month": month, "collected_usd": gross, "collected_freelance_usd": round(collected_biz, 2), "collected_microtask_usd": round(collected_all - collected_biz, 2),
            "platform_and_payment_fees_usd": round(fees, 2), "operating_costs_usd": round(costs, 2), "net_operating_income_usd": net,
            "invoiced_unpaid_usd": round(invoiced_unpaid, 2), "contracted_not_invoiced_usd": round(contracted, 2), "pipeline_usd": round(pipeline, 2),
            "recurring_monthly_usd": round(mrr, 2), "effective_hourly_usd": round(collected_biz / hours, 2) if hours and collected_biz else None,
            "by_service_line": by_line, "by_channel": by_channel, "funnel": funnel, "targets": targets,
            "target_progress": {k: {"target": v, "collected": gross, "percent": round(100 * gross / v, 1) if v else None, "gap": round(max(0, v - gross), 2)} for k, v in targets.items()},
            "note": "Collected = payments you recorded with a reference (both channels). Pipeline, contracted and invoiced amounts are not income."}


def gap_analysis(conn, month: str | None = None, target_key: str = "primary") -> dict[str, Any]:
    o = overview(conn, month)
    cfg = bc.load()
    target = cfg["targets"][target_key]
    gap = max(0.0, target - o["collected_usd"])
    avg = conn.execute("SELECT AVG(price) FROM biz_projects WHERE status NOT IN ('cancelled') AND currency='USD' AND pricing='fixed'").fetchone()[0]
    basis = "your projects so far" if avg else "your smallest package price"
    avg = float(avg or min(p["from_usd"] for p in cfg["packages"]))
    win = o["funnel"]["win_rate"] or 0.15
    projects = int(-(-gap // avg)) if gap else 0
    proposals = int(-(-projects // win)) if projects else 0
    return {"target": target, "collected": o["collected_usd"], "gap": round(gap, 2), "average_project_usd": round(avg, 2), "average_basis": basis,
            "win_rate_used": win, "win_rate_basis": "measured" if o["funnel"]["win_rate"] else "planning assumption (15%) until you have sent proposals",
            "projects_needed": projects, "proposals_needed": proposals, "invoiced_unpaid_that_would_close_it": o["invoiced_unpaid_usd"],
            "options": ([f"Collect the USD {o['invoiced_unpaid_usd']:g} already invoiced"] if o["invoiced_unpaid_usd"] else []) +
                       ([f"Win {projects} more project(s) at about USD {avg:,.0f} (about {proposals} well-matched proposals at a {round(win * 100)}% win rate)"] if projects else []) +
                       ([f"One retainer at USD {cfg['packages'][-1]['from_usd']}/month closes {round(100 * min(1, cfg['packages'][-1]['from_usd'] / gap))}% of the gap"] if gap else []) +
                       (["Raise prices on the service line with the best win rate once you have 3 genuine reviews"] if gap else [])}


def monthly_review(conn, month: str | None = None) -> dict[str, Any]:
    o = overview(conn, month)
    g = gap_analysis(conn, month)
    m = o["month"]
    conv = db.rows(conn.execute("""SELECT l.id, l.title, l.source_id, pr.price FROM biz_leads l JOIN biz_projects pr ON pr.lead_id=l.id
                                   WHERE substr(pr.created_at,1,7)=?""", (m,)))
    prof = []
    for p in conn.execute("SELECT id,title,price,hours_logged,status FROM biz_projects WHERE status IN ('delivered','accepted')"):
        got = conn.execute(f"SELECT COALESCE(SUM(p.amount),0) FROM payments p JOIN {BIZ_PAY} bp ON bp.payment_id=p.id JOIN biz_invoices i ON i.id=bp.invoice_id WHERE i.project_id=?", (p["id"],)).fetchone()[0]
        cost = conn.execute("SELECT COALESCE(SUM(amount),0) FROM biz_costs WHERE project_id=?", (p["id"],)).fetchone()[0]
        hourly = round((got - cost) / p["hours_logged"], 2) if p["hours_logged"] else None
        prof.append({"project_id": p["id"], "title": p["title"], "collected": got, "costs": cost, "hours": p["hours_logged"], "net_hourly": hourly,
                     "verdict": "profitable" if hourly and hourly >= bc.load()["pricing"]["min_effective_hourly_usd"] else ("unpaid so far" if not got else "below your minimum rate")})
    lines = [f"Monthly review {m}", f"Collected: USD {o['collected_usd']:,.2f} (freelance {o['collected_freelance_usd']:,.2f}, microtask {o['collected_microtask_usd']:,.2f})",
             f"Net operating income: USD {o['net_operating_income_usd']:,.2f} after fees {o['platform_and_payment_fees_usd']:,.2f} and costs {o['operating_costs_usd']:,.2f}",
             f"Target USD {g['target']}: gap USD {g['gap']:,.2f}"]
    nxt = g["options"][:3] or ["Keep the current mix: the target is met"]
    if not o["funnel"]["proposals_sent"]:
        nxt.insert(0, "No proposal has been submitted yet: submit the prepared proposals (mode B) for the top-scored leads")
    return {"month": m, "summary": lines, "overview": o, "gap": g, "converted_leads": conv, "project_profitability": prof,
            "best_channels": o["by_channel"][:3], "best_service_lines": o["by_service_line"][:3], "next_month": nxt}
