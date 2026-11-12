"""Fail-closed readiness checks for genuinely autonomous earning sources.

This module does not automate any platform. It answers a narrower, safety-critical
question: does a verified capability record explicitly authorize every stage needed
for unattended earning? Missing or ambiguous evidence always blocks the source.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

REQUIRED_STAGES = (
    "public_discovery",
    "application_submission",
    "task_acceptance",
    "task_execution",
    "evidence_submission",
    "outcome_retrieval",
    "payout_confirmation",
)

STAGE_STATES = {"permitted", "human_required", "prohibited", "unverified"}
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "example.test", "test", "localhost"}
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")

CONTRACT = {
    "official_api": "A documented official API or permitted integration is required.",
    "verified_terms": "Current official terms must explicitly cover the automated action.",
    "country": "Kenya eligibility and a Kenya-compatible payout route must be explicit.",
    "human_bypass": "No CAPTCHA, MFA, KYC, geo restriction, or private-dashboard bypass.",
    "evidence": "The source must return verifiable task, submission, outcome, and payment evidence.",
}


def _url_host(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower().rstrip(".")
    if host in _PLACEHOLDER_DOMAINS or any(host.endswith("." + d) for d in _PLACEHOLDER_DOMAINS):
        return None
    return host


def _official_url(value: Any, domain: Any) -> bool:
    host = _url_host(value)
    if not host or not isinstance(domain, str):
        return False
    expected = domain.lower().strip().rstrip(".")
    if not expected or expected in _PLACEHOLDER_DOMAINS:
        return False
    return host == expected or host.endswith("." + expected)


def _stage(auto: dict[str, Any], key: str) -> str:
    stages = auto.get("stages", {})
    result = stages.get(key) if isinstance(stages, dict) else None
    return result if result in STAGE_STATES else "unverified"


def _date_is_current(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value[:10]) >= date.today()
    except ValueError:
        return False


def evaluate(platform_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Return an auditable decision; never infer permission from reachability."""
    reasons: list[str] = []
    auto = record.get("autonomous_earning")
    if not isinstance(auto, dict):
        reasons.append("no autonomous_earning capability record")
        auto = {}
    if auto.get("status") != "verified":
        reasons.append("end-to-end automation is not verified")
    if auto.get("official_api") is not True:
        reasons.append("no verified official end-to-end API/integration")
    domain = auto.get("official_domain")
    if not isinstance(domain, str) or not _url_host("https://" + domain):
        reasons.append("official platform domain is missing or invalid")
    for key in ("terms_url", "documentation_url"):
        if not _official_url(auto.get(key), domain):
            reasons.append(f"{key} is not a verified HTTPS official-domain source")
    if not isinstance(auto.get("policy_clause"), str) or not auto["policy_clause"].strip():
        reasons.append("an exact reviewed policy clause is missing")
    if not _SHA256.fullmatch(str(auto.get("evidence_sha256", ""))):
        reasons.append("reviewed policy evidence SHA-256 is missing or invalid")
    if not auto.get("reviewer"):
        reasons.append("policy evidence reviewer or verification method is missing")
    if not _date_is_current(auto.get("next_review")):
        reasons.append("policy verification is expired or has no valid next review date")
    if not _date_is_current(auto.get("last_verified")):
        reasons.append("automation permission has no current verification date")
    if auto.get("contradictions"):
        reasons.append("platform permission evidence contains unresolved contradictions")
    if auto.get("restrictions"):
        reasons.append("platform contract records restrictions that must be resolved manually")
    for field in ("api_scope", "api_operations", "api_authentication", "rate_limits", "required_user_authorization"):
        value = auto.get(field)
        if value is None or value == "" or value == [] or value == {}:
            reasons.append(f"documented {field.replace('_', ' ')} is missing")
    for stage in REQUIRED_STAGES:
        state = _stage(auto, stage)
        if state != "permitted":
            reasons.append(f"{stage} is {state}, not explicitly permitted for unattended operation")
    country = record.get("country_eligibility")
    if not isinstance(country, dict) or country.get("kenya") not in ("confirmed", "eligible") or not country.get("evidence"):
        reasons.append("Kenya eligibility is not directly evidenced and explicitly confirmed")
    payout = record.get("payment_verification")
    if not isinstance(payout, dict) or payout.get("status") != "documented" or not payout.get("evidence"):
        reasons.append("Kenya-compatible payout route is not directly evidenced")
    eligible = not reasons
    return {
        "platform_id": platform_id,
        "eligible": eligible,
        "status": "eligible" if eligible else "blocked",
        "reasons": reasons,
        "verified_on": auto.get("last_verified"),
        "terms_url": auto.get("terms_url"),
        "stages": {stage: _stage(auto, stage) for stage in REQUIRED_STAGES},
    }


def assess(caps: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Assess all capability records and produce a stable dashboard/API payload."""
    results = [evaluate(pid, record) for pid, record in sorted(caps.items())]
    eligible = [r for r in results if r["eligible"]]
    return {
        "mode": "autonomous_earning",
        "enabled": bool(eligible),
        "eligible_platforms": eligible,
        "blocked_platforms": [r for r in results if not r["eligible"]],
        "required_contract": CONTRACT,
        "message": (
            "No platform has a verified end-to-end autonomous earning contract."
            if not eligible else "At least one platform has a verified end-to-end contract; connector review is still required."
        ),
    }
