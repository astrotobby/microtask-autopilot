"""Client acquisition: lead discovery, extraction, de-duplication and scoring for the freelance business.

Sources (business_sources.json) are read only through what each permits:
    hn_thread   the official Hacker News API ("Freelancer? Seeking Freelancer?": clients post their own contact details)
    rss         published RSS feeds
    engine      listings the existing Microtask Autopilot engine already found through its permitted channels
    inbound     website enquiries and client emails (read-only), your manual imports
Marketplaces without verified automated access (Braintrust, Contra, Freelancer.com, Truelancer) contribute through their
alert emails and your imports only. Fiverr and Upwork are excluded.

Lead text is untrusted data: it is parsed for facts and scored, never executed or followed as instructions.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

try:
    from microtask_monitor import biz_config as bc, db
except ImportError:  # pragma: no cover
    import biz_config as bc, db

UA = "MicrotaskAutopilot-BusinessEngine/1.0 (personal lead monitor; respects published feeds and official APIs)"
MAX_BYTES = 3_000_000
STATUSES = ("new", "qualified", "rejected", "proposal_prepared", "proposal_sent", "replied", "won", "lost", "archived")
QUALIFY_AT = 55.0

TECH = ["python", "javascript", "typescript", "node.js", "node", "react", "next.js", "vue", "html", "css", "flask", "fastapi", "django", "express",
        "sqlite", "postgres", "postgresql", "mysql", "mongodb", "rest", "graphql", "api", "n8n", "make.com", "zapier", "airtable", "openai", "claude",
        "llm", "gpt", "langchain", "cloudflare", "docker", "aws", "stripe", "shopify", "wordpress", "php", "laravel", "ruby", "rails", "java", "c#",
        ".net", "go", "golang", "rust", "swift", "kotlin", "flutter", "react native", "heygen", "runway", "pika", "capcut", "canva", "ffmpeg",
        "whisper", "pandas", "excel", "google sheets", "selenium", "scraping"]
UNSUPPORTED_TECH = {"php", "laravel", "ruby", "rails", "java", "c#", ".net", "go", "golang", "rust", "swift", "kotlin", "flutter", "react native", "wordpress"}
EMPLOYMENT = re.compile(r"\b(full[- ]time|salary|salaried|benefits|401k|equity|w-2|permanent|employee|hiring for (?:a|our) team|join our team|relocat\w+|on-?site|hybrid)\b", re.I)
CONTRACT = re.compile(r"\b(freelance\w*|contract(?:or)?|project[- ]based|fixed[- ]price|one[- ]off|gig|short[- ]term|part[- ]time|per hour|hourly|budget|seeking freelancer)\b", re.I)
SUSPICIOUS = re.compile(r"\b(pay (?:a|the) (?:fee|deposit)|registration fee|send (?:us )?your (?:bank|password|login)|telegram only|whatsapp only|crypto (?:wallet|payment) upfront|"
                        r"guaranteed income|unpaid (?:trial|test)|work for free|exposure only|rating|fake reviews?|bypass|captcha)\b", re.I)


# ------------------------------------------------------------------------------------------- extraction
def _money(text: str) -> tuple[float | None, float | None, str | None, str | None]:
    t = text.replace(",", "")
    unit = r"(\s*(?:/\s*|per\s+|an?\s+)(hr|hour|month|mo|year|yr)\b)?"

    def kind(u: str | None, v: float) -> str:
        u = (u or "").lower()
        return "hourly" if u in ("hr", "hour") else "monthly" if u in ("month", "mo") else "annual" if (u in ("year", "yr") or v > 20000) else "fixed"
    m = re.search(r"(?:\$|usd\s?)(\d{2,6}(?:\.\d+)?)\s*(k)?\s*(?:-|to|–|—)\s*\$?(\d{2,6}(?:\.\d+)?)\s*(k)?" + unit, t, re.I)
    if m:
        lo, hi = float(m.group(1)) * (1000 if m.group(2) else 1), float(m.group(3)) * (1000 if m.group(4) else 1)
        return lo, hi, "USD", kind(m.group(6), hi)
    m = re.search(r"(?:\$|usd\s?)(\d{2,6}(?:\.\d+)?)\s*(k)?" + unit, t, re.I)
    if m:
        v = float(m.group(1)) * (1000 if m.group(2) else 1)
        return v, v, "USD", kind(m.group(4), v)
    m = re.search(r"(\d{2,6})\s*(usd|dollars)\b", t, re.I)
    if m:
        v = float(m.group(1))
        return v, v, "USD", "fixed"
    return None, None, None, None


OTHER_ROLE = re.compile(r"\b(designer|account executive|sales|marketing|recruiter|manager|director|appointment|appt|customer success|support specialist|"
                        r"accountant|legal|counsel|nurse|teacher|tutor|writer for hire|seo specialist|executive assistant|virtual assistant)\b", re.I)


SPECIALIST = re.compile(r"\b(ph\.?d|doctorate|bioscience|biolog\w+|chemist\w*|physic(?:s|ist)|medical|clinical|physician|nurs(?:e|ing)|lawyer|attorney|legal expert|"
                        r"life scien\w+|real estate|patent|tax|accounting|finance expert|cpa|chartered|licen[cs]ed|certified|security clearance|penetration test\w*|pentest\w*|actuar\w+|mathematician|professor)\b", re.I)


def service_line(text: str, cfg: dict[str, Any], title: str = "") -> tuple[str | None, dict[str, int]]:
    """Service line by keyword evidence. A title match counts three times; a line needs a title match or two body matches."""
    def count(s: str, kws: list[str]) -> int:
        low = s.lower()
        return sum(1 for kw in kws if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", low))
    hits = {}
    for k, v in cfg["service_lines"].items():
        t, b = count(title, v["keywords"]), count(text, v["keywords"])
        hits[k] = (3 * t + b) if (t or b >= 2) else 0
    best = max(hits, key=lambda k: (hits[k], -cfg["service_lines"][k]["priority"]))
    return (best if hits[best] else None), hits


def extract(item: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Structured facts from one raw opportunity. Missing facts stay None: nothing is guessed."""
    title, desc = bc.norm(item.get("title"))[:300], bc.norm(item.get("description"))[:8000]
    text = f"{title}\n{desc}"
    low = text.lower()
    lo, hi, cur, btype = _money(text)
    if item.get("budget") is not None:
        lo = hi = float(item["budget"])
        cur, btype = (item.get("currency") or "USD").upper(), item.get("budget_type") or "fixed"
    techs = sorted({t for t in TECH if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low)})
    line, hits = service_line(text, cfg, title)
    dl = re.search(r"\b(?:deadline|due|deliver(?:ed)? by|needed by|within)\s*:?\s*([A-Za-z]{3,9} \d{1,2}(?:,? \d{4})?|\d{4}-\d{2}-\d{2}|\d{1,2}\s*(?:days?|weeks?))", text, re.I)
    work = re.search(r"\b(\d{1,3})\s*(?:-\s*\d{1,3}\s*)?(hours?|hrs?|days?|weeks?)\b(?:\s*(?:of work|per week|/week|a week))?", text, re.I)
    country = re.search(r"\b(us|usa|u\.s\.|uk|eu|europe|canada|australia)\s*(?:-based\s*)?(?:only|residents only|citizens only)\b|\bmust (?:be|reside) in ([A-Z][a-z]+(?: [A-Z][a-z]+)?)"
                        r"|\bremote\s*\((?!worldwide|global|anywhere)([^)]{2,40})\)", text, re.I)
    comp = re.search(r"\b(\d{1,4})\s*(?:proposals?|bids?|applicants?)\b", text, re.I)
    history = re.search(r"\b(payment (?:method )?verified|\d+ (?:hires|jobs posted)|\$\d+[kK]?\+? spent|repeat client)\b", text, re.I)
    terms = re.search(r"\b(milestones?|upfront|deposit|net ?\d{1,2}|on completion|weekly pay|paid (?:weekly|monthly|hourly)|escrow)\b", text, re.I)
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", desc)
    reqs = [s.strip(" -•*") for s in re.split(r"(?:\n|(?<=[.;])\s)", desc)
            if re.search(r"\b(must|need|required?|should|looking for|deliver|build|create|fix|integrate|automate)\b", s, re.I)
            and not re.search(r"\b(deliver(?:ed)? (?:within|by|in)|deadline|due (?:by|on|date)|budget|pay\w*|deposit|invoice|hourly rate)\b", s, re.I)][:12]
    return {"title": title or "(untitled)", "description": desc, "client_name": bc.norm(item.get("client_name"))[:120] or None,
            "contact": bc.norm(item.get("contact"))[:200] or (emails[0] if emails else None), "url": (item.get("url") or "").strip() or None,
            "budget_min": lo, "budget_max": hi, "currency": cur, "budget_type": btype, "technologies": techs, "service_line": line, "line_hits": hits,
            "requirements": [r[:240] for r in reqs if len(r) > 12], "deadline": dl.group(1) if dl else None,
            "workload": work.group(0) if work else None, "payment_terms": terms.group(0) if terms else None,
            "client_history": item.get("client_history") or (history.group(0) if history else None),
            "competition": item.get("competition") or (comp.group(0) if comp else None),
            "country_restrictions": (country.group(0) if country else None), "platform_rules": item.get("platform_rules"),
            "employment": (bool(EMPLOYMENT.search(text)) or bool(item.get("job_board"))) and not CONTRACT.search(text),
            "contract": bool(CONTRACT.search(text)), "other_role": bool(OTHER_ROLE.search(title)),
            "suspicious": [m.group(0) for m in SUSPICIOUS.finditer(text)][:5], "discovered_at": item.get("discovered_at") or db.now_iso()}


