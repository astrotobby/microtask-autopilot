"""Project estimation, pricing, scope and tailored proposals.

    estimate()   requirements -> tasks with dependencies, effort, risks, missing information, capability and profitability flags,
                 a price inside your configured boundaries, delivery time and included revisions. Never quotes blindly: a lead
                 with a blocking flag gets no proposal.
    scope()      acceptance criteria, assumptions and exclusions, so the project cannot grow without a priced scope change.
    propose()    a proposal written from THIS client's requirements, your genuine own projects (labelled as such), the estimate
                 and the scope. Mode A (automatic, only where the platform's terms permit it and you switched it on), mode B
                 (prepared for you to submit: the default), mode C (a reply to a client who contacted you, inside your rules).
Nothing here signs, accepts terms, negotiates outside your pricing or claims experience you have not stated.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any

try:
    from microtask_monitor import biz_config as bc, biz_leads as bl, db
except ImportError:  # pragma: no cover
    import biz_config as bc, biz_leads as bl, db

COMPLEXITY = {r"\b(auth|login|sign[- ]?up|oauth|jwt)\b": ("Authentication", 3),
              r"\b(stripe|checkout|payment (?:gateway|integration|processing)|accept (?:card )?payments|m-?pesa (?:api|integration|daraja)|billing system)\b": ("Payments integration", 4),
              r"\b(database|sql|postgres|sqlite|mongodb|schema)\b": ("Database design and migrations", 1), r"\b(deploy\w*|hosting|server|docker|ci/cd)\b": ("Deployment", 1.5),
              r"\b(admin panel|admin dashboard|admin area|(?:build|create|design) (?:a |an |the )?(?:\w+ )?dashboard|dashboard (?:ui|views?|pages?|screens?))\b": ("Dashboard or admin views", 3), r"\b(api|integration|webhook)s?\b": ("API integration", 1.5),
              r"\b(scrap\w+|crawl\w*)\b": ("Data collection from a website", 2), r"\b(llm|openai|gpt|claude|ai agent|chatbot)\b": ("AI/LLM step with prompts and fallbacks", 2.5),
              r"\b(responsive|mobile[- ]friendly)\b": ("Responsive layout", 1), r"\b(video|render|captions?|clips?)\b": ("Video processing and exports", 2),
              r"\b(csv|excel|spreadsheet|data cleaning)\b": ("Data processing script", 1)}
COMMERCIAL = re.compile(r"\b(pay(?:ment|ments|ing|s)? (?:by|on|via|in|upon|after|through|terms)|budget|deposit|invoice|milestones? payment|paid (?:by|on|via|weekly|monthly))\b", re.I)
ACCESS_RISK = re.compile(r"\b(hack\w*|crack\w*|bypass\w*|someone else'?s account|their account without|access (?:to )?(?:an?|the) (?:account|system) (?:of|without)|"
                         r"recover (?:a|my) (?:password|account)|ddos|spam (?:bot|tool)|fake (?:reviews?|accounts?|followers?)|captcha solv\w*)\b", re.I)
EXCLUSIONS = ["Hosting, domain, licence or API subscription costs (paid by you, the client, directly)",
              "Work not listed in the scope (changes are quoted separately before they start)",
              "Your production credentials: you enter them yourself; placeholders are used during development",
              "Legal, compliance or security certification"]


def _days_until(deadline: str | None) -> int | None:
    if not deadline:
        return None
    m = re.match(r"(\d{1,2})\s*(day|week)", deadline, re.I)
    if m:
        return int(m.group(1)) * (7 if m.group(2).lower().startswith("week") else 1)
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return (datetime.strptime(deadline, fmt).date() - date.today()).days
        except ValueError:
            continue
    return None


def estimate(lead: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or bc.load()
    pr, lines = cfg["pricing"], cfg["service_lines"]
    line = lead.get("service_line")
    text = f"{lead['title']}\n{lead.get('description') or ''}"
    work = " ".join(s for s in re.split(r"(?<=[.!?\n])\s+", text) if not COMMERCIAL.search(s))      # effort comes from the work, not the deal terms
    reqs = lead.get("requirements") or []
    unit = lines.get(line, {}).get("hours_per_unit", 5)
    tasks: list[dict[str, Any]] = [{"id": 1, "title": "Requirements review and technical plan", "hours": 0.5, "depends_on": [], "worker": "research"}]
    for label_rx, (label, h) in COMPLEXITY.items():
        if re.search(label_rx, work, re.I):
            tasks.append({"id": len(tasks) + 1, "title": label, "hours": float(h), "depends_on": [1], "worker": lines.get(line, {}).get("worker", "software")})
    tasks.sort(key=lambda t: t["id"])
    worker = lines.get(line, {}).get("worker", "software")
    tasks.insert(1, {"id": 2, "title": "Core implementation" + (f": {reqs[0][:100]}" if reqs else ""), "hours": float(unit), "depends_on": [1], "worker": worker})
    for r in reqs[1:6]:
        tasks.append({"id": 0, "title": "Implement: " + r[:120], "hours": 0.75, "depends_on": [1], "worker": worker})
    for i, t in enumerate(tasks, 1):
        t["id"] = i
    build_ids = [t["id"] for t in tasks[1:]]
    build_hours = sum(t["hours"] for t in tasks[1:])
    tasks.append({"id": len(tasks) + 1, "title": "Testing and independent QA against the acceptance criteria", "hours": round(max(1.0, 0.25 * build_hours), 1), "depends_on": build_ids, "worker": "qa"})
    tasks.append({"id": len(tasks) + 1, "title": "Handover documentation and delivery package", "hours": 0.5, "depends_on": [len(tasks)], "worker": "research"})
    work_hours = sum(t["hours"] for t in tasks)
    hours = round(work_hours * (1 + pr["risk_buffer"]) + pr["unpaid_overhead_hours"], 1)
    risks, missing, flags = [], [], []
    if re.search(r"\bapi|integration|webhook", text, re.I):
        risks.append("Third-party API behaviour and rate limits: tested against sandbox or recorded responses")
    if re.search(r"\bpayments?|stripe|m-?pesa", text, re.I):
        risks.append("Payments must be tested in the provider's test mode only")
    if len(reqs) < 2:
        risks.append("Requirements are thin: the scope below states assumptions to confirm")
    if not (lead.get("budget_max")):
        missing.append("Budget")
    if not lead.get("deadline"):
        missing.append("Deadline")
    if not re.search(r"acceptance|done when|success criteria|should be able to", text, re.I):
        missing.append("How you will judge the work complete (acceptance criteria): proposed below for confirmation")
    if re.search(r"\b(existing|current) (?:code|app|site|repo|system)", text, re.I):
        missing.append("Read access to the existing code repository (a collaborator invite, never a password)")
    if re.search(r"\bapi|integration", text, re.I):
        missing.append("Which accounts/APIs are involved and whether sandbox keys exist")
    unsupported = sorted(set(lead.get("technologies") or []) & bl.UNSUPPORTED_TECH)
    if unsupported:
        flags.append({"code": "beyond_capability", "blocking": True, "why": "Needs " + ", ".join(unsupported) + ", outside your stated skills"})
    if not line:
        flags.append({"code": "beyond_capability", "blocking": True, "why": "Not one of your service lines"})
    if ACCESS_RISK.search(text):
        flags.append({"code": "unauthorised_access_or_prohibited", "blocking": True, "why": f"Asks for something you must not do: '{ACCESS_RISK.search(text).group(0)}'"})
    if bl.SUSPICIOUS.search(text):
        flags.append({"code": "suspicious_client", "blocking": True, "why": "Warning signs: " + bl.SUSPICIOUS.search(text).group(0)})
    per_day = cfg["capacity"]["hours_per_week"] / 6
    delivery_days = int(math.ceil(hours / per_day)) + 1
    left = _days_until(lead.get("deadline"))
    rush = False
    if left is not None:
        if left < max(1, math.ceil(delivery_days * 0.6)):
            flags.append({"code": "impossible_deadline", "blocking": True, "why": f"{left} day(s) available for about {delivery_days} day(s) of work"})
        elif left < delivery_days:
            rush = True
            risks.append(f"Tight deadline ({left} days): rush pricing applies and other work waits")
            delivery_days = left
    if not lead.get("payment_terms") and lead.get("source_id") in ("hn_seeking_freelancer", "email_enquiry", "website_enquiry"):
        flags.append({"code": "unclear_payment", "blocking": False, "why": "Direct client: payment terms are proposed (deposit before work, balance on delivery)"})
    base = hours * pr["target_hourly_usd"] * (pr["rush_multiplier"] if rush else 1.0)
    price = max(pr["min_project_usd"], round(base / 5) * 5)
    budget = lead.get("budget_max") if (lead.get("currency") or "USD") == "USD" else None
    if lead.get("budget_type") == "hourly" and budget:
        effective = budget
        price = round(budget * hours, 2)
    elif budget:
        effective = round(budget / hours, 2)
        if budget >= price:
            price = round(min(budget, price * 1.15) / 5) * 5
        else:
            flags.append({"code": "budget_below_price", "blocking": effective < pr["min_effective_hourly_usd"],
                          "why": f"Client budget USD {budget:g} is below your price USD {price:g} (about USD {effective}/h on {hours} h)"})
    else:
        effective = round(price / hours, 2)
    eff_at_price = round(price / hours, 2)
    if eff_at_price < pr["min_effective_hourly_usd"]:
        flags.append({"code": "unprofitable", "blocking": True, "why": f"About USD {eff_at_price}/h, below your minimum USD {pr['min_effective_hourly_usd']}/h"})
    if price > pr["max_project_usd_without_you"]:
        flags.append({"code": "needs_your_decision", "blocking": False, "why": f"USD {price:g} is above the value you let the engine quote alone (USD {pr['max_project_usd_without_you']})"})
    return {"tasks": tasks, "risks": risks, "missing": missing, "flags": flags, "hours": hours, "price": float(price), "currency": "USD",
            "delivery_days": delivery_days, "revisions": int(pr["revisions_included"]), "effective_hourly": eff_at_price, "client_budget_hourly": effective,
            "feasible": not any(f["blocking"] for f in flags), "deposit_percent": pr["deposit_percent"]}


def save_estimate(conn, lead_id: int, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    lead = bl.get(conn, lead_id)
    e = estimate(lead, cfg)
    with db.tx(conn):
        eid = conn.execute("""INSERT INTO biz_estimates(lead_id,tasks_json,risks_json,missing_json,flags_json,hours,price,currency,delivery_days,revisions,
                              effective_hourly,feasible,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (lead_id, json.dumps(e["tasks"]), json.dumps(e["risks"]), json.dumps(e["missing"]), json.dumps(e["flags"]), e["hours"], e["price"],
                            e["currency"], e["delivery_days"], e["revisions"], e["effective_hourly"], int(e["feasible"]), db.now_iso())).lastrowid
        bc.audit(conn, "estimate.create", "lead", lead_id, {"estimate_id": eid, "price": e["price"], "hours": e["hours"], "feasible": e["feasible"]},
                 permission="pricing boundaries in business configuration")
    return {"id": eid, **e}


