"""Mindrift / Toloka eligibility monitor.

Mindrift's official policy
(https://mindrift.ai/legal/eligibility-and-geographic-restrictions) lists Kenya among the
countries that are NOT currently supported. This module therefore does NOT discover or
recommend opportunities. It only watches the public policy page and flags a change for
MANUAL review. Opportunity discovery stays disabled until a human reviews the change and
decides - nothing here ever enables it automatically, and no VPN, proxy, alternate
identity or other workaround is used or supported.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from microtask_monitor import policy
except ImportError:  # pragma: no cover
    import policy

PLATFORM_ID = "mindrift"
PLATFORM_NAME = "Mindrift"
POLICY_URL = "https://mindrift.ai/legal/eligibility-and-geographic-restrictions"
DASHBOARD_URL = "https://mindrift.toloka.ai/"
WEBSITE_URL = "https://mindrift.ai/"
USER_COUNTRY = "Kenya"
ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "microtask_monitor" / "eligibility_baseline.json"
CACHE_PATH = ROOT / "microtask_monitor" / "cache" / "mindrift_policy.json"
CHECK_INTERVAL_SECONDS = 6 * 3600
MAX_BYTES = 3_000_000
USER_AGENT = "MicrotaskOpportunityMonitor/2.0 (+public policy page reader)"

RESTRICTED = "restricted_for_user_location"
REASON = ("Kenya is explicitly listed as not currently supported in Mindrift's official "
          "eligibility and geographic restrictions policy.")


class PolicyError(RuntimeError):
    pass


def fetch_policy_html(url: str = POLICY_URL, timeout: int = 90) -> str:
    if not url.startswith("https://mindrift.ai/"):
        raise PolicyError("Refusing to fetch a URL outside mindrift.ai")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
    except urllib.error.HTTPError as exc:
        raise PolicyError(f"HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PolicyError(f"Network error: {getattr(exc, 'reason', exc)}") from exc
    if len(body) > MAX_BYTES:
        raise PolicyError("Policy page too large")
    return body.decode("utf-8", "replace")


def _strip(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_policy(page: str) -> dict[str, Any]:
    """Extract 'Last updated' and every location named in the policy's bullet lists."""
    text = _strip(re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S | re.I))
    match = re.search(r"Last updated:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    entries = [_strip(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", page, flags=re.S | re.I)]
    entries = sorted({e for e in entries if 0 < len(e) <= 80})
    return {
        "last_updated": match.group(1) if match else None,
        "unsupported_entries": entries,
        "entry_count": len(entries),
        "kenya_listed": any(e.lower() == "kenya" for e in entries),
        "list_sha256": hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest(),
    }


def looks_valid(parsed: dict[str, Any]) -> bool:
    """Sanity check so a broken/changed page layout is never mistaken for 'Kenya removed'."""
    return bool(parsed.get("last_updated")) and parsed.get("entry_count", 0) >= 20


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(PLATFORM_ID, {})
    except (OSError, ValueError):
        return {}


def evaluate(parsed: dict[str, Any] | None, baseline: dict[str, Any], error: str | None = None,
             now: datetime | None = None) -> dict[str, Any]:
    """Compare the live policy with the reviewed baseline. Discovery is never auto-enabled."""
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "platform_id": PLATFORM_ID, "user_location": USER_COUNTRY,
        "eligibility_status": RESTRICTED, "reason": REASON, "policy_url": POLICY_URL,
        "opportunity_discovery_enabled": False, "recommend_applications": False,
        "last_eligibility_verification": baseline.get("verified_on"),
        "policy_last_updated": baseline.get("policy_last_updated"),
        "change_detected": False, "needs_manual_review": False, "alert": None,
        "checked_at": now.isoformat(), "error": error,
    }
    if parsed is None or not looks_valid(parsed):
        result["error"] = error or "Policy page layout not recognised; kept the last reviewed status"
        result["health"] = "error_using_baseline"
        return result
    result["last_eligibility_verification"] = now.date().isoformat()
    result["policy_last_updated"] = parsed["last_updated"]
    result["health"] = "ok"
    changed = (parsed["list_sha256"] != baseline.get("list_sha256")
               or parsed["last_updated"] != baseline.get("policy_last_updated"))
    if not parsed["kenya_listed"]:
        # Kenya no longer appears. Flag for a human; DO NOT enable discovery automatically.
        result.update(change_detected=True, needs_manual_review=True,
                      alert="KENYA NO LONGER LISTED as unsupported - read the official policy and "
                            "your Mindrift account terms before any action. Discovery stays disabled "
                            "until you approve it manually.",
                      reason="Kenya is no longer listed in the policy, but support is unconfirmed "
                             "and requires manual review.")
    elif changed and baseline:
        result.update(change_detected=True, needs_manual_review=True,
                      alert="The Mindrift eligibility policy changed. Kenya is still listed as "
                            "unsupported; review the change for accuracy.")
    return result


def scan(fetch: Callable[[], str] = fetch_policy_html, *, baseline_path: Path = BASELINE_PATH,
         cache_path: Path = CACHE_PATH, now: datetime | None = None,
         force: bool = False) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    baseline = load_baseline(baseline_path)
    if not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            checked = datetime.fromisoformat(cached["checked_at"])
            if (now - checked).total_seconds() < CHECK_INTERVAL_SECONDS:
                return {"eligibility": cached, "items": []}
        except (OSError, ValueError, KeyError):
            pass
    policy.require(PLATFORM_ID, "policy_monitor")   # only the public policy page may be read
    parsed, error = None, None
    try:
        parsed = parse_policy(fetch())
    except PolicyError as exc:
        error = str(exc)
    result = evaluate(parsed, baseline, error, now)
    if result["health"] == "ok":
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(result), encoding="utf-8")
        except OSError:
            pass
    return {"eligibility": result, "items": []}   # never emits opportunity items


if __name__ == "__main__":
    print(json.dumps(scan(force=True)["eligibility"], indent=2))