def dedupe_key(source_id: str, x: dict[str, Any]) -> str:
    """Same listing = same key: the link without its fragment and tracking parameters (ids in the query are kept: item?id=1 != item?id=2)."""
    raw = (x.get("url") or "").split("#")[0]
    base, _, query = raw.partition("?")
    keep = "&".join(sorted(p for p in query.split("&") if p and not re.match(r"(utm_|ref=|source=|fbclid|gclid)", p, re.I)))
    u = (base.rstrip("/") + ("?" + keep if keep else "")).lower()
    basis = u if u else f"{source_id}|{(x.get('client_name') or '').lower()}|{x['title'].lower()}|{x['description'][:300].lower()}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


# ------------------------------------------------------------------------------------------- scoring
def score(x: dict[str, Any], src: dict[str, Any], cfg: dict[str, Any], history: dict[str, Any] | None = None) -> tuple[float, dict[str, float], list[str]]:
    """0-100 with every part shown, and the reasons a lead can never qualify (blockers)."""
    p, blockers = {}, []
    pricing, lines = cfg["pricing"], cfg["service_lines"]
    line = x["service_line"]
    p["skill_match"] = 0.0 if not line else min(25.0, 8.0 + 4.0 * x["line_hits"].get(line, 0))
    unsupported = sorted(set(x["technologies"]) & UNSUPPORTED_TECH)
    if unsupported:
        p["skill_match"] = max(0.0, p["skill_match"] - 8.0 * len(unsupported))
    p["priority"] = {1: 8.0, 2: 7.0, 3: 5.0, 4: 3.0}.get(lines.get(line, {}).get("priority"), 0.0)
    value = x["budget_max"]
    if x["budget_type"] == "hourly" and value:
        p["value"] = 15.0 if value >= pricing["target_hourly_usd"] else (8.0 if value >= pricing["min_effective_hourly_usd"] else 0.0)
    elif x["budget_type"] == "monthly" and value:
        p["value"] = 15.0 if value >= 1000 else (8.0 if value >= 400 else 0.0)
    elif x["budget_type"] == "annual":
        p["value"] = 4.0
    elif value:
        p["value"] = 15.0 if value >= 2 * pricing["min_project_usd"] else (9.0 if value >= pricing["min_project_usd"] else 0.0)
        if value < pricing["min_project_usd"]:
            blockers.append(f"budget USD {value:g} is below your minimum project price (USD {pricing['min_project_usd']})")
    else:
        p["value"] = 5.0
    p["engagement"] = 10.0 if x["contract"] and not x["employment"] else (2.0 if x["employment"] else 5.0)
    p["clarity"] = min(10.0, 2.0 * len(x["requirements"]) + (2.0 if len(x["description"]) > 300 else 0.0))
    p["client_credibility"] = (4.0 if x["client_history"] else 0.0) + (3.0 if x["client_name"] else 0.0) + (3.0 if x["contact"] or src["kind"] in ("marketplace", "official_api") else 0.0)
    comp = re.search(r"\d+", x["competition"] or "")
    p["win_probability"] = 8.0 if src["kind"] == "inbound" else (2.0 if comp and int(comp.group()) > 30 else 5.0)
    rel = [it for it in cfg["portfolio"] if line in it.get("service_lines", [])]
    p["portfolio_relevance"] = min(6.0, 2.0 * len(rel))
    pol = src["policy"]["discovery"]
    p["policy"] = 5.0 if pol == "allowed" else 0.0
    if history and history.get("won"):
        p["channel_history"] = min(6.0, 2.0 * history["won"])
    if x["suspicious"]:
        blockers.append("warning signs: " + ", ".join(x["suspicious"]))
        p["risk"] = -25.0
    if x["country_restrictions"] and not re.search(r"kenya|africa|anywhere|worldwide", x["country_restrictions"], re.I):
        blockers.append(f"country restriction: {x['country_restrictions']}")
    if not line:
        blockers.append("no match with your service lines")
    spec = SPECIALIST.search(x["title"] + " " + x["description"][:1500])
    if spec:
        blockers.append(f"needs specialist credentials or domain expertise you have not established ('{spec.group(0)}')")
    if x.get("other_role"):
        blockers.append("a non-technical role (title), not one of your services")
    if re.search(r"\b(expert|specialist)\b", x["title"], re.I) and not service_line("", cfg, x["title"])[0]:
        blockers.append("a domain-expert role (title): it needs expertise in that field, not your services")
    if x["employment"]:
        blockers.append("an employment role, not a client project: apply yourself if you want a job; the business engine targets projects and contracts")
    low = (x["title"] + " " + x["description"]).lower()
    for never in cfg.get("never_take", []):
        key = never.split(" or ")[0].split(",")[0].lower()
        if key and key in low:
            blockers.append(f"outside what you take on: {never}")
    for dom in bc.excluded_domains():
        if dom in (x.get("url") or ""):
            blockers.append(f"{dom} is excluded")
    return round(max(0.0, min(100.0, sum(p.values()))), 1), p, blockers


