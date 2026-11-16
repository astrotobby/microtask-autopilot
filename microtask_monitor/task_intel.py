"""Task intelligence: what a task asks for, whether it may and can be automated, and what it is really worth.

Nothing here is decided from how simple a task looks. The automation class combines three independent facts:
the platform's verified permission (permissions.py), the task's own words (AI rules, human-only requirements,
never-automated actions) and whether a local specialist can genuinely produce the deliverable.

Money: an advertised reward is an advertisement. ``economics`` discounts it by the measured acceptance history and
payout evidence and reports it as an expectation, never as earnings. No exchange rate is invented: values in other
currencies are kept in their own currency and are not ranked by hourly rate.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

try:
    from microtask_monitor import control, db, execution as ex, intelligence as ig, opportunity_common as oc, permissions, policy
except ImportError:  # pragma: no cover
    import control, db, execution as ex, intelligence as ig, opportunity_common as oc, permissions, policy

AUTOMATION_CLASSES = ("FULLY_AUTOMATABLE_WITH_PERMISSION", "AI_ASSISTED_WITH_PERMISSION", "DETERMINISTIC_LOCAL_PROCESSING", "HUMAN_REQUIRED",
                      "PLATFORM_AUTOMATION_PROHIBITED", "AI_PROHIBITED", "ELIGIBILITY_UNVERIFIED", "NOT_FEASIBLE", "MANUAL_REVIEW_REQUIRED")
EXECUTABLE = ("FULLY_AUTOMATABLE_WITH_PERMISSION", "AI_ASSISTED_WITH_PERMISSION", "DETERMINISTIC_LOCAL_PROCESSING")
# The eight opportunity categories shown to you (the automation class above stays the engine's internal decision).
CATEGORY8 = {1: "Fully automatable and permitted", 2: "AI-assisted and permitted", 3: "Preparation permitted, submission restricted",
             4: "Human required", 5: "Platform prohibited", 6: "Country or account ineligible", 7: "Policy unclear",
             8: "Not economically worthwhile"}

# (category, pattern) in priority order: the first match wins, so the specific kinds come before the general ones.
_CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    ("usability_testing", r"usability test|user test(?:ing)?|think[- ]aloud|unmoderated|user interview"),
    ("survey", r"\bsurvey|questionnaire|\bpoll\b|screener"),
    ("voice_recording", r"voice record|read (?:the|these) (?:sentences|phrases|prompts) aloud|audio recording of you|speech collection|record your voice"),
    ("video_recording", r"record (?:a )?video of (?:yourself|you)|film yourself|selfie video|video of yourself|ugc video"),
    ("physical_capture", r"data[- ]capture|egocentric|head[- ]mount|take (?:a )?photos? of|photograph (?:a|the|your)|mystery shopp|in[- ]person|visit (?:a|the) (?:store|shop)"),
    ("social_engagement", r"\b(like|follow|subscribe|retweet|upvote|repost|comment on)\b|clipping|post (?:on|to) (?:your )?(?:tiktok|instagram|x|twitter|youtube|facebook)"),
    ("transcription", r"transcri(?:be|ption)|subtitle|caption(?:ing)? (?:the )?(?:audio|video)"),
    ("translation", r"translat(?:e|ion)|locali[sz]ation"),
    ("llm_evaluation", r"\b(llm|model|chatbot|ai) (?:response|output|answer)s?\b.{0,40}\b(rate|rank|evaluat|compar|scor)|(?:rate|rank|evaluat|compar|scor)\w*.{0,40}\b(llm|model|chatbot) (?:response|output|answer)s?|side[- ]by[- ]side|rlhf"),
    ("ai_evaluation", r"rubric|evaluat(?:e|ion) (?:the )?(?:ai|responses|outputs|results)|search (?:quality )?rat(?:er|ing)|relevance rat"),
    ("prompt_engineering", r"prompt (?:engineer|writing|design)|write (?:\d+ )?prompts|red[- ]team"),
    ("image_labeling", r"bounding box|image (?:label|annotat|tag|classif)|label (?:the )?(?:images|photos)|segmentation|draw (?:a )?box"),
    ("video_annotation", r"video (?:label|annotat|tag)|annotate (?:the )?videos?|frame[- ]by[- ]frame"),
    ("text_classification", r"(?:classify|categori[sz]e|label|tag) (?:the |each |these )?(?:texts?|sentences|comments|tweets|reviews|messages|rows|records)|sentiment (?:label|analysis)|intent label"),
    ("data_annotation", r"annotat|labell?ing|data label"),
    ("spreadsheet_processing", r"spreadsheet|excel|\.xlsx|google sheets?|pivot table|vlookup"),
    ("data_processing", r"\bcsv\b|dedup|clean (?:the |this )?(?:data|dataset|list|file)|data (?:clean|entry|processing|validation|transform|extract)|json(?:l)? file|normali[sz]e (?:the )?(?:data|records)"),
    ("coding_debugging", r"\bdebug|fix (?:the |this |a )?(?:bug|error)|stack trace|failing (?:unit |integration )?tests?"),
    ("software_development", r"\b(python|javascript|typescript|node\.?js|html|css|api|backend|script|function|unit tests?|code review|refactor)\b"),
    ("technical_writing", r"technical (?:writing|documentation|article)|api documentation|readme|user guide|how[- ]to guide|documentation"),
    ("research", r"\bresearch\b|literature review|find sources|fact[- ]check|market analysis|competitor analysis"),
    ("content_creation", r"script (?:for|of)|video script|caption|marketing (?:copy|text)|content brief|product description|social media (?:post|content)|ad copy"),
    ("writing", r"\b(write|article|blog|essay|report|summar(?:y|ise|ize)|proofread|edit(?:ing)?|rewrite)\b"),
    ("administrative", r"data entry|schedule|calendar|inbox|virtual assistant|admin(?:istrative)? (?:task|support)|form filling"),
)
_CATEGORIES = tuple((c, re.compile(rx, re.I | re.S)) for c, rx in _CATEGORY_RULES)
# Which local specialist (execution.WORK_TYPES) can genuinely produce each category. None = no local specialist exists.
WORKER_FOR = {"software_development": "software", "coding_debugging": "software", "data_processing": "data", "spreadsheet_processing": "data",
              "text_classification": "evaluation", "data_annotation": "evaluation", "ai_evaluation": "evaluation", "llm_evaluation": "evaluation",
              "writing": "writing", "technical_writing": "writing", "prompt_engineering": "writing", "translation": "writing", "research": "research",
              "content_creation": "content", "administrative": "data", "image_labeling": None, "video_annotation": None, "transcription": None,
              "other": None, **{c: None for c in control.HUMAN_ONLY_CATEGORIES}}
# Planning estimates only (minutes of local execution, prior chance of acceptance). They are replaced by measured history as it accumulates.
_DEFAULT_MINUTES = {"software": 45, "data": 5, "evaluation": 30, "writing": 30, "content": 25, "automation": 45, "research": 40, None: 60}
_PRIOR_ACCEPT = {"DETERMINISTIC_LOCAL_PROCESSING": 0.85, "AI_ASSISTED_WITH_PERMISSION": 0.65, "FULLY_AUTOMATABLE_WITH_PERMISSION": 0.65}
_PRIOR_WEIGHT = 4
_DURATION = re.compile(r"(?:about|approx(?:imately)?|around|est(?:imated)?\.?(?: time)?:?|takes?|up to)?\s*(\d{1,3}(?:\.\d)?)\s*(?:-\s*\d{1,3}\s*)?(minutes?|mins?|hours?|hrs?)\b", re.I)
_ACCEPTANCE = re.compile(r"[^.\n]*\b(must|required|will be rejected|rejected if|accepted if|only accepted|acceptance criteria|do not|don't)\b[^.\n]*", re.I)
_EVIDENCE = re.compile(r"[^.\n]*\b(screenshot|proof|link to|url of|upload|attach|submit (?:the|a|your))\b[^.\n]*", re.I)
_COUNTRY_ONLY = re.compile(r"\b((?:us|u\.s\.|usa|united states|uk|united kingdom|canada|australia|india|germany|eu|europe)(?:[\s,/&]+(?:and |or )?(?:us|usa|uk|canada|australia|eu))*)\s+(?:residents? |workers? |citizens? )?only\b", re.I)


def categorize(title: str, instructions: str) -> str:
    text = f"{title}\n{instructions}"
    for name, rx in _CATEGORIES:
        if rx.search(text):
            return name
    return "other"


def extract(a: dict[str, Any], caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Everything the task states about itself. Unknown stays None: nothing is filled in by assumption."""
    caps = policy.load_capabilities() if caps is None else caps
    title, body = a.get("title") or "", a.get("instructions") or ""
    text = f"{title}\n{body}"
    req = a.get("requirements") or {}
    rec = caps.get(a.get("platform_id") or "", {})
    reqs = ig.extract_requirements(text)
    dur = _DURATION.search(body)
    minutes = None
    if dur:
        minutes = float(dur.group(1)) * (60 if dur.group(2).lower().startswith("h") else 1)
    ai = ex.detect_ai_rules(body)
    country = _COUNTRY_ONLY.search(text)
    prof = permissions.profile(a.get("platform_id") or "", caps) or {"actions": {}}
    hard = permissions.hard_restrictions(text)
    costs = oc.find_cost_flags(text)
    return {"title": title, "description": body[:2000], "platform": a.get("platform_id"),
            "compensation": a.get("reward_net") if a.get("reward_net") is not None else a.get("reward_gross"),
            "compensation_gross": a.get("reward_gross"), "currency": a.get("currency"),
            "payment_method": (rec.get("payment_verification") or {}).get("note"),
            "payment_status": (rec.get("payment_verification") or {}).get("status", "unverified"),
            "country_restrictions": {"stated_in_task": country.group(0) if country else None, "allowed_countries": req.get("allowed_countries"),
                                     "platform_kenya": (rec.get("country_eligibility") or {}).get("kenya", "unverified")},
            "required_skills": sorted(oc.match_skills(text)), "required_equipment": reqs.get("equipment") or [],
            "estimated_minutes": minutes, "deadline": a.get("deadline"),
            "acceptance_conditions": [m.group(0).strip()[:200] for m in _ACCEPTANCE.finditer(body)][:8],
            "evidence_requirements": [m.group(0).strip()[:200] for m in _EVIDENCE.finditer(body)][:6],
            "ai_policy": {"declared": a.get("ai_use") or "unknown", "source": a.get("ai_use_source") or None, "detected": ai["status"], "detected_evidence": ai.get("evidence")},
            "automation_policy": {k: (prof["actions"].get(k) or {}).get("result", "unverified") for k in ("task_execution", "auto_submission")},
            "submission_policy": rec.get("evidence_submission") or ("human_only" if a.get("platform_id") == permissions.DIRECT_LANE else "unverified"),
            "risks": [h["label"] for h in hard] + [f"possible upfront cost: {c}" for c in costs], "hard_restrictions": hard, "upfront_cost_flags": costs,
            "deterministic_spec": bool(req.get("deterministic_spec"))}