def scope(lead: dict[str, Any], est: dict[str, Any]) -> dict[str, Any]:
    reqs = lead.get("requirements") or []
    criteria = [f"{r.rstrip('.')}: demonstrated and checked" for r in reqs[:8]] or [f"The deliverable for '{lead['title']}' works as described in the brief, on the agreed sample inputs"]
    criteria += ["Automated tests (or a documented check list for no-code work) pass", "A handover document explains setup, configuration and how to run it"]
    return {"deliverables": [t["title"].replace("Implement: ", "") for t in est["tasks"] if t["worker"] not in ("research", "qa")],
            "acceptance_criteria": criteria, "assumptions": ["Requirements as written in the brief; anything unclear is confirmed in writing before work starts"] +
            [f"To confirm: {m}" for m in est["missing"]], "exclusions": EXCLUSIONS, "revisions_included": est["revisions"],
            "change_control": "Requests outside this scope get a written change estimate (price and days) and start only after you approve it"}


# ------------------------------------------------------------------------------------------- proposals
def mode_for(lead: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    src = bc.source(lead["source_id"])
    auto = src["policy"].get("automated_proposals")
    if src["kind"] == "inbound":
        return "C", "The client contacted you: a reply inside your configured pricing and rules"
    if auto == "allowed" and cfg["autonomy"].get("send_proposals_where_permitted"):
        return "A", f"{src['name']} permits automated applications ({src['policy'].get('evidence_url')}) and you switched it on"
    return "B", f"{src['name']}: automated applications are {auto.replace('_', ' ')}. The proposal is prepared for you to submit"


def write(lead: dict[str, Any], est: dict[str, Any], sc: dict[str, Any], cfg: dict[str, Any]) -> str:
    prof = cfg["profile"]
    who = (prof.get("contact") or {}).get("name") or cfg["business_name"]
    line = lead.get("service_line")
    shown = [t for t in (lead.get("technologies") or []) if t not in ("api", "rest", "node")]
    techs = ", ".join(shown) or "the stack you are using"
    reqs = lead.get("requirements") or []
    understood = ("\n".join(f"- {r.rstrip('.')}" for r in reqs[:6]) if reqs else f"- {lead['title']}: {bc.norm(lead.get('description'))[:300]}")
    rel = [p for p in cfg["portfolio"] if line in p.get("service_lines", [])][:2]
    portfolio = "\n".join(f"- {p['title']} ({p['kind']}): {p['summary']}" + (f" {p['url']}" if p.get("url") else "") for p in rel) or "- (no directly comparable project yet: happy to do a small paid first milestone)"
    plan = "\n".join(f"{i}. {t['title']} (~{t['hours']:g} h)" for i, t in enumerate(est["tasks"], 1))
    greet = f"Hi {lead['client_name']}," if lead.get("client_name") and not re.search(r"\b(inc|llc|ltd|gmbh)\b", lead["client_name"], re.I) else "Hello,"
    dep = est["deposit_percent"]
    terms = f"{dep}% to start, the balance on delivery" if lead.get("source_id") in ("hn_seeking_freelancer", "email_enquiry", "website_enquiry") else "through the platform's own payment protection"
    questions = "\n".join(f"- {m}" for m in est["missing"]) or "- None: the brief is clear."
    return f"""{greet}

Thanks for posting "{lead['title']}". Here is what I understand you need:
{understood}

How I would approach it
I would build this with {techs}, working in an isolated copy, with automated tests for each piece and an independent review before anything reaches you.
{plan}

What you receive
""" + "\n".join(f"- {d}" for d in sc["deliverables"]) + f"""
- Tests and a handover document (setup, configuration, how to run and extend it)

Relevant work (my own projects, not client engagements)
{portfolio}

Timeline and price
About {est['delivery_days']} working days after we confirm the scope. Fixed price: USD {est['price']:,.0f}, {terms}. It includes {est['revisions']} revision rounds; anything outside the scope is quoted before it starts.

Assumptions and exclusions
""" + "\n".join(f"- {a}" for a in sc["assumptions"][:4]) + "\n" + "\n".join(f"- Not included: {x}" for x in sc["exclusions"][:3]) + f"""

Questions before I start
{questions}

If this fits, reply with any corrections to the scope and I will confirm the plan the same day.

{who}"""


EXPERIENCE = re.compile(r"\b(\d+\+?\s*years?|experience (?:with|in|of)|you (?:should|must) have|proven|track record|personally (?:shipped|built)|"
                        r"previously (?:built|shipped)|portfolio of|degree|certif\w+|fluent|native)\b", re.I)


def write_application(lead: dict[str, Any], cfg: dict[str, Any]) -> str:
    """For an hourly contract role (e.g. AI-training or coding-task work on a platform): an honest application, not a fixed quote."""
    prof = cfg["profile"]
    who = (prof.get("contact") or {}).get("name") or cfg["business_name"]
    line = lead.get("service_line")
    reqs = lead.get("requirements") or []
    mine = [s for s in prof.get("skills", []) if s.lower() in (lead["title"] + " " + (lead.get("description") or "")).lower()]
    rel = [p for p in cfg["portfolio"] if line in p.get("service_lines", [])][:2]
    text = bc.norm(lead.get("description"))
    checks = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if EXPERIENCE.search(s)][:6]
    body = f"""Hello,

I am applying for "{lead['title']}".

What the role asks for, and how I match it
""" + ("\n".join(f"- {r.rstrip('.')}" for r in reqs[:5]) or "- " + bc.norm(lead.get("description"))[:300]) + f"""

Skills I can show for this: {", ".join(mine) or ", ".join(prof.get("skills", [])[:8])}.

Recent work (my own projects, not client engagements)
""" + ("\n".join(f"- {p['title']}: {p['summary']}" + (f" {p['url']}" if p.get("url") else "") for p in rel) or "- (no directly comparable project yet)") + f"""

Availability: about {cfg['capacity']['hours_per_week']} hours a week, remote from {prof.get('country') or 'Kenya'}. The listed rate (USD {lead['budget_max']:g}/{'hour' if lead.get('budget_type') == 'hourly' else 'month'}) works for me.
I am happy to complete any assessment the role requires.

{who}"""
    if checks:
        body = ("BEFORE YOU SUBMIT (delete this block): confirm you genuinely meet each requirement below; remove any line you cannot back up.\n"
                + "\n".join(f"  [ ] {c[:200]}" for c in checks) + "\n\n" + body)
    return body


