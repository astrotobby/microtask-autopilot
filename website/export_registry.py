#!/usr/bin/env python3
"""Export a deliberately limited, public-safe platform snapshot for the static site.

This script does not read the local SQLite database, profile answers, applications,
tasks, submitted evidence, notifications, or earnings records.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SITE = Path(__file__).resolve().parent


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def country_label(value: str | None, platform_id: str, discovery: str) -> str:
    value = (value or "").lower()
    if platform_id == "prolific" or "waitlist" in discovery:
        return "Unsupported-country waitlist"
    if value == "unsupported":
        return "Unsupported for Kenya"
    if value == "unverified" or not value:
        return "Unverified"
    labels = {
        "task_specific": "Per task; verify",
        "job_specific": "Per job; verify",
        "project_specific": "Per project; verify",
        "role_specific": "Per role; verify",
        "account_check": "Account-level check required",
    }
    return labels.get(value, "Verify for each listing")


def discovery_label(mode: str) -> str:
    labels = {
        "documented_public_api": "Documented public API — read-only discovery",
        "manual_links_only": "Manual link review only",
        "policy_monitor_only": "Public policy monitoring only; job discovery disabled",
        "none_task_lists_require_login": "No public worker feed; manual portal review only",
        "disabled_restricted_for_user_location": "Discovery disabled by location policy",
        "disabled_current_country_policy": "Discovery disabled by current country policy",
        "unsupported_country_waitlist": "Unsupported-country waitlist only",
        "unavailable_no_supported_public_interface": "No supported public discovery interface",
        "unavailable": "No supported public discovery interface",
    }
    return labels.get(mode, "Manual review only — no automated task discovery")


def tracking_label(health: str) -> str:
    labels = {
        "automated": "Public API · read-only discovery",
        "policy_monitor": "Policy monitor only",
        "manual_only": "Manual only",
        "manual_review_required": "Manual review required",
        "disabled_pending_identity": "Disabled pending identity verification",
        "disabled_country_restricted": "Disabled by country restriction",
        "country_waitlist": "Waitlist only",
    }
    return labels.get(health, "Manual review required")


def main() -> None:
    platforms = read_json(ROOT / "microtask_monitor" / "platforms.json")
    integrations = read_json(ROOT / "microtask_monitor" / "integrations.json")
    capabilities = read_json(ROOT / "microtask_monitor" / "capabilities.json")
    integration_by_id = integrations.get("platforms", {})
    capabilities_by_id = {
        item["platform_id"]: item for item in capabilities.get("platforms", [])
    }

    public_platforms = []
    for platform in platforms.get("platforms", []):
        platform_id = platform["id"]
        integration = integration_by_id.get(platform_id, {})
        capability = capabilities_by_id.get(platform_id, {})
        discovery = integration.get("public_discovery", "none")
        country = (capability.get("country_eligibility") or {}).get("kenya")
        review = capability.get("review") or {}
        official_url = platform.get("official_url")
        if not isinstance(official_url, str) or not official_url.startswith("https://"):
            official_url = None

        public_platforms.append(
            {
                "id": platform_id,
                "name": platform["name"],
                "type": platform.get("platform_type", "unknown"),
                "official_url": official_url,
                "kenya_status": country_label(country, platform_id, discovery),
                "discovery": discovery_label(discovery),
                "tracking_status": tracking_label(integration.get("health", "manual_review_required")),
                "review_date": capability.get("last_policy_verified") or review.get("reviewed_on"),
            }
        )

    document = {
        "updated_at": max(
            platforms.get("updated_at", ""),
            integrations.get("updated_at", ""),
            capabilities.get("updated_at", ""),
        ),
        "platform_count": len(public_platforms),
        "automated_public_discovery_sources": sum(
            1 for item in integration_by_id.values()
            if item.get("public_discovery") == "documented_public_api"
        ),
        "fully_autonomous_earning_sources": 0,
        "platforms": public_platforms,
    }
    output = SITE / "data" / "platforms.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {output.relative_to(ROOT)}: {document['platform_count']} platforms, "
        f"{document['automated_public_discovery_sources']} documented public discovery source(s)."
    )


if __name__ == "__main__":
    main()
