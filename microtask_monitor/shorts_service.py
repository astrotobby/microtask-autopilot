"""Owner lane, phase 1: the Podcast Shorts service on your own Whop business fulfils its orders by itself.

    paid order ─► support chat: "send your episode link" ─► link arrives ─► moments planned ─► clips rendered
        ─► executed QC (fail = re-cut, never shipped) ─► clips uploaded to Whop ─► delivered in the chat ─► sale booked

Every step is idempotent (database keys plus Whop Idempotency-Keys), so a crash or a re-run never messages a buyer
twice, never delivers twice and never books a sale twice. You are pulled in only when it matters:
refund or dispute, an order for a plan the config doesn't know, non-English or unusable footage, or a clip that
fails QC twice. The engine never refunds, withdraws or moves money; it only reads the payment and books it.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import clip_worker as cw, control, db, notify, seller as sl, workflow as wf
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import clip_worker as cw, control, db, notify, seller as sl, workflow as wf
    from secret_store import redact

CONFIG_PATH = db.DATA_DIR / "owner_config.json"          # data/ is git-ignored
REMINDER_AFTER = timedelta(hours=24)
MAX_ATTEMPTS = 2
OPEN_STATES = ("new", "awaiting_footage", "footage_received", "clipping", "delivering")
STATES = OPEN_STATES + ("delivered", "needs_you", "cancelled")
LEDGER_SOURCE = "owner:ai_video_pro"
REQUIRED_PERMISSIONS = ["payment:basic:read", "member:basic:read", "support_chat:create", "support_chat:read", "support_chat:message:create"]


class ServiceError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------- config
def load_config(path: Path | None = None) -> dict[str, Any]:
    p = Path(path or CONFIG_PATH)
    if not p.exists():
        raise ServiceError(f"No owner config at {p}. It names your business, product, plans and clipper folder.")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    for key in ("account_id", "product_id", "plans", "clipper_dir", "start_after"):
        if not cfg.get(key):
            raise ServiceError(f"Owner config is missing '{key}'")
    for pid, plan in cfg["plans"].items():
        if not (str(pid).startswith("plan_") and int(plan.get("clips", 0)) > 0 and int(plan.get("per_link", 0)) > 0):
            raise ServiceError(f"Plan {pid} needs clips and per_link")
    cfg.setdefault("min_clip_seconds", 25)
    cfg.setdefault("max_clip_seconds", 50)
    cfg.setdefault("work_dir", str(db.DATA_DIR / "owner" / "work"))
    return cfg


# ------------------------------------------------------------------------------------------- helpers
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _order(conn, oid: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM owner_orders WHERE id=?", (oid,)).fetchone()
    if not r:
        raise ServiceError("Order not found")
    return dict(r)


def _set(conn, oid: int, **fields: Any) -> None:
    fields["updated_at"] = db.now_iso()
    conn.execute(f"UPDATE owner_orders SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), oid))


def event(conn, oid: int | None, kind: str, detail: str, key: str | None = None) -> bool:
    """Log once per key. Returns False when that key was already logged (the action already happened)."""
    cur = conn.execute("INSERT OR IGNORE INTO owner_events(order_id,at,kind,detail,key) VALUES(?,?,?,?,?)",
                       (oid, db.now_iso(), kind, redact(detail)[:1000], key))
    return cur.rowcount == 1


def _done(conn, key: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM owner_events WHERE key=?", (key,)).fetchone())


def _money(obj: Any) -> float | None:
    try:
        return float((obj or {}).get("amount"))
    except (TypeError, ValueError, AttributeError):
        return None


def escalate(conn, oid: int, reason: str) -> None:
    o = _order(conn, oid)
    if o["state"] == "needs_you":
        return
    with db.tx(conn):
        _set(conn, oid, state="needs_you", state_reason=redact(reason)[:300])
        event(conn, oid, "needs_you", reason)
        db.audit(conn, "owner.escalate", "owner_order", oid, {"reason": redact(reason)[:200]}, actor="system")
        notify.create(conn, "task_attention", f"Shorts order needs you: {o['buyer_username'] or o['buyer_user_id']}", reason[:900],
                      severity="urgent", key=f"owner-escalate-{oid}")


# ------------------------------------------------------------------------------------------- intake and booking
def intake(conn, s: sl.Seller, cfg: dict[str, Any]) -> dict[str, int]:
    """New paid orders become owner_orders (once). Existing orders are re-checked for refunds and disputes."""
    stats = {"new": 0, "booked": 0, "escalated": 0}
    for p in s.paid_payments([cfg["product_id"]], since=cfg["start_after"]):
        pay_id = p["id"]
        line = next((li for li in p.get("line_items") or [] if li.get("product_id") == cfg["product_id"]), {})
        variant = p.get("variant_id") or line.get("variant_id")
        sub = str(p.get("substatus") or "succeeded")
        row = conn.execute("SELECT id, state FROM owner_orders WHERE payment_id=?", (pay_id,)).fetchone()
        if row:
            if sub != "succeeded" and row["state"] not in ("needs_you", "cancelled"):
                escalate(conn, row["id"], f"Payment {pay_id} is now '{sub}' (refund or dispute): fulfilment stopped")
                stats["escalated"] += 1
            continue
        plan = cfg["plans"].get(variant) or cfg.get("test_plans", {}).get(variant)
        buyer = p.get("user") or {}
        test = bool(variant in cfg.get("test_plans", {}) or (cfg.get("owner_user_id") and buyer.get("id") == cfg["owner_user_id"]))
        now = db.now_iso()
        with db.tx(conn):
            cur = conn.execute("""INSERT INTO owner_orders(payment_id,account_id,product_id,variant_id,plan_label,buyer_user_id,buyer_username,clips_total,per_link,
                gross_amount,net_amount,currency,paid_at,state,state_reason,test_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (pay_id, cfg["account_id"], cfg["product_id"], variant, (plan or {}).get("label"), buyer.get("id") or "",
                                buyer.get("username"), int((plan or {}).get("clips") or 0), int((plan or {}).get("per_link") or 0),
                                _money(p.get("total")), _money(p.get("amount_after_fees")), str(p.get("currency") or "").upper() or None,
                                p.get("paid_at"), "new", None, 1 if test else 0, now, now))
            oid = cur.lastrowid
            event(conn, oid, "order", f"Paid order {pay_id} ({(plan or {}).get('label') or variant}) from {buyer.get('username')}", f"order-{pay_id}")
        stats["new"] += 1
        if not plan:
            escalate(conn, oid, f"Order for plan {variant}, which is not in the owner config: map it to a clip count")
            stats["escalated"] += 1
            continue
        if sub != "succeeded":
            escalate(conn, oid, f"Payment {pay_id} arrived as '{sub}'")
            stats["escalated"] += 1
            continue
        if not buyer.get("id"):
            escalate(conn, oid, "Whop did not return the buyer's user id (check the key has member:basic:read)")
            stats["escalated"] += 1
            continue
        if book(conn, oid):
            stats["booked"] += 1
    return stats


