from __future__ import annotations

import email
import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from email import policy as email_policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Any

try:
    from microtask_monitor import db, notify
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import db, notify
    from secret_store import redact

MAX_INBOX_MESSAGES = 20
MAX_SENT_HEADERS = 100
MAX_REPLIES_PER_PASS = 5
LOOKBACK_DAYS = 45

_JOB_SUBJECT = re.compile(r"\b(application|job|position|vacancy|career|interview|recruit(?:er|ment)?|hiring|role|candidate|employment|talent)\b", re.I)
_SECURITY = re.compile(r"\b(password|passcode|one[- ]time|otp|2fa|verification code|login code|security code|bank details|bank account|passport|national id|identity document|social security|ssn|date of birth|birth date|driver.?s license|id card)\b", re.I)
_AUTOMATED = re.compile(r"\b(auto(?:matic)? reply|out of office|do not reply|no[- ]reply|delivery status notification|undeliverable|message delivered)\b", re.I)
_OPT_OUT = re.compile(r"\b(do not contact|don't contact|unsubscribe|remove me|stop emailing|no further contact)\b", re.I)
_ASSESSMENT = re.compile(r"\b(assessment|test link|coding challenge|take[- ]home|technical challenge|complete (?:your )?(?:ai )?interview|interview questions|answer (?:the following|these) questions|screening questions|record (?:a )?(?:video|response)|one[- ]way interview)\b", re.I)
_COMMITMENT = re.compile(r"\b(salary|pay expectation|expected salary|compensation|hourly rate|rate expectation|contract|offer letter|accept (?:the )?offer|sign (?:the )?(?:contract|agreement)|start date|notice period|work authorization|work permit|visa|relocation|references?|background check|tax form|preferred interview slot|confirm (?:the )?(?:time|date|slot))\b", re.I)
_INTERVIEW = re.compile(r"\b(interview invitation|invite you to (?:an )?interview|would like to interview|schedule (?:an )?interview|interview availability|interview with)\b", re.I)
_AVAILABILITY = re.compile(r"\b(availability|available hours|hours per day|how many hours|working hours|when can you start|when are you available|time zone|timezone)\b", re.I)
_PORTFOLIO = re.compile(r"\b(portfolio|work samples?|samples of your work|linkedin|resume|résumé|\bcv\b)\b", re.I)
_SKILLS = re.compile(r"\b(skills?|experience|tools?|technologies|proficien(?:cy|t)|what can you do|background)\b", re.I)
_INTEREST = re.compile(r"\b(still interested|are you interested|confirm your interest|interested in the role|would you like to proceed)\b", re.I)
_REJECTION = re.compile(r"\b(unfortunately|not moving forward|not selected|unsuccessful|decided to proceed with another|filled the position|will not be progressing)\b", re.I)
_ACK = re.compile(r"\b(application received|received your application|thank you for applying|we have your application|we'll review your application|under review|application update)\b", re.I)


def _body(msg: email.message.Message) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    try:
        body = part.get_content()
    except (LookupError, UnicodeError, AttributeError):
        return ""
    if part.get_content_type() == "text/html":
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return re.sub(r"[ \t]+", " ", str(body))[:12000]


def _ids(value: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def _header_fetch_rows(m: Any, mailbox: str, days: int, limit: int) -> list[tuple[int, bytes]]:
    typ, _ = m.select(mailbox, readonly=True)
    if typ != "OK":
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    typ, data = m.search(None, "SINCE", since)
    if typ != "OK" or not data or not data[0]:
        return []
    out: list[tuple[int, bytes]] = []
    numbers = data[0].split()[-limit:]
    spec = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM IN-REPLY-TO REFERENCES TO DATE)])"
    for start in range(0, len(numbers), 10):
        group = b",".join(numbers[start:start + 10])
        typ, parts = m.fetch(group, spec)
        if typ == "OK" and parts:
            for part in parts:
                if isinstance(part, tuple) and isinstance(part[1], bytes):
                    match = re.match(rb"(\d+)", part[0] or b"")
                    if match:
                        out.append((int(match.group(1)), part[1]))
    return out


def _header_fetch(m: Any, mailbox: str, days: int, limit: int, header_only: bool) -> list[bytes]:
    typ, _ = m.select(mailbox, readonly=True)
    if typ != "OK":
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    typ, data = m.search(None, "SINCE", since)
    if typ != "OK" or not data or not data[0]:
        return []
    out: list[bytes] = []
    numbers = data[0].split()[-limit:]
    spec = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT TO DATE)])" if header_only else "(BODY.PEEK[])"
    for start in range(0, len(numbers), 10):
        group = b",".join(numbers[start:start + 10])
        typ, parts = m.fetch(group, spec)
        if typ == "OK" and parts:
            for part in parts:
                if isinstance(part, tuple) and isinstance(part[1], bytes):
                    out.append(part[1])
    return out


def _employment_thread(subject: str) -> bool:
    return bool(_JOB_SUBJECT.search(subject or ""))


