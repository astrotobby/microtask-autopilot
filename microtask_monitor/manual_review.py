"""Manual-review adapters for selected platforms with no permitted public task feed.

These adapters NEVER touch the network. A human reviews the portal and may record what they
actually saw in ``data/manual/<platform>.json``; this module normalizes, scores, deduplicates
and tracks availability for those records. Country-restricted services are deliberately not
wired to this importer. No login, scraping, acceptance, task completion or submission occurs.

Nothing is accepted, applied for, paid for, or submitted here.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import opportunity_common as oc
except ImportError:  # pragma: no cover
    import opportunity_common as oc

ROOT = Path(__file__).resolve().parents[1]
MANUAL_DIR = ROOT / "data" / "manual"          # gitignored: never committed

UNAVAILABLE_WORDS = {"expired": "expired", "filled": "filled", "full": "filled", "closed": "closed",
                     "completed": "filled", "unavailable": "unavailable", "paused": "unavailable",
                     "cancelled": "closed", "canceled": "closed"}
WORLDWIDE_WORDS = ("international", "worldwide", "global", "all countries", "anywhere")
KENYA_WORDS = ("kenya", "ke")

PLATFORMS = {
    "gainworkers": {
        "name": "GainWorkers", "kind": "task", "file": "gainworkers_tasks.json",
        "portal": "https://www.gainworkers.com/",
        "reason": ("Task listings sit behind login (/jobs redirects to /login and is disallowed in "
                   "robots.txt), the Terms of Service prohibit scraping and bots, and the published "
                   "API (/api/v2) is an employer/SMM-reseller API, not a worker-task API."),
    },
    "reworkio": {
        "name": "REWORKIO", "kind": "fixed", "file": "reworkio_briefs.json",
        "portal": "https://reworkio.com/projects/",
        "reason": ("The public /projects/ and /opportunities/ pages are client-rendered shells whose "
                   "briefs load from an undocumented backend, and the dashboard requires sign-in. "
                   "No supported public discovery interface was found."),
    },
    "usertesting": {
        "name": "UserTesting", "kind": "task", "file": "usertesting_tests.json",
        "portal": "https://www.usertesting.com/",
        "reason": ("Contributor availability is country- and demand-dependent. Kenya must first be "
                   "accepted by the official sign-up flow. Tests are visible in the signed-in account; "
                   "record only tests you personally see. Verified PayPal is required."),
    },
    "paidviewpoint": {
        "name": "PaidViewpoint", "kind": "task", "file": "paidviewpoint_surveys.json",
        "portal": "https://app.paidviewpoint.com/",
        "reason": ("The registration flow dynamically checks country availability and there is no "
                   "published full country list. Kenya must be accepted by signup before any survey "
                   "is considered eligible. Survey opportunities and volume are not guaranteed."),
    },
    "whop_workforce": {
        "name": "Whop Workforce", "kind": "task", "file": "whop_workforce_bounties.json",
        "portal": "https://whop.com/whop-workforce",
        "reason": ("Workforce bounties are community/account-facing. Whop's Terms prohibit automated "
                   "access such as robots, spiders and scrapers without written consent. Manually "
                   "record only bounties you can actually access and whose country and payout rules you verify."),
    },
}


def _status_from(row: dict[str, Any], now: datetime) -> str:
    raw = str(row.get("status") or "open").strip().lower()
    for word, status in UNAVAILABLE_WORDS.items():
        if word in raw:
            return status
    slots = row.get("remaining_slots")
    if isinstance(slots, (int, float)) and not isinstance(slots, bool) and slots <= 0:
        return "filled"
    deadline = oc.parse_time(row.get("deadline"))
    if deadline and deadline < now:
        return "expired"
    return "open"


def _region_class(region: Any) -> tuple[str, list[str]]:
    """Worldwide only when explicitly stated; a blank region is 'unspecified', never Kenya."""
    if isinstance(region, (list, tuple)):
        parts = [str(r).strip() for r in region if str(r).strip()]
    else:
        parts = [p.strip() for p in re.split(r"[,;/|]", str(region or "")) if p.strip()]
    lowered = [p.lower() for p in parts]
    if any(p in KENYA_WORDS for p in lowered):
        return oc.KENYA_EXPLICIT, parts
    if any(w in p for p in lowered for w in WORLDWIDE_WORDS):
        return oc.WORLDWIDE, parts
    if parts:
        return oc.RESTRICTED_OTHER, parts
    return oc.UNSPECIFIED, parts


def _payment(row: dict[str, Any], kind: str) -> tuple[str, float | None, str]:
    value = row.get("payment", row.get("budget"))
    amount = oc.parse_amount(value)
    if amount is None and isinstance(value, str):
        match = re.search(r"\$\s*([0-9][0-9,]*\.?[0-9]*)", value)
        amount = oc.parse_amount(match.group(1)) if match else None
    if amount is None or amount <= 0:
        return "Not disclosed", None, ""
    return f"USD {amount:g}", amount, oc.payment_flag(kind, amount)


def normalize_row(platform_id: str, row: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    cfg = PLATFORMS[platform_id]
    now = now or datetime.now(timezone.utc)
    if not isinstance(row, dict):
        return None
    title = str(row.get("title") or "").strip()
    url = str(row.get("url") or row.get("application_url") or "").strip()
    if not title or not url.startswith("https://"):
        return None
    description = str(row.get("description") or "")
    skills = [str(s) for s in (row.get("skills") or []) if isinstance(s, str)]
    text = " ".join([title, description, " ".join(skills)])
    eligibility, region_parts = _region_class(row.get("region", row.get("location")))
    payment_text, amount, pay_flag = _payment(row, cfg["kind"])
    status = _status_from(row, now)
    cost_flags = oc.find_cost_flags(text + " " + " ".join(str(c) for c in (row.get("cost_notes") or [])))
    matched = oc.match_skills(text)
    posted = oc.parse_time(row.get("posted") or row.get("discovered_at"))
    hourly = None   # no hourly rate is invented for task/fixed-price items
    score, priority = oc.score_opportunity(
        eligibility=eligibility, skills=matched, hourly_usd=hourly, payment_flagged=bool(pay_flag),
        posted=posted, available=(status == "open"), now=now)
    if cost_flags and priority == "high_review":
        priority = "medium_review"          # upfront costs always need a human decision first
    ext_id = str(row.get("id") or hashlib.sha1(url.split("#")[0].rstrip("/").encode()).hexdigest()[:16])
    flags = [f for f in (pay_flag,) if f] + [f"possible_upfront_cost:{c}" for c in cost_flags]
    return {
        "platform_id": platform_id, "source": cfg["name"], "external_id": ext_id,
        "title": title, "url": url, "apply_url": url, "summary": re.sub(r"\s+", " ", description)[:600],
        "published": str(row.get("posted") or ""), "category": str(row.get("category") or "manual_review"),
        "skills": skills, "region": region_parts, "kenya_eligibility": eligibility,
        "payment_display": payment_text, "payment_flag": pay_flag, "flags": flags,
        "remaining_slots": row.get("remaining_slots") if isinstance(row.get("remaining_slots"), (int, float)) else None,
        "status": status, "matched_skill_groups": sorted(matched),
        "relevance_score": score, "priority": priority,
        "discovered_at": str(row.get("discovered_at") or now.isoformat()),
        "last_seen_at": now.isoformat(), "last_verified": str(row.get("last_verified") or ""),
        "entry_method": "manual_review", "requires_human_verification": True,
        "approval_required": "A human must review, apply, pay or accept anything on this platform.",
    }


def ingest(platform_id: str, rows: list[Any], previous: list[dict[str, Any]] | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """Normalize manual records: dedupe, drop expired/filled, and mark vanished records unavailable."""
    now = now or datetime.now(timezone.utc)
    stats = {"records": len(rows), "invalid": 0, "duplicates": 0, "unavailable": 0}
    seen: dict[str, dict[str, Any]] = {}
    urls: set[str] = set()
    unavailable: list[dict[str, Any]] = []
    for row in rows:
        item = normalize_row(platform_id, row, now)
        if item is None:
            stats["invalid"] += 1
            continue
        url_key = item["url"].split("#", 1)[0].rstrip("/")
        if item["external_id"] in seen or url_key in urls:
            stats["duplicates"] += 1
            continue
        seen[item["external_id"]] = item
        urls.add(url_key)
    open_items = []
    for item in seen.values():
        if item["status"] == "open":
            open_items.append(item)
        else:
            stats["unavailable"] += 1
            unavailable.append(item)
    for old in previous or []:                       # known before, absent now => no longer listed
        if old.get("external_id") not in seen:
            gone = dict(old, status="not_seen_since_last_review")
            unavailable.append(gone)
            stats["unavailable"] += 1
    open_items.sort(key=lambda i: (-i["relevance_score"], i["title"]))
    return {"items": open_items, "unavailable": unavailable, "stats": stats}


def scan(platform_id: str, manual_dir: Path = MANUAL_DIR, now: datetime | None = None) -> dict[str, Any]:
    """No network access. Reads the user's own manual records if present."""
    cfg = PLATFORMS[platform_id]
    now = now or datetime.now(timezone.utc)
    path = manual_dir / cfg["file"]
    health: dict[str, Any] = {
        "status": "manual_review_required", "automated_discovery": "unavailable",
        "reason": cfg["reason"], "portal_url": cfg["portal"], "manual_records": 0,
        "last_successful_scan": None, "error": None}
    items: list[dict[str, Any]] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get("records", data) if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError("expected a list of records")
            result = ingest(platform_id, rows, now=now)
            items = result["items"]
            health.update(manual_records=len(rows), last_successful_scan=now.isoformat(),
                          **{k: v for k, v in result["stats"].items() if k != "records"})
        except (OSError, ValueError) as exc:
            health["error"] = f"Could not read {path.name}: {exc}"
    return {"items": items, "health": health}
