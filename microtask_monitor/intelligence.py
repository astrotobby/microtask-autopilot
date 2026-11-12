"""Opportunity intelligence: transparent Kenya-eligibility classes, scoring with a breakdown,
requirement extraction, change tracking, expiry and duplicate detection.

Nothing here guesses eligibility: a listing is KENYA_CONFIRMED only when its own data lists
Kenya, WORLDWIDE_CONFIRMED only when it states it is open worldwide, and everything else is
UNVERIFIED, UNSUPPORTED, EXPIRED or MANUAL_REVIEW_REQUIRED.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import db, opportunity_common as oc, policy, risk
except ImportError:  # pragma: no cover
    import db, opportunity_common as oc, policy, risk

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "microtask_monitor" / "profile.json"

KENYA_CONFIRMED = "KENYA_CONFIRMED"
WORLDWIDE_CONFIRMED = "WORLDWIDE_CONFIRMED"
KENYA_UNVERIFIED = "KENYA_UNVERIFIED"
KENYA_UNSUPPORTED = "KENYA_UNSUPPORTED"
EXPIRED = "EXPIRED"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
CLASSES = (KENYA_CONFIRMED, WORLDWIDE_CONFIRMED, KENYA_UNVERIFIED, KENYA_UNSUPPORTED, EXPIRED,
           MANUAL_REVIEW_REQUIRED)

_UNAVAILABLE = {"expired", "filled", "closed", "unavailable", "not_seen_since_last_review", "cancelled", "archived"}
FEED_STALE_AFTER_DAYS = 14
TRACKED_FIELDS = ("title", "payment_text", "status", "eligibility_class", "deadline")


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- eligibility ----------------------
def classify(item: dict[str, Any], caps: dict[str, dict[str, Any]] | None = None,
             now: datetime | None = None) -> tuple[str, str, str]:
    """Return (class, confidence, reason). Never infers Kenya eligibility from site accessibility."""
    now = now or datetime.now(timezone.utc)
    pid = item.get("platform_id")
    status = str(item.get("status") or "open").lower()
    deadline = oc.parse_time(item.get("deadline") or item.get("valid_through"))
    if status in _UNAVAILABLE or (deadline and deadline < now):
        return EXPIRED, "high", f"status '{status}'" if status in _UNAVAILABLE else "deadline has passed"
    if pid and policy.kenya_status(pid, caps) == "unsupported":
        return KENYA_UNSUPPORTED, "high", "the platform's official policy lists Kenya as unsupported"
    raw = str(item.get("kenya_eligibility") or "")
    table = {
        "kenya_explicitly_eligible": (KENYA_CONFIRMED, "high", "the listing's own country list includes Kenya"),
        "worldwide_eligible": (WORLDWIDE_CONFIRMED, "medium", "the listing states it is open worldwide"),
        "restricted_other_countries": (KENYA_UNSUPPORTED, "high", "the listing's country list does not include Kenya"),
        "restricted_for_user_location": (KENYA_UNSUPPORTED, "high", "restricted for the user's location"),
        "likely_restricted_check_listing": (MANUAL_REVIEW_REQUIRED, "low", "restriction wording found; read the listing"),
        "kenya_mentioned_verify_requirements": (KENYA_UNVERIFIED, "low", "Kenya/Africa mentioned in text; no explicit eligibility"),
        "kenya_eligibility_unspecified": (KENYA_UNVERIFIED, "low", "no country list or statement; not assumed eligible"),
        "remote_scope_unclear_kenya_not_confirmed": (KENYA_UNVERIFIED, "low", "remote role with unclear country scope"),
        "location_not_established": (KENYA_UNVERIFIED, "low", "location not established"),
    }
    if raw in table:
        cls, conf, reason = table[raw]
        if cls == KENYA_UNVERIFIED and item.get("entry_method") == "manual_review":
            return MANUAL_REVIEW_REQUIRED, "low", "manually recorded without a stated region"
        return cls, conf, reason
    return MANUAL_REVIEW_REQUIRED, "low", "no usable eligibility data"


# ---------------------------------------------------------------- requirement extraction -----------
_EXP_RX = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?", re.I)
_HOURS_RX = re.compile(r"(?:less than|up to|under|max(?:imum)?)?\s*(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*(?:hours|hrs|h)\s*(?:/|per|a)\s*week", re.I)
EQUIPMENT_TERMS = {"iphone": "iPhone", "ios app": "iOS device", "ipad": "iPad", "macbook": "Mac computer",
                   "mac only": "Mac computer", "webcam": "webcam", "microphone": "microphone", "headset": "headset",
                   "gpu": "GPU", "smartphone": "smartphone", "android": "Android phone", "camera": "camera"}
APPLICATION_TERMS = {"resume": "resume/CV", "cv ": "resume/CV", "portfolio": "portfolio", "cover letter": "cover letter",
                     "assessment": "assessment", "interview": "interview", "test": "skills test",
                     "writing sample": "writing sample", "identity verification": "identity verification",
                     "kyc": "KYC/identity check", "github": "GitHub profile", "background check": "background check"}


def extract_requirements(text: str) -> dict[str, Any]:
    low = (text or "").lower()
    years = [int(m.group(1)) for m in _EXP_RX.finditer(low)]
    quals = [q for q, rx in (("PhD/doctorate", r"\bph\.?d\b|doctorate"), ("master's degree", r"master'?s|\bmsc\b|\bmba\b"),
                             ("bachelor's/degree", r"bachelor|degree|\bbsc\b"), ("certification", r"certif"))
             if re.search(rx, low)]
    equipment = sorted({label for term, label in EQUIPMENT_TERMS.items() if term in low})
    hours = [int(m.group(1)) for m in _HOURS_RX.finditer(low)]
    effort = f"about {max(hours)} hours/week" if hours else None
    if not effort:
        for phrase in ("part-time", "full-time", "flexible", "one-off", "per task"):
            if phrase in low:
                effort = phrase
                break
    application = sorted({label for term, label in APPLICATION_TERMS.items() if term in low})
    return {"min_years_experience": max(years) if years else None, "qualifications": quals,
            "equipment": equipment, "effort": effort, "hours_per_week": max(hours) if hours else None,
            "application_requirements": application}


def qualification_gaps(req: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    gaps = []
    for qual in req.get("qualifications", []):
        gaps.append(f"requires {qual} (not established in your profile)")
    years = req.get("min_years_experience")
    if years and years >= 3:
        gaps.append(f"mentions {years}+ years of experience (not established)")
    ios_unavailable = not profile.get("devices", {}).get("ios_available", False)
    if ios_unavailable and any(e in ("iPhone", "iOS device", "iPad", "Mac computer") for e in req.get("equipment", [])):
        gaps.append("needs Apple hardware (you have a Samsung A05 / Windows laptop)")
    return gaps


# ---------------------------------------------------------------- scoring -------------------------
ELIGIBILITY_POINTS = {KENYA_CONFIRMED: 35, WORLDWIDE_CONFIRMED: 22, KENYA_UNVERIFIED: 6,
                      MANUAL_REVIEW_REQUIRED: 4, KENYA_UNSUPPORTED: -50, EXPIRED: -60}


def score(item: dict[str, Any], cls: str, req: dict[str, Any], caps: dict[str, dict[str, Any]] | None = None,
          profile: dict[str, Any] | None = None, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    """Transparent 0-100 score; the breakdown records every component and its reason."""
    now = now or datetime.now(timezone.utc)
    profile = profile if profile is not None else load_profile()
    caps = caps if caps is not None else policy.load_capabilities()
    parts: dict[str, Any] = {}
    parts["eligibility"] = ELIGIBILITY_POINTS.get(cls, 0)
    text = " ".join(str(item.get(k) or "") for k in ("title", "summary", "category", "source")) + " " + \
        " ".join(str(s) for s in (item.get("skills") or []))
    matched = oc.match_skills(text)
    parts["skills"] = min(24, 8 * len(matched))
    parts["matched_skill_groups"] = sorted(matched)
    hourly = oc.parse_amount(item.get("payment_usd_per_hour"))
    flagged = bool(item.get("payment_flag"))
    amount = oc.parse_amount(item.get("payment_amount"))
    comp = 0
    if flagged:
        parts["compensation_note"] = "unusually high advertised pay: verify manually; no bonus given"
    elif hourly is not None:
        comp = int(min(15, hourly / 4))
    elif amount is not None and (item.get("payment_currency") or "USD") == "USD":
        comp = int(min(8, amount / 100))
    parts["compensation"] = comp
    available = cls not in (EXPIRED,) and str(item.get("status") or "open").lower() not in _UNAVAILABLE
    parts["availability"] = 5 if available else -30
    posted = oc.parse_time(item.get("published") or item.get("datePosted") or item.get("posted"))
    fresh = 0
    if posted:
        age = (now - posted).total_seconds() / 86400
        fresh = 8 if age <= 3 else 5 if age <= 14 else 2 if age <= 30 else 0
    parts["freshness"] = fresh
    gaps = qualification_gaps(req, profile)
    equipment_gap = any("Apple hardware" in g for g in gaps)
    parts["experience_penalty"] = -8 * min(2, len([g for g in gaps if "Apple" not in g]))
    parts["equipment_penalty"] = -20 if equipment_gap else 0
    hours = req.get("hours_per_week")
    parts["time_penalty"] = -6 if hours and hours >= 40 else 0
    pv = (caps.get(item.get("platform_id") or "", {}).get("payment_verification", {}) or {}).get("status")
    parts["payment_reliability"] = 6 if pv == "documented" else 0
    parts["qualification_gaps"] = gaps
    total = sum(v for k, v in parts.items() if isinstance(v, (int, float)) and not isinstance(v, bool))
    return max(0, min(100, int(total))), parts


def priority_for(score_value: int, cls: str) -> str:
    if cls in (EXPIRED, KENYA_UNSUPPORTED):
        return "low_or_unrelated"
    if score_value >= 60 and cls in (KENYA_CONFIRMED, WORLDWIDE_CONFIRMED):
        return "high_review"
    if score_value >= 35:
        return "medium_review"
    return "low_or_unrelated"


# ---------------------------------------------------------------- persistence ---------------------
def canonical_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def enrich(item: dict[str, Any], caps=None, profile=None, now: datetime | None = None) -> dict[str, Any]:
    """Return the structured record stored for an opportunity."""
    now = now or datetime.now(timezone.utc)
    text = " ".join(str(item.get(k) or "") for k in ("title", "summary"))
    req = extract_requirements(text + " " + " ".join(str(s) for s in (item.get("skills") or [])))
    cls, conf, reason = classify(item, caps, now)
    sc, breakdown = score(item, cls, req, caps, profile, now)
    risk_result = risk.assess(item)
    breakdown["risk"] = risk_result
    return {
        "url": canonical_url(item["url"]), "platform_id": item.get("platform_id"),
        "external_id": item.get("external_id"), "eligibility_class": cls,
        "eligibility_confidence": conf, "eligibility_reason": reason,
        "status": "expired" if cls == EXPIRED else "open", "deadline": item.get("deadline") or item.get("valid_through") or None,
        "payment_text": item.get("payment_display") or None,
        "payment_currency": (item.get("payment_display") or "").split(" ")[0] if item.get("payment_display") and item.get("payment_display") != "Not disclosed" else None,
        "payment_amount": oc.parse_amount(item.get("payment_amount")),
        "payment_usd_per_hour": oc.parse_amount(item.get("payment_usd_per_hour")),
        "skills_json": json.dumps(sorted(set((breakdown.get("matched_skill_groups") or []))), ensure_ascii=False),
        "requirements_json": json.dumps(req, ensure_ascii=False),
        "equipment": ", ".join(req["equipment"]) or None, "effort": req["effort"],
        "score": sc, "priority": priority_for(sc, cls), "breakdown": breakdown,
        "risk_label": risk_result["label"], "risk_score": risk_result["score"],
        "risk_flags_json": json.dumps(risk_result["flags"], ensure_ascii=False),
        "risk_evidence_json": json.dumps(risk_result["evidence"], ensure_ascii=False),
        "apply_url": item.get("apply_url") or item["url"],
        "dedupe_key": f"{item.get('platform_id')}:{item.get('external_id')}" if item.get("external_id")
        else f"title:{_norm_title(item.get('title', ''))}",
    }


def _hash(rec: dict[str, Any], item: dict[str, Any]) -> str:
    basis = [rec["eligibility_class"], rec["payment_text"], rec["status"], rec["deadline"], item.get("title"),
             hashlib.sha1((item.get("summary") or "").encode()).hexdigest()]
    return hashlib.sha1(json.dumps(basis, default=str).encode()).hexdigest()


def ingest(conn, items: list[dict[str, Any]], *, now: datetime | None = None, complete_platforms: set[str] | None = None,
           caps=None, profile=None) -> dict[str, int]:
    """Upsert enriched opportunities; track changes, duplicates and expiry. Caller manages the transaction."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    caps = caps if caps is not None else policy.load_capabilities()
    profile = profile if profile is not None else load_profile()
    stats = {"new": 0, "updated": 0, "changed": 0, "expired": 0, "duplicates": 0}
    seen: set[str] = set()
    for item in items:
        if not isinstance(item.get("url"), str) or not item["url"].startswith("https://"):
            continue
        rec = enrich(item, caps, profile, now)
        url = rec["url"]
        seen.add(url)
        old = conn.execute("SELECT * FROM opportunities WHERE url=?", (url,)).fetchone()
        dup = conn.execute("SELECT url FROM opportunities WHERE url<>? AND duplicate_of IS NULL AND ("
                           "(platform_id=? AND external_id=? AND ? IS NOT NULL) OR (platform_id IS NULL AND ?=? AND ?<>'title:'))",
                           (url, rec["platform_id"], rec["external_id"], rec["external_id"],
                            rec["dedupe_key"], f"title:{_norm_title(item.get('title', ''))}",
                            rec["dedupe_key"])).fetchone() if not old else None
        duplicate_of = dup[0] if dup else (old["duplicate_of"] if old else None)
        if duplicate_of:
            stats["duplicates"] += 1
        chash = _hash(rec, item)
        if old:
            stats["updated"] += 1
            for fld in TRACKED_FIELDS:
                new_val = rec.get(fld) if fld != "title" else item.get("title")
                if str(old[fld] if old[fld] is not None else "") != str(new_val if new_val is not None else ""):
                    conn.execute("INSERT INTO opportunity_changes(url,changed_at,field,old_value,new_value) VALUES(?,?,?,?,?)",
                                 (url, stamp, fld, old[fld], new_val))
                    stats["changed"] += 1
        else:
            stats["new"] += 1
        conn.execute("""INSERT INTO opportunities(url,title,source,summary,priority,relevance_score,category,
            kenya_eligibility,published,first_seen_utc,last_seen_utc,report_checked_at_utc,raw_json,
            platform_id,external_id,eligibility_class,eligibility_confidence,eligibility_reason,status,deadline,
            payment_text,payment_currency,payment_amount,payment_usd_per_hour,skills_json,requirements_json,
            equipment,effort,score_breakdown_json,content_hash,last_verified_utc,expired_utc,duplicate_of,apply_url,
            risk_label,risk_score,risk_flags_json,risk_evidence_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET title=excluded.title, source=excluded.source, summary=excluded.summary,
            priority=excluded.priority, relevance_score=excluded.relevance_score, category=excluded.category,
            kenya_eligibility=excluded.kenya_eligibility, published=excluded.published, last_seen_utc=excluded.last_seen_utc,
            report_checked_at_utc=excluded.report_checked_at_utc, raw_json=excluded.raw_json,
            platform_id=excluded.platform_id, external_id=excluded.external_id,
            eligibility_class=excluded.eligibility_class, eligibility_confidence=excluded.eligibility_confidence,
            eligibility_reason=excluded.eligibility_reason, status=excluded.status, deadline=excluded.deadline,
            payment_text=excluded.payment_text, payment_currency=excluded.payment_currency,
            payment_amount=excluded.payment_amount, payment_usd_per_hour=excluded.payment_usd_per_hour,
            skills_json=excluded.skills_json, requirements_json=excluded.requirements_json,
            equipment=excluded.equipment, effort=excluded.effort, score_breakdown_json=excluded.score_breakdown_json,
            content_hash=excluded.content_hash, last_verified_utc=excluded.last_verified_utc,
            expired_utc=excluded.expired_utc, duplicate_of=excluded.duplicate_of, apply_url=excluded.apply_url,
            risk_label=excluded.risk_label, risk_score=excluded.risk_score,
            risk_flags_json=excluded.risk_flags_json, risk_evidence_json=excluded.risk_evidence_json""",
                     (url, item.get("title") or "Untitled", item.get("source", ""), (item.get("summary") or "")[:1200],
                      rec["priority"], rec["score"], item.get("category", ""), item.get("kenya_eligibility", ""),
                      item.get("published", ""), old["first_seen_utc"] if old else stamp, stamp,
                      item.get("report_checked_at_utc", stamp), json.dumps(item, ensure_ascii=False, default=str),
                      rec["platform_id"], rec["external_id"], rec["eligibility_class"], rec["eligibility_confidence"],
                      rec["eligibility_reason"], rec["status"], rec["deadline"], rec["payment_text"],
                      rec["payment_currency"], rec["payment_amount"], rec["payment_usd_per_hour"], rec["skills_json"],
                      rec["requirements_json"], rec["equipment"], rec["effort"],
                      json.dumps(rec["breakdown"], ensure_ascii=False), chash, stamp,
                      stamp if rec["status"] == "expired" else None, duplicate_of, rec["apply_url"],
                      rec["risk_label"], rec["risk_score"], rec["risk_flags_json"], rec["risk_evidence_json"]))
    stats["expired"] = expire_missing(conn, seen, complete_platforms or set(), now)
    return stats


