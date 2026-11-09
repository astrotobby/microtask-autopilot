"""Daily earning-opportunity pipeline: ranks suitable opportunities and prepares a shortlist.

Runs (a) in the cloud inside the hourly GitHub Actions scan, so it works with your laptop off, and
(b) locally against the SQLite database after each sync that brought changes, and once a day from
08:00 East Africa Time (UTC+3, no daylight saving). It never contacts a platform: it only ranks data
that the permitted discovery adapters already collected, and prepares drafts for YOU to review.

Ranking principles: only Kenya-confirmed or worldwide-confirmed, open, fee-free, skill-matching
listings with realistic entry requirements are "suitable". High advertised pay is NOT rewarded on its
own: pay counts only when disclosed, plausible and capped, and suspicious pay gets nothing.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import db, intelligence as ig, notify, opportunity_common as oc, policy
except ImportError:  # pragma: no cover
    import db, intelligence as ig, notify, opportunity_common as oc, policy

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS_PATH = ROOT / "microtask_monitor" / "platforms.json"
EAT = timezone(timedelta(hours=3), "EAT")          # Africa/Nairobi: UTC+3 all year
SCHEDULE_HOUR_EAT = 8
SHORTLIST_SIZE = 20
TOP_N = 5
KEEP_ANALYSES = 60
STALE_RUNNING_MINUTES = 15
MIN_FORCE_INTERVAL_SECONDS = 60
HIGH_QUALITY_SCORE = 60
# Languages/engines you have not established (profile: Python, JavaScript, TypeScript, HTML, Node.js). Matched in the title only.
UNESTABLISHED_LANG_RX = re.compile(r"\bRust\b|C\+\+|\bC#|\bGolang\b|(?-i:\bGo\b)|\bJava\b|\bScala\b|\bKotlin\b|(?-i:\bSwift\b)|\bPHP\b|(?-i:\bRuby\b)|\bGodot\b|(?-i:\bUnity\b)|\bUnreal\b|\bDefold\b|"
                                  r"Panda3D|OpenSCAD|Open 3D Engine|\bgame (developer|engineer|design)", re.I)
MAX_NEW_ALERTS = 5          # individual "new opportunity" alerts per analysis; the daily summary covers the rest

# Research priority (NOT permission to automate): earlier platforms get a small tie-breaking bonus.
PLATFORM_PRIORITY = ["opentrain_ai", "afterquery", "braintrust", "clickworker", "atlas_capture", "handshake_ai",
                     "microworkers", "rapidworkers", "sproutgigs", "gainworkers", "reworkio", "mindrift"]
PRIORITY_BONUS = {pid: max(0, 10 - i) for i, pid in enumerate(PLATFORM_PRIORITY)}
SUITABLE_CLASSES = (ig.KENYA_CONFIRMED, ig.WORLDWIDE_CONFIRMED)
# Skill groups the user actually has (profile.json). The two generic AI-training groups are NOT enough on their own:
# nearly every OpenTrain posting mentions "AI training" in boilerplate, so a role must match your skills in its TITLE/tags.
CORE_GROUPS = {"python", "javascript", "backend_fullstack", "web_development", "ai_assisted_coding", "ai_automation",
               "prompt_engineering", "ai_tools", "ai_video_creation", "data_entry", "writing_research"}
DOMAIN_TITLE_RX = re.compile(r"\b(expert|specialist|researcher|scientist|mechanic|surveyor|physician|attorney|lawyer|consultant|"
                             r"analyst|inspector|supervisor|technician|economist|chemist|physicist|biologist|pharmacist|nurse|architect|"
                             r"estimator|metrology|machinist|operator)\b", re.I)
UNESTABLISHED_TOOLS_RX = re.compile(r"premiere|after effects|davinci|final cut|avid media|photoshop|illustrator|blender|autocad|solidworks|"
                                    r"matlab|revit|quickbooks|salesforce|\bsap\b|oracle|kubernetes|solvespace|designspark|catia|ansys", re.I)
ASSESSMENT_LABELS = {"assessment", "interview", "skills test", "background check", "identity verification", "KYC/identity check"}
MATERIAL_FIELDS = ("eligibility_class", "status", "payment_text", "deadline")


# ---------------------------------------------------------------- time ---------------------------------
def to_eat(now: datetime) -> datetime:
    return now.astimezone(EAT)


def analysis_date(now: datetime) -> str:
    return to_eat(now).date().isoformat()


def after_schedule_time(now: datetime) -> bool:
    return to_eat(now).hour >= SCHEDULE_HOUR_EAT


def _platform_names() -> dict[str, dict[str, Any]]:
    try:
        return {p["id"]: p for p in json.loads(PLATFORMS_PATH.read_text(encoding="utf-8")).get("platforms", [])}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- candidates ----------------------------
def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def candidate_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a database row (opportunities table) into a candidate."""
    raw = _loads(row.get("raw_json"), {})
    breakdown = _loads(row.get("score_breakdown_json"), {})
    req = _loads(row.get("requirements_json"), {})
    text = " ".join(str(row.get(k) or "") for k in ("title", "summary"))
    return {
        "url": row["url"], "apply_url": row.get("apply_url") or row["url"], "title": row.get("title") or "Untitled",
        "summary": (row.get("summary") or "")[:400], "source": row.get("source") or "", "category": row.get("category") or "",
        "platform_id": row.get("platform_id"), "eligibility_class": row.get("eligibility_class"),
        "eligibility_confidence": row.get("eligibility_confidence"), "eligibility_reason": row.get("eligibility_reason"),
        "status": row.get("status") or "open", "deadline": row.get("deadline"), "payment_text": row.get("payment_text"),
        "payment_usd_per_hour": row.get("payment_usd_per_hour"),
        "payment_flagged": bool(raw.get("payment_flag")) or (oc.parse_amount(row.get("payment_usd_per_hour")) or 0) > oc.SUSPICIOUS_HOURLY_USD,
        "skills": breakdown.get("matched_skill_groups") or _loads(row.get("skills_json"), []),
        # The ROLE TITLE decides fit. Listing tags/categories are boilerplate-prone ("AI training" appears everywhere).
        "title_skills": sorted(oc.match_skills(str(row.get("title") or ""))),
        "domain_title": bool(DOMAIN_TITLE_RX.search(str(row.get("title") or ""))),
        "engineer_title": bool(re.search(r"engineer", str(row.get("title") or ""), re.I)),
        "unestablished_tools": sorted({m.lower() for m in UNESTABLISHED_TOOLS_RX.findall(str(row.get("title") or "") + " " + (row.get("summary") or "")[:300])}
                                      | {m.group(0).lower() for m in UNESTABLISHED_LANG_RX.finditer(str(row.get("title") or ""))}),
        "requirements": req, "gaps": breakdown.get("qualification_gaps") or [],
        "cost_flags": oc.find_cost_flags(text), "equipment": row.get("equipment"),
        "published": row.get("published"), "first_seen": row.get("first_seen_utc"), "last_verified": row.get("last_verified_utc"),
        "countries": raw.get("countries") or [], "base_score": row.get("relevance_score") or 0,
    }


