"""Reusable profile answers: the facts every application asks for, stored once in SQLite.

Only established facts are pre-filled (country, time zone, skills, tools, equipment from profile.json).
Everything that must come from YOU (name, contact, availability, links to real work, experience) starts
empty and is flagged as required, so no application packet can claim something you have not said.
Never store passwords, one-time codes, ID numbers or payment details here.
"""
from __future__ import annotations

import re
from typing import Any

try:
    from microtask_monitor import db, intelligence as ig
except ImportError:  # pragma: no cover
    import db, intelligence as ig

MAX_ANSWER = 2000
# (key, question, category, required, kind) - kind decides how a pre-fill is derived (None = user must provide)
TEMPLATES: list[tuple[str, str, str, int, str | None]] = [
    ("full_name", "Full name", "identity", 1, None),
    ("contact_email", "Contact email you are happy to share with employers", "identity", 1, None),
    ("preferred_contact", "Preferred contact method", "identity", 0, None),
    ("location", "Country of residence", "eligibility", 1, "location"),
    ("timezone", "Time zone", "eligibility", 0, "timezone"),
    ("availability", "Weekly availability and earliest start date", "work", 1, None),
    ("hourly_rate", "Preferred hourly rate in USD (only if an application asks)", "work", 0, None),
    ("skills_summary", "Skills summary", "skills", 0, "skills"),
    ("tools", "Tools and AI assistants you use", "skills", 0, "tools"),
    ("english_level", "English level (state it honestly)", "skills", 0, None),
    ("experience_summary", "Short summary of real, relevant experience (only what is true)", "experience", 1, None),
    ("ai_training_experience", "Previous AI training / data labeling work: platforms and task types (only if true)", "experience", 0, None),
    ("why_fit", "Why are you a good fit for AI evaluation / coding tasks?", "experience", 0, None),
    ("portfolio_links", "Links to real work: portfolio, GitHub, writing samples", "evidence", 1, None),
    ("linkedin", "LinkedIn URL (if you have one)", "evidence", 0, None),
    ("equipment", "Equipment you work on", "logistics", 0, "equipment"),
]
_SENSITIVE = re.compile(r"(password|passcode|passwd|one[- ]time|otp\b|cvv|card number|pin code|secret key|api[_ -]?key|private key|recovery code)", re.I)
_LONG_NUMBER = re.compile(r"\d{12,}")


class AnswerError(ValueError):
    pass


def looks_sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(text or "") or _LONG_NUMBER.search(re.sub(r"[\s-]", "", text or "")))


def _prefill(kind: str | None, profile: dict[str, Any]) -> str | None:
    if kind == "location":
        return profile.get("country") or None
    if kind == "timezone":
        return "East Africa Time (UTC+3)" if profile.get("country") == "Kenya" else None
    if kind == "skills":
        skills = profile.get("established_skills", {})
        return "; ".join(s for group in ("programming", "development", "ai_workflows", "writing", "operations", "automation") for s in skills.get(group, [])) or None
    if kind == "tools":
        skills = profile.get("established_skills", {})
        return "; ".join(skills.get("ai_coding_tools", []) + skills.get("ai_assistants", []) + skills.get("ai_video_and_design", [])) or None
    if kind == "equipment":
        d = profile.get("devices", {})
        parts = [d.get("computer"), d.get("phone")]
        return "; ".join(p for p in parts if p) or None
    return None


def ensure_defaults(conn, profile: dict[str, Any] | None = None) -> int:
    """Insert missing template rows. Never overwrites an answer you have written."""
    profile = profile if profile is not None else ig.load_profile()
    now = db.now_iso()
    added = 0
    for pos, (key, question, category, required, kind) in enumerate(TEMPLATES, 1):
        if conn.execute("SELECT 1 FROM profile_answers WHERE key=?", (key,)).fetchone():
            continue
        pre = _prefill(kind, profile)
        conn.execute("INSERT INTO profile_answers(key,question,answer,category,required,source,position,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (key, question, pre, category, required, "profile" if pre else "template", pos, now))
        added += 1
    return added


def list_answers(conn) -> list[dict[str, Any]]:
    rows = db.rows(conn.execute("SELECT * FROM profile_answers ORDER BY position, key"))
    for r in rows:
        r["required"] = bool(r["required"])
        r["needs_input"] = r["required"] and not (r["answer"] or "").strip()
    return rows


def set_answer(conn, key: str, answer: str) -> dict[str, Any]:
    row = conn.execute("SELECT key FROM profile_answers WHERE key=?", (key,)).fetchone()
    if not row:
        raise AnswerError(f"Unknown profile answer '{key}'")
    answer = (answer or "").strip()
    if len(answer) > MAX_ANSWER:
        raise AnswerError(f"Answers are limited to {MAX_ANSWER} characters")
    if looks_sensitive(answer):
        raise AnswerError("That looks like a password, code, card or ID number. Never store those here.")
    with db.tx(conn):
        conn.execute("UPDATE profile_answers SET answer=?, source='user', updated_at=? WHERE key=?", (answer or None, db.now_iso(), key))
        db.audit(conn, "profile.answer", "profile_answer", key, {"length": len(answer)})
    return next(a for a in list_answers(conn) if a["key"] == key)


def missing_required(conn) -> list[dict[str, Any]]:
    return [a for a in list_answers(conn) if a["needs_input"]]


def as_map(conn) -> dict[str, str]:
    return {a["key"]: (a["answer"] or "").strip() for a in list_answers(conn)}
