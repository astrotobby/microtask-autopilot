"""JSON endpoints of the freelance business engine (everything under /api/business). Called from api.Api, which owns the
connection, the credential-field guard and error mapping. Actions that need a person are recorded with your evidence."""
from __future__ import annotations

from typing import Any

try:
    from microtask_monitor import (biz_config as bc, biz_engine as be, biz_finance as bf, biz_leads as bl, biz_portfolio as bport, biz_projects as bpj,
                                   biz_proposals as bp, db)
except ImportError:  # pragma: no cover
    import biz_config as bc, biz_engine as be, biz_finance as bf, biz_leads as bl, biz_portfolio as bport, biz_projects as bpj, biz_proposals as bp, db


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise bc.BizError("An id must be a number")


def _q(q: dict, k: str) -> str:
    v = q.get(k, "")
    return ((v[0] if isinstance(v, list) else v) or "").strip()


def dashboard(conn) -> dict[str, Any]:
    o = bf.overview(conn)
    return {"overview": o, "gap": bf.gap_analysis(conn), "leads": bl.ranked(conn, limit=60),
            "proposals": db.rows(conn.execute("""SELECT p.id,p.lead_id,p.mode,p.status,p.price,p.currency,p.created_at,p.sent_at,p.reply_at,l.title,l.source_id,l.url,l.contact
                                                 FROM biz_proposals p JOIN biz_leads l ON l.id=p.lead_id ORDER BY p.id DESC LIMIT 60""")),
            "projects": bpj.listing(conn),
            "invoices": db.rows(conn.execute("SELECT i.*, p.title FROM biz_invoices i LEFT JOIN biz_projects p ON p.id=i.project_id ORDER BY i.id DESC LIMIT 60")),
            "costs": db.rows(conn.execute("SELECT * FROM biz_costs ORDER BY id DESC LIMIT 60")),
            "retainers": db.rows(conn.execute("SELECT r.*, c.name client FROM biz_retainers r LEFT JOIN biz_clients c ON c.id=r.client_id ORDER BY r.id DESC")),
            "clients": db.rows(conn.execute("""SELECT c.id,c.name,c.email,c.source_id,c.do_not_contact,COUNT(p.id) projects FROM biz_clients c
                                               LEFT JOIN biz_projects p ON p.client_id=c.id GROUP BY c.id ORDER BY projects DESC, c.id DESC LIMIT 100""")),
            "catalogue": bport.catalogue(), "health": be.health(conn), "statuses": list(bl.STATUSES)}


