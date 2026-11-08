"""OpenTrain AI public job discovery (PUBLIC DISCOVERY only).

Uses the documented anonymous endpoint ``GET https://app.opentrain.ai/api/public/v1/jobs``
(OpenAPI: https://app.opentrain.ai/api/public/v1/openapi.json). No API key, cookie or
login is used. The endpoint has its own per-IP rate limit (documented as 120 requests per
minute); this adapter stays at or below ~100/minute, honours HTTP 429 Retry-After, and
caches the last good result.

Applications are never submitted: each opportunity carries an application URL for a human
to review and use.
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:  # package import (tests) or direct script import (GitHub Actions)
    from microtask_monitor import opportunity_common as oc
except ImportError:  # pragma: no cover
    import opportunity_common as oc

PLATFORM_ID = "opentrain_ai"
PLATFORM_NAME = "OpenTrain AI"
BASE_URL = "https://app.opentrain.ai/api/public/v1/jobs"
ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "microtask_monitor" / "cache" / "opentrain_jobs.json"
USER_AGENT = "MicrotaskOpportunityMonitor/2.0 (+public job reader; no login)"
MIN_INTERVAL_SECONDS = 0.6          # => at most 100 requests/minute (limit is 120)
MAX_PAGES = 40                      # 40 x 50 = 2,000 jobs per scan
MAX_RESPONSE_BYTES = 5_000_000
CACHE_FRESH_SECONDS = 45 * 60       # do not re-poll more often than this when cache is fresh

WORLDWIDE_RX = re.compile(
    r"(open\s+(to\s+candidates\s+)?worldwide|worldwide|global(ly)?\b|anywhere\s+in\s+the\s+world|all\s+countries)",
    re.IGNORECASE)


class OpenTrainError(RuntimeError):
    def __init__(self, stage: str, message: str, status: int | None = None, retry_after: float | None = None):
        self.stage, self.status, self.retry_after = stage, status, retry_after
        super().__init__(f"[{stage}]" + (f" HTTP {status}" if status else "") + f": {message}")


def http_get_json(url: str, timeout: int = 60) -> dict[str, Any]:
    """Fetch one JSON page with size limit and gzip support; raises OpenTrainError."""
    if not url.startswith("https://app.opentrain.ai/api/public/"):
        raise OpenTrainError("config", "Refusing to call a non-OpenTrain public API URL")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
    except urllib.error.HTTPError as exc:
        retry = None
        try:
            retry = float(exc.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            pass
        raise OpenTrainError("api", str(exc.reason), exc.code, retry) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OpenTrainError("network", str(getattr(exc, "reason", exc))) from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise OpenTrainError("api", "Response too large")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OpenTrainError("parse", f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise OpenTrainError("parse", "Unexpected response shape (no 'jobs' list)")
    return data


def fetch_all_jobs(fetch: Callable[[str], dict[str, Any]] = http_get_json, *,
                   extra_query: str = "", max_pages: int = MAX_PAGES,
                   sleep: Callable[[float], None] = time.sleep, retries: int = 2
                   ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Page through the cursor API. Returns (raw_jobs, info); never raises on partial failure."""
    jobs: list[dict[str, Any]] = []
    info: dict[str, Any] = {"pages": 0, "requests": 0, "complete": False, "error": None}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while info["pages"] < max_pages:
        url = f"{BASE_URL}?limit=50{extra_query}" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        data = None
        for attempt in range(retries + 1):
            info["requests"] += 1
            try:
                data = fetch(url)
                break
            except OpenTrainError as exc:
                info["error"] = str(exc)
                if exc.status == 429:                       # rate limited: obey Retry-After
                    sleep(min(exc.retry_after or 30.0, 60.0))
                elif exc.status and 400 <= exc.status < 500:  # other client errors will not heal
                    return jobs, info
                else:
                    sleep(min(2.0 * (attempt + 1), 10.0))
        if data is None:
            return jobs, info                               # partial result; caller decides
        info["error"] = None
        jobs.extend(j for j in data["jobs"] if isinstance(j, dict))
        info["pages"] += 1
        cursor = data.get("nextCursor")
        if not cursor:
            info["complete"] = True
            break
        if cursor in seen_cursors:                          # defensive loop guard
            info["error"] = "Pagination cursor repeated; stopping"
            break
        seen_cursors.add(cursor)
        sleep(MIN_INTERVAL_SECONDS)
    return jobs, info


def classify_eligibility(countries: Any, text: str) -> str:
    """Kenya-eligibility class. A missing or empty country list is NEVER read as Kenya-eligible."""
    codes = [str(c).strip().upper() for c in countries] if isinstance(countries, list) else []
    if oc.KENYA in codes:
        return oc.KENYA_EXPLICIT
    if codes:
        return oc.RESTRICTED_OTHER
    if WORLDWIDE_RX.search(text or ""):
        return oc.WORLDWIDE          # posting text itself states it is open worldwide
    return oc.UNSPECIFIED