def expire_missing(conn, seen_urls: set[str], complete_platforms: set[str], now: datetime) -> int:
    """Mark listings that disappeared. Only trusts platforms whose scan was complete, and feed items after 14 days."""
    stamp = now.isoformat()
    count = 0
    rows = conn.execute("SELECT url, platform_id, last_seen_utc FROM opportunities WHERE status='open'").fetchall()
    for row in rows:
        if row["url"] in seen_urls:
            continue
        pid = row["platform_id"]
        gone = pid in complete_platforms
        if not pid:
            last = oc.parse_time(row["last_seen_utc"])
            gone = bool(last and now - last > timedelta(days=FEED_STALE_AFTER_DAYS))
        if gone:
            conn.execute("UPDATE opportunities SET status='expired', eligibility_class='EXPIRED', expired_utc=?, "
                         "priority='low_or_unrelated' WHERE url=?", (stamp, row["url"]))
            conn.execute("INSERT INTO opportunity_changes(url,changed_at,field,old_value,new_value) VALUES(?,?,?,?,?)",
                         (row["url"], stamp, "status", "open", "expired"))
            count += 1
    return count


def backfill(conn, now: datetime | None = None) -> int:
    """Classify legacy rows (created before the intelligence columns) from their stored raw_json."""
    rows = conn.execute("SELECT url, raw_json FROM opportunities WHERE eligibility_class IS NULL").fetchall()
    items = []
    for row in rows:
        try:
            item = json.loads(row["raw_json"])
        except ValueError:
            continue
        item["url"] = row["url"]
        items.append(item)
    if not items:
        return 0
    ingest(conn, items, now=now)
    return len(items)