def candidate_from_item(item: dict[str, Any], caps, profile, now: datetime) -> dict[str, Any]:
    """Normalize a raw report item (cloud side, no database)."""
    rec = ig.enrich(item, caps, profile, now)
    row = {k: rec.get(k) for k in ("url", "apply_url", "platform_id", "eligibility_class", "eligibility_confidence",
                                   "eligibility_reason", "status", "deadline", "payment_text", "payment_usd_per_hour", "equipment")}
    row.update(title=item.get("title"), summary=item.get("summary"), source=item.get("source"), category=item.get("category"),
               raw_json=item, score_breakdown_json=rec["breakdown"], requirements_json=rec["requirements_json"],
               published=item.get("published"), first_seen_utc=item.get("first_seen_utc") or now.isoformat(),
               last_verified_utc=item.get("last_seen_at") or now.isoformat(), relevance_score=rec["score"])
    return candidate_from_row(row)


# ---------------------------------------------------------------- suitability & score -------------------
def exclusion_reasons(c: dict[str, Any], now: datetime) -> list[str]:
    reasons = []
    if c["eligibility_class"] not in SUITABLE_CLASSES:
        reasons.append("eligibility_not_confirmed")
    if c["status"] != "open":
        reasons.append("not_open")
    deadline = oc.parse_time(c.get("deadline"))
    if deadline and deadline < now:
        reasons.append("deadline_passed")
    if c["cost_flags"]:
        reasons.append("fees_or_deposit")
    if not c["title_skills"]:
        reasons.append("no_skill_match")                       # the role itself must match your skills, not just its boilerplate
    elif (c["domain_title"] or c["engineer_title"]) and not (set(c["title_skills"]) & CORE_GROUPS):
        reasons.append("domain_expertise_not_established")     # e.g. "Econometrics Expert", "Elevator Mechanic"
    if c["unestablished_tools"]:
        reasons.append("specific_tools_not_established")       # e.g. Premiere Pro, DaVinci, SolidWorks
    if len([g for g in c["gaps"] if "Apple" not in g]) >= 2 or any("Apple" in g for g in c["gaps"]):
        reasons.append("unrealistic_entry_requirements")
    return reasons