def format_payment(pay: Any) -> tuple[str, float | None, str]:
    """(display text, comparable USD/hour or None, flag). Invalid values become 'Not disclosed'."""
    if not isinstance(pay, dict):
        return "Not disclosed", None, ""
    currency = str(pay.get("currency") or "USD").upper()[:3]
    hourly = oc.parse_amount(pay.get("hourlyRate"))
    low, high = oc.parse_amount(pay.get("hourlyMin")), oc.parse_amount(pay.get("hourlyMax"))
    fixed, label = oc.parse_amount(pay.get("fixedPrice")), oc.parse_amount(pay.get("perLabelRate"))
    usd = currency == "USD"
    if hourly is not None and hourly > 0:
        flag = oc.payment_flag("hour", hourly if usd else None)
        return f"{currency} {hourly:g}/hour", (hourly if usd else None), flag
    if low is not None or high is not None:
        top = high if high is not None else low
        flag = oc.payment_flag("hour", top if usd else None)
        rng = f"{low:g}-{high:g}" if low is not None and high is not None else f"{top:g}"
        return f"{currency} {rng}/hour", (top if usd else None), flag
    if fixed is not None and fixed > 0:
        return f"{currency} {fixed:g} fixed", None, oc.payment_flag("fixed", fixed if usd else None)
    if label is not None and label > 0:
        return f"{currency} {label:g} per label", None, ""
    return "Not disclosed", None, ""


def job_status(raw: dict[str, Any], now: datetime) -> str:
    status = str(raw.get("status") or "").upper()
    valid_through = oc.parse_time(raw.get("validThrough"))
    if status in ("CLOSED", "FILLED", "EXPIRED", "PAUSED", "ARCHIVED", "CANCELLED", "DRAFT"):
        return status.lower()
    if valid_through and valid_through < now:
        return "expired"
    return "open" if status in ("OPEN", "ONGOING", "") else status.lower()


