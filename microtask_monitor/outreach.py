"""Owner lane phase 2: find podcast buyers and email them a free sample clip, with no one in the loop.

    discover (YouTube Data API) ─► qualify (gap, country, published business email, no "no solicitation")
      ─► sample clip from their latest episode (captions → section → clip → QC, hosted as an unlisted Whop file)
      ─► first email (approved template + automatic legal footer) ─► one follow-up after 4 days if no reply
      ─► replies and opt-outs read over IMAP: any reply stops the sequence; "no thanks" adds a permanent block

Rules enforced here, not in a template:
* Sending is off until you approve the exact template text (its SHA-256); any edit needs approval again.
* Every email carries your postal address and an opt-out line (CAN-SPAM); an opt-out is honoured at once, forever.
* At most ``daily_cap`` first emails per day; one follow-up; never a third email; never anyone on the block list or
  anyone already found in your Gmail "Sent" folder.
* Only publicly published business addresses, only in allowed countries, never channels that refuse solicitation.
* Credentials: a Gmail app password in Windows Credential Manager (owner_gmail/app_password). Never in files or logs.
"""
from __future__ import annotations

import hashlib
import imaplib
import json
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Callable

try:
    from microtask_monitor import clip_worker as cw, control, db, notify, secret_store as ss
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import clip_worker as cw, control, db, notify, secret_store as ss
    from secret_store import redact

FOLLOWUP_AFTER = timedelta(days=4)
RATE_LIMIT_PAUSE = timedelta(hours=6)
RATE_LIMITED = re.compile(r"429|too many requests|sign in to confirm|not a bot", re.I)
OPT_OUT = re.compile(r"\b(no thanks|no thank you|unsubscribe|stop|remove me|not interested|don'?t (?:email|contact)|opt[- ]?out)\b", re.I)
STATUSES = ("new", "qualified", "sample_ready", "sent", "followed_up", "replied", "opted_out", "bounced", "skipped", "manual_hold", "won")
GMAIL_KEY = ("owner_gmail", "app_password")
FIELDS = ("first_name_or_team", "episodes_median", "shorts_median", "episode_title_short", "clip_link", "clip_seconds", "trial_link")


class OutreachError(RuntimeError):
    pass


class SamplesPaused(OutreachError):
    """YouTube refused downloads for now (rate limit). Sample-making waits; nothing else stops."""


# ------------------------------------------------------------------------------------------- templates and approval
def parse_templates(text: str) -> dict[str, dict[str, str]]:
    """Read '## 1. First email' and '## 2. Follow-up' (with a sample clip) and, optionally, '## 3.' and '## 4.' (the same pair
    without a clip, offering free clips on reply). Each has a 'Subject:' line, then the body."""
    out = {}
    for kind, rx in (("first", r"^##\s*1\..*?$"), ("followup", r"^##\s*2\..*?$"), ("first_noclip", r"^##\s*3\..*?$"), ("followup_noclip", r"^##\s*4\..*?$")):
        m = re.search(rx, text, re.M)
        if not m and kind.endswith("_noclip"):
            continue
        if not m:
            raise OutreachError(f"Template section for the {kind} email is missing")
        rest = text[m.end():]
        nxt = re.search(r"^##\s", rest, re.M)
        block = rest[:nxt.start()] if nxt else rest
        sm = re.search(r"^Subject:\s*(.+)$", block, re.M)
        if not sm:
            raise OutreachError(f"The {kind} email has no 'Subject:' line")
        body = block[sm.end():].strip("\n")
        unknown = set(re.findall(r"\{(\w+)\}", sm.group(1) + body)) - set(FIELDS)
        if unknown:
            raise OutreachError(f"Unknown fields in the {kind} email: {', '.join(sorted(unknown))}")
        if kind.endswith("_noclip") and re.search(r"\{clip_(link|seconds)\}", sm.group(1) + body):
            raise OutreachError(f"The {kind} email has no clip, so it cannot use {{clip_link}} or {{clip_seconds}}")
        out[kind] = {"subject": sm.group(1).strip(), "body": body.strip()}
    if ("first_noclip" in out) != ("followup_noclip" in out):
        raise OutreachError("The no-clip emails come as a pair: sections 3 and 4")
    return out