def book(conn, oid: int) -> bool:
    """A paid Whop payment is money in your Whop balance: book it once, with the payment id as the reference. Test orders are not income."""
    o = _order(conn, oid)
    if o["booked_payment_row"] or o["test_order"] or not o["net_amount"] or not o["currency"]:
        return False
    day = str(o["paid_at"] or db.now_iso())[:10]
    pay = wf.record_payment(conn, o["net_amount"], o["currency"], day, None, LEDGER_SOURCE, "Whop sale (in your Whop balance)", o["payment_id"])
    with db.tx(conn):
        _set(conn, oid, booked_payment_row=pay["id"])
        event(conn, oid, "booked", f"{o['currency']} {o['net_amount']} after Whop fees, payment {o['payment_id']}", f"booked-{o['payment_id']}")
    return True


# ------------------------------------------------------------------------------------------- chat
def ask_for_footage(conn, s: sl.Seller, oid: int) -> None:
    o = _order(conn, oid)
    key = f"ask-{o['payment_id']}-{o['batch']}"
    channel = o["channel_id"] or s.support_channel(o["buyer_user_id"])
    remaining = o["clips_total"] - o["clips_delivered"]
    n = min(o["per_link"], remaining)
    if o["batch"] == 0:
        text = (f"Thanks for ordering **{o['plan_label']}**! Reply here with the link to your episode (YouTube or Google Drive). "
                f"Your {n} clips arrive within 48 hours of the link.")
    else:
        text = f"Ready for the next batch: reply with your next episode link (YouTube or Google Drive) for {n} more clips ({remaining} left on your plan)."
    if not _done(conn, key):
        s.send(channel, text, key)
    with db.tx(conn):
        event(conn, oid, "asked", text, key)
        _set(conn, oid, channel_id=channel, state="awaiting_footage", footage_requested_at=o["footage_requested_at"] or db.now_iso(), footage_url=None)


