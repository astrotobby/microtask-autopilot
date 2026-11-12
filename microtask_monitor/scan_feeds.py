"""Hourly public-feed monitor with transparent relevance and eligibility triage."""
import csv
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:   # allow `python microtask_monitor/scan_feeds.py`
    sys.path.insert(0, str(ROOT))
FEEDS = ROOT / "microtask_monitor" / "feeds.json"
PLATFORMS = ROOT / "microtask_monitor" / "platforms.json"
DISCOVERY_SOURCES = ROOT / "microtask_monitor" / "discovery_sources.json"
REPORTS = ROOT / "microtask_monitor" / "reports"
INTEGRATIONS = ROOT / "microtask_monitor" / "integrations.json"
DAILY_STATE = ROOT / "microtask_monitor" / "cache" / "daily_state.json"

MICROTASK_TERMS = [
    "microtask", "data annotation", "data annotator", "data labeling",
    "data labelling", "labeling", "labelling", "ai training", "ai trainer",
    "human evaluation", "model evaluation", "search evaluation",
    "internet assessor", "data collection", "transcription", "voice recording",
    "image annotation", "video annotation", "survey", "crowdworker",
    "crowdsourcing", "rater", "content evaluator", "prompt evaluator",
    "tasker", "user research"
]
AI_TERMS = [
    "artificial intelligence", "machine learning", "generative ai", "llm",
    "large language model", "chatbot", "prompt", "ai model", "model training",
    "ai evaluator", "ai evaluation"
]
REMOTE_TERMS = ["remote", "work from home", "anywhere", "distributed", "telecommute"]
KENYA_TERMS = ["kenya", "nairobi", "mombasa", "east africa", "africa"]
RESTRICT_TERMS = [
    "us only", "usa only", "united states only", "must be based in the us",
    "must reside in the us", "uk only", "united kingdom only", "canada only",
    "must be based in the uk", "eu only", "europe only", "must reside in"
]

def local_text(node, names):
    for child in list(node):
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names:
            return "".join(child.itertext()).strip()
    return ""