def route(api, conn, method: str, rest: list[str], q: dict, body: dict) -> dict[str, Any]:
    n, head = len(rest), (rest[0] if rest else "")
    if method == "GET":
        if n == 0:
            return {"ok": True, **dashboard(conn)}
        if head == "leads" and n == 1:
            return {"ok": True, "leads": bl.ranked(conn, _q(q, "status") or None, 300)}
        if head == "leads" and n == 2:
            return {"ok": True, "lead": bl.get(conn, _int(rest[1]))}
        if head == "proposals" and n == 2:
            r = conn.execute("SELECT * FROM biz_proposals WHERE id=?", (_int(rest[1]),)).fetchone()
            if not r:
                raise bc.BizError("Proposal not found")
            return {"ok": True, "proposal": dict(r)}
        if head == "projects" and n == 2:
            return {"ok": True, "project": bpj.view(conn, _int(rest[1]))}
        if head == "finance":
            return {"ok": True, **bf.overview(conn, _q(q, "month") or None)}
        if head == "review":
            return {"ok": True, **bf.monthly_review(conn, _q(q, "month") or None)}
        if head == "config":
            return {"ok": True, "config": bc.load(), "sources": bc.sources()}
        if head == "health":
            return {"ok": True, **be.health(conn)}
    if method == "PUT" and rest == ["config"]:
        return {"ok": True, "config": bc.save_overrides(body)}
    if method == "POST":
        if rest == ["enquiry"]:
            return {"ok": True, "result": bl.import_enquiry(conn, body)}
        if rest == ["discover"]:
            return {"ok": True, "result": bl.discover(conn, force=bool(body.get("force")))}
        if rest == ["tick"]:
            return {"ok": True, "result": be.tick(conn)}
        if rest == ["inbox", "import"]:
            return {"ok": True, "result": be.inbound(conn)}
        if rest == ["leads", "import"]:
            items = body.get("items")
            if not isinstance(items, list) or not items:
                raise bc.BizError("items must be a non-empty list of opportunities (title, description, url, budget)")
            return {"ok": True, "result": bl.ingest(conn, items[:100], "manual")}
        if head == "leads" and n == 3 and rest[2] == "propose":
            return {"ok": True, **bp.propose(conn, _int(rest[1]))}
        if head == "leads" and n == 3 and rest[2] == "status":
            bl.set_status(conn, _int(rest[1]), str(body.get("status")), "you: " + str(body.get("reason") or ""))
            return {"ok": True}
        if head == "proposals" and n == 3:
            pid, act = _int(rest[1]), rest[2]
            if act == "sent":
                return {"ok": True, **bp.mark_sent(conn, pid, str(body.get("evidence") or ""))}
            if act == "reply":
                return {"ok": True, **bp.record_reply(conn, pid, str(body.get("text") or ""), str(body.get("outcome") or ""))}
            if act == "award":
                return {"ok": True, **bpj.award(conn, pid, str(body.get("evidence") or ""), body.get("ai_permitted") is True, str(body.get("ai_source") or ""),
                                                 str(body.get("delivery_channel") or "platform"), body.get("deadline") or None, body.get("price"))}
        if head == "projects" and n == 3:
            pid, act = _int(rest[1]), rest[2]
            if act == "start":
                return {"ok": True, **bpj.start(conn, pid, exec_dir=getattr(api, "exec_dir", None))}
            if act == "execute":
                return {"ok": True, **bpj.execute(conn, pid, None, getattr(api, "db_path", None), getattr(api, "exec_dir", None))}
            if act == "refresh":
                return {"ok": True, **bpj.refresh(conn, pid)}
            if act == "handover":
                return {"ok": True, **bpj.handover(conn, pid)}
            if act == "deliver":
                return {"ok": True, **bpj.deliver(conn, pid, None, str(body.get("evidence") or "") or None)}
            if act == "accept":
                return {"ok": True, **bpj.client_accepted(conn, pid, str(body.get("evidence") or ""), body.get("hours_logged"))}
            if act == "change":
                return {"ok": True, **bpj.propose_change(conn, pid, str(body.get("description") or ""))}
            if act == "progress":
                return {"ok": True, **bpj.progress_note(conn, pid)}
            if act == "invoice":
                return {"ok": True, "invoice": bf.create_invoice(conn, pid, body.get("amount"), str(body.get("kind") or "one_time"), int(body.get("due_days") or 7))}
        if head == "changes" and n == 3 and rest[2] == "decide":
            return {"ok": True, **bpj.decide_change(conn, _int(rest[1]), body.get("approved") is True, str(body.get("evidence") or ""))}
        if head == "invoices" and n == 3:
            iid, act = _int(rest[1]), rest[2]
            if act == "sent":
                bf.mark_invoice_sent(conn, iid, str(body.get("evidence") or ""))
                return {"ok": True}
            if act == "payment":
                return {"ok": True, **bf.record_payment(conn, iid, body.get("amount"), str(body.get("received_date") or ""), str(body.get("method") or ""),
                                                         str(body.get("reference") or ""))}
        if rest == ["costs"]:
            return {"ok": True, "id": bf.add_cost(conn, str(body.get("kind") or ""), float(body.get("amount") or 0), str(body.get("currency") or "USD"),
                                                  body.get("project_id"), str(body.get("note") or ""))}
        if head == "retainers" and n == 3 and rest[2] == "activate":
            bf.activate_retainer(conn, _int(rest[1]), str(body.get("evidence") or ""), body.get("monthly_amount"))
            return {"ok": True}
        if rest == ["portfolio", "build"]:
            return {"ok": True, **bport.build_site()}
    raise bc.BizError(f"Unknown business endpoint: {method} /api/business/{'/'.join(rest)}")
