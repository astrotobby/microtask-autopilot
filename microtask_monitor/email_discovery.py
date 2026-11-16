"""Opportunity discovery from platform notification emails ("new task available", "you've been invited", bounty alerts).

Two sources, same parser:
* ``EmlFolder``   .eml files you export into data/email_inbox (always local; no account access at all)
* ``ImapInbox``   your own Gmail, READ-ONLY, through the app password you already stored for outreach
                  (owner_gmail/app_password). Messages are fetched with BODY.PEEK, so nothing is marked read,
                  moved, deleted or answered. Off until you switch on "email_monitor_enabled".
No mailbox password, cookie or session is ever asked for or handled here.

Email text is untrusted data. It is parsed for facts (platform, title, link, reward, deadline, skills, availability,
eligibility) and imported through the same manual-import path as anything you paste, so every rule still applies:
the platform's capability profile, hard restrictions, the AI-use rule, eligibility, and human-only categories.
"""
from __future__ import annotations

import email
import hashlib
import html
import imaplib
import re
import ssl
from datetime import datetime, timedelta, timezone
from email import policy as email_policy
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

try:
    from microtask_monitor import db, discovery, opportunity_common as oc, permissions, policy
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import db, discovery, opportunity_common as oc, permissions, policy
    from secret_store import redact

INBOX_DIR = db.DATA_DIR / "email_inbox"
URL_RX = re.compile(r"https://[^\s<>\"')\]]+")
MONEY_RX = re.compile(r"(?:(USD|US\$|\$|KES|KSh|EUR|€|GBP|£)\s?(\d{1,5}(?:[.,]\d{1,2})?))|(?:(\d{1,5}(?:[.,]\d{1,2})?)\s?(USD|KES|EUR|GBP|dollars))", re.I)
DEADLINE_RX = re.compile(r"(?:deadline|due(?: date)?|expires?(?: on)?|closes?(?: on)?|submit by|apply by|until)\s*[:\-]?\s*([A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2}|\d{1,2} [A-Za-z]{3,9},? \d{4})", re.I)
AVAIL_RX = re.compile(r"(\d+\s+(?:spots?|slots?|places?|tasks?|seats?)\s+(?:left|remaining|available))|(limited (?:spots|slots|availability))|(first come,? first served)", re.I)
COUNTRY_RX = re.compile(r"(?i:available|open|eligible)\s+(?i:only\s+)?(?i:in|to|for)\s*:?\s*([A-Z][A-Za-z ,&]+?)(?:\.|\n|$)", re.M)
WORLDWIDE_RX = re.compile(r"\b(worldwide|all countries|any country|globally|from anywhere)\b", re.I)
COUNTRY_CODES = {"kenya": "KE", "united states": "US", "usa": "US", "us": "US", "canada": "CA", "united kingdom": "GB", "uk": "GB", "australia": "AU",
                 "new zealand": "NZ", "india": "IN", "nigeria": "NG", "south africa": "ZA", "germany": "DE", "france": "FR", "philippines": "PH", "ireland": "IE"}
CURRENCY = {"$": "USD", "us$": "USD", "usd": "USD", "dollars": "USD", "kes": "KES", "ksh": "KES", "eur": "EUR", "€": "EUR", "gbp": "GBP", "£": "GBP"}
MAX_BYTES = 2_000_000


def platform_domains() -> dict[str, str]:
    """registrable host -> platform id, from each platform's official URL and its reviewed policy URLs."""
    out: dict[str, str] = {}
    caps = policy.load_capabilities()
    for pid, p in discovery.registry().items():
        urls = [p.get("official_url")] + list((caps.get(pid) or {}).get("policy_urls") or [])
        for u in urls:
            host = (urlparse(u or "").hostname or "").lower()
            if host:
                out.setdefault(".".join(host.split(".")[-2:]), pid)
    out.pop("google.com", None)
    return out