def template_sha(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def approve_templates(conn, text: str, confirmed_by_user: bool) -> str:
    if confirmed_by_user is not True:
        raise OutreachError("Only you can approve the email templates (confirmed_by_user=true)")
    parse_templates(text)
    sha = template_sha(text)
    with db.tx(conn):
        db.set_state(conn, "outreach_templates_approved", {"sha256": sha, "at": db.now_iso()})
        db.audit(conn, "outreach.templates_approved", "outreach", "templates", {"sha256": sha})
    return sha


def approved(conn, text: str) -> bool:
    return (db.get_state(conn, "outreach_templates_approved", {}) or {}).get("sha256") == template_sha(text)


def footer(cfg: dict[str, Any]) -> str:
    addr = (cfg.get("postal_address") or "").strip()
    if not addr:
        raise OutreachError("A postal address (street address or registered P.O. box) is required in every email")
    return f"\n\n--\n{cfg['sender_name']} · {addr}\nYou're receiving this because your channel lists this address for business enquiries. Reply \"no thanks\" and you won't hear from me again."


def render(tpl: dict[str, str], lead: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    title = (lead.get("episode_title") or "your latest episode").strip()
    short = title if len(title) <= 60 else title[:57].rsplit(" ", 1)[0] + "…"
    values = {"first_name_or_team": f"{lead['name'].strip()} team", "episodes_median": f"{int(lead['episodes_median']):,}",
              "shorts_median": f"{int(lead['shorts_median']):,}", "episode_title_short": short, "clip_link": lead.get("clip_url") or "",
              "clip_seconds": str(int(round(lead.get("clip_seconds") or 0))), "trial_link": cfg["trial_link"]}
    if "{clip_link}" in tpl["body"] and not values["clip_link"]:
        raise OutreachError("The sample clip link is missing")
    fill = lambda s: re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], s)   # noqa: E731
    return fill(tpl["subject"]), fill(tpl["body"]) + footer(cfg)


# ------------------------------------------------------------------------------------------- mail transport
class Mailer:
    """Gmail over SMTP (send) and IMAP (read replies, check Sent). Tests replace this class."""

    def __init__(self, address: str, store: Any = None, password: str | None = None):
        self.address, self._store, self._password = address, store, password

    def _pw(self) -> str:
        if self._password:
            return self._password
        pw = (self._store or ss.default_store()).get(*GMAIL_KEY)
        if not pw:
            raise OutreachError("No Gmail app password stored. Create one (Google Account → Security → 2-Step Verification → App passwords), "
                                "then run: py -3 microtask_monitor/connect.py set-key owner_gmail app_password")
        return pw

    def send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(self.address, self._pw())
            s.send_message(msg)

    def _imap(self) -> imaplib.IMAP4_SSL:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context(), timeout=60)
        m.login(self.address, self._pw())
        return m

    def messages_from(self, email: str, since: datetime) -> list[str]:
        """Plain-text bodies of messages from ``email`` in All Mail since a date."""
        m = self._imap()
        try:
            m.select('"[Gmail]/All Mail"', readonly=True)
            typ, data = m.search(None, "FROM", f'"{email}"', "SINCE", since.strftime("%d-%b-%Y"))
            out = []
            for num in (data[0].split() if typ == "OK" and data and data[0] else [])[-10:]:
                typ, parts = m.fetch(num, "(BODY.PEEK[TEXT])")
                if typ == "OK" and parts and isinstance(parts[0], tuple):
                    out.append(parts[0][1].decode("utf-8", "ignore")[:4000])
            return out
        finally:
            m.logout()

    def bounced(self, email: str, since: datetime) -> bool:
        m = self._imap()
        try:
            m.select('"[Gmail]/All Mail"', readonly=True)
            typ, data = m.search(None, "FROM", '"mailer-daemon"', "TEXT", f'"{email}"', "SINCE", since.strftime("%d-%b-%Y"))
            return bool(typ == "OK" and data and data[0])
        finally:
            m.logout()

    def already_emailed(self, email: str) -> bool:
        m = self._imap()
        try:
            m.select('"[Gmail]/Sent Mail"', readonly=True)
            typ, data = m.search(None, "TO", f'"{email}"')
            return bool(typ == "OK" and data and data[0])
        finally:
            m.logout()