def draft_reply(incoming_subject: str, body: str, profile: dict[str, Any]) -> tuple[str, str, str]:
    """Return (status, category, body). Only simple, factual, reversible replies may be sent."""
    text = f"{incoming_subject}\n{body}"
    low = text.lower()
    name = ((profile.get("contact") or {}).get("full_name") or "Tobias Chanzera").strip()
    signature = f"\n\nKind regards,\n{name}\nKenya (East Africa Time)\n"
    if _SECURITY.search(text):
        return "human_hold", "security_or_identity", ""
    if _AUTOMATED.search(text):
        return "no_reply", "automated_message", ""
    if _OPT_OUT.search(text):
        return "no_reply", "opt_out_or_do_not_contact", ""
    if _ASSESSMENT.search(text):
        return "human_hold", "assessment_or_interview_content", ""
    if _COMMITMENT.search(text):
        return "human_hold", "contract_compensation_or_commitment", ""
    if _REJECTION.search(text):
        return "auto_reply", "rejection_acknowledgement", (
            "Thank you for letting me know and for considering my application. "
            "I appreciate your time and wish your team the best with the appointment." + signature
        )
    if _AVAILABILITY.search(text):
        availability = profile.get("availability") or {}
        hours = availability.get("hours_per_day")
        tz = availability.get("time_zone") or "Africa/Nairobi"
        if not isinstance(hours, int) or hours < 1 or hours > 16:
            return "human_hold", "availability_not_confirmed", ""
        return "auto_reply", "general_availability", (
            f"Thank you for your message. I can commit up to {hours} hours per day and am based in Kenya "
            f"({tz}). Please share the working pattern or proposed time slots, and I will confirm whether they fit."
            + signature
        )
    if _INTERVIEW.search(text):
        return "auto_reply", "interview_interest_not_scheduling", (
            "Thank you for the invitation. I am interested in proceeding. "
            "Could you please share a few available interview dates and times in East Africa Time (EAT)? "
            "I will review the options and confirm one." + signature
        )
    if _PORTFOLIO.search(text):
        links = (profile.get("contact") or {}).get("linkedin"), (profile.get("portfolio") or {}).get("youtube_channel")
        if not all(links):
            return "human_hold", "portfolio_links_incomplete", ""
        return "auto_reply", "portfolio_links", (
            "Thank you for your message. Here are my public professional and work links:\n"
            f"LinkedIn: {links[0]}\nYouTube channel: {links[1]}\n"
            "Please let me know which specific skills or type of work you would like me to highlight."
            + signature
        )
    if _SKILLS.search(text):
        skills = profile.get("established_skills") or {}
        selected: list[str] = []
        if re.search(r"\b(video|premiere|after effects|motion graphics|editing)\b", low):
            selected += [s for s in skills.get("ai_video_and_design", []) if any(k in s.lower() for k in ("premiere", "after effects", "capcut", "runway", "pika", "heygen", "canva"))]
        if re.search(r"\b(develop|software|backend|full.?stack|programming|python|javascript|typescript|coding)\b", low):
            selected += skills.get("programming", []) + skills.get("development", [])
        if re.search(r"\b(automation|workflow|n8n|make\.com|prompt|llm|ai training|ai evaluation)\b", low):
            selected += skills.get("automation", []) + skills.get("ai_workflows", []) + skills.get("ai_assistants", [])
        if re.search(r"\b(writ|content|blog|academic)\b", low):
            selected += skills.get("writing", [])
        selected = list(dict.fromkeys(selected))
        if not selected:
            return "human_hold", "specific_skill_not_verified", ""
        exp = profile.get("overall_work_experience")
        exp_line = f"\nI have {exp}" if exp else ""
        return "auto_reply", "verified_skills", (
            "Thank you for asking. Relevant skills and tools I work with include:\n"
            + "\n".join(f"- {s}" for s in selected)
            + exp_line
            + "\nI would be happy to discuss how these align with the role."
            + signature
        )
    if _INTEREST.search(text):
        return "auto_reply", "interest_confirmation", (
            "Thank you for following up. Yes, I remain interested in the opportunity. "
            "Please let me know the next steps." + signature
        )
    if _ACK.search(text):
        return "auto_reply", "application_acknowledgement", (
            "Thank you for the update and for confirming receipt of my application. "
            "I appreciate your consideration and remain interested in the opportunity."
            + signature
        )
    return "human_hold", "unclassified_employer_question", ""