def classify_automation(a: dict[str, Any], category: str, x: dict[str, Any], caps: dict[str, dict[str, Any]] | None = None) -> tuple[str, list[str]]:
    """One of AUTOMATION_CLASSES with every reason. Blocking facts are checked before any permissive one."""
    reasons: list[str] = []
    pid = a.get("platform_id") or ""
    prof = permissions.profile(pid, caps)
    if prof is None:
        return "MANUAL_REVIEW_REQUIRED", [f"No capability record for '{pid}' (fail closed)"]
    if prof["disabled_reason"]:
        return "NOT_FEASIBLE", [prof["disabled_reason"]]
    cr = x["country_restrictions"]
    if isinstance(cr["allowed_countries"], list) and cr["allowed_countries"] and "KE" not in [str(c).upper() for c in cr["allowed_countries"]]:
        return "NOT_FEASIBLE", ["Kenya is not in this task's allowed countries: " + ", ".join(map(str, cr["allowed_countries"][:8]))]
    if cr["stated_in_task"] and "kenya" not in cr["stated_in_task"].lower():
        return "NOT_FEASIBLE", [f"The task restricts who may do it: \"{cr['stated_in_task']}\""]
    if x["upfront_cost_flags"]:
        return "NOT_FEASIBLE", ["It appears to need an upfront payment, deposit or paid membership (" + ", ".join(x["upfront_cost_flags"][:3]) + "): excluded under the $0 rule"]
    human = [h for h in x["hard_restrictions"] if h["class"] == "HUMAN_REQUIRED"]
    never = [h for h in x["hard_restrictions"] if h["class"] == "NOT_FEASIBLE"]
    if never:
        return "NOT_FEASIBLE", [f"Never automated: {h['label']} (\"{h['evidence']}\")" for h in never]
    if human or category in control.HUMAN_ONLY_CATEGORIES or a.get("ai_use") == "human_only":
        why = [f"Needs you personally: {h['label']} (\"{h['evidence']}\")" for h in human]
        if category in control.HUMAN_ONLY_CATEGORIES:
            why.append(f"'{category.replace('_', ' ')}' is a real person's own experience, voice, presence or opinion")
        if a.get("ai_use") == "human_only":
            why.append("Marked as human-only work")
        return "HUMAN_REQUIRED", why
    if a.get("stage") == "discovered":
        return "ELIGIBILITY_UNVERIFIED", ["Nothing on record establishes that Kenya is accepted for this task" if a.get("eligibility_checked_at")
                                          else "Country eligibility has not been checked for this task yet"]
    execution = prof["actions"]["task_execution"]
    if execution["result"] == "prohibited":
        return "PLATFORM_AUTOMATION_PROHIBITED", [f"{prof['name']}: {execution['why'] or 'automated task completion is prohibited'}"]
    declared, detected = x["ai_policy"]["declared"], x["ai_policy"]["detected"]
    worker = WORKER_FOR.get(category)
    deterministic = worker == "data" and x["deterministic_spec"]
    if (declared == "prohibited" or detected == "prohibited") and not deterministic:
        return "AI_PROHIBITED", ["The task's instructions prohibit AI or automation" + (f": \"{x['ai_policy']['detected_evidence']}\"" if x["ai_policy"]["detected_evidence"] else "")]
    if execution["result"] != "permitted":
        return "MANUAL_REVIEW_REQUIRED", [f"{prof['name']}: automated completion is {execution['result'].replace('_', ' ')} ({execution['why'] or 'no evidence recorded'}). Do it yourself or record the platform's permission with its source."]
    if worker is None:
        return "NOT_FEASIBLE", [f"No local specialist can genuinely produce '{category.replace('_', ' ')}' work"]
    if len((a.get("instructions") or "").strip()) < 20:
        return "MANUAL_REVIEW_REQUIRED", ["The real task instructions are not on file"]
    if deterministic:
        return "DETERMINISTIC_LOCAL_PROCESSING", ["A deterministic specification was supplied: the data specialist runs it without any AI", f"Permission: {execution['clause']} ({execution['verified_on']})"]
    if declared != "allowed":
        return "MANUAL_REVIEW_REQUIRED", ["Whether AI help is permitted for this task has not been established (record the rule and its source)"]
    reasons.append(f"Platform permission: {execution['clause']} ({execution['verified_on']})")
    reasons.append(f"AI use permitted by: {x['ai_policy']['source'] or 'the recorded task rule'}")
    if prof["actions"]["auto_submission"]["result"] == "permitted":
        return "FULLY_AUTOMATABLE_WITH_PERMISSION", reasons + ["Automated submission is also verified for this platform"]
    return "AI_ASSISTED_WITH_PERMISSION", reasons + ["Submission stays with you: " + (prof["actions"]["auto_submission"]["why"] or "not verified")]


