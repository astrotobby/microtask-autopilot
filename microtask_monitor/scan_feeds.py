"""Hourly public-feed monitor with transparent relevance and eligibility triage."""
import csv
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
FEEDS = ROOT / "microtask_monitor" / "feeds.json"
REPORTS = ROOT / "microtask_monitor" / "reports"

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

def main():
    config = json.loads(FEEDS.read_text(encoding="utf-8"))
    results, errors = [], []
    for feed in config.get("feeds", []):
        try:
            results.extend(parse_feed(feed))
        except Exception as exc:
            errors.append({"source": feed.get("name", "Unknown"), "error": str(exc)})

    # Deduplicate by canonical URL while preserving source attribution.
    unique = {}
    for item in results:
        key = item["url"].split("#", 1)[0].rstrip("/")
        if key not in unique or item["relevance_score"] > unique[key]["relevance_score"]:
            unique[key] = item
    items = sorted(unique.values(), key=lambda x: (x["relevance_score"], x["title"]), reverse=True)

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "checked_at_utc": stamp,
        "feed_count": len(config.get("feeds", [])),
        "unique_listing_count": len(items),
        "high_review_count": sum(x["priority"] == "high_review" for x in items),
        "medium_review_count": sum(x["priority"] == "medium_review" for x in items),
        "errors": errors,
        "important_note": "Scores are keyword triage, not proof of legitimacy, pay, availability, or Kenya eligibility. Verify every listing at its source.",
        "items": items
    }
    json_path = REPORTS / f"hourly-{stamp}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    csv_path = REPORTS / f"hourly-{stamp}.csv"
    fields = ["priority", "relevance_score", "category", "kenya_eligibility",
              "title", "source", "published", "url", "summary",
              "matched_microtask_terms", "matched_ai_terms", "restriction_phrases_found"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            row = dict(item)
            for field in ("matched_microtask_terms", "matched_ai_terms", "restriction_phrases_found"):
                row[field] = "; ".join(row.get(field, []))
            writer.writerow(row)

    print(f"Scanned {len(config.get('feeds', []))} feeds")
    print(f"Saved {len(items)} unique listings ({report['high_review_count']} high-review, {report['medium_review_count']} medium-review)")
    print(f"Feed errors: {len(errors)}")
    print(f"JSON report: {json_path}")
    print(f"CSV report: {csv_path}")

if __name__ == "__main__":
    main()