def scan_and_reply(conn, mailer: Any, profile: dict[str, Any], now: datetime | None = None) -> dict[str, int]:
    """Read Inbox and Sent headers only; auto-reply solely to verified replies in job-application threads."""
    now = now or datetime.now(timezone.utc)
    m = mailer._imap()
    try:
        sent_raw = _header_fetch(m, '"[Gmail]/Sent Mail"', LOOKBACK_DAYS, MAX_SENT_HEADERS, True)
        inbox_raw = _header_fetch(m, "INBOX", LOOKBACK_DAYS, MAX_INBOX_MESSAGES, False)
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass

    sent_index: dict[str, str] = {}
    for raw in sent_raw:
        msg = email.message_from_bytes(raw, policy=email_policy.default)
        mid = str(msg.get("Message-ID") or "").strip()
        subj = re.sub(r"^(?:re|fwd?):\s*", "", str(msg.get("Subject") or ""), flags=re.I).strip()
        if mid and _employment_thread(subj) and "X-Autopilot-Employer-Reply" not in msg:
            sent_index[mid] = subj

    state = db.get_state(conn, "employer_email_reply_state", {}) or {}
    if not isinstance(state, dict):
        state = {}
    counts = {"scanned": 0, "matched": 0, "replied": 0, "held": 0, "skipped": 0, "errors": 0}
    for raw in inbox_raw:
        msg = email.message_from_bytes(raw, policy=email_policy.default)
        mid = str(msg.get("Message-ID") or "").strip()
        key = mid or "sha256:" + hashlib.sha256(raw).hexdigest()
        if key in state:
            continue
        refs = _ids(str(msg.get("In-Reply-To") or "")) + _ids(str(msg.get("References") or ""))
        parent = next((sent_index[x] for x in refs if x in sent_index), None)
        if not parent:
            continue
        counts["scanned"] += 1
        counts["matched"] += 1
        _, sender_addr = parseaddr(str(msg.get("From") or ""))
        sender_addr = sender_addr.strip()
        subject = str(msg.get("Subject") or "").strip()
        body = _body(msg)
        own = (getattr(mailer, "address", "") or "").lower()
        if not sender_addr or sender_addr.lower() == own:
            state[key] = {"status": "skipped", "reason": "not an external sender", "at": now.isoformat()}
            counts["skipped"] += 1
            continue

        status, category, reply_body = draft_reply(subject or parent, body, profile)
        if status == "no_reply":
            state[key] = {"status": status, "category": category, "at": now.isoformat()}
            counts["skipped"] += 1
            continue
        if status == "human_hold":
            state[key] = {"status": status, "category": category, "subject": subject[:200],
                          "sender": redact(sender_addr)[:120], "at": now.isoformat()}
            notify.create(conn, "application_response", f"Employer reply needs your input: {subject[:120]}",
                          f"Sender: {redact(sender_addr)}. Held because it involves {category.replace('_', ' ')}. Open the original email and respond personally.",
                          severity="urgent", key=f"employer-reply-hold:{hashlib.sha256(key.encode()).hexdigest()[:20]}")
            counts["held"] += 1
            continue
        if counts["replied"] >= MAX_REPLIES_PER_PASS:
            break

        reply = EmailMessage()
        reply["From"] = formataddr((str((profile.get("contact") or {}).get("full_name") or "Tobias Chanzera"), mailer.address))
        reply["To"] = sender_addr
        reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject or parent}"
        reply["Message-ID"] = make_msgid(domain=mailer.address.split("@")[-1])
        reply["X-Autopilot-Employer-Reply"] = "1"
        if mid:
            reply["In-Reply-To"] = mid
            refs_chain = _ids(str(msg.get("References") or ""))
            reply["References"] = " ".join(refs_chain + [mid]) if refs_chain else mid
        reply.set_content(reply_body)
        # Persist a sending marker before SMTP so an ambiguous SMTP failure cannot cause duplicate replies.
        state[key] = {"status": "sending", "category": category, "subject": subject[:200],
                      "sender": redact(sender_addr)[:120], "at": now.isoformat()}
        with db.tx(conn):
            db.set_state(conn, "employer_email_reply_state", state)
        try:
            mailer.send(reply)
        except Exception as exc:  # noqa: BLE001
            state[key] = {"status": "send_unknown", "category": category, "error": redact(str(exc))[:200], "at": now.isoformat()}
            counts["errors"] += 1
            notify.create(conn, "task_attention", "Employer reply send needs checking",
                          f"SMTP returned an error for {subject[:120]}. Check Sent Mail before retrying; no automatic resend will occur.",
                          severity="urgent", key=f"employer-reply-send:{hashlib.sha256(key.encode()).hexdigest()[:20]}")
        else:
            state[key] = {"status": "sent", "category": category, "subject": subject[:200],
                          "sender": redact(sender_addr)[:120], "sent_at": now.isoformat(), "message_id": reply["Message-ID"]}
            counts["replied"] += 1
            notify.create(conn, "application_response", f"Automatic employer reply sent: {subject[:120]}",
                          f"Sent a factual {category.replace('_', ' ')} reply to {redact(sender_addr)}. Review the Sent folder for the full message.",
                          severity="info", key=f"employer-reply-sent:{hashlib.sha256(key.encode()).hexdigest()[:20]}")
        if len(state) > 5000:
            state = dict(list(state.items())[-4000:])
        with db.tx(conn):
            db.set_state(conn, "employer_email_reply_state", state)
    return counts

[executed on device: DESKTOP-8CRRB6G (6c914e80-efa3-459f-9dac-5e673d08264f)]