def history(conn, platform_id: str) -> dict[str, int]:
    """Measured outcomes on one platform: how many submissions were accepted, rejected and actually paid."""
    def n(sql: str) -> int:
        return conn.execute(sql, (platform_id,)).fetchone()[0]
    return {"accepted": n("SELECT COUNT(*) FROM assignments WHERE platform_id=? AND stage IN ('work_accepted','payment_pending','paid')"),
            "rejected": n("SELECT COUNT(*) FROM assignments WHERE platform_id=? AND stage='rejected'"),
            "paid": n("SELECT COUNT(*) FROM assignments WHERE platform_id=? AND stage='paid'"),
            "similar": 0}


def economics(a: dict[str, Any], x: dict[str, Any], automation_class: str, category: str, hist: dict[str, int]) -> dict[str, Any]:
    advertised, currency = x["compensation"], (x["currency"] or "").upper() or None
    worker = WORKER_FOR.get(category)
    minutes = x["estimated_minutes"] or _DEFAULT_MINUTES.get(worker, 60)
    prior = _PRIOR_ACCEPT.get(automation_class, 0.6)
    decided = hist["accepted"] + hist["rejected"]
    p_accept = (hist["accepted"] + prior * _PRIOR_WEIGHT) / (decided + _PRIOR_WEIGHT)
    # Payout reliability: measured when accepted work exists; otherwise the documented/unverified state of the payout route.
    if hist["accepted"]:
        p_pay = (hist["paid"] + 1) / (hist["accepted"] + 2)
    else:
        p_pay = 0.9 if x["payment_status"] == "documented" else 0.6
    cost = 0.0                                      # runs on this PC with the subscription you already have: no marginal spend
    expected = net = hourly = risk_adjusted = None
    if isinstance(advertised, (int, float)) and advertised > 0:
        expected = round(advertised * p_accept, 4)
        risk_adjusted = round(advertised * p_accept * p_pay, 4)
        net = round(risk_adjusted - cost, 4)
        hourly = round(net / (minutes / 60), 2) if minutes else None
    return {"advertised": advertised, "currency": currency, "expected_compensation": expected, "risk_adjusted_value": risk_adjusted, "net_value": net,
            "estimated_minutes": minutes, "minutes_source": "stated in the task" if x["estimated_minutes"] else "planning estimate",
            "effective_hourly": hourly, "hourly_comparable_usd": hourly if currency == "USD" else None,
            "acceptance_probability": round(p_accept, 3), "acceptance_basis": f"{decided} decided submission(s) on this platform" if decided else "prior only (no history yet)",
            "payment_reliability": round(p_pay, 3), "execution_cost": cost, "similar_tasks_seen": hist.get("similar", 0),
            "note": "Estimates for ranking only. Nothing here is income until a payment is recorded."}


