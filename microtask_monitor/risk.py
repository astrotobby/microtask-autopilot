"""Deterministic legitimacy and risk checks for public opportunity records.

This module never declares a platform legitimate from a name alone. It only reports
signals present in the supplied listing and registered official-domain metadata.
Unknowns remain reviewable instead of being converted into earnings claims.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS_PATH = ROOT / "microtask_monitor" / "platforms.json"

HIGH_RISK = "HIGH_RISK"
REQUIRES_REVIEW = "REQUIRES_REVIEW"
UNVERIFIED_SOURCE = "UNVERIFIED_SOURCE"
REPUTABLE_SOURCE = "REPUTABLE_SOURCE"
VERIFIED_SOURCE = "VERIFIED_SOURCE"

_PATTERNS: tuple[tuple[str, str, int], ...] = (
    ("registration_fee", r"registration\s+fee|pay\s+to\s+(?:join|register)|joining\s+fee", 35),
    ("deposit_required", r"deposit\s+required|security\s+deposit|pay\s+(?:a\s+)?deposit|deposit\s+before\s+starting", 40),
    ("upfront_purchase", r"buy\s+(?:a|an)\s+|purchase\s+(?:a|an)\s+|upfront\s+(?:cost|purchase)|starter\s+kit", 30),
    ("suspicious_transfer", r"send\s+(?:us|me)\s+(?:crypto|bitcoin|ethereum)|wire\s+transfer\s+to|pay\s+in\s+crypto", 40),
    ("unrealistic_claim", r"guaranteed\s+(?:income|earnings)|earn\s+\$?\s*[0-9,]+\s*(?:daily|per\s+day)\s+guaranteed", 25),
    ("excessive_personal_data", r"send\s+(?:your\s+)?(?:password|otp|one[- ]time\s+code|private\s+key|seed\s+phrase)", 50),
)


def _registry() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(PLATFORMS_PATH.read_text(encoding="utf-8"))
        return {p["id"]: p for p in data.get("platforms", []) if p.get("id")}
    except (OSError, ValueError, TypeError):
        return {}


def _host(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def _same_or_subdomain(host: str, official: str) -> bool:
    if not host or not official:
        return False
    if host == official or host.endswith("." + official) or official.endswith("." + host):
        return True
    # Allow sibling subdomains when the registered record identifies the same
    # base domain (for example app.example.org and www.example.org).
    host_parts, official_parts = host.split("."), official.split(".")
    return len(host_parts) >= 2 and len(official_parts) >= 2 and host_parts[-2:] == official_parts[-2:]


def assess(item: dict[str, Any], *, registry: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return risk label, score, flags and evidence without making legitimacy claims."""
    registry = registry if registry is not None else _registry()
    text = " ".join(str(item.get(k) or "") for k in ("title", "summary", "description", "payment_display", "requirements"))
    lowered = text.lower()
    flags: list[str] = []
    evidence: list[str] = []
    score = 0
    for flag, pattern, weight in _PATTERNS:
        if re.search(pattern, lowered, re.I):
            flags.append(flag)
            evidence.append(f"listing text matched {flag.replace('_', ' ')}")
            score += weight

    url = str(item.get("url") or "")
    host = _host(url)
    if not url.startswith("https://"):
        flags.append("insecure_source_url")
        evidence.append("source URL is not HTTPS")
        score += 30
    platform = registry.get(str(item.get("platform_id") or ""), {})
    official = _host(platform.get("official_url"))
    if platform and official and host and not _same_or_subdomain(host, official):
        flags.append("source_domain_mismatch")
        evidence.append(f"listing host {host} differs from registered official host {official}")
        score += 25
    if not host:
        flags.append("missing_source_url")
        evidence.append("no parseable source URL was supplied")
        score += 20
    if not item.get("payment_display") or str(item.get("payment_display")) == "Not disclosed":
        flags.append("unclear_payment")
        evidence.append("payment is not disclosed in the listing")
        score += 8
    if not item.get("source"):
        flags.append("missing_source_identity")
        evidence.append("source identity is missing")
        score += 12

    score = min(100, score)
    if score >= 50:
        label = HIGH_RISK
    elif score >= 20:
        label = REQUIRES_REVIEW
    elif platform and official and host and _same_or_subdomain(host, official) and not flags:
        label = VERIFIED_SOURCE
    elif platform and official and host and _same_or_subdomain(host, official):
        label = REPUTABLE_SOURCE
    else:
        label = UNVERIFIED_SOURCE
    return {"label": label, "score": score, "flags": sorted(set(flags)), "evidence": evidence}