def normalize_job(raw: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """Convert one API job to the unified opportunity schema (None if unusable)."""
    now = now or datetime.now(timezone.utc)
    job_id = raw.get("id")
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not job_id or not title or not url.startswith("https://"):
        return None
    description = str(raw.get("descriptionText") or raw.get("summary") or "")
    skills_list = [str(s) for s in (raw.get("skills") or []) if isinstance(s, (str, int))]
    haystack = " ".join([title, description, " ".join(skills_list), str(raw.get("category") or ""),
                         " ".join(str(d) for d in (raw.get("domains") or []))])
    eligibility = classify_eligibility(raw.get("countries"), description + " " + str(raw.get("summary") or ""))
    payment_text, hourly, pay_flag = format_payment(raw.get("pay"))
    posted = oc.parse_time(raw.get("datePosted"))
    status = job_status(raw, now)
    matched = oc.match_skills(haystack)
    score, priority = oc.score_opportunity(
        eligibility=eligibility, skills=matched, hourly_usd=hourly,
        payment_flagged=bool(pay_flag), posted=posted, available=(status == "open"), now=now)
    apply_mode = str(raw.get("applyMode") or "")
    # applyMode "external" = the application happens on a partner's site; a partner-origin job
    # (EXTERNAL_AGGREGATED) can still be applied for on OpenTrain.
    external = apply_mode != "opentrain"
    partner = raw.get("jobOrigin") == "EXTERNAL_AGGREGATED"
    apply_url = str(raw.get("applyUrl") or url)
    if not apply_url.startswith("https://"):
        apply_url = url
    flags = [f for f in (pay_flag,) if f]
    return {
        "platform_id": PLATFORM_ID, "source": PLATFORM_NAME, "external_id": str(job_id),
        "title": title, "url": url, "apply_url": apply_url,
        "summary": re.sub(r"\s+", " ", str(raw.get("summary") or description))[:600],
        "published": str(raw.get("datePosted") or ""),
        "category": str(raw.get("category") or "uncategorised"),
        "domains": [str(d) for d in (raw.get("domains") or [])],
        "skills": skills_list, "languages": [str(l) for l in (raw.get("languages") or [])],
        "employment_types": [str(t) for t in (raw.get("employmentTypes") or [])],
        "countries": [str(c).upper() for c in (raw.get("countries") or [])],
        "kenya_eligibility": eligibility,
        "payment_display": payment_text, "payment_usd_per_hour": hourly,
        "payment_flag": pay_flag, "flags": flags,
        "status": status, "valid_through": str(raw.get("validThrough") or ""),
        "external_application": external, "partner_job": partner, "apply_mode": apply_mode or "unknown",
        "company": str(raw.get("companyName") or ""),
        "matched_skill_groups": sorted(matched), "relevance_score": score, "priority": priority,
        "last_seen_at": now.isoformat(), "requires_human_verification": True,
        "approval_required": "A human must review and submit any application or contract.",
    }


def normalize_jobs(raw_jobs: list[dict[str, Any]], now: datetime | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Normalize, drop unavailable listings, and deduplicate by stable job ID then URL."""
    now = now or datetime.now(timezone.utc)
    stats = {"retrieved": len(raw_jobs), "invalid": 0, "duplicates": 0, "unavailable": 0}
    by_key: dict[str, dict[str, Any]] = {}
    seen_urls: set[str] = set()
    for raw in raw_jobs:
        item = normalize_job(raw, now)
        if item is None:
            stats["invalid"] += 1
            continue
        url_key = item["url"].split("#", 1)[0].rstrip("/")
        if item["external_id"] in by_key or url_key in seen_urls:
            stats["duplicates"] += 1
            continue
        if item["status"] != "open":
            stats["unavailable"] += 1
            continue
        by_key[item["external_id"]] = item
        seen_urls.add(url_key)
    items = sorted(by_key.values(), key=lambda i: (-i["relevance_score"], i["title"]))
    return items, stats


def summarize(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"open_listings": len(items), "kenya_explicit": 0, "worldwide": 0,
              "restricted_other": 0, "unspecified": 0, "external_application": 0, "partner_jobs": 0}
    key = {oc.KENYA_EXPLICIT: "kenya_explicit", oc.WORLDWIDE: "worldwide",
           oc.RESTRICTED_OTHER: "restricted_other", oc.UNSPECIFIED: "unspecified"}
    for item in items:
        counts[key[item["kenya_eligibility"]]] += 1
        counts["external_application"] += bool(item["external_application"])
        counts["partner_jobs"] += bool(item.get("partner_job"))
    return counts


def load_cache(path: Path = CACHE_PATH) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None
    except (OSError, ValueError):
        return None


def save_cache(items: list[dict[str, Any]], path: Path = CACHE_PATH, saved_at: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"saved_at": (saved_at or datetime.now(timezone.utc)).isoformat(), "items": items}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def scan(fetch: Callable[[str], dict[str, Any]] = http_get_json, *, cache_path: Path = CACHE_PATH,
         now: datetime | None = None, sleep: Callable[[float], None] = time.sleep,
         max_pages: int = MAX_PAGES, use_fresh_cache: bool = True) -> dict[str, Any]:
    """Run one scan. Returns {'items': [...], 'health': {...}}; failures degrade, never crash."""
    now = now or datetime.now(timezone.utc)
    cache = load_cache(cache_path)
    if use_fresh_cache and cache:
        saved = oc.parse_time(cache.get("saved_at"))
        if saved and (now - saved).total_seconds() < CACHE_FRESH_SECONDS:
            items = cache["items"]
            return {"items": items, "health": {
                "status": "ok_cached", "last_successful_scan": cache["saved_at"], "error": None,
                "message": "Used cache younger than the polling interval", **summarize(items)}}
    # Pass 1: server-side country=KE filter (small and complete); pass 2: the unfiltered list for
    # total coverage. Eligibility is ALWAYS re-derived locally from each job's own country list,
    # because the server filter also lets through some jobs that do not list Kenya.
    raw_ke, info = fetch_all_jobs(fetch, extra_query="&country=KE", max_pages=max_pages, sleep=sleep)
    raw_all, info_all = (fetch_all_jobs(fetch, max_pages=max_pages, sleep=sleep) if info["complete"]
                         else ([], {"pages": 0, "requests": 0, "complete": False, "error": info["error"]}))
    raw = raw_ke + raw_all
    info = {"pages": info["pages"] + info_all["pages"], "requests": info["requests"] + info_all["requests"],
            "complete": info["complete"], "error": info["error"] or info_all["error"],
            "unfiltered_complete": info_all["complete"]}
    items, stats = normalize_jobs(raw, now)
    if raw and (info["complete"] or len(raw) >= 50):
        status = "ok" if info["complete"] and info["unfiltered_complete"] else "partial"
        if items:
            save_cache(items, cache_path, now)
        return {"items": items, "health": {
            "status": status, "last_successful_scan": now.isoformat(), "error": info["error"],
            "pages": info["pages"], "requests": info["requests"], **stats, **summarize(items)}}
    if cache:                                               # total failure: fall back to last good data
        items = cache["items"]
        return {"items": items, "health": {
            "status": "degraded_using_cache", "last_successful_scan": cache.get("saved_at"),
            "error": info["error"] or "No jobs returned", **summarize(items)}}
    return {"items": [], "health": {"status": "error", "last_successful_scan": None,
                                    "error": info["error"] or "No jobs returned", **summarize([])}}


if __name__ == "__main__":  # manual connection test
    result = scan(use_fresh_cache=False)
    print(json.dumps(result["health"], indent=2))