def daily_score(c: dict[str, Any], caps: dict[str, dict[str, Any]], now: datetime) -> tuple[int, dict[str, Any]]:
    parts: dict[str, Any] = {}
    parts["eligibility"] = 30 if c["eligibility_class"] == ig.KENYA_CONFIRMED else 18 if c["eligibility_class"] == ig.WORLDWIDE_CONFIRMED else 0
    title_groups = set(c["title_skills"])
    summary_only = set(c["skills"]) - title_groups
    parts["skills"] = min(28, 10 * len(title_groups & CORE_GROUPS) + 4 * len(title_groups - CORE_GROUPS) + 2 * len(summary_only))
    parts["domain_expertise_penalty"] = -8 if c["domain_title"] else 0
    comp = 0
    rate = oc.parse_amount(c.get("payment_usd_per_hour"))
    if c.get("payment_text") and c["payment_text"] != "Not disclosed" and not c["payment_flagged"]:
        comp += 3                                           # clearly stated compensation
        if rate:
            comp += int(min(5, rate / 12))                  # capped: high pay alone cannot dominate the ranking
    parts["compensation"] = comp
    if c["payment_flagged"]:
        parts["compensation_note"] = "unusually high advertised pay: no credit, verify manually"
    pv = (caps.get(c.get("platform_id") or "", {}).get("payment_verification") or {}).get("status")
    parts["verified_payment_route"] = 6 if pv == "documented" else 0
    gaps = len(c["gaps"])
    years = (c["requirements"] or {}).get("min_years_experience") or 0
    parts["realistic_entry"] = max(-10, 10 - 6 * gaps - (6 if years >= 3 else 0))
    posted = oc.parse_time(c.get("published")) or oc.parse_time(c.get("first_seen"))
    age = (now - posted).total_seconds() / 86400 if posted else None
    parts["freshness"] = 0 if age is None else 8 if age <= 3 else 4 if age <= 14 else 1 if age <= 30 else 0
    parts["platform_priority"] = PRIORITY_BONUS.get(c.get("platform_id") or "", 0)
    total = sum(v for v in parts.values() if isinstance(v, int) and not isinstance(v, bool))
    return max(0, min(100, total)), parts


def _evidence(c: dict[str, Any]) -> str:
    base = f"{c['eligibility_class']} ({c.get('eligibility_confidence') or 'n/a'} confidence): {c.get('eligibility_reason') or ''}"
    if c["countries"]:
        base += f" Country list has {len(c['countries'])} entries" + (" including KE." if "KE" in c["countries"] else ".")
    return base