# ------------------------------------------------------------------------------------------- leads
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lead(conn, lid: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM outreach_leads WHERE id=?", (lid,)).fetchone()
    if not r:
        raise OutreachError("Lead not found")
    return dict(r)


def _set(conn, lid: int, **f: Any) -> None:
    f["updated_at"] = db.now_iso()
    conn.execute(f"UPDATE outreach_leads SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), lid))


def suppressed(conn, email: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM outreach_suppression WHERE email=?", (email.lower(),)).fetchone())


def suppress(conn, email: str, reason: str) -> None:
    conn.execute("INSERT OR IGNORE INTO outreach_suppression(email,reason,at) VALUES(?,?,?)", (email.lower(), redact(reason)[:200], db.now_iso()))


def add_lead(conn, lead: dict[str, Any], origin: str, status: str = "qualified", reason: str = "") -> int | None:
    """Insert once per channel. A channel already known is never re-added or re-contacted."""
    if not lead.get("channel_id") or not lead.get("email") or not lead.get("name"):
        return None
    now = db.now_iso()
    with db.tx(conn):
        cur = conn.execute("""INSERT OR IGNORE INTO outreach_leads(channel_id,name,channel_url,email,email_source,country,subscribers,shorts_median,
            episodes_median,gap,episode_id,episode_title,status,status_reason,origin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (lead["channel_id"], lead["name"][:200], lead.get("channel_url"), lead["email"].lower(), (lead.get("email_source") or "")[:300],
                            lead.get("country"), lead.get("subscribers"), lead.get("shorts_median"), lead.get("episodes_median"), lead.get("gap"),
                            lead.get("episode_id"), lead.get("episode_title"), status, reason[:300], origin, now, now))
    return cur.lastrowid if cur.rowcount else None


def import_legacy(conn, path: Path) -> dict[str, int]:
    """Bring in leads found earlier. Anyone already drafted/contacted by hand is held, so they are never emailed twice."""
    stats = {"added": 0, "held": 0, "skipped": 0}
    for x in json.loads(Path(path).read_text(encoding="utf-8")):
        st = x.get("status")
        if st == "excluded" or not x.get("email") or not x.get("channel_id"):
            stats["skipped"] += 1
            continue
        hold = st not in ("new", None)
        lid = add_lead(conn, {**x, "channel_url": x.get("channel"), "email_source": x.get("email_context"), "subscribers": x.get("subs"),
                              "gap": x.get("gap") if isinstance(x.get("gap"), (int, float)) else None},
                       "legacy_import", "manual_hold" if hold else "new",
                       f"Earlier status '{st}': held so it is never contacted twice. Release it yourself if it was never sent." if hold else "Imported; needs country and episode data from the API")
        if lid:
            stats["held" if hold else "added"] += 1
    return stats


def enrich(conn, yt: Any, cfg: dict[str, Any]) -> dict[str, int]:
    """Leads imported without API data get their country, medians, email check and latest episode from the API, then qualify or are skipped."""
    try:
        from microtask_monitor import youtube_leads as yl
    except ImportError:  # pragma: no cover
        import youtube_leads as yl
    rows = conn.execute("SELECT id, channel_id FROM outreach_leads WHERE status='new' LIMIT 20").fetchall()
    stats = {"qualified": 0, "skipped": 0}
    for ch in yt.channels([r["channel_id"] for r in rows]):
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        r = yl.assess(ch, yt.recent_videos(uploads) if uploads else [], cfg["allowed_countries"], cfg.get("min_gap", 5.0))
        lid = next(x["id"] for x in rows if x["channel_id"] == ch.get("id"))
        with db.tx(conn):
            if r["ok"]:
                _set(conn, lid, status="qualified", status_reason=r["reason"], country=r["country"], subscribers=r["subscribers"], shorts_median=r["shorts_median"],
                     episodes_median=r["episodes_median"], gap=r["gap"], episode_id=r["episode_id"], episode_title=r["episode_title"], email=r["email"], email_source=r["email_source"])
            else:
                _set(conn, lid, status="skipped", status_reason=r["reason"], country=r.get("country"))
        stats["qualified" if r["ok"] else "skipped"] += 1
    return stats


def release(conn, lid: int, confirmed_by_user: bool) -> dict[str, Any]:
    """You confirm that a held lead was never actually emailed; it then goes through the normal checks (it may still be skipped)."""
    lead = _lead(conn, lid)
    if confirmed_by_user is not True:
        raise OutreachError("Only you can release a held lead (confirmed_by_user=true)")
    if lead["status"] != "manual_hold":
        raise OutreachError("Only held leads can be released")
    with db.tx(conn):
        _set(conn, lid, status="new", status_reason="Released by you; re-checked before anything is sent")
        db.audit(conn, "outreach.release", "outreach_lead", lid)
    return _lead(conn, lid)


def discover(conn, yt: Any, cfg: dict[str, Any]) -> dict[str, int]:
    try:
        from microtask_monitor import youtube_leads as yl
    except ImportError:  # pragma: no cover
        import youtube_leads as yl
    known = {r["channel_id"] for r in conn.execute("SELECT channel_id FROM outreach_leads")} | set(db.get_state(conn, "outreach_rejected_channels", []) or [])
    results = yl.discover(yt, cfg["queries"], cfg["allowed_countries"], known, cfg.get("max_channels_per_run", 30), cfg.get("min_gap", 5.0))
    added = 0
    rejected = sorted(known | {r["channel_id"] for r in results if not r["ok"] and r.get("channel_id")})
    for r in results:
        if r["ok"] and not suppressed(conn, r["email"]) and add_lead(conn, r, "youtube_api", "qualified", r["reason"]):
            added += 1
    with db.tx(conn):
        db.set_state(conn, "outreach_rejected_channels", rejected[-5000:])
    return {"checked": len(results), "added": added, "quota_units": getattr(yt, "units", 0)}


# ------------------------------------------------------------------------------------------- sample clip
def make_sample(conn, lid: int, media: Any, seller: Any, cfg: dict[str, Any]) -> bool:
    """One clip from the lead's latest episode, QC'd, hosted as an unlisted Whop file. Never posted anywhere public."""
    lead = _lead(conn, lid)
    url = f"https://www.youtube.com/watch?v={lead['episode_id']}"
    work = Path(cfg["work_dir"]) / f"lead-{lid}"
    try:
        (a, b), = cw.plan_moments(media, url, work, 1, cfg.get("min_clip_seconds", 25), cfg.get("max_clip_seconds", 50), [])
        r = cw.make_clip(media, url, a, b, work, work / "sample.mp4", cfg.get("min_clip_seconds", 25), cfg.get("max_clip_seconds", 50))
    except Exception as exc:                                       # noqa: BLE001 - this lead is skipped, the rest continue
        detail = " ".join(str(x) for x in (exc, getattr(exc, "stderr", b"") or b""))
        if RATE_LIMITED.search(detail if isinstance(detail, str) else str(detail)):
            # YouTube is rate-limiting this PC: wait and retry later. Never work around it (no proxies, cookies or impersonation).
            with db.tx(conn):
                db.set_state(conn, "outreach_samples_paused_until", (_now() + RATE_LIMIT_PAUSE).isoformat())
                _set(conn, lid, status_reason="YouTube rate-limited the sample download; retrying after a pause")
            raise SamplesPaused("YouTube is rate-limiting downloads from this PC; samples paused for 6 hours")
        with db.tx(conn):
            _set(conn, lid, status="skipped", status_reason=f"No sample possible: {str(exc)[:200]}")
        return False
    if not r.passed:
        with db.tx(conn):
            _set(conn, lid, status="skipped", status_reason="Sample failed QC: " + ", ".join(q["name"] for q in r.qc if q["status"] != "pass"))
        return False
    rec = seller.upload(r.path, f"sample-{lid}-{cw.sha256(r.path)[:16]}")
    for _ in range(10):
        f = seller.file(rec["id"])
        if f.get("upload_status") == "ready" and f.get("url"):
            break
        time.sleep(3)
    else:
        raise OutreachError("Whop did not finish processing the sample upload; the next run retries")
    with db.tx(conn):
        _set(conn, lid, status="sample_ready", clip_path=str(r.path), clip_url=f["url"], clip_seconds=r.end - r.start, status_reason="Sample passed QC")
    return True


# ------------------------------------------------------------------------------------------- sending
def sent_today(conn, now: datetime) -> int:
    return conn.execute("SELECT COUNT(*) FROM outreach_messages WHERE kind='first' AND status='sent' AND sent_at>=?", (now.date().isoformat(),)).fetchone()[0]


def _send(conn, mailer: Any, lead: dict[str, Any], kind: str, subject: str, body: str, cfg: dict[str, Any], sha: str, reply_to_id: str | None = None) -> bool:
    key = f"{kind}-{lead['id']}"
    if conn.execute("SELECT 1 FROM outreach_messages WHERE idem_key=?", (key,)).fetchone():
        return False                                               # already sent (or attempted): never twice
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["sender_name"], cfg["from_address"]))
    msg["To"] = lead["email"]
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg["from_address"].split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{cfg['from_address']}?subject=unsubscribe>"
    if reply_to_id:
        msg["In-Reply-To"] = msg["References"] = reply_to_id
    msg.set_content(body)
    with db.tx(conn):                                              # recorded before sending: a crash can never cause a resend
        conn.execute("INSERT INTO outreach_messages(lead_id,kind,idem_key,to_email,subject,message_id,status,template_sha,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (lead["id"], kind, key, lead["email"], subject[:300], msg["Message-ID"], "sending", sha, db.now_iso()))
    try:
        mailer.send(msg)
    except Exception as exc:                                       # noqa: BLE001
        with db.tx(conn):
            conn.execute("UPDATE outreach_messages SET status='failed', error=? WHERE idem_key=?", (redact(str(exc))[:300], key))
        raise OutreachError(f"Sending failed: {redact(str(exc))[:200]}")
    with db.tx(conn):
        conn.execute("UPDATE outreach_messages SET status='sent', sent_at=? WHERE idem_key=?", (db.now_iso(), key))
    return True


def send_firsts(conn, mailer: Any, cfg: dict[str, Any], tpls: dict, sha: str, now: datetime, noclip: bool = False) -> int:
    """With a ready sample: the clip email. With ``noclip``: qualified leads get the no-clip email (offering free clips on reply)."""
    n = 0
    room = cfg.get("daily_cap", 15) - sent_today(conn, now)
    if noclip and "first_noclip" not in tpls:
        return 0
    where = "status='qualified' AND episode_id IS NOT NULL AND episodes_median IS NOT NULL" if noclip else "status='sample_ready'"
    for r in conn.execute(f"SELECT id FROM outreach_leads WHERE {where} ORDER BY gap DESC, id LIMIT ?", (max(0, room),)).fetchall():
        lead = _lead(conn, r["id"])
        if suppressed(conn, lead["email"]):
            with db.tx(conn):
                _set(conn, lead["id"], status="opted_out", status_reason="On your do-not-contact list")
            continue
        if mailer.already_emailed(lead["email"]):
            with db.tx(conn):
                _set(conn, lead["id"], status="manual_hold", status_reason="Your Gmail Sent folder already has an email to this address")
            continue
        subject, body = render(tpls["first_noclip" if noclip else "first"], lead, cfg)
        if _send(conn, mailer, lead, "first", subject, body, cfg, sha):
            with db.tx(conn):
                _set(conn, lead["id"], status="sent", first_sent_at=db.now_iso(),
                     status_reason="First email sent (no clip: offered 3 free clips on reply)" if noclip else "First email sent")
            n += 1
    return n


def scan_replies(conn, mailer: Any, now: datetime) -> dict[str, int]:
    """Any reply stops the sequence; an opt-out also blocks the address for good; a bounce stops it too. You are told about replies."""
    stats = {"replied": 0, "opted_out": 0, "bounced": 0}
    for r in conn.execute("SELECT * FROM outreach_leads WHERE status IN ('sent','followed_up')").fetchall():
        lead = dict(r)
        since = datetime.fromisoformat(lead["first_sent_at"])
        if mailer.bounced(lead["email"], since):
            with db.tx(conn):
                _set(conn, lead["id"], status="bounced", status_reason="The address bounced")
                suppress(conn, lead["email"], "bounced")
            stats["bounced"] += 1
            continue
        bodies = mailer.messages_from(lead["email"], since)
        if not bodies:
            continue
        text = "\n".join(bodies)
        first_lines = "\n".join(b.strip().splitlines()[0] if b.strip() else "" for b in bodies)
        opted = bool(OPT_OUT.search(first_lines)) or bool(OPT_OUT.search(text[:300]))
        with db.tx(conn):
            if opted:
                _set(conn, lead["id"], status="opted_out", replied_at=db.now_iso(), status_reason="They asked not to be contacted")
                suppress(conn, lead["email"], "opt-out reply")
            else:
                _set(conn, lead["id"], status="replied", replied_at=db.now_iso(), status_reason="They replied: answer them yourself")
                notify.create(conn, "application_response", f"{lead['name']} replied to your Shorts email", redact(text[:600]),
                              severity="urgent", key=f"outreach-reply-{lead['id']}")
        stats["opted_out" if opted else "replied"] += 1
    return stats


def send_followups(conn, mailer: Any, cfg: dict[str, Any], tpls: dict, sha: str, now: datetime) -> int:
    n = 0
    for r in conn.execute("SELECT * FROM outreach_leads WHERE status='sent'").fetchall():
        lead = dict(r)
        if now - datetime.fromisoformat(lead["first_sent_at"]) < FOLLOWUP_AFTER or suppressed(conn, lead["email"]):
            continue
        first = conn.execute("SELECT message_id FROM outreach_messages WHERE lead_id=? AND kind='first'", (lead["id"],)).fetchone()
        variant = "followup" if lead.get("clip_url") or "followup_noclip" not in tpls else "followup_noclip"
        subject, body = render(tpls[variant], lead, cfg)
        if _send(conn, mailer, lead, "followup", subject, body, cfg, sha, first["message_id"] if first else None):
            with db.tx(conn):
                _set(conn, lead["id"], status="followed_up", followup_sent_at=db.now_iso(), status_reason="Follow-up sent; no more emails")
            n += 1
    return n


# ------------------------------------------------------------------------------------------- one pass
CONFIG_PATH = db.DATA_DIR / "outreach_config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = Path(path or CONFIG_PATH)
    if not p.exists():
        raise OutreachError(f"No outreach config at {p}")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg.setdefault("work_dir", str(db.DATA_DIR / "owner" / "outreach"))
    cfg.setdefault("daily_cap", 15)
    return cfg


REQUIRED = ("from_address", "sender_name", "postal_address", "trial_link", "templates_path", "allowed_countries", "queries", "work_dir")


def check_config(cfg: dict[str, Any]) -> list[str]:
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if cfg.get("trial_link") and not str(cfg["trial_link"]).startswith("https://whop.com/"):
        missing.append("trial_link must be your Whop checkout link")
    if cfg.get("daily_cap") is not None and not 1 <= int(cfg["daily_cap"]) <= 15:
        missing.append("daily_cap must be between 1 and 15")
    return missing


def run(conn, cfg: dict[str, Any], mailer: Any, yt: Any = None, media: Any = None, seller: Any = None, now: datetime | None = None,
        budget_seconds: int = 900) -> dict[str, Any]:
    now = now or _now()
    ok, why = control.may_run(conn)
    if not ok:
        return {"skipped": why}
    if not control.settings(conn).get("owner_outreach_enabled"):
        return {"skipped": "Outreach is switched off in Automation Settings"}
    problems = check_config(cfg)
    if problems:
        return {"skipped": "Outreach setup incomplete: " + "; ".join(problems)}
    text = Path(cfg["templates_path"]).read_text(encoding="utf-8")
    if not approved(conn, text):
        return {"skipped": "The email templates are not approved (or changed since you approved them)"}
    tpls, sha = parse_templates(text), template_sha(text)
    deadline = time.monotonic() + budget_seconds
    out: dict[str, Any] = {"replies": scan_replies(conn, mailer, now)}
    out["followups"] = send_followups(conn, mailer, cfg, tpls, sha, now)
    if yt is not None:
        out["enriched"] = enrich(conn, yt, cfg)
    room = cfg.get("daily_cap", 15) - sent_today(conn, now)
    ready = conn.execute("SELECT COUNT(*) FROM outreach_leads WHERE status='sample_ready'").fetchone()[0]
    if yt is not None and room > ready and conn.execute("SELECT COUNT(*) FROM outreach_leads WHERE status='qualified'").fetchone()[0] < room:
        out["discovery"] = discover(conn, yt, cfg)
    made = 0
    paused = db.get_state(conn, "outreach_samples_paused_until", None)
    if paused and paused > now.isoformat():
        out["samples_paused_until"] = paused
        media = None
    if media is not None and seller is not None:
        for r in conn.execute("SELECT id FROM outreach_leads WHERE status='qualified' AND episode_id IS NOT NULL ORDER BY gap DESC LIMIT ?", (max(0, room - ready),)).fetchall():
            if time.monotonic() > deadline:
                break
            try:
                made += make_sample(conn, r["id"], media, seller, cfg)
            except SamplesPaused as exc:
                out["samples_paused"] = str(exc)
                break
    out["samples"] = made
    out["sent"] = send_firsts(conn, mailer, cfg, tpls, sha, now)
    samples_blocked = bool(out.get("samples_paused") or out.get("samples_paused_until"))
    if (samples_blocked or cfg.get("always_noclip")) and "first_noclip" in tpls:   # YouTube refuses downloads: start conversations anyway
        out["sent_noclip"] = send_firsts(conn, mailer, cfg, tpls, sha, now, noclip=True)
    return out


def view(conn, cfg: dict[str, Any] | None) -> dict[str, Any]:
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM outreach_leads GROUP BY status")}
    text = None
    try:
        text = Path(cfg["templates_path"]).read_text(encoding="utf-8") if cfg and cfg.get("templates_path") else None
    except OSError:
        text = None
    return {"counts": counts, "sent_today": sent_today(conn, _now()), "daily_cap": (cfg or {}).get("daily_cap", 15),
            "templates_approved": bool(text and approved(conn, text)), "config_problems": check_config(cfg or {}),
            "enabled": control.settings(conn).get("owner_outreach_enabled"),
            "leads": db.rows(conn.execute("SELECT id,name,country,email,gap,status,status_reason,first_sent_at,followup_sent_at,replied_at,clip_url FROM outreach_leads ORDER BY updated_at DESC LIMIT 200")),
            "suppressed": conn.execute("SELECT COUNT(*) FROM outreach_suppression").fetchone()[0]}