# ------------------------------------------------------------------------------------------- persistence
def _client(conn, name: str | None, email: str | None, source_id: str, country: str | None = None) -> int | None:
    if not (name or email):
        return None
    name = (name or email or "").strip()[:120]
    r = conn.execute("SELECT id FROM biz_clients WHERE lower(name)=lower(?) AND lower(COALESCE(email,''))=lower(?)", (name, email or "")).fetchone()
    if r:
        return r["id"]
    now = db.now_iso()
    return conn.execute("INSERT INTO biz_clients(name,email,channel,country,source_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (name, email, source_id, country, source_id, now, now)).lastrowid


def channel_history(conn, source_id: str) -> dict[str, int]:
    r = conn.execute("SELECT SUM(status='won') won, SUM(status='lost') lost, COUNT(*) n FROM biz_leads WHERE source_id=?", (source_id,)).fetchone()
    return {"won": r["won"] or 0, "lost": r["lost"] or 0, "total": r["n"] or 0}


def ingest(conn, items: Iterable[dict[str, Any]], source_id: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store each new lead once, scored, as qualified or rejected with the reason. Re-ingesting the same lead is a no-op."""
    cfg = cfg or bc.load()
    src = bc.source(source_id)
    stats = {"seen": 0, "new": 0, "duplicates": 0, "qualified": 0, "rejected": 0}
    hist = channel_history(conn, source_id)
    for item in items:
        stats["seen"] += 1
        x = extract(item, cfg)
        key = dedupe_key(source_id, x)
        if conn.execute("SELECT 1 FROM biz_leads WHERE dedupe_key=?", (key,)).fetchone():
            stats["duplicates"] += 1
            continue
        sc, parts, blockers = score(x, src, cfg, hist)
        status = "rejected" if blockers or sc < QUALIFY_AT else "qualified"
        reason = "; ".join(blockers) if blockers else (f"score {sc} below {QUALIFY_AT}" if sc < QUALIFY_AT else f"score {sc}")
        now = db.now_iso()
        with db.tx(conn):
            cid = _client(conn, x["client_name"], x["contact"] if "@" in (x["contact"] or "") else None, source_id) if status == "qualified" else None
            lid = conn.execute("""INSERT INTO biz_leads(dedupe_key,source_id,client_id,client_name,contact,title,description,requirements_json,technologies_json,
                budget_min,budget_max,currency,budget_type,deadline,workload,payment_terms,client_history,competition,country_restrictions,platform_rules,url,
                discovered_at,service_line,score,score_parts_json,status,status_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, source_id, cid, x["client_name"], x["contact"], x["title"], x["description"], json.dumps(x["requirements"]), json.dumps(x["technologies"]),
                 x["budget_min"], x["budget_max"], x["currency"], x["budget_type"], x["deadline"], x["workload"], x["payment_terms"], x["client_history"],
                 x["competition"], x["country_restrictions"], x["platform_rules"] or src["policy"]["note"], x["url"], x["discovered_at"], x["service_line"],
                 sc, json.dumps(parts), status, reason[:600], now, now)).lastrowid
            bc.audit(conn, "lead.ingest", "lead", lid, {"source": source_id, "status": status, "score": sc}, permission=f"{source_id}: discovery {src['policy']['discovery']}")
        stats["new"] += 1
        stats[status] += 1
    with db.tx(conn):
        conn.execute("""INSERT INTO biz_sources(id,last_run,last_status,leads_found,updated_at) VALUES(?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET last_run=excluded.last_run, last_status=excluded.last_status, last_error=NULL,
                        leads_found=biz_sources.leads_found+excluded.leads_found, updated_at=excluded.updated_at""",
                     (source_id, db.now_iso(), "ok", stats["new"], db.now_iso()))
    return stats


def get(conn, lead_id: int) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM biz_leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        raise bc.BizError("Lead not found")
    d = dict(r)
    for k in ("requirements_json", "technologies_json", "score_parts_json"):
        d[k[:-5]] = json.loads(d.pop(k) or "null")
    return d


def set_status(conn, lead_id: int, status: str, reason: str = "") -> None:
    if status not in STATUSES:
        raise bc.BizError(f"Unknown lead status '{status}'")
    with db.tx(conn):
        conn.execute("UPDATE biz_leads SET status=?, status_reason=?, updated_at=? WHERE id=?", (status, reason[:600], db.now_iso(), lead_id))
        bc.audit(conn, "lead.status", "lead", lead_id, {"status": status, "reason": reason}, actor="user" if reason.startswith("you:") else "engine")


def ranked(conn, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql, args = "SELECT id FROM biz_leads", []
    if status:
        sql, args = sql + " WHERE status=?", [status]
    return [get(conn, r["id"]) for r in conn.execute(sql + " ORDER BY score DESC, discovered_at DESC LIMIT ?", (*args, limit))]


# ------------------------------------------------------------------------------------------- source readers
def http_get(url: str, timeout: int = 30) -> bytes:
    host = (urlparse(url).hostname or "").lower()
    if not url.startswith("https://") or any(d in host for d in bc.excluded_domains()):
        raise bc.BizError(f"Refused to fetch {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, application/rss+xml, text/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:          # noqa: S310 - https only, fixed published sources
        return r.read(MAX_BYTES)


def read_rss(src: dict[str, Any], http: Callable[[str], bytes] = http_get) -> list[dict[str, Any]]:
    root = ET.fromstring(http(src["url"]))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))
        company = title.split(":")[0].strip() if ":" in title else None
        out.append({"title": title, "description": desc, "url": (it.findtext("link") or "").strip(), "client_name": company,
                    "discovered_at": db.now_iso(), "job_board": True})
    return out


HN_CONTRACT = re.compile(r"\b(contract(?:or)?|freelance\w*|part[- ]time|project[- ]based|consultant)\b", re.I)


def read_hn(src: dict[str, Any], http: Callable[[str], bytes] = http_get, max_comments: int = 200, max_age_days: int = 60) -> list[dict[str, Any]]:
    """Client posts from the current monthly threads of the official 'whoishiring' account:
    'Freelancer? Seeking freelancer?' (only 'SEEKING FREELANCER' posts) and 'Who is hiring?' (only posts whose header says contract,
    freelance or part-time). Threads older than max_age_days are ignored; freelancer self-promotion is never read as a lead."""
    user = json.loads(http(src["url"]))
    now = datetime.now(timezone.utc).timestamp()
    threads: dict[str, dict[str, Any]] = {}
    for sid in (user.get("submitted") or [])[:60]:
        item = json.loads(http(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")) or {}
        title = (item.get("title") or "").lower()
        kind = "freelancer" if "seeking freelancer" in title else ("hiring" if "who is hiring" in title else None)
        if kind and kind not in threads:
            threads[kind] = item
        if len(threads) == 2 or (item.get("time") and now - item["time"] > max_age_days * 86400 and threads):
            break
    out = []
    for kind, thread in threads.items():
        if thread.get("time") and now - thread["time"] > max_age_days * 86400:
            continue
        for cid in (thread.get("kids") or [])[:max_comments]:
            c = json.loads(http(f"https://hacker-news.firebaseio.com/v0/item/{cid}.json")) or {}
            text = re.sub(r"<[^>]+>", " ", html.unescape(re.sub(r"<p>", "\n", c.get("text") or "")))
            first = text.strip().split("\n")[0]
            if c.get("deleted") or c.get("dead"):
                continue
            if kind == "freelancer" and not re.match(r"\s*seeking\s+freelancer", text, re.I):
                continue
            if kind == "hiring" and not HN_CONTRACT.search(first):
                continue
            if re.match(r"\s*seeking\s+(?:work|freelance(?!r))", text, re.I):          # a freelancer advertising themselves, not a client
                continue
            company = first.split("|")[0].strip() if "|" in first else c.get("by")
            out.append({"title": first[:160], "description": text, "url": f"https://news.ycombinator.com/item?id={cid}", "client_name": company[:120] if company else None,
                        "discovered_at": datetime.fromtimestamp(c.get("time", 0), timezone.utc).isoformat() if c.get("time") else None})
    return out


def read_engine(conn, cfg: dict[str, Any], days: int = 30, limit: int = 400) -> list[dict[str, Any]]:
    """Reuse what the microtask engine already discovered (feeds, OpenTrain, Whop): skilled listings only."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    for r in conn.execute("""SELECT url,title,summary,source,payment_amount,payment_currency FROM opportunities
                             WHERE status='open' AND duplicate_of IS NULL AND COALESCE(last_seen_utc, first_seen_utc) >= ?
                             ORDER BY COALESCE(last_seen_utc, first_seen_utc) DESC LIMIT ?""", (since, limit)):
        line, _ = service_line(r["summary"] or "", cfg, r["title"])
        if not line:
            continue
        item = {"title": r["title"], "description": r["summary"] or "", "url": r["url"], "client_name": (r["title"].split(":")[0] if ":" in r["title"] else None),
                "platform_rules": f"found by the engine via {r['source']}", "job_board": True}
        if r["payment_amount"] and (r["payment_currency"] or "").upper() == "USD":
            item.update(budget=r["payment_amount"], currency="USD")
        out.append(item)
    return out


def run_source(conn, source_id: str, http: Callable[[str], bytes] = http_get, force: bool = False, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """One permitted read of one source, at most once per its minimum interval. Errors are recorded, never retried in a loop."""
    src = bc.source(source_id)
    if src["policy"]["discovery"] != "allowed" or not src.get("fetch"):
        return {"source": source_id, "status": "not_fetched", "reason": "No permitted automated reading: leads arrive by email or your import"}
    row = conn.execute("SELECT last_run FROM biz_sources WHERE id=?", (source_id,)).fetchone()
    if row and row["last_run"] and not force:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["last_run"])).total_seconds() / 3600
        if age < src.get("min_interval_hours", 6):
            return {"source": source_id, "status": "skipped", "reason": f"read {age:.1f} h ago (interval {src.get('min_interval_hours', 6)} h)"}
    cfg = cfg or bc.load()
    try:
        items = {"rss": lambda: read_rss(src, http), "hn_thread": lambda: read_hn(src, http), "engine": lambda: read_engine(conn, cfg)}[src["fetch"]]()
    except Exception as exc:                                               # noqa: BLE001 - recorded for System Health
        with db.tx(conn):
            conn.execute("""INSERT INTO biz_sources(id,last_run,last_status,last_error,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                            last_run=excluded.last_run,last_status=excluded.last_status,last_error=excluded.last_error,updated_at=excluded.updated_at""",
                         (source_id, db.now_iso(), "error", f"{type(exc).__name__}: {str(exc)[:300]}", db.now_iso()))
        return {"source": source_id, "status": "error", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"source": source_id, "status": "ok", **ingest(conn, items, source_id, cfg)}


def discover(conn, http: Callable[[str], bytes] = http_get, force: bool = False) -> dict[str, Any]:
    cfg = bc.load()
    return {s["id"]: run_source(conn, s["id"], http, force, cfg) for s in bc.sources() if s.get("fetch")}


# ------------------------------------------------------------------------------------------- inbound
def import_enquiry(conn, form: dict[str, Any]) -> dict[str, Any]:
    """A client enquiry from your website form (or one you paste). Requires a name, a reply address and a description."""
    name, email, msg = bc.norm(form.get("name"))[:120], bc.norm(form.get("email"))[:200], str(form.get("message") or "").strip()[:8000]
    if not (name and re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email) and len(msg) >= 20):
        raise bc.BizError("An enquiry needs a name, a valid email address and a description of at least 20 characters")
    budget = form.get("budget")
    item = {"title": bc.norm(form.get("subject")) or f"Enquiry from {name}", "description": msg, "client_name": bc.norm(form.get("company")) or name,
            "contact": email, "url": None}
    if budget not in (None, ""):
        try:
            item.update(budget=float(budget), currency="USD")
        except (TypeError, ValueError):
            pass
    return ingest(conn, [item], "website_enquiry")


def import_email(conn, raws: Iterable[bytes]) -> dict[str, Any]:
    """Client enquiries and marketplace alert emails (.eml). Marketplace alerts are attributed to their source by sender domain."""
    import email as em
    from email import policy as ep
    domain_src = {s["domain"]: s["id"] for s in bc.sources() if s.get("domain")}
    groups: dict[str, list[dict[str, Any]]] = {}
    for raw in raws:
        if len(raw) > MAX_BYTES:
            continue
        msg = em.message_from_bytes(raw, policy=ep.default)
        sender = str(msg.get("From") or "")
        host = (re.search(r"@([A-Za-z0-9.-]+)", sender) or [None, ""])[1].lower()
        if any(d in host for d in bc.excluded_domains()):
            continue
        sid = next((v for d, v in domain_src.items() if host == d or host.endswith("." + d)), "email_enquiry")
        part = msg.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part is not None else ""
        if part is not None and part.get_content_type() == "text/html":
            body = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", body)))
        addr = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", sender)
        name = re.sub(r"\s*<.*>", "", sender).strip().strip('"') or None
        links = re.findall(r"https://[^\s<>\"')]+", body)
        groups.setdefault(sid, []).append({"title": bc.norm(msg.get("Subject"))[:200], "description": body, "client_name": name if sid == "email_enquiry" else None,
                                           "contact": addr.group(0) if (addr and sid == "email_enquiry") else None,
                                           "url": next((u for u in links if not re.search(r"unsubscribe|settings|preferences", u)), None) if sid != "email_enquiry" else None})
    return {sid: ingest(conn, items, sid) for sid, items in groups.items()}