def snapshot(c: dict[str, Any], score: int, parts: dict[str, Any], caps, names) -> dict[str, Any]:
    pid = c.get("platform_id")
    pv = (caps.get(pid or "", {}).get("payment_verification") or {})
    req = c["requirements"] or {}
    assessments = sorted(set(req.get("application_requirements") or []) & ASSESSMENT_LABELS)
    return {
        "url": c["url"], "apply_url": c["apply_url"], "title": c["title"], "summary": c["summary"][:300],
        "platform_id": pid, "platform": names.get(pid, {}).get("name") if pid else (c["source"] or "Job feed"),
        "platform_priority_rank": PLATFORM_PRIORITY.index(pid) + 1 if pid in PLATFORM_PRIORITY else None,
        "compensation": c.get("payment_text") or "Not disclosed", "payment_usd_per_hour": c.get("payment_usd_per_hour"),
        "payment_flagged": c["payment_flagged"],
        "payment_method": pv.get("note") if pv.get("status") == "documented" else "not verified",
        "eligibility": {"class": c["eligibility_class"], "confidence": c.get("eligibility_confidence"),
                        "reason": c.get("eligibility_reason"), "evidence": _evidence(c)},
        "skills": c["skills"], "requirements": req, "assessments": assessments,
        "gaps": c["gaps"], "equipment": c.get("equipment"), "deadline": c.get("deadline"), "published": c.get("published"),
        "title_skills": c["title_skills"],
        "flags": (["May need subject-matter expertise you have not established: read the requirements before applying"] if c["domain_title"] else [])
                 + (["Payment looks unusually high: verify manually"] if c["payment_flagged"] else []),
        "first_seen": c.get("first_seen"), "last_verified": c.get("last_verified"), "daily_score": score, "score_parts": parts,
    }


def build_sections(candidates: list[dict[str, Any]], caps, now: datetime, new_after: datetime | None = None,
                   seen_urls: set[str] | None = None) -> dict[str, Any]:
    """Pure function: candidates in, ranked sections out. ``new_after``/``seen_urls`` decide what counts as new."""
    names = _platform_names()
    excluded: dict[str, int] = {}
    ranked = []
    for c in candidates:
        reasons = exclusion_reasons(c, now)
        for r in reasons:
            excluded[r] = excluded.get(r, 0) + 1
        if reasons:
            continue
        score, parts = daily_score(c, caps, now)
        first = oc.parse_time(c.get("first_seen"))
        if seen_urls is not None:
            is_new = c["url"] not in seen_urls
        else:
            is_new = bool(first and (first > new_after if new_after else (now - first) <= timedelta(hours=24)))
        ranked.append((score, snapshot(c, score, parts, caps, names), is_new))
    ranked.sort(key=lambda t: (-t[0], t[1]["platform_priority_rank"] or 99, t[1]["title"].lower()))
    entries = [dict(s, is_new=n) for _, s, n in ranked]
    shortlist = entries[:SHORTLIST_SIZE]
    return {
        "top5": shortlist[:TOP_N], "shortlist": shortlist,
        "kenya_confirmed": [e for e in entries if e["eligibility"]["class"] == ig.KENYA_CONFIRMED][:SHORTLIST_SIZE],
        "worldwide_confirmed": [e for e in entries if e["eligibility"]["class"] == ig.WORLDWIDE_CONFIRMED][:SHORTLIST_SIZE],
        "new": [e for e in entries if e["is_new"]][:SHORTLIST_SIZE],
        "suitable_count": len(entries), "candidate_count": len(candidates), "excluded": excluded,
        "message": None if entries else "No suitable opportunities today: nothing open, Kenya- or worldwide-confirmed, fee-free and matching your skills.",
    }


def platform_coverage(candidates: list[dict[str, Any]], sections: dict[str, Any], caps) -> list[dict[str, Any]]:
    """Research priority order with what each platform can and cannot contribute (no automation implied)."""
    names = _platform_names()
    open_by = {}
    for c in candidates:
        if c["status"] == "open" and c.get("platform_id"):
            open_by[c["platform_id"]] = open_by.get(c["platform_id"], 0) + 1
    suit_by = {}
    for e in sections["shortlist"]:
        if e["platform_id"]:
            suit_by[e["platform_id"]] = suit_by.get(e["platform_id"], 0) + 1
    out = []
    for i, pid in enumerate(PLATFORM_PRIORITY, 1):
        cap = caps.get(pid, {})
        levels = cap.get("levels", ["UNVERIFIED"])
        automated = "DOCUMENTED_API_ALLOWED" in levels or "PUBLIC_DISCOVERY_ALLOWED" in levels
        restricted = cap.get("country_eligibility", {}).get("kenya") == "unsupported"
        out.append({"rank": i, "platform_id": pid, "name": names.get(pid, {}).get("name", pid),
                    "discovery": "automated public discovery" if automated else "none (manual review only)",
                    "open_listings": open_by.get(pid, 0), "in_shortlist": suit_by.get(pid, 0),
                    "manual_check_url": (cap.get("policy_urls") or [names.get(pid, {}).get("official_url")])[0],
                    "action": ("Restricted for Kenya: do not apply" if restricted else
                               "Shortlisted from automated discovery" if automated else
                               "No permitted automated discovery: check this platform yourself today")})
    return out


