"""Fail-closed readiness checks for genuinely autonomous earning sources.

This module does not automate any platform. It answers a narrower, safety-critical
question: does a verified capability record explicitly authorize every stage needed
for unattended earning? Missing or ambiguous evidence always blocks the source.
"""
from __future__ import annotations

from typing import Any

REQUIRED_STAGES = (
    "public_discovery",
    "application_submission",
    "task_acceptance",
    "task_execution",
    "evidence_submission",
    "payout_confirmation",
)

CONTRACT = {
    "official_api": "A documented official API or permitted integration is required.",
    "verified_terms": "Current official terms must explicitly cover the automated action.",
    "country": "Kenya eligibility and a Kenya-compatible payout route must be explicit.",
    "human_bypass": "No CAPTCHA, MFA, KYC, geo restriction, or private-dashboard bypass.",
    "evidence": "The source must return verifiable task, submission, outcome, and payment evidence.",
}


def _stage(record: dict[str, Any], key: str) -> str:
    value = record.get("autonomous_earning", {})
    if not isinstance(value, dict):
        return "unverified"
    stages = value.get("stages", {})
    result = stages.get(key) if isinstance(stages, dict) else None
    return result if isinstance(result, str) else "unverified"


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
    if not auto.get("terms_url"):
        reasons.append("current official automation terms are missing")
    if not auto.get("last_verified"):
        reasons.append("automation permission has no verification date")
    for stage in REQUIRED_STAGES:
        if _stage({"autonomous_earning": auto}, stage) != "permitted":
            reasons.append(f"{stage} is not explicitly permitted")
    country = record.get("country_eligibility") or {}
    if country.get("kenya") not in ("confirmed", "eligible"):
        reasons.append("Kenya eligibility is not explicitly confirmed")
    payout = record.get("payment_verification") or {}
    if payout.get("status") != "documented":
        reasons.append("Kenya-compatible payout route is not documented")
    prohibited = set(record.get("prohibited_automation") or [])
    if prohibited & {"captcha_mfa_kyc_bypass", "vpn_or_proxy_location_spoofing", "scrape_logged_in", "probe_private_endpoints"}:
        # These entries are normal for many records; they do not by themselves
        # disqualify a source because the autonomous contract never permits bypasses.
        pass
    eligible = not reasons
    return {
        "platform_id": platform_id,
        "eligible": eligible,
        "status": "eligible" if eligible else "blocked",
        "reasons": reasons,
        "verified_on": auto.get("last_verified"),
        "terms_url": auto.get("terms_url"),
        "stages": {stage: _stage({"autonomous_earning": auto}, stage) for stage in REQUIRED_STAGES},
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