def score(a: dict[str, Any], x: dict[str, Any], automation_class: str, eco: dict[str, Any], now: datetime | None = None) -> tuple[float, dict[str, float]]:
    """0-100. Permission and feasibility dominate; advertised pay alone can never lift a blocked or unverified task."""
    now = now or datetime.now(timezone.utc)
    parts: dict[str, float] = {}
    parts["automation"] = {"DETERMINISTIC_LOCAL_PROCESSING": 30, "FULLY_AUTOMATABLE_WITH_PERMISSION": 30, "AI_ASSISTED_WITH_PERMISSION": 24,
                           "HUMAN_REQUIRED": 8, "MANUAL_REVIEW_REQUIRED": 6, "ELIGIBILITY_UNVERIFIED": 3}.get(automation_class, 0)
    req = a.get("requirements") or {}
    parts["eligibility"] = 20 if a.get("eligibility_checked_at") and a.get("stage") != "discovered" else (6 if req.get("eligibility_class") in ("KENYA_CONFIRMED", "WORLDWIDE_CONFIRMED") else 0)
    hourly = eco["hourly_comparable_usd"]
    parts["value"] = round(min(20.0, hourly), 2) if hourly else (4.0 if eco["advertised"] else 0.0)
    parts["acceptance"] = round(10 * eco["acceptance_probability"], 2)
    parts["payment"] = 8.0 if x["payment_status"] == "documented" else 2.0
    parts["skills"] = min(6.0, 2.0 * len(x["required_skills"]))
    parts["repeat"] = min(3.0, float(eco["similar_tasks_seen"]))
    deadline = oc.parse_time(a.get("deadline")) if a.get("deadline") else None
    if deadline:
        hours = (deadline - now).total_seconds() / 3600
        parts["deadline"] = -100.0 if hours <= 0 else (3.0 if hours > eco["estimated_minutes"] / 60 * 3 else -5.0)
    parts["risk"] = -4.0 * len(x["risks"])
    if automation_class in ("NOT_FEASIBLE", "PLATFORM_AUTOMATION_PROHIBITED", "AI_PROHIBITED"):
        parts["blocked"] = -40.0
    return round(max(0.0, min(100.0, sum(parts.values()))), 2), parts