# ---------------------------------------------------------------- persistence ---------------------------
def _recover_stale(conn, now: datetime) -> int:
    cutoff = (now - timedelta(minutes=STALE_RUNNING_MINUTES)).isoformat()
    cur = conn.execute("UPDATE analysis_runs SET status='failed', error='interrupted (stale running state recovered)' "
                       "WHERE status='running' AND started_at<?", (cutoff,))
    return cur.rowcount


def latest_completed(conn) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM analysis_runs WHERE status='completed' ORDER BY completed_at DESC, id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def run_analysis(conn, now: datetime | None = None, trigger: str = "sync", source_key: str | None = None,
                 force: bool = False, create_drafts: bool = True, settings: dict | None = None,
                 channels: dict | None = None) -> dict[str, Any]:
    """Run one analysis and persist its shortlist. Idempotent per ``source_key``; safe after interruption."""
    now = now or datetime.now(timezone.utc)
    source_key = source_key or f"{trigger}:{now.isoformat()}"
    caps, profile = policy.load_capabilities(), ig.load_profile()
    with db.tx(conn):
        _recover_stale(conn, now)
    prev = latest_completed(conn)
    existing = conn.execute("SELECT id,status,started_at FROM analysis_runs WHERE source_key=?", (source_key,)).fetchone()
    if existing and existing["status"] == "completed":
        return {"skipped": "already analysed", "analysis_id": existing["id"]}
    if existing and existing["status"] == "running":
        return {"skipped": "analysis already running", "analysis_id": existing["id"]}
    if force and prev:
        done = oc.parse_time(prev["completed_at"])
        if done and (now - done).total_seconds() < MIN_FORCE_INTERVAL_SECONDS:
            return {"skipped": "an analysis finished moments ago", "analysis_id": prev["id"]}
    with db.tx(conn):
        if existing:                                                       # retry a failed run
            conn.execute("UPDATE analysis_runs SET status='running', started_at=?, error=NULL WHERE id=?", (now.isoformat(), existing["id"]))
            run_id = existing["id"]
        else:
            run_id = conn.execute("INSERT INTO analysis_runs(source_key,analysis_date,trigger,started_at,status) VALUES(?,?,?,?, 'running')",
                                  (source_key, analysis_date(now), trigger, now.isoformat())).lastrowid
    try:
        rows = conn.execute("SELECT * FROM opportunities WHERE duplicate_of IS NULL AND status='open'").fetchall()
        candidates = [candidate_from_row(dict(r)) for r in rows]
        new_after = oc.parse_time(prev["completed_at"]) if prev else None
        sections = build_sections(candidates, caps, now, new_after)
        coverage = platform_coverage(candidates, sections, caps)
        stats = {"candidates": sections["candidate_count"], "suitable": sections["suitable_count"], "excluded": sections["excluded"],
                 "top5": len(sections["top5"]), "shortlist": len(sections["shortlist"]), "new": len(sections["new"]),
                 "kenya_confirmed": len(sections["kenya_confirmed"]), "worldwide_confirmed": len(sections["worldwide_confirmed"]),
                 "message": sections["message"], "generated_at": now.isoformat()}
        with db.tx(conn):                                                  # all-or-nothing: entries + completion together
            conn.execute("DELETE FROM shortlist_entries WHERE analysis_id=?", (run_id,))
            by_url: dict[str, dict] = {}
            for key, flag in (("shortlist", "in_shortlist"), ("kenya_confirmed", "in_kenya"), ("worldwide_confirmed", "in_worldwide"),
                              ("new", "is_new"), ("top5", "in_top5")):
                for rank, e in enumerate(sections[key], 1):
                    d = by_url.setdefault(e["url"], {"e": e, "rank": None, "in_top5": 0, "in_shortlist": 0, "in_kenya": 0,
                                                     "in_worldwide": 0, "is_new": 0})
                    d[flag] = 1
                    if key == "shortlist":
                        d["rank"] = rank
            for url, d in by_url.items():
                e = d["e"]
                conn.execute("INSERT INTO shortlist_entries(analysis_id,url,rank,daily_score,platform_id,eligibility_class,in_top5,in_shortlist,"
                             "in_kenya,in_worldwide,is_new,snapshot_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (run_id, url, d["rank"], e["daily_score"], e["platform_id"], e["eligibility"]["class"], d["in_top5"],
                              d["in_shortlist"], d["in_kenya"], d["in_worldwide"], d["is_new"], json.dumps(e, ensure_ascii=False)))
            conn.execute("UPDATE analysis_runs SET status='completed', completed_at=?, stats_json=?, coverage_json=? WHERE id=?",
                         (now.isoformat(), json.dumps(stats), json.dumps(coverage), run_id))
            db.audit(conn, "daily.analysis", "analysis", run_id, {"trigger": trigger, **{k: stats[k] for k in ("suitable", "top5", "new")}}, actor="system")
            old_ids = [r[0] for r in conn.execute("SELECT id FROM analysis_runs ORDER BY id DESC LIMIT -1 OFFSET ?", (KEEP_ANALYSES,))]
            for oid in old_ids:
                conn.execute("DELETE FROM analysis_runs WHERE id=?", (oid,))
    except Exception as exc:
        with db.tx(conn):
            conn.execute("UPDATE analysis_runs SET status='failed', error=? WHERE id=?", (str(exc)[:400], run_id))
        raise
    # follow-ups never undo a completed analysis
    drafts = prepare_drafts(conn, sections["top5"]) if create_drafts else {"created": 0, "skipped": 0, "blocked": 0}
    made = notify_followups(conn, run_id, sections, prev, now, settings, channels)
    return {"analysis_id": run_id, "stats": stats, "drafts": drafts, "notifications": len(made)}


