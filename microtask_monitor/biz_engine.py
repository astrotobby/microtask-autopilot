"""The freelance business orchestrator: one scheduled cycle that does everything your rules pre-authorize, and nothing else.

Each cycle (every 30 minutes from the main scheduler, at most once per lease):
  1. discover    permitted sources at their own intervals (feeds, official APIs, listings the engine found), when online
  2. inbound     enquiry and marketplace-alert .eml files dropped in data/business_inbox, and (only if you switched on the
                 read-only Gmail monitor) emails whose subject starts with "Project enquiry"
  3. propose     prepare tailored proposals for the best new qualified leads (autonomy.prepare_proposals), capped per day
  4. deliver     refresh awarded projects; start and run them through the orchestrator under autonomy.execute_awarded_projects
  5. recurring   suggest retainers after accepted deliveries, draft the month's retainer invoices, flag overdue invoices
Everything that needs a person (submitting a mode-B proposal, accepting terms, signing, payment details, scope changes) waits.
Every step is idempotent: a restart repeats nothing that already happened.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

try:
    from microtask_monitor import biz_config as bc, biz_finance as bf, biz_leads as bl, biz_projects as bpj, biz_proposals as bp, db
except ImportError:  # pragma: no cover
    import biz_config as bc, biz_finance as bf, biz_leads as bl, biz_projects as bpj, biz_proposals as bp, db

INBOX = db.DATA_DIR / "business_inbox"
MAX_PROPOSALS_PER_DAY = 5


def inbound(conn, inbox: Path | None = None, imap: Any = None) -> dict[str, Any]:
    folder = Path(inbox or INBOX)
    seen = set(db.get_state(conn, "biz_inbox_seen", []) or [])
    import hashlib
    raws, keys = [], []
    if folder.is_dir():
        for p in sorted(folder.glob("*.eml")):
            raw = p.read_bytes()
            h = hashlib.sha256(raw).hexdigest()
            if h not in seen:
                raws.append(raw)
                keys.append(h)
    imap_error = None
    if imap is not None:
        try:
            fetched = imap()
        except Exception as exc:                                    # noqa: BLE001 - network or login problem: recorded, retried next cycle
            fetched, imap_error = [], f"{type(exc).__name__}: {str(exc)[:200]}"
        for raw in fetched:
            h = hashlib.sha256(raw).hexdigest()
            if h not in seen:
                raws.append(raw)
                keys.append(h)
    out = bl.import_email(conn, raws) if raws else {}
    with db.tx(conn):
        db.set_state(conn, "biz_inbox_seen", sorted(seen | set(keys))[-20000:])
    return {"messages": len(raws), "by_source": out, "imap_error": imap_error}


def enquiry_reader(address: str, password: str, days: int = 14, connect: Any = None) -> Callable[[], list[bytes]]:
    """Read-only: messages in your inbox whose subject contains 'Project enquiry' (the website form's fallback). BODY.PEEK, readonly select."""
    def fetch() -> list[bytes]:
        import imaplib
        import ssl
        from datetime import datetime, timedelta, timezone
        m = (connect or (lambda: imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context(), timeout=60)))()
        out: list[bytes] = []
        try:
            m.login(address, password)
            m.select("INBOX", readonly=True)
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
            typ, data = m.search(None, "SUBJECT", '"Project enquiry"', "SINCE", since)
            for num in (data[0].split() if typ == "OK" and data and data[0] else [])[-50:]:
                typ, parts = m.fetch(num, "(BODY.PEEK[])")
                if typ == "OK" and parts and isinstance(parts[0], tuple):
                    out.append(parts[0][1])
        finally:
            try:
                m.logout()
            except Exception:                                     # noqa: BLE001
                pass
        return out
    return fetch


def auto_propose(conn, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    if not cfg["autonomy"].get("prepare_proposals"):
        return []
    today = date.today().isoformat()
    done = conn.execute("SELECT COUNT(*) FROM biz_proposals WHERE substr(created_at,1,10)=?", (today,)).fetchone()[0]
    out = []
    for lead in bl.ranked(conn, "qualified", limit=max(0, MAX_PROPOSALS_PER_DAY - done)):
        r = bp.propose(conn, lead["id"], cfg)
        out.append({"lead": lead["id"], "created": r["created"], "mode": r.get("mode"), "reason": r.get("reason")})
    return out


def deliver_cycle(conn, cfg: dict[str, Any], runner: Any = None, db_path: Any = None, exec_dir: Any = None) -> list[dict[str, Any]]:
    out = []
    for p in conn.execute("SELECT id,status FROM biz_projects WHERE status IN ('awarded','in_progress','qa')").fetchall():
        try:
            if p["status"] == "awarded" and cfg["autonomy"].get("execute_awarded_projects"):
                bpj.start(conn, p["id"], exec_dir=exec_dir)
            aid = bpj._assignment(conn, p["id"])
            has_job = bool(aid and conn.execute("SELECT execution_job_id FROM assignments WHERE id=?", (aid,)).fetchone()["execution_job_id"])
            if cfg["autonomy"].get("execute_awarded_projects") and p["status"] in ("awarded", "in_progress") and not has_job:
                r = bpj.execute(conn, p["id"], runner, db_path, exec_dir)
                out.append({"project": p["id"], "started": r.get("started"), "status": r.get("status"), "reasons": r.get("reasons")})
            else:
                out.append({"project": p["id"], **bpj.refresh(conn, p["id"])})
        except bc.BizError as exc:
            out.append({"project": p["id"], "error": str(exc)})
    return out


def recurring(conn) -> dict[str, Any]:
    with db.tx(conn):
        overdue = conn.execute("UPDATE biz_invoices SET status='overdue' WHERE status='sent' AND due_at < date('now')").rowcount
    return {"retainers_suggested": bf.suggest_retainers(conn), "retainer_invoices": [i["number"] for i in bf.bill_retainers(conn)], "invoices_overdue": overdue}


def tick(conn, http: Callable[[str], bytes] = bl.http_get, online: bool = True, runner: Any = None, db_path: Any = None, exec_dir: Any = None,
         imap: Any = None) -> dict[str, Any]:
    cfg = bc.load()
    out: dict[str, Any] = {}
    out["discover"] = bl.discover(conn, http) if online else "offline: discovery skipped"
    out["inbound"] = inbound(conn, imap=imap)
    out["proposals"] = auto_propose(conn, cfg)
    out["projects"] = deliver_cycle(conn, cfg, runner, db_path, exec_dir)
    out["recurring"] = recurring(conn)
    with db.tx(conn):
        db.set_state(conn, "biz_last_tick", {"at": db.now_iso(), "summary": {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in out.items()}})
    return out


def health(conn) -> dict[str, Any]:
    srcs = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM biz_sources")}
    return {"last_tick": db.get_state(conn, "biz_last_tick", {}) or {},
            "sources": [{**{k: s.get(k) for k in ("id", "name", "kind", "fetch", "kenya", "fees")}, "policy": s["policy"], **(srcs.get(s["id"]) or {"last_status": "never_run"})}
                        for s in bc.sources()],
            "security_events": db.rows(conn.execute("SELECT at,action,entity,entity_id,detail_json FROM biz_audit WHERE action LIKE '%refused%' OR action LIKE '%blocked%' ORDER BY id DESC LIMIT 20"))}