def category8(a: dict[str, Any], cls: str, x: dict[str, Any], eco: dict[str, Any], settings: dict[str, Any],
              caps: dict[str, dict[str, Any]] | None = None) -> tuple[int, str]:
    """Map the automation decision onto the eight categories, with the one-line reason. Economics can only demote."""
    prof = permissions.profile(a.get("platform_id") or "", caps) or {}
    cr = x["country_restrictions"]
    if cls == "NOT_FEASIBLE":
        if prof.get("disabled_reason") or (isinstance(cr["allowed_countries"], list) and cr["allowed_countries"]) or cr["stated_in_task"]:
            cat = 6
        elif x["upfront_cost_flags"]:
            cat = 8
        elif any(h["class"] == "NOT_FEASIBLE" for h in x["hard_restrictions"]):
            cat = 5
        else:
            cat = 4
    else:
        cat = {"HUMAN_REQUIRED": 4, "PLATFORM_AUTOMATION_PROHIBITED": 5, "AI_PROHIBITED": 5, "ELIGIBILITY_UNVERIFIED": 7, "MANUAL_REVIEW_REQUIRED": 7}.get(cls, 0)
    if cls in EXECUTABLE:
        auto_sub = (prof.get("actions") or {}).get("auto_submission", {}).get("result") == "permitted"
        cat = (1 if cls != "AI_ASSISTED_WITH_PERMISSION" else 2) if auto_sub else 3
    if cat in (1, 2, 3, 7):
        value, hourly = eco.get("advertised"), eco.get("hourly_comparable_usd")
        if eco.get("currency") == "USD" and value is not None and value < float(settings.get("min_reward_usd") or 0):
            return 8, f"USD {value} is below your minimum reward (USD {settings['min_reward_usd']})"
        if hourly is not None and float(settings.get("min_hourly_rate_usd") or 0) > 0 and hourly < settings["min_hourly_rate_usd"]:
            return 8, f"About USD {hourly}/hour, below your minimum (USD {settings['min_hourly_rate_usd']})"
    return cat, CATEGORY8.get(cat, "Unclassified")