def prepare_drafts(conn, top: list[dict[str, Any]]) -> dict[str, int]:
    """Create application drafts (never submissions) for the strongest opportunities, once each."""
    from microtask_monitor import workflow as wf
    out = {"created": 0, "skipped": 0, "blocked": 0}
    for e in top:
        if conn.execute("SELECT 1 FROM applications WHERE opportunity_url=? AND status NOT IN ('rejected','withdrawn')", (e["url"],)).fetchone():
            out["skipped"] += 1
            continue
        try:
            wf.create_application(conn, e["url"])
            out["created"] += 1
        except wf.Blocked:
            out["blocked"] += 1
        except (wf.WorkflowError, policy.PermissionDenied):
            out["skipped"] += 1
    return out


def notify_followups(conn, run_id: int, sections: dict[str, Any], prev: dict | None, now: datetime, settings, channels) -> list:
    made = []
    def add(*args, **kw):
        with db.tx(conn):
            res = notify.create(conn, *args, settings=settings, channels=channels, **kw)
        if res:
            made.append(res)
    day = analysis_date(now)
    if after_schedule_time(now):                                           # once per day, first analysis after 08:00 EAT
        s = sections
        body = (f"{s['suitable_count']} suitable opportunities · top 5 ready with drafts · {len(s['kenya_confirmed'])} Kenya-confirmed · "
                f"{len(s['worldwide_confirmed'])} worldwide · {len(s['new'])} new") if s["suitable_count"] else (s["message"] or "")
        add("daily_shortlist", f"Daily earning shortlist ready ({day})", body, severity="important", key=day)
    shortlisted = {e["url"] for e in sections["shortlist"]}
    alerted = 0
    for e in sections["new"]:                                              # genuinely new, high-quality, Kenya-confirmed
        if alerted >= MAX_NEW_ALERTS:
            break
        alerted += 1 if (e["eligibility"]["class"] == ig.KENYA_CONFIRMED and e["daily_score"] >= HIGH_QUALITY_SCORE and e["url"] in shortlisted) else 0
        if e["eligibility"]["class"] == ig.KENYA_CONFIRMED and e["daily_score"] >= HIGH_QUALITY_SCORE and e["url"] in shortlisted:
            add("new_opportunity", f"New Kenya-confirmed opportunity: {e['title']}",
                f"{e['platform']} · score {e['daily_score']} · {e['compensation']}", url=e["url"], severity="important", key=e["url"])
    horizon = now + timedelta(days=3)
    for e in sections["shortlist"]:                                        # close to deadline
        d = oc.parse_time(e.get("deadline"))
        if d and now < d <= horizon:
            add("expiring_opportunity", f"Expiring soon: {e['title']}", f"Deadline {str(e['deadline'])[:16]}", url=e["url"],
                severity="important", key=e["url"], state=e["deadline"])
    if prev:                                                               # material changes on things you track
        since = prev["completed_at"]
        tracked = {r[0] for r in conn.execute("SELECT url FROM shortlist_entries WHERE analysis_id=? AND in_shortlist=1", (prev["id"],))}
        tracked |= {r[0] for r in conn.execute("SELECT url FROM opportunity_review WHERE status IN ('shortlisted','applied')")}
        tracked |= {r[0] for r in conn.execute("SELECT opportunity_url FROM applications WHERE status IN ('draft','approved','submitted','interview')") if r[0]}
        for ch in conn.execute("SELECT c.url,c.field,c.old_value,c.new_value,o.title FROM opportunity_changes c JOIN opportunities o ON o.url=c.url "
                               "WHERE c.changed_at>? AND c.field IN (?,?,?,?)", (since, *MATERIAL_FIELDS)).fetchall():
            if ch["url"] in tracked:
                add("opportunity_changed", f"{ch['field'].replace('_', ' ').title()} changed: {ch['title']}",
                    f"{ch['old_value']} → {ch['new_value']}", url=ch["url"], severity="important", key=ch["url"],
                    state=f"{ch['field']}:{ch['new_value']}")
    return made


