"""Shared, transparent scoring helpers for platform adapters.

Nothing here invents earnings: payment fields are copied from the source listing,
and anything missing, malformed or implausibly high is flagged for manual checking.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

KENYA = "KE"

# Eligibility vocabulary (kept compatible with the existing feed vocabulary).
KENYA_EXPLICIT = "kenya_explicitly_eligible"
WORLDWIDE = "worldwide_eligible"
RESTRICTED_OTHER = "restricted_other_countries"
UNSPECIFIED = "kenya_eligibility_unspecified"
RESTRICTED_FOR_USER = "restricted_for_user_location"

# The user's skills, as groups of search terms (matched on word boundaries).
SKILL_GROUPS: dict[str, list[str]] = {
    "ai_training_evaluation": ["ai trainer", "ai training", "rlhf", "evaluator", "evaluation",
                               "red team", "red-team", "model evaluation", "rater", "grader"],
    "data_annotation": ["annotation", "annotator", "labeling", "labelling", "data labeling"],
    "prompt_engineering": ["prompt", "prompts", "prompt engineering"],
    "python": ["python"],
    "javascript": ["javascript", "node.js", "nodejs", "node js", "typescript", "react"],
    "backend_fullstack": ["backend", "back-end", "full-stack", "full stack", "fullstack", "api", "apis",
                          "rest api", "webhook", "webhooks", "sql", "data pipeline", "data pipelines", "etl"],
    "ai_assisted_coding": ["claude code", "codex", "copilot", "cursor", "ai-assisted", "vibe coding",
                           "vibecode", "vibe code"],
    "ai_automation": ["automation", "n8n", "make.com", "zapier", "workflow", "workflows", "agent",
                      "agents", "chatbot", "chatbots", "llm", "llm integration", "generative ai"],
    "writing_research": ["writing", "writer", "research", "copywriting", "editor", "content creation",
                         "blog", "blogging", "content writer", "academic writing", "article", "proofreading"],
    "ai_video_creation": ["ai video", "video generation", "runway", "pika", "heygen", "capcut", "video editing",
                          "video editor", "canva", "short-form video", "text-to-video"],
    "data_entry": ["data entry", "typing", "spreadsheet", "copy paste", "copy-paste", "transcription"],
    "ai_tools": ["chatgpt", "claude", "gemini", "manus", "openai", "anthropic", "cursor", "vs code", "vscode"],
    "web_development": ["html", "css", "web development", "website", "web developer", "frontend", "front-end"],
}

UPFRONT_COST_TERMS = [
    "paid membership", "membership fee", "subscription", "deposit", "fund your wallet",
    "fund the escrow", "escrow funding", "pay to apply", "application fee", "registration fee",
    "starter kit", "buy credits", "purchase credits", "upfront", "joining fee",
]

# Advertised amounts above these are treated as "verify manually", never as expected earnings.
SUSPICIOUS_HOURLY_USD = 150.0
SUSPICIOUS_FIXED_USD = 10_000.0
SUSPICIOUS_MICROTASK_USD = 5.0


def _term_regex(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", re.IGNORECASE)


_SKILL_REGEX = {g: [(_t, _term_regex(_t)) for _t in terms] for g, terms in SKILL_GROUPS.items()}


def match_skills(text: str) -> dict[str, list[str]]:
    """Return {skill_group: [matched terms]} for the user's skill groups."""
    found: dict[str, list[str]] = {}
    for group, patterns in _SKILL_REGEX.items():
        hits = [term for term, rx in patterns if rx.search(text or "")]
        if hits:
            found[group] = hits
    return found


def find_cost_flags(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [term for term in UPFRONT_COST_TERMS if term in lowered]


def parse_amount(value: Any) -> float | None:
    """Return a non-negative finite number, or None for missing/invalid payment values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def payment_flag(kind: str, amount: float | None) -> str:
    """'' when fine/undisclosed, 'suspicious_verify_manually' for unusually high amounts."""
    if amount is None:
        return ""
    limit = {"hour": SUSPICIOUS_HOURLY_USD, "fixed": SUSPICIOUS_FIXED_USD,
             "task": SUSPICIOUS_MICROTASK_USD}.get(kind)
    return "suspicious_verify_manually" if limit is not None and amount > limit else ""


def parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def score_opportunity(*, eligibility: str, skills: dict[str, list[str]], hourly_usd: float | None,
                      payment_flagged: bool, posted: datetime | None, available: bool,
                      now: datetime | None = None) -> tuple[int, str]:
    """Deterministic 0-100 score in the user's priority order, and a review priority.

    Order: Kenya-explicit > worldwide > skills > payment > recency > availability.
    """
    now = now or datetime.now(timezone.utc)
    score = {KENYA_EXPLICIT: 40, WORLDWIDE: 25, UNSPECIFIED: 5,
             RESTRICTED_OTHER: -40, RESTRICTED_FOR_USER: -100}.get(eligibility, 0)
    score += min(24, 8 * len(skills))
    if hourly_usd is not None and not payment_flagged:
        score += int(min(15, hourly_usd / 4))
    if posted:
        age_days = (now - posted).total_seconds() / 86400
        score += 10 if age_days <= 7 else 5 if age_days <= 30 else 0
    score += 5 if available else -30
    score = max(0, min(100, score))
    if not available or eligibility in (RESTRICTED_OTHER, RESTRICTED_FOR_USER):
        priority = "low_or_unrelated"
    elif score >= 60 and eligibility in (KENYA_EXPLICIT, WORLDWIDE):
        priority = "high_review"
    elif score >= 35:
        priority = "medium_review"
    else:
        priority = "low_or_unrelated"
    return score, priority


def dedupe(items: Iterable[dict[str, Any]], key=lambda i: (i.get("platform_id"), i.get("external_id") or i.get("url"))):
    """Keep one item per stable key; the highest-scoring copy wins."""
    best: dict[Any, dict[str, Any]] = {}
    for item in items:
        k = key(item)
        if k not in best or item.get("relevance_score", 0) > best[k].get("relevance_score", 0):
            best[k] = item
    return list(best.values())