def _text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    body = part.get_content() if part.get_content_type() in ("text/plain", "text/html") else ""
    if part.get_content_type() == "text/html":
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
        body = re.sub(r"(?i)<a\s[^>]*href=\"(https://[^\"]+)\"[^>]*>", r" \1 ", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return re.sub(r"[ \t]+", " ", body)[:20000]


def parse(raw: bytes) -> dict[str, Any] | None:
    """Facts from one notification email, or None when it is not from a registered platform."""
    if len(raw) > MAX_BYTES:
        return None
    msg = email.message_from_bytes(raw, policy=email_policy.default)
    sender = str(msg.get("From") or "")
    subject = re.sub(r"\s+", " ", str(msg.get("Subject") or "")).strip()
    body = _text(msg)
    domains = platform_domains()
    host = (re.search(r"@([A-Za-z0-9.-]+)", sender) or [None, ""])[1].lower()
    pid = domains.get(".".join(host.split(".")[-2:])) if host else None
    def owner(u: str) -> str | None:
        return domains.get(".".join((urlparse(u).hostname or "").lower().split(".")[-2:]))
    urls = [u for u in URL_RX.findall(body) if owner(u)]
    if not pid and urls:
        pid = owner(urls[0])
    platform_urls = [u for u in urls if owner(u) == pid]
    if not pid:
        return None
    link = next((u.rstrip(".,") for u in platform_urls if not re.search(r"unsubscribe|preferences|privacy|terms|help|support|settings", u, re.I)), None)
    money = MONEY_RX.search(subject + "\n" + body)
    reward, currency = None, None
    if money:
        sym = (money.group(1) or money.group(4) or "").lower()
        reward = float((money.group(2) or money.group(3)).replace(",", "."))
        currency = CURRENCY.get(sym)
    dl = DEADLINE_RX.search(body)
    deadline = None
    if dl:
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y"):
            try:
                deadline = datetime.strptime(dl.group(1).replace(".", ""), fmt).date().isoformat()
                break
            except ValueError:
                continue
    countries, kenya_note = None, None
    cm = COUNTRY_RX.search(body)
    if cm:
        names = [n.strip().lower() for n in re.split(r",|&|\band\b", cm.group(1)) if n.strip()]
        codes = [COUNTRY_CODES[n] for n in names if n in COUNTRY_CODES]
        if codes:
            countries = sorted(set(codes))
    if WORLDWIDE_RX.search(body) and countries is None:
        kenya_note = "worldwide"
    title = re.sub(r"^(?:fwd?|re):\s*", "", subject, flags=re.I)[:200] or "Notification from " + pid
    return {"platform_id": pid, "title": title, "url": link, "reward": reward, "currency": currency, "deadline": deadline,
            "skills": sorted(oc.match_skills(subject + " " + body)), "availability": (AVAIL_RX.search(body).group(0) if AVAIL_RX.search(body) else None),
            "allowed_countries": countries, "kenya": kenya_note, "sender": redact(sender)[:200], "date": str(msg.get("Date") or ""),
            "instructions": body.strip()[:6000], "id": "email-" + hashlib.sha256(raw).hexdigest()[:20]}


def import_messages(conn, raws: Iterable[bytes], source: str) -> dict[str, Any]:
    """Parse and import each message once (by content hash). Disabled platforms and non-platform mail are skipped with a reason."""
    seen = set(db.get_state(conn, "email_imported", []) or [])
    stats: dict[str, Any] = {"messages": 0, "imported": 0, "duplicates": 0, "not_platform": 0, "skipped": []}
    new_seen = []
    for raw in raws:
        stats["messages"] += 1
        h = hashlib.sha256(raw).hexdigest()
        if h in seen:
            stats["duplicates"] += 1
            continue
        new_seen.append(h)
        facts = parse(raw)
        if not facts:
            stats["not_platform"] += 1
            continue
        prof = permissions.profile(facts["platform_id"])
        if prof and prof.get("disabled_reason"):
            stats["skipped"].append(f"{facts['platform_id']}: {prof['disabled_reason']}")
            continue
        row = {k: facts[k] for k in ("id", "title", "url", "reward", "currency", "deadline", "allowed_countries") if facts.get(k) is not None}
        row["instructions"] = (f"[Imported from a notification email ({source}); sender {facts['sender']}; availability: {facts['availability'] or 'not stated'}; "
                               f"eligibility: {', '.join(facts['allowed_countries'] or []) or facts['kenya'] or 'not stated'}]\n\n" + facts["instructions"])
        r = discovery.ManualImportAdapter(facts["platform_id"]).import_records(conn, [row])
        stats["imported"] += r["created"]
        stats["duplicates"] += r["duplicates"]
        stats["skipped"] += r["skipped"]
    with db.tx(conn):
        db.set_state(conn, "email_imported", sorted(seen | set(new_seen))[-20000:])
        health = db.get_state(conn, "email_discovery_health", {}) or {}
        health[source] = {"last_run": db.now_iso(), **{k: v if not isinstance(v, list) else len(v) for k, v in stats.items()}}
        db.set_state(conn, "email_discovery_health", health)
    return stats


class EmlFolder:
    name = "eml_folder"

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder or INBOX_DIR)

    def fetch(self) -> list[bytes]:
        if not self.folder.is_dir():
            return []
        return [p.read_bytes() for p in sorted(self.folder.glob("*.eml")) if p.is_file() and p.stat().st_size <= MAX_BYTES]


class ImapInbox:
    """Read-only access to your own Gmail: search by each platform's sender domain, fetch with BODY.PEEK (never marks as read)."""
    name = "gmail_readonly"

    def __init__(self, address: str, password: str, days: int = 14, connect=None):
        self.address, self._pw, self.days, self._connect = address, password, days, connect

    def fetch(self) -> list[bytes]:
        m = (self._connect or (lambda: imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context(), timeout=60)))()
        out: list[bytes] = []
        try:
            m.login(self.address, self._pw)
            m.select("INBOX", readonly=True)
            since = (datetime.now(timezone.utc) - timedelta(days=self.days)).strftime("%d-%b-%Y")
            for domain in sorted(set(platform_domains())):
                typ, data = m.search(None, "FROM", f'"@{domain}"', "SINCE", since)
                if typ != "OK" or not data or not data[0]:
                    typ, data = m.search(None, "FROM", f'".{domain}"', "SINCE", since)
                for num in (data[0].split() if typ == "OK" and data and data[0] else [])[-25:]:
                    typ, parts = m.fetch(num, "(BODY.PEEK[])")
                    if typ == "OK" and parts and isinstance(parts[0], tuple):
                        out.append(parts[0][1])
        finally:
            try:
                m.logout()
            except Exception:                                     # noqa: BLE001
                pass
        return out


def health(conn) -> dict[str, Any]:
    return db.get_state(conn, "email_discovery_health", {}) or {}