def analyse(conn, a: dict[str, Any], caps: dict[str, dict[str, Any]] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Classify, price and score one assignment. Pure with respect to the assignment row; ``store`` persists the result."""
    category = categorize(a.get("title") or "", a.get("instructions") or "")
    x = extract(a, caps)
    cls, reasons = classify_automation(a, category, x, caps)
    hist = history(conn, a.get("platform_id") or "")
    hist["similar"] = conn.execute("SELECT COUNT(*) FROM assignments WHERE platform_id=? AND title=? AND id<>?", (a.get("platform_id"), a.get("title"), a.get("id") or 0)).fetchone()[0]
    eco = economics(a, x, cls, category, hist)
    sc, parts = score(a, x, cls, eco, now)
    st = control.settings(conn) if conn is not None else dict(control.DEFAULTS)
    cat8, why8 = category8(a, cls, x, eco, st, caps)
    if a.get("platform_id") in st.get("preferred_platforms", []):
        parts["preferred_platform"] = 3.0
    if category in st.get("preferred_categories", []):
        parts["preferred_category"] = 3.0
    if cat8 == 7:
        parts["permission_unclear"] = -5.0
    if cat8 == 8:
        parts["not_worthwhile"] = -10.0
    sc = round(max(0.0, min(100.0, sum(parts.values()))), 2)
    x = {**x, "category8": cat8, "category8_label": CATEGORY8.get(cat8), "category8_reason": why8}
    return {"assignment_id": a.get("id"), "category": category, "worker": WORKER_FOR.get(category), "automation_class": cls, "reasons": reasons,
            "extracted": x, "economics": eco, "score": sc, "score_parts": parts}


def store(conn, result: dict[str, Any]) -> None:
    conn.execute("""INSERT INTO task_intel(assignment_id,category,automation_class,reasons_json,extracted_json,economics_json,score,classified_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(assignment_id) DO UPDATE SET category=excluded.category, automation_class=excluded.automation_class,
        reasons_json=excluded.reasons_json, extracted_json=excluded.extracted_json, economics_json=excluded.economics_json, score=excluded.score,
        classified_at=excluded.classified_at""",
                 (result["assignment_id"], result["category"], result["automation_class"], json.dumps(result["reasons"], ensure_ascii=False),
                  json.dumps({**result["extracted"], "worker": result["worker"], "score_parts": result["score_parts"]}, ensure_ascii=False),
                  json.dumps(result["economics"], ensure_ascii=False), result["score"], db.now_iso()))


def load(conn, assignment_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM task_intel WHERE assignment_id=?", (assignment_id,)).fetchone()
    if not r:
        return None
    return {"assignment_id": r["assignment_id"], "category": r["category"], "automation_class": r["automation_class"], "reasons": json.loads(r["reasons_json"]),
            "extracted": json.loads(r["extracted_json"]), "economics": json.loads(r["economics_json"]), "score": r["score"], "classified_at": r["classified_at"]}