def clean(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def classify(item):
    title = item.get("title", "")
    summary = item.get("summary", "")
    text = f"{title} {summary}".lower()
    micro_hits = [term for term in MICROTASK_TERMS if term in text]
    ai_hits = [term for term in AI_TERMS if term in text]
    remote_hits = [term for term in REMOTE_TERMS if term in text]
    kenya_hits = [term for term in KENYA_TERMS if term in text]
    restrictions = [term for term in RESTRICT_TERMS if term in text]

    score = min(50, len(micro_hits) * 18) + min(24, len(ai_hits) * 8)
    score += 10 if remote_hits else 0
    score += 12 if kenya_hits else 0
    if restrictions:
        score -= 30

    if micro_hits:
        category = "microtask_or_data_work"
    elif ai_hits:
        category = "ai_related_role"
    else:
        category = "general_remote_job"

    if restrictions:
        eligibility = "likely_restricted_check_listing"
    elif kenya_hits:
        eligibility = "kenya_mentioned_verify_requirements"
    elif remote_hits:
        eligibility = "remote_scope_unclear_kenya_not_confirmed"
    else:
        eligibility = "location_not_established"

    if score >= 35 and not restrictions:
        priority = "high_review"
    elif score >= 15 and not restrictions:
        priority = "medium_review"
    else:
        priority = "low_or_unrelated"

    item.update({
        "category": category,
        "relevance_score": score,
        "priority": priority,
        "kenya_eligibility": eligibility,
        "matched_microtask_terms": micro_hits,
        "matched_ai_terms": ai_hits,
        "matched_remote_terms": remote_hits,
        "matched_kenya_terms": kenya_hits,
        "restriction_phrases_found": restrictions,
        "requires_human_verification": True
    })
    return item

def parse_feed(feed):
    url = feed.get("url", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Only valid HTTPS feed URLs are accepted")
    request = urllib.request.Request(
        url, headers={"User-Agent": "MicrotaskOpportunityMonitor/2.0 (+public RSS reader)"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(2_000_000)
    root = ET.fromstring(raw)
    nodes = [n for n in root.iter() if n.tag.rsplit("}", 1)[-1].lower() in ("item", "entry")]
    parsed_items = []
    for node in nodes:
        title = clean(local_text(node, {"title"}))
        link = ""
        for child in list(node):
            if child.tag.rsplit("}", 1)[-1].lower() == "link":
                link = child.attrib.get("href", "") or (child.text or "").strip()
                if link:
                    break
        summary = clean(local_text(node, {"description", "summary", "content"}))[:1200]
        published = local_text(node, {"pubdate", "published", "updated", "date"})
        if not title or not link or urlparse(link).scheme != "https":
            continue
        parsed_items.append(classify({
            "source": feed.get("name", url),
            "source_category": feed.get("category", "unspecified"),
            "title": title,
            "url": link,
            "summary": summary,
            "published": published
        }))
    return parsed_items


def run_integrations(platforms):
    """Run every platform adapter. One adapter failing never stops the others."""
    from microtask_monitor import opentrain, mindrift, manual_review
    modes = json.loads(INTEGRATIONS.read_text(encoding="utf-8")).get("platforms", {})
    health, items, eligibility = {}, [], {}
    for platform in platforms:
        pid = platform["id"]
        health[pid] = {"platform_id": pid, "name": platform.get("name", pid),
                       "platform_type": platform.get("platform_type", "unclassified"),
                       "mode": modes.get(pid, {}), "status": modes.get(pid, {}).get("health", "manual_only"),
                       "last_successful_scan": None, "error": None}
    adapters = {
        "opentrain_ai": opentrain.scan,
        "gainworkers": lambda: manual_review.scan("gainworkers"),
        "reworkio": lambda: manual_review.scan("reworkio"),
        "mindrift": mindrift.scan,
    }
    for pid, run in adapters.items():
        if pid not in health:
            continue
        try:
            result = run()
        except Exception as exc:                       # adapters should degrade, but never crash the scan
            health[pid].update(status="error", error=f"{type(exc).__name__}: {exc}")
            continue
        items.extend(result.get("items", []))
        if "health" in result:
            health[pid].update(result["health"])
        if "eligibility" in result:
            eligibility[pid] = result["eligibility"]
            ok = result["eligibility"].get("health") == "ok"
            health[pid].update(status=result["eligibility"].get("health", "policy_monitor"),
                               last_successful_scan=result["eligibility"].get("checked_at") if ok else None,
                               error=result["eligibility"].get("error"))
    return health, items, eligibility


def active_kenya_eligible_sources(platforms, eligibility):
    """Return only platforms whose registry explicitly confirms Kenyan eligibility.

    Reachability, a remote label, or absence from the restricted list is not proof.
    Listings can still have narrower role-specific country rules.
    """
    restricted = {pid for pid, e in eligibility.items() if e.get("eligibility_status") == "restricted_for_user_location"}
    confirmed = {"KENYA_CONFIRMED", "WORLDWIDE_CONFIRMED"}
    return [p["id"] for p in platforms
            if p["id"] not in restricted and p.get("kenya_eligibility_class") in confirmed]


def safe_link(url):
    """Return an escaped HTTPS URL, or an empty string for unsafe destinations."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return html.escape(url, quote=True)


def render_html(report):
    """Render a self-contained human-readable dashboard from a report dictionary."""
    platform_names = {p.get("id"): p.get("name", p.get("id", "Platform"))
                      for p in report.get("platforms", [])}
    source_rows = []
    for source in report.get("discovery_sources", {}).get("sources", []):
        url = safe_link(source.get("url"))
        name = html.escape(source.get("name", "Discovery source"))
        platform = html.escape(platform_names.get(source.get("platform_id"), source.get("platform_id", "Other")))
        purpose = html.escape(source.get("use", source.get("reason", "")))
        link = f'<a href="{url}" target="_blank" rel="noopener noreferrer">Open official/source page</a>' if url else "Link unavailable"
        source_rows.append(
            f"<tr><td>{platform}</td><td><strong>{name}</strong><br>{purpose}</td><td>{link}</td></tr>"
        )

    role_rows = []
    for role in report.get("discovery_sources", {}).get("current_reviewed_roles", []):
        title = html.escape(role.get("title", "Untitled role"))
        rate = html.escape(str(role.get("advertised_rate") or "Not stated"))
        fit = html.escape(role.get("fit_assessment", "Review manually"))
        source_url = safe_link(role.get("source_url"))
        apply_url = safe_link(role.get("apply_via"))
        source_link = f'<a href="{source_url}" target="_blank" rel="noopener noreferrer">Listing</a>' if source_url else "—"
        apply_link = f'<a href="{apply_url}" target="_blank" rel="noopener noreferrer">Apply/review</a>' if apply_url else "—"
        role_rows.append(f"<tr><td>{title}</td><td>{rate}</td><td>{fit}</td><td>{source_link} · {apply_link}</td></tr>")

    listing_rows = []
    for item in report.get("items", []):
        title = html.escape(item.get("title", "Untitled listing"))
        url = safe_link(item.get("url"))
        link = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>' if url else title
        listing_rows.append(
            "<tr>"
            f"<td>{html.escape(item.get('priority', ''))}</td>"
            f"<td>{html.escape(str(item.get('relevance_score', '')))}</td>"
            f"<td>{html.escape(item.get('kenya_eligibility', ''))}</td>"
            f"<td>{link}<br><small>{html.escape(item.get('source', ''))}"
            f" · {html.escape(str(item.get('payment_display', '')))}</small></td>"
            f"<td>{html.escape(item.get('summary', ''))}</td>"
            "</tr>"
        )

    health_rows = []
    for entry in report.get("integrations", {}).values():
        health_rows.append(
            f"<tr><td>{html.escape(str(entry.get('name', '')))}</td>"
            f"<td>{html.escape(str(entry.get('platform_type', '')))}</td>"
            f"<td>{html.escape(str(entry.get('status', '')))}</td>"
            f"<td>{html.escape(str(entry.get('last_successful_scan') or 'never'))}</td>"
            f"<td>{html.escape(str(entry.get('error') or ''))}</td></tr>")

    checked = html.escape(report.get("checked_at_utc", "Unknown"))
    note = html.escape(report.get("important_note", "Verify every listing manually."))
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Microtask Autopilot Opportunity Report</title>
<style>
:root{color-scheme:light}body{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;background:#f4f6f8;color:#17202a}
main{max-width:1180px;margin:auto;padding:24px}h1{margin-bottom:4px}h2{margin-top:30px}
.muted,small{color:#596579}.stats{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}
.stat{background:white;border:1px solid #dce2e8;border-radius:10px;padding:14px 18px;min-width:140px}
.stat strong{display:block;font-size:24px}section{background:white;border:1px solid #dce2e8;border-radius:10px;padding:16px;margin-top:18px;overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e8edf1;vertical-align:top}
th{background:#f7f9fb}a{color:#0759a5} .notice{background:#fff8e6;border-left:4px solid #d59b22;padding:12px}
</style></head><body><main>
<h1>Microtask Autopilot</h1><div class="muted">Report generated (UTC): """ + checked + """</div>
<div class="stats">
<div class="stat"><strong>""" + str(len(report.get("platforms", []))) + """</strong>Target platforms</div>
<div class="stat"><strong>""" + str(report.get("feed_count", 0)) + """</strong>Public feeds scanned</div>
<div class="stat"><strong>""" + str(report.get("unique_listing_count", 0)) + """</strong>Unique listings</div>
<div class="stat"><strong>""" + str(report.get("high_review_count", 0)) + """</strong>High-priority reviews</div>
<div class="stat"><strong>""" + str(report.get("medium_review_count", 0)) + """</strong>Medium-priority reviews</div>
</div>
<p class="notice">""" + note + """ This report does not log in to platform accounts, accept tasks, or submit work.</p>
<section><h2>Twelve-platform discovery directory</h2><p class="muted">These are navigation links, not proof of live tasks or Kenya eligibility.</p>
<table><thead><tr><th>Platform</th><th>Source and notes</th><th>Link</th></tr></thead><tbody>""" + "".join(source_rows) + """</tbody></table></section>
<section><h2>Integration health</h2><p class="muted">Eligibility and integration health are separate: a healthy integration does not mean you are eligible.</p>
<table><thead><tr><th>Platform</th><th>Type</th><th>Status</th><th>Last successful scan</th><th>Error</th></tr></thead><tbody>""" + ("".join(health_rows) or "<tr><td colspan='5'>No integration data in this report.</td></tr>") + """</tbody></table></section>
<section><h2>Reviewed role snapshots</h2><p class="muted">Third-party listings may be stale. Verify availability, pay and eligibility on the official platform.</p>
<table><thead><tr><th>Role</th><th>Advertised rate</th><th>Fit assessment</th><th>Links</th></tr></thead><tbody>""" + ("".join(role_rows) or "<tr><td colspan='4'>No reviewed role snapshots.</td></tr>") + """</tbody></table></section>
<section><h2>Public-feed opportunities</h2><p class="muted">Keyword scoring is a triage aid, not a guarantee.</p>
<table><thead><tr><th>Priority</th><th>Score</th><th>Kenya status</th><th>Listing / payment</th><th>Summary</th></tr></thead><tbody>""" + ("".join(listing_rows) or "<tr><td colspan='5'>No listings found in the configured feeds.</td></tr>") + """</tbody></table></section>
</main></body></html>"""


def main():
    config = json.loads(FEEDS.read_text(encoding="utf-8"))
    platform_config = json.loads(PLATFORMS.read_text(encoding="utf-8"))
    platforms = platform_config.get("platforms", [])
    discovery_config = json.loads(DISCOVERY_SOURCES.read_text(encoding="utf-8"))
    results, errors = [], []
    for feed in config.get("feeds", []):
        try:
            results.extend(parse_feed(feed))
        except Exception as exc:
            errors.append({"source": feed.get("name", "Unknown"), "error": str(exc)})

    integration_health, adapter_items, eligibility = run_integrations(platforms)
    results.extend(adapter_items)

    # Deduplicate by canonical URL while preserving source attribution.
    unique = {}
    for item in results:
        key = item["url"].split("#", 1)[0].rstrip("/")
        if key not in unique or item["relevance_score"] > unique[key]["relevance_score"]:
            unique[key] = item
    items = sorted(unique.values(), key=lambda x: (x["relevance_score"], x["title"]), reverse=True)

    # Daily earning-opportunity analysis runs here too, so it works with the laptop switched off.
    daily_shortlist, daily_error = None, None
    try:
        from microtask_monitor import daily
        daily_shortlist = daily.cloud_analysis(items, state_path=DAILY_STATE,
                                               trigger=os.environ.get("ANALYSIS_TRIGGER", "hourly"))
    except Exception as exc:                      # a failed analysis must never break the scan or the report
        daily_error = f"{type(exc).__name__}: {exc}"

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "checked_at_utc": stamp,
        "feed_count": len(config.get("feeds", [])),
        "platform_registry_count": len(platforms),
        "platforms": platforms,
        "discovery_sources": discovery_config,
        "unique_listing_count": len(items),
        "high_review_count": sum(x["priority"] == "high_review" for x in items),
        "medium_review_count": sum(x["priority"] == "medium_review" for x in items),
        "errors": errors,
        "platform_counts_by_type": {t: sum(p.get("platform_type") == t for p in platforms)
                                    for t in ("microtask", "ai_training", "professional_marketplace")},
        "active_kenya_eligible_sources": active_kenya_eligible_sources(platforms, eligibility),
        "integrations": integration_health,
        "eligibility_monitors": eligibility,
        "daily_shortlist": daily_shortlist,
        "daily_shortlist_error": daily_error,
        "important_note": "Scores are keyword triage, not proof of legitimacy, pay, availability, or Kenya eligibility. Verify every listing at its source.",
        "items": items
    }
    json_path = REPORTS / f"hourly-{stamp}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = REPORTS / f"hourly-{stamp}.csv"
    fields = ["priority", "relevance_score", "category", "kenya_eligibility", "platform_id",
              "payment_display", "status", "title", "source", "published", "url", "summary",
              "matched_microtask_terms", "matched_ai_terms", "restriction_phrases_found"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            row = dict(item)
            for field in ("matched_microtask_terms", "matched_ai_terms", "restriction_phrases_found"):
                row[field] = "; ".join(row.get(field, []))
            writer.writerow(row)

    print(f"Scanned {len(config.get('feeds', []))} feeds; loaded {len(platforms)} target platform records")
    print(f"Saved {len(items)} unique listings ({report['high_review_count']} high-review, {report['medium_review_count']} medium-review)")
    print(f"Feed errors: {len(errors)}")
    print(f"JSON report: {json_path}")
    html_path = REPORTS / f"hourly-{stamp}.html"
    html_path.write_text(render_html(report), encoding="utf-8")

    print(f"CSV report: {csv_path}")
    print(f"HTML dashboard: {html_path}")

if __name__ == "__main__":
    main()
