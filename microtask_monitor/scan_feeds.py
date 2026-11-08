"""Hourly public-feed opportunity monitor. No logins or task submissions."""
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEEDS = ROOT / "microtask_monitor" / "feeds.json"
REPORTS = ROOT / "microtask_monitor" / "reports"

def clean(value):
    return re.sub(r"<[^>]+>", " ", value or "").strip()

def main():
    config = json.loads(FEEDS.read_text(encoding="utf-8"))
    results = []
    for feed in config.get("feeds", []):
        url = feed.get("url", "")
        if not url.startswith("https://"):
            results.append({"source": feed.get("name", "Unknown"), "error": "Only HTTPS feed URLs are accepted"})
            continue
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "MicrotaskOpportunityMonitor/1.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(2_000_000)
            root = ET.fromstring(raw)
            entries = root.findall(".//item") + root.findall(".//{*}entry")
            for node in entries:
                title = node.findtext("title") or ""
                link_node = node.find("link")
                link = (link_node.text or "").strip() if link_node is not None else ""
                if link_node is not None and not link:
                    link = link_node.attrib.get("href", "")
                summary = node.findtext("description") or node.findtext("{*}summary") or ""
                results.append({
                    "source": feed.get("name", url),
                    "title": clean(title),
                    "url": link,
                    "summary": clean(summary)[:500]
                })
        except Exception as exc:
            results.append({"source": feed.get("name", url), "error": str(exc)})
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = REPORTS / f"hourly-{stamp}.json"
    output.write_text(json.dumps({"checked_at_utc": stamp, "items": results}, indent=2), encoding="utf-8")
    print(f"Saved {len(results)} feed entries to {output}")

if __name__ == "__main__":
    main()