def propose(conn, lead_id: int, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estimate, scope and write one proposal for a qualified lead. Idempotent: the same lead and estimate never get two proposals."""
    cfg = cfg or bc.load()
    lead = bl.get(conn, lead_id)
    if lead["status"] not in ("qualified", "proposal_prepared"):
        raise bc.BizError(f"Lead #{lead_id} is {lead['status']}: only qualified leads get proposals ({lead['status_reason']})")
    if lead.get("budget_type") in ("hourly", "monthly") and lead.get("budget_max"):
        return _apply_hourly(conn, lead, cfg)
    est = save_estimate(conn, lead_id, cfg)
    if not est["feasible"]:
        why = "; ".join(f["why"] for f in est["flags"] if f["blocking"])
        bl.set_status(conn, lead_id, "rejected", "estimate: " + why)
        return {"created": False, "reason": why, "estimate": est}
    sc = scope(lead, est)
    mode, mode_why = mode_for(lead, cfg)
    body = write(lead, est, sc, cfg)
    key = hashlib.sha256(f"{lead_id}|{est['price']}|{est['hours']}|{len(est['tasks'])}".encode()).hexdigest()[:32]
    old = conn.execute("SELECT * FROM biz_proposals WHERE idem_key=?", (key,)).fetchone()
    if old:
        return {"created": False, "reason": "identical proposal already exists", "proposal": dict(old)}
    now = db.now_iso()
    with db.tx(conn):
        pid = conn.execute("""INSERT INTO biz_proposals(lead_id,estimate_id,mode,body,price,currency,status,idem_key,created_at,updated_at)
                              VALUES(?,?,?,?,?,?,'prepared',?,?,?)""", (lead_id, est["id"], mode, body, est["price"], "USD", key, now, now)).lastrowid
        conn.execute("UPDATE biz_leads SET status='proposal_prepared', status_reason=?, updated_at=? WHERE id=?", (f"proposal #{pid} (mode {mode})", now, lead_id))
        bc.audit(conn, "proposal.prepare", "proposal", pid, {"lead": lead_id, "mode": mode, "price": est["price"]}, permission=mode_why)
    return {"created": True, "proposal_id": pid, "mode": mode, "mode_reason": mode_why, "body": body, "estimate": est, "scope": sc}


def _apply_hourly(conn, lead: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    rate = float(lead["budget_max"])
    monthly = lead.get("budget_type") == "monthly"
    floor = cfg["pricing"]["min_effective_hourly_usd"] * (cfg["capacity"]["hours_per_week"] * 4.33 if monthly else 1)
    if rate < floor:
        why = f"USD {rate:g}/{'month' if monthly else 'hour'} is below your minimum (USD {floor:,.0f})"
        bl.set_status(conn, lead["id"], "rejected", "estimate: " + why)
        return {"created": False, "reason": why}
    mode, mode_why = mode_for(lead, cfg)
    body = write_application(lead, cfg)
    key = hashlib.sha256(f"{lead['id']}|hourly|{rate}".encode()).hexdigest()[:32]
    old = conn.execute("SELECT * FROM biz_proposals WHERE idem_key=?", (key,)).fetchone()
    if old:
        return {"created": False, "reason": "identical proposal already exists", "proposal": dict(old)}
    now = db.now_iso()
    with db.tx(conn):
        # currency 'USD/h' or 'USD/mo' marks a rate-based application: it is never added to fixed-price pipeline totals
        pid = conn.execute("""INSERT INTO biz_proposals(lead_id,estimate_id,mode,body,price,currency,status,idem_key,created_at,updated_at)
                              VALUES(?,NULL,?,?,?,?,'prepared',?,?,?)""", (lead["id"], mode, body, rate, "USD/mo" if monthly else "USD/h", key, now, now)).lastrowid
        conn.execute("UPDATE biz_leads SET status='proposal_prepared', status_reason=?, updated_at=? WHERE id=?", (f"application #{pid} (mode {mode}, hourly)", now, lead["id"]))
        bc.audit(conn, "proposal.prepare", "proposal", pid, {"lead": lead["id"], "mode": mode, "hourly_rate": rate}, permission=mode_why)
    return {"created": True, "proposal_id": pid, "mode": mode, "mode_reason": mode_why, "body": body,
            "kind": "monthly_application" if monthly else "hourly_application", "rate": rate}


def mark_sent(conn, proposal_id: int, evidence: str, by_user: bool = True) -> dict[str, Any]:
    """You submitted it yourself (mode B), or the engine sent it (A/C). Evidence is required: where it was sent."""
    if not bc.norm(evidence):
        raise bc.BizError("Say where the proposal was submitted (e.g. 'Freelancer.com bid on project 123')")
    p = conn.execute("SELECT * FROM biz_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not p:
        raise bc.BizError("Proposal not found")
    if p["status"] != "prepared":
        return {"changed": False, "status": p["status"]}
    with db.tx(conn):
        conn.execute("UPDATE biz_proposals SET status='sent', sent_at=?, sent_evidence=?, updated_at=? WHERE id=?", (db.now_iso(), evidence[:300], db.now_iso(), proposal_id))
        conn.execute("UPDATE biz_leads SET status='proposal_sent', updated_at=? WHERE id=?", (db.now_iso(), p["lead_id"]))
        bc.audit(conn, "proposal.sent", "proposal", proposal_id, {"evidence": evidence}, actor="user" if by_user else "engine")
    return {"changed": True, "status": "sent"}


def record_reply(conn, proposal_id: int, text: str, outcome: str) -> dict[str, Any]:
    """A client's reply: 'replied' (question/negotiation), 'accepted' (they want to go ahead) or 'declined'."""
    if outcome not in ("replied", "accepted", "declined"):
        raise bc.BizError("outcome must be replied, accepted or declined")
    p = conn.execute("SELECT * FROM biz_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not p:
        raise bc.BizError("Proposal not found")
    lead_status = {"replied": "replied", "accepted": "won", "declined": "lost"}[outcome]
    with db.tx(conn):
        conn.execute("UPDATE biz_proposals SET status=?, reply_at=?, reply_text=?, updated_at=? WHERE id=?", (outcome, db.now_iso(), text[:4000], db.now_iso(), proposal_id))
        conn.execute("UPDATE biz_leads SET status=?, updated_at=? WHERE id=?", (lead_status, db.now_iso(), p["lead_id"]))
        lead = conn.execute("SELECT client_id FROM biz_leads WHERE id=?", (p["lead_id"],)).fetchone()
        conn.execute("""INSERT OR IGNORE INTO biz_communications(client_id,lead_id,direction,kind,channel,subject,body,status,idem_key,at)
                        VALUES(?,?,'in','proposal_reply','recorded',?,?,'received',?,?)""",
                     (lead["client_id"], p["lead_id"], f"Reply to proposal #{proposal_id}", text[:4000],
                      hashlib.sha256(f"reply|{proposal_id}|{text}".encode()).hexdigest()[:32], db.now_iso()))
        bc.audit(conn, "proposal.reply", "proposal", proposal_id, {"outcome": outcome}, actor="user")
    return {"status": outcome, "lead_status": lead_status}