def check_footage(conn, s: sl.Seller, oid: int, now: datetime) -> bool:
    """Look for the buyer's link after the request. One friendly reminder after 24 hours; never more."""
    o = _order(conn, oid)
    asked = datetime.fromisoformat(o["footage_requested_at"])
    for m in s.messages(o["channel_id"]):
        author = (m.get("author") or {}).get("id")
        try:
            at = datetime.fromisoformat(str(m.get("created_at")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if author != o["buyer_user_id"] or at < asked - timedelta(seconds=5) or m.get("message_type") not in (None, "regular"):
            continue
        link = cw.find_link(m.get("content") or "")
        if link and not _done(conn, f"link-{m.get('id')}"):
            with db.tx(conn):
                event(conn, oid, "footage", f"Link received: {link}", f"link-{m.get('id')}")
                _set(conn, oid, state="footage_received", footage_url=link, footage_message_id=m.get("id"))
            return True
    if not o["reminder_sent_at"] and now - asked > REMINDER_AFTER:
        key = f"remind-{o['payment_id']}-{o['batch']}"
        text = "Just checking in: send your episode link (YouTube or Google Drive) here whenever you're ready and we'll start on your clips."
        if not _done(conn, key):
            s.send(o["channel_id"], text, key)
        with db.tx(conn):
            event(conn, oid, "reminded", text, key)
            _set(conn, oid, reminder_sent_at=db.now_iso())
    return False


# ------------------------------------------------------------------------------------------- fulfilment
def plan_batch(conn, media: Any, cfg: dict[str, Any], oid: int) -> None:
    o = _order(conn, oid)
    n = min(o["per_link"], o["clips_total"] - o["clips_delivered"])
    used = [(r["start"], r["end"]) for r in conn.execute("SELECT start,end FROM owner_clips WHERE order_id=? AND source_url=?", (oid, o["footage_url"]))]
    work = Path(cfg["work_dir"]) / f"order-{oid}" / f"batch-{o['batch']}"
    try:
        moments = cw.plan_moments(media, o["footage_url"], work, n, cfg["min_clip_seconds"], cfg["max_clip_seconds"], used)
    except (cw.ClipError, OSError, ValueError) as exc:
        escalate(conn, oid, f"Could not plan clips from {o['footage_url']}: {exc}")
        return
    except Exception as exc:                                       # noqa: BLE001 - download tools fail in many ways; a person decides
        escalate(conn, oid, f"Footage could not be read ({exc.__class__.__name__}: {str(exc)[:160]})")
        return
    now = db.now_iso()
    with db.tx(conn):
        for k, (a, b) in enumerate(moments):
            conn.execute("INSERT OR IGNORE INTO owner_clips(order_id,batch,idx,source_url,start,end,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (oid, o["batch"], k, o["footage_url"], a, b, "planned", now, now))
        _set(conn, oid, state="clipping", state_reason=f"{len(moments)} of {n} moments planned")
        event(conn, oid, "planned", f"{len(moments)} moments from {o['footage_url']}", f"planned-{oid}-{o['batch']}")
    if len(moments) < n:
        escalate(conn, oid, f"Only {len(moments)} usable moments found for {n} clips: a person should decide (another episode, or fewer clips)")


def cut_clips(conn, media: Any, cfg: dict[str, Any], oid: int, deadline: float) -> None:
    o = _order(conn, oid)
    work = Path(cfg["work_dir"]) / f"order-{oid}" / f"batch-{o['batch']}"
    while True:                                                    # a clip that fails once is re-cut in the same pass
        todo = conn.execute("SELECT * FROM owner_clips WHERE order_id=? AND batch=? AND status IN ('planned','qc_failed') ORDER BY attempts, idx LIMIT 1",
                            (oid, o["batch"])).fetchone()
        if todo is None or time.monotonic() > deadline:
            return
        c = todo
        out = work / f"clip-{c['idx'] + 1:02d}.mp4"
        try:
            r = cw.make_clip(media, c["source_url"], c["start"], c["end"], work, out, cfg["min_clip_seconds"], cfg["max_clip_seconds"], cfg.get("show_name", ""))
            ok, qc_rows, err = r.passed, r.qc, None
        except Exception as exc:                                   # noqa: BLE001 - a failed render counts as a failed attempt
            r, ok, qc_rows, err = None, False, [], f"{exc.__class__.__name__}: {str(exc)[:200]}"
        attempts = c["attempts"] + 1
        with db.tx(conn):
            if ok:
                conn.execute("UPDATE owner_clips SET status='ready', attempts=?, path=?, sha256=?, qc_json=?, hook=?, caption=?, hashtags_json=?, error=NULL, updated_at=? WHERE id=?",
                             (attempts, str(r.path), cw.sha256(r.path), json.dumps(qc_rows), r.hook, r.caption, json.dumps(r.hashtags), db.now_iso(), c["id"]))
                event(conn, oid, "clip_ready", f"Clip {c['idx'] + 1} passed all {len(qc_rows)} checks", f"ready-{c['id']}-{attempts}")
            else:
                failed = [q["name"] for q in qc_rows if q["status"] != "pass"] or [err or "render failed"]
                conn.execute("UPDATE owner_clips SET status=?, attempts=?, qc_json=?, error=?, updated_at=? WHERE id=?",
                             ("qc_failed" if attempts < MAX_ATTEMPTS else "failed", attempts, json.dumps(qc_rows), redact(", ".join(failed))[:300], db.now_iso(), c["id"]))
                event(conn, oid, "clip_failed", f"Clip {c['idx'] + 1} attempt {attempts} failed: {', '.join(failed)}", f"failed-{c['id']}-{attempts}")
        if not ok and attempts >= MAX_ATTEMPTS:
            escalate(conn, oid, f"Clip {c['idx'] + 1} failed QC twice ({(err or ', '.join(q['name'] for q in qc_rows if q['status'] != 'pass'))}): it was not shipped")
            return


def deliver(conn, s: sl.Seller, oid: int) -> bool:
    """When every clip of the batch is ready: upload (once each), send one delivery message (once), advance the order."""
    o = _order(conn, oid)
    clips = [dict(r) for r in conn.execute("SELECT * FROM owner_clips WHERE order_id=? AND batch=? ORDER BY idx", (oid, o["batch"]))]
    if not clips or any(c["status"] != "ready" for c in clips):
        return False
    for c in clips:
        if c["file_id"]:
            continue
        if cw.sha256(Path(c["path"])) != c["sha256"]:
            escalate(conn, oid, f"Clip {c['idx'] + 1} changed on disk after QC: not delivered")
            return False
        rec = s.upload(Path(c["path"]), f"file-{c['id']}-{c['sha256'][:16]}")
        with db.tx(conn):
            conn.execute("UPDATE owner_clips SET file_id=?, file_url=?, updated_at=? WHERE id=?", (rec["id"], rec.get("url"), db.now_iso(), c["id"]))
        c["file_id"] = rec["id"]
    pending = [c for c in clips if s.file(c["file_id"]).get("upload_status") != "ready"]
    if pending:
        with db.tx(conn):
            _set(conn, oid, state="delivering", state_reason=f"waiting for Whop to process {len(pending)} upload(s)")
        return False
    delivered_after = o["clips_delivered"] + len(clips)
    remaining = o["clips_total"] - delivered_after
    lines = [f"Your {len(clips)} clips are ready ✅", ""]
    for c in clips:
        tags = " ".join("#" + t for t in json.loads(c["hashtags_json"] or "[]"))
        lines += [f"**Clip {c['idx'] + 1}**", c["caption"] or "", tags, ""]
    lines.append(f"{remaining} clips left on your plan: reply with your next episode link whenever you like." if remaining > 0
                 else "That completes your order. Thanks! Reply here if anything needs a change.")
    key = f"deliver-{o['payment_id']}-{o['batch']}"
    if not _done(conn, key):
        s.send(o["channel_id"], "\n".join(lines), key, [c["file_id"] for c in clips])
    with db.tx(conn):
        event(conn, oid, "delivered", f"Batch {o['batch'] + 1}: {len(clips)} clips delivered", key)
        if remaining > 0:
            _set(conn, oid, clips_delivered=delivered_after, batch=o["batch"] + 1, state="awaiting_footage", footage_url=None,
                 footage_requested_at=db.now_iso(), reminder_sent_at=None, state_reason="waiting for the next episode link")
            event(conn, oid, "asked", "Next-episode request included in the delivery message", f"ask-{o['payment_id']}-{o['batch'] + 1}")
        else:
            _set(conn, oid, clips_delivered=delivered_after, state="delivered", state_reason="all clips delivered")
    return True


# ------------------------------------------------------------------------------------------- one pass
def run(conn, s: sl.Seller, media: Any, cfg: dict[str, Any], now: datetime | None = None, budget_seconds: int = 600) -> dict[str, Any]:
    """One pass of the whole loop. Stops at the global stop / pause and when the owner lane is switched off."""
    now = now or _now()
    ok, why = control.may_run(conn)
    if not ok:
        return {"skipped": why}
    if not control.settings(conn).get("owner_shorts_enabled"):
        return {"skipped": "The Shorts service autopilot is switched off in Automation Settings"}
    deadline = time.monotonic() + budget_seconds
    out: dict[str, Any] = {"intake": intake(conn, s, cfg), "asked": 0, "links": 0, "planned": 0, "delivered": 0}
    for r in conn.execute("SELECT id, state FROM owner_orders WHERE state IN ('new','awaiting_footage','footage_received','clipping','delivering') ORDER BY id").fetchall():
        if time.monotonic() > deadline:
            break
        try:
            if r["state"] == "new":                                # each step re-reads the state, so one pass goes as far as it can
                ask_for_footage(conn, s, r["id"])
                out["asked"] += 1
            if _order(conn, r["id"])["state"] == "awaiting_footage":
                out["links"] += check_footage(conn, s, r["id"], now)
            if _order(conn, r["id"])["state"] == "footage_received":
                plan_batch(conn, media, cfg, r["id"])
                out["planned"] += 1
            if _order(conn, r["id"])["state"] == "clipping":
                cut_clips(conn, media, cfg, r["id"], deadline)
            if _order(conn, r["id"])["state"] in ("clipping", "delivering"):
                out["delivered"] += deliver(conn, s, r["id"])
        except sl.NotConfigured:
            raise
        except sl.SellerError as exc:                              # temporary API trouble: the next pass retries
            with db.tx(conn):
                event(conn, r["id"], "retry", f"Whop API problem, will retry: {exc}")
    return out


def view(conn, cfg: dict[str, Any] | None = None, store: Any = None) -> dict[str, Any]:
    orders = db.rows(conn.execute("SELECT * FROM owner_orders ORDER BY id DESC LIMIT 100"))
    for o in orders:
        o["clips"] = db.rows(conn.execute("SELECT id,batch,idx,status,attempts,start,end,hook,file_id,error FROM owner_clips WHERE order_id=? ORDER BY batch,idx", (o["id"],)))
        o["events"] = db.rows(conn.execute("SELECT at,kind,detail FROM owner_events WHERE order_id=? ORDER BY id DESC LIMIT 30", (o["id"],)))
    key = None
    try:
        from microtask_monitor import secret_store as ss
        key = bool((store or ss.default_store()).get(sl.KEY_OWNER, sl.KEY_NAME))
    except Exception:                                              # noqa: BLE001 - shown as unknown
        key = None
    return {"config": {k: (cfg or {}).get(k) for k in ("account_id", "product_id", "plans", "test_plans", "start_after", "min_clip_seconds", "max_clip_seconds")} if cfg else None,
            "api_key_stored": key, "required_permissions": REQUIRED_PERMISSIONS, "enabled": control.settings(conn).get("owner_shorts_enabled"),
            "orders": orders, "counts": {st: sum(1 for o in orders if o["state"] == st) for st in STATES}}
