"""Free, deduplicated notifications.

Channels: dashboard (always, stored in SQLite), Windows desktop balloon (free, on by default on
Windows), optional email via SMTP and optional Telegram bot. Email/Telegram credentials are read ONLY
from environment variables and are never stored, logged or committed:
  AUTOPILOT_SMTP_HOST / _PORT / _USER / _PASSWORD / _FROM / _TO      (free Gmail/Outlook app password etc.)
  AUTOPILOT_TELEGRAM_BOT_TOKEN / AUTOPILOT_TELEGRAM_CHAT_ID          (a bot you create yourself with @BotFather)
A notification is created once per fingerprint, so an unchanged opportunity never alerts twice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

try:
    from microtask_monitor import db, intelligence as ig, opportunity_common as oc
except ImportError:  # pragma: no cover
    import db, intelligence as ig, opportunity_common as oc

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "data" / "settings.json"
DEFAULT_SETTINGS = {"desktop_notifications": True, "external_min_severity": "important", "min_score": 60,
                    "max_external_per_cycle": 5, "expiring_days": 3, "max_individual_new": 10}
SEVERITY_RANK = {"info": 0, "important": 1, "urgent": 2}
TYPES = ("new_opportunity", "expiring_opportunity", "application_response", "task_attention", "payment_update",
         "sync_failed", "policy_change", "daily_shortlist", "opportunity_changed")


def load_settings(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(Path(path).read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def scrub(text: str) -> str:
    """Remove anything that looks like a credential before text is stored or sent."""
    text = re.sub(r"bot\d{6,}:[A-Za-z0-9_-]{20,}", "bot<redacted>", text or "")
    for var in ("AUTOPILOT_SMTP_PASSWORD", "AUTOPILOT_TELEGRAM_BOT_TOKEN"):
        secret = os.environ.get(var)
        if secret and len(secret) >= 6:
            text = text.replace(secret, "<redacted>")
    return text


# ---------------------------------------------------------------- channels ---------------------------
def desktop_notify(title: str, body: str) -> bool:
    """Windows balloon notification through built-in PowerShell. Text is passed via environment variables."""
    if sys.platform != "win32":
        return False
    script = ("Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
              "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
              "$n.ShowBalloonTip(8000, $env:AP_TITLE, $env:AP_BODY, [System.Windows.Forms.ToolTipIcon]::Info); "
              "Start-Sleep -Seconds 9; $n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         env={**os.environ, "AP_TITLE": title[:60], "AP_BODY": body[:240]},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("AUTOPILOT_SMTP_HOST", "AUTOPILOT_SMTP_USER", "AUTOPILOT_SMTP_PASSWORD",
                                           "AUTOPILOT_SMTP_TO"))


def email_notify(title: str, body: str) -> bool:
    if not email_configured():
        return False
    env = os.environ
    msg = EmailMessage()
    msg["Subject"] = title[:150]
    msg["From"] = env.get("AUTOPILOT_SMTP_FROM") or env["AUTOPILOT_SMTP_USER"]
    msg["To"] = env["AUTOPILOT_SMTP_TO"]
    msg.set_content(body)
    try:
        port = int(env.get("AUTOPILOT_SMTP_PORT") or 587)
        if port == 465:
            server = smtplib.SMTP_SSL(env["AUTOPILOT_SMTP_HOST"], port, timeout=30)
        else:
            server = smtplib.SMTP(env["AUTOPILOT_SMTP_HOST"], port, timeout=30)
            server.starttls()
        with server:
            server.login(env["AUTOPILOT_SMTP_USER"], env["AUTOPILOT_SMTP_PASSWORD"])
            server.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException, ValueError):
        return False


def telegram_configured() -> bool:
    return bool(os.environ.get("AUTOPILOT_TELEGRAM_BOT_TOKEN") and os.environ.get("AUTOPILOT_TELEGRAM_CHAT_ID"))


def telegram_notify(title: str, body: str) -> bool:
    if not telegram_configured():
        return False
    token, chat = os.environ["AUTOPILOT_TELEGRAM_BOT_TOKEN"], os.environ["AUTOPILOT_TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n{body}"[:3500]}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=20):
            return True
    except (OSError, ValueError):
        return False            # never include the URL (it contains the token) in any error output


CHANNELS: dict[str, Callable[[str, str], bool]] = {"desktop": desktop_notify, "email": email_notify, "telegram": telegram_notify}


def default_channels() -> dict[str, Callable[[str, str], bool]]:
    """The real channels, or none when AUTOPILOT_NO_NOTIFY=1 (tests and quiet runs: dashboard only)."""
    return {} if os.environ.get("AUTOPILOT_NO_NOTIFY") == "1" else CHANNELS


# ---------------------------------------------------------------- core --------------------------------
def fingerprint(ntype: str, key: str, state: str = "") -> str:
    return hashlib.sha1(f"{ntype}|{key}|{state}".encode()).hexdigest()


def create(conn, ntype: str, title: str, body: str = "", url: str | None = None, severity: str = "info",
           key: str = "", state: str = "", settings: dict | None = None, channels: dict | None = None) -> dict[str, Any] | None:
    """Store a notification once per fingerprint and fan out to enabled channels. Returns None if duplicate."""
    if ntype not in TYPES or severity not in SEVERITY_RANK:
        raise ValueError("unknown notification type or severity")
    settings = settings or load_settings()
    channels = default_channels() if channels is None else channels      # explicit channels (tests) are used as given
    fp = fingerprint(ntype, key or title, state)
    title, body = scrub(title)[:200], scrub(body)[:1000]
    cur = conn.execute("INSERT OR IGNORE INTO notifications(fingerprint,type,severity,title,body,url,created_at) VALUES(?,?,?,?,?,?,?)",
                       (fp, ntype, severity, title, body, url, db.now_iso()))
    if cur.rowcount == 0:
        return None
    nid = cur.lastrowid
    delivered = []
    if SEVERITY_RANK[severity] >= SEVERITY_RANK.get(settings["external_min_severity"], 1):
        wanted = [c for c in ("desktop", "email", "telegram") if c != "desktop" or settings.get("desktop_notifications")]
        for name in wanted:
            sender = channels.get(name)
            if sender and sender(title, body):
                delivered.append(name)
    conn.execute("UPDATE notifications SET channels_json=? WHERE id=?", (json.dumps(["dashboard"] + delivered), nid))
    return {"id": nid, "type": ntype, "title": title, "delivered": ["dashboard"] + delivered}


def generate(conn, now: datetime | None = None, settings: dict | None = None, channels: dict | None = None,
             report_health: dict | None = None, include_new: bool = True) -> list[dict[str, Any]]:
    """Scan the database for things worth telling you. Safe to call after every sync (deduplicated)."""
    now = now or datetime.now(timezone.utc)
    settings = settings or load_settings()
    made: list[dict[str, Any]] = []
    budget = {"left": int(settings["max_external_per_cycle"]), "suppressed": 0}
    base_channels = default_channels() if channels is None else channels

    def add(ntype, title, body="", url=None, severity="info", key="", state=""):
        qualifies = SEVERITY_RANK[severity] >= SEVERITY_RANK.get(settings["external_min_severity"], 1)
        allow = budget["left"] > 0
        res = create(conn, ntype, title, body, url, severity, key, state, settings=settings,
                     channels=base_channels if allow else {})
        if not res:
            return
        made.append(res)
        if len(res["delivered"]) > 1:
            budget["left"] -= 1
        elif qualifies and not allow:
            budget["suppressed"] += 1     # stored on the dashboard, not pushed (avoids notification floods)

    # new valuable opportunities: the best few individually, the rest as one digest (never a flood)
    notified = set(db.get_state(conn, "notified_urls", []) or [])
    candidates = conn.execute(
        "SELECT url,title,source,relevance_score,eligibility_class,payment_text FROM opportunities "
        "WHERE status='open' AND duplicate_of IS NULL AND relevance_score>=? AND eligibility_class IN (?,?) "
        "ORDER BY relevance_score DESC, last_seen_utc DESC LIMIT 2000",
        (settings["min_score"], ig.KENYA_CONFIRMED, ig.WORLDWIDE_CONFIRMED)).fetchall()
    fresh = [r for r in candidates if r["url"] not in notified] if include_new else []
    limit = int(settings["max_individual_new"])
    for r in fresh[:limit]:
        label = "Kenya confirmed" if r["eligibility_class"] == ig.KENYA_CONFIRMED else "Worldwide"
        add("new_opportunity", f"New {label} opportunity: {r['title']}",
            f"{r['source']} · score {r['relevance_score']} · {r['payment_text'] or 'pay not disclosed'}", url=r["url"],
            severity="important", key=r["url"])
    if len(fresh) > limit:
        digest = hashlib.sha1(",".join(r["url"] for r in fresh[limit:]).encode()).hexdigest()
        add("new_opportunity", f"{len(fresh) - limit} more high-scoring opportunities discovered",
            "Open the Opportunity Explorer, filter to Kenya-confirmed or worldwide, and sort by score.",
            severity="important", key="digest", state=digest)
    if fresh:
        notified.update(r["url"] for r in fresh)
        db.set_state(conn, "notified_urls", sorted(notified)[-5000:])
    # expiring soon
    horizon = (now + timedelta(days=settings["expiring_days"])).isoformat()
    for r in conn.execute("SELECT url,title,deadline FROM opportunities WHERE status='open' AND deadline IS NOT NULL AND deadline<=? AND deadline>?",
                          (horizon, now.isoformat())):
        add("expiring_opportunity", f"Expiring soon: {r['title']}", f"Deadline {r['deadline'][:16]}", url=r["url"],
            severity="important", key=r["url"], state=r["deadline"])
    # application responses
    for r in conn.execute("SELECT id,title,status,response_text FROM applications WHERE status IN ('interview','accepted','rejected')"):
        add("application_response", f"Application {r['status']}: {r['title']}", r["response_text"] or "", severity="important",
            key=str(r["id"]), state=r["status"])
    # tasks needing attention
    soon = (now + timedelta(days=2)).isoformat()
    for r in conn.execute("SELECT id,title,due_at,status FROM tasks WHERE status IN ('accepted','in_progress','completed') AND due_at IS NOT NULL AND due_at<=?", (soon,)):
        add("task_attention", f"Task due soon: {r['title']}", f"Due {r['due_at'][:16]} · status {r['status']}", severity="urgent",
            key=str(r["id"]), state=r["due_at"])
    for r in conn.execute("SELECT id,title FROM tasks WHERE status='rejected'"):
        add("task_attention", f"Task rejected: {r['title']}", "Review the platform's feedback.", severity="important", key=str(r["id"]), state="rejected")
    # payments
    for r in conn.execute("SELECT id,amount,currency,platform_id,received_date FROM payments"):
        add("payment_update", f"Payment recorded: {r['amount']:g} {r['currency']}", f"{r['platform_id'] or 'platform'} · {r['received_date']}",
            severity="info", key=str(r["id"]))
    # sync failures (three or more in a row)
    health = report_health if report_health is not None else db.get_state(conn, "sync_health", {}) or {}
    if int(health.get("consecutive_failures", 0)) >= 3:
        add("sync_failed", "Report synchronization keeps failing",
            f"{health.get('consecutive_failures')} failures in a row. Last error: {health.get('last_error', 'unknown')}", severity="important",
            key="sync", state=str(health.get("failure_streak_started") or health.get("consecutive_failures")))
    # platform policy changes
    for pid, mon in (db.get_state(conn, "eligibility_monitors", {}) or {}).items():
        if mon.get("needs_manual_review") and mon.get("alert"):
            add("policy_change", f"Policy change: {pid}", mon["alert"], severity="urgent", key=pid, state=mon["alert"])
    if budget["suppressed"]:
        summary = create(conn, "new_opportunity", f"{budget['suppressed']} more notifications",
                         "Open the dashboard Notifications page to review them.", severity="important",
                         key=f"summary-{now.isoformat()}", settings=settings, channels=base_channels)
        if summary:
            made.append(summary)
    return made


def list_notifications(conn, unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM notifications" + (" WHERE read_at IS NULL" if unread_only else "") + " ORDER BY id DESC LIMIT ?"
    return db.rows(conn.execute(sql, (limit,)))


def mark_read(conn, ids: list[int] | None = None) -> int:
    if ids:
        marks = ",".join("?" * len(ids))
        cur = conn.execute(f"UPDATE notifications SET read_at=? WHERE read_at IS NULL AND id IN ({marks})", (db.now_iso(), *ids))
    else:
        cur = conn.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL", (db.now_iso(),))
    return cur.rowcount