# ---------------------------------------------------------------- triggers -------------------------------
def due_today(conn, now: datetime) -> bool:
    """True if it is past 08:00 EAT and no analysis has completed since today's 08:00 EAT."""
    local = to_eat(now)
    if local.hour < SCHEDULE_HOUR_EAT:
        return False
    start = local.replace(hour=SCHEDULE_HOUR_EAT, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    row = conn.execute("SELECT 1 FROM analysis_runs WHERE status='completed' AND completed_at>=?", (start,)).fetchone()
    return row is None


def run_if_due(db_path, now: datetime | None = None, **kw) -> dict[str, Any] | None:
    """Scheduled trigger (called every few minutes by the local server): runs at most once per Nairobi day."""
    from contextlib import closing
    now = now or datetime.now(timezone.utc)
    with closing(db.connect(db_path)) as conn:
        if not due_today(conn, now):
            return None
        return run_analysis(conn, now, "scheduled", f"daily:{analysis_date(now)}", **kw)


def run_after_sync(conn, report_key: str, import_result: dict[str, Any], now: datetime | None = None, **kw) -> dict[str, Any]:
    """Post-sync trigger: only when the new report actually changed something (no churn on identical data)."""
    changed = int(import_result.get("inserted", 0)) + int(import_result.get("changed", 0)) + int(import_result.get("expired", 0))
    if import_result.get("already_imported"):
        return {"skipped": "report already imported"}
    if not changed and latest_completed(conn):
        return {"skipped": "no changes in the new report"}
    return run_analysis(conn, now, "sync", f"report:{report_key}", **kw)


def set_review(conn, url: str, status: str, notes: str = "") -> dict[str, Any]:
    if status not in db.REVIEW_STATUSES:
        raise ValueError(f"status must be one of {', '.join(db.REVIEW_STATUSES)}")
    if not conn.execute("SELECT 1 FROM opportunities WHERE url=?", (ig.canonical_url(url),)).fetchone():
        raise LookupError("Opportunity not found")
    url = ig.canonical_url(url)
    with db.tx(conn):
        conn.execute("INSERT INTO opportunity_review(url,status,notes,updated_at) VALUES(?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                     "status=excluded.status, notes=excluded.notes, updated_at=excluded.updated_at", (url, status, notes[:500], db.now_iso()))
        db.audit(conn, "daily.review", "opportunity", url, {"status": status})
    return {"url": url, "status": status}


def load_shortlist(conn, analysis_id: int | None = None) -> dict[str, Any]:
    """Read a saved analysis with each opportunity's review status and application/draft state."""
    run = (dict(conn.execute("SELECT * FROM analysis_runs WHERE id=?", (analysis_id,)).fetchone() or {}) if analysis_id
           else latest_completed(conn))
    if not run:
        return {"analysis": None, "message": "No analysis has run yet. It runs after the next sync that brings changes, or press Run analysis now."}
    reviews = {r["url"]: dict(r) for r in conn.execute("SELECT * FROM opportunity_review")}
    apps = {}
    for a in conn.execute("SELECT a.id,a.opportunity_url,a.status,(SELECT COUNT(*) FROM application_drafts d WHERE d.application_id=a.id) drafts "
                          "FROM applications a WHERE a.opportunity_url IS NOT NULL ORDER BY a.id"):
        apps[a["opportunity_url"]] = dict(a)
    sections: dict[str, list] = {k: [] for k in ("top5", "shortlist", "kenya_confirmed", "worldwide_confirmed", "new")}
    for r in conn.execute("SELECT * FROM shortlist_entries WHERE analysis_id=? ORDER BY COALESCE(rank,999), daily_score DESC", (run["id"],)):
        e = json.loads(r["snapshot_json"])
        e["rank"], e["review_status"] = r["rank"], (reviews.get(r["url"]) or {}).get("status", "new")
        e["review_notes"] = (reviews.get(r["url"]) or {}).get("notes") or ""
        app = apps.get(r["url"])
        e["application"] = ({"id": app["id"], "status": app["status"], "has_draft": app["drafts"] > 0} if app else None)
        for key, col in (("top5", "in_top5"), ("shortlist", "in_shortlist"), ("kenya_confirmed", "in_kenya"),
                         ("worldwide_confirmed", "in_worldwide"), ("new", "is_new")):
            if r[col]:
                sections[key].append(e)
    sections["top5"].sort(key=lambda e: e["rank"] or 99)
    stats = json.loads(run["stats_json"] or "{}")
    return {"analysis": {k: run[k] for k in ("id", "analysis_date", "trigger", "started_at", "completed_at", "status")},
            "stats": stats, "coverage": json.loads(run["coverage_json"] or "[]"), "message": stats.get("message"), **sections}


# ---------------------------------------------------------------- cloud side ------------------------------
def cloud_analysis(items: list[dict[str, Any]], now: datetime | None = None, state_path: Path | None = None,
                   trigger: str = "hourly") -> dict[str, Any]:
    """Runs inside the GitHub Actions scan (laptop off is fine). Uses a small cache file to know what is new."""
    now = now or datetime.now(timezone.utc)
    caps, profile = policy.load_capabilities(), ig.load_profile()
    state: dict[str, Any] = {"seen": {}}
    if state_path and Path(state_path).exists():
        try:
            state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except ValueError:
            state = {"seen": {}}
    seen = state.get("seen", {})
    candidates = []
    for item in items:
        if isinstance(item.get("url"), str) and item["url"].startswith("https://"):
            c = candidate_from_item(item, caps, profile, now)
            if c["status"] == "open":
                candidates.append(c)
    sections = build_sections(candidates, caps, now, seen_urls=set(seen) if seen else None)
    coverage = platform_coverage(candidates, sections, caps)
    new_seen = {c["url"]: seen.get(c["url"]) or now.isoformat() for c in candidates}     # forget listings that disappeared
    if state_path:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(json.dumps({"seen": new_seen, "updated_at": now.isoformat()}), encoding="utf-8")
    return {"generated_at": now.isoformat(), "analysis_date": analysis_date(now), "trigger": trigger,
            "suitable_count": sections["suitable_count"], "candidate_count": sections["candidate_count"], "excluded": sections["excluded"],
            "message": sections["message"], "first_run_no_history": not seen,
            **{k: sections[k] for k in ("top5", "shortlist", "kenya_confirmed", "worldwide_confirmed", "new")}, "coverage": coverage}
