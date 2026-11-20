"""JSON endpoints of the autonomous engine (everything under /api/engine). Called from api.Api, which owns the
connection, the credential-field guard and error mapping. Inputs are validated strictly; nothing here accepts a secret.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import (assignments as asg, connections as cx, control, db, discovery, execution as ex, ledger, lifecycle, orchestrator,
                                   permissions, policy, qa_gate, scheduler, submission, task_intel)
except ImportError:  # pragma: no cover
    import assignments as asg, connections as cx, control, db, discovery, execution as ex, ledger, lifecycle, orchestrator, permissions, policy, qa_gate, scheduler, submission, task_intel


class EngineApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise EngineApiError(400, "An id must be a number")


def _q(q: dict, key: str) -> str:
    v = q.get(key, "")
    return ((v[0] if isinstance(v, list) else v) or "").strip()


def _files(body: dict) -> dict[str, bytes]:
    out = {}
    for f in (body.get("inputs") or [])[:10]:
        try:
            out[str(f.get("name", "input"))] = base64.b64decode(str(f.get("content_base64", "")), validate=True)
        except (binascii.Error, ValueError, AttributeError):
            raise EngineApiError(400, "An input file is not valid base64")
    return out


# ------------------------------------------------------------------------------------------- views
def executive(conn) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    def one(sql: str, *a: Any) -> int:
        return conn.execute(sql, a).fetchone()[0]
    states = cx.list_states(conn)
    health = discovery.health(conn)
    matrix = permissions.matrix(conn)
    day = (now - timedelta(hours=24)).isoformat()
    active = [m["platform_id"] for m in matrix if m["level"] >= 1 and not m["suspended_reason"] and health.get(m["platform_id"], {}).get("status") in ("ok", "ok_skipped")]   # ran successfully; a platform awaiting your authorization is not active
    totals = ledger.totals(conn)
    return {"registered_platforms": len(matrix), "connected_accounts": sum(1 for s in states if s["session"] == "CONNECTED"),
            "connected_api_verified": sum(1 for s in states if s["status"] == "connected_verified"),
            "active_integrations": len(active), "active_integration_ids": active,
            "automation_levels": {str(n): sum(1 for m in matrix if m["level"] == n) for n in range(6)},
            "opportunities_found_today": one("SELECT COUNT(*) FROM opportunities WHERE duplicate_of IS NULL AND first_seen_utc>=?", day) + one("SELECT COUNT(*) FROM assignments WHERE created_at>=?", day),
            "kenya_eligible_opportunities": one("SELECT COUNT(*) FROM opportunities WHERE duplicate_of IS NULL AND status='open' AND eligibility_class IN ('KENYA_CONFIRMED','WORLDWIDE_CONFIRMED')"),
            "qualified_opportunities": one("SELECT COUNT(*) FROM assignments WHERE stage IN ('qualified','ready','accepted') AND hold IS NULL"),
            "tasks_executing": one("SELECT COUNT(*) FROM execution_jobs WHERE status='running'"),
            "tasks_awaiting_qa": one("SELECT COUNT(*) FROM execution_jobs WHERE status IN ('awaiting_review','qa_failed')"),
            "submissions_awaiting_review": one("SELECT COUNT(*) FROM assignments WHERE stage IN ('submitted','awaiting_review')"),
            "accepted_assignments": one("SELECT COUNT(*) FROM assignments WHERE stage IN ('work_accepted','payment_pending','paid')"),
            "pending_payments": totals["payment_pending"], "verified_earnings": totals["money_received"],
            "human_required": one("SELECT COUNT(*) FROM assignments WHERE hold='human_required'"), "policy_blocked": one("SELECT COUNT(*) FROM assignments WHERE hold='policy_blocked'"),
            "control": {k: v for k, v in control.view(conn).items() if k in ("kill_switch", "may_run", "blocked_reason", "resources")},
            "scheduler": {k: v for k, v in scheduler.status(conn).items() if k in ("alive", "heartbeat")},
            "note": "Registered platforms are not the same as automated integrations: each platform's real capability is listed in the Platform Manager."}


def platform_manager(conn) -> list[dict[str, Any]]:
    reg, caps, health = discovery.registry(), policy.load_capabilities(), discovery.health(conn)
    states = {s["platform_id"]: s for s in cx.list_states(conn)}
    counts = {r["platform_id"]: r["n"] for r in conn.execute("SELECT platform_id, COUNT(*) n FROM opportunities WHERE status='open' AND duplicate_of IS NULL AND platform_id IS NOT NULL GROUP BY platform_id")}
    tasks = {r["platform_id"]: r["n"] for r in conn.execute("SELECT platform_id, COUNT(*) n FROM assignments GROUP BY platform_id")}
    payouts = {p["platform_id"]: p for p in ledger.payout_compatibility(conn)}
    rows = []
    for m in permissions.matrix(conn):
        pid = m["platform_id"]
        p, cap, st, h = reg.get(pid, {}), caps[pid], states[pid], health.get(pid, {})
        ad = discovery.adapter_for(pid)
        identity = ad.verify_platform_identity()
        manual = []
        if not identity["verified"]:
            manual.append("Confirm the exact service and its official URL")
        if m["suspended_reason"]:
            manual.append("Re-read the current terms, then lift the suspension")
        if m["level"] == 0 and not m["disabled_reason"]:
            manual.append("Use it by hand: import task details you copied from your own account")
        if st["required_action"] and st["session"] not in ("NOT_CONNECTED", "DISABLED"):
            manual.append(st["required_action"])
        if h.get("status") == "needs_you":
            manual.append(h.get("message") or "Authorize the official API yourself")
        rows.append({"platform_id": pid, "name": m["name"], "platform_type": p.get("platform_type"), "official_url": p.get("official_url"), "identity": identity,
                     "connection": {k: st[k] for k in ("state", "session", "automation", "last_success_at", "verified_by", "login_url")},
                     "eligibility": {"kenya": (cap.get("country_eligibility") or {}).get("kenya", "unverified"), "note": (cap.get("country_eligibility") or {}).get("note")},
                     "payment": {"general": payouts[pid]["general_availability"], "note": payouts[pid]["general_note"], "account_verified": payouts[pid]["account_verified"],
                                 "methods_mentioned": payouts[pid]["methods_mentioned"]},
                     "automation": {"level": m["level"], "label": m["level_label"], "actions": m["actions"], "suspended_reason": m["suspended_reason"],
                                    "disabled_reason": m["disabled_reason"], "official_api": m["official_api"], "ai_use": m["ai_use"], "reviewed_on": m["reviewed_on"]},
                     "discovery": {"mode": ad.mode, "last_scan": h.get("last_run"), "last_success": h.get("last_success"), "status": h.get("status", "never_run"),
                                   "message": h.get("message"), "error": h.get("error")},
                     "opportunities": counts.get(pid, 0), "tasks": tasks.get(pid, 0), "account_requirements": cap.get("access_note") or cap.get("notes"),
                     "equipment": (cap.get("equipment") or {}).get("requirements"), "required_manual_action": manual})
    return rows


def pipeline(conn, q: dict) -> dict[str, Any]:
    """The opportunity explorer for tasks: every assignment with its state, classification, economics and score, filterable."""
    items = lifecycle.list_view(conn)
    f = {k: _q(q, k) for k in ("platform", "eligibility", "category", "automation", "state", "deadline_before", "min_score", "min_compensation", "max_risks")}
    def keep(a: dict[str, Any]) -> bool:
        intel = a.get("intel") or {}
        eco, x = intel.get("economics") or {}, intel.get("extracted") or {}
        try:
            if f["platform"] and a["platform_id"] != f["platform"]:
                return False
            if f["eligibility"] and lifecycle.eligibility(a)["status"] != f["eligibility"]:
                return False
            if f["category"] and intel.get("category") != f["category"]:
                return False
            if f["automation"] and intel.get("automation_class") != f["automation"]:
                return False
            if f["state"] and a["state"] != f["state"]:
                return False
            if f["deadline_before"] and not (a.get("deadline") and a["deadline"] <= f["deadline_before"]):
                return False
            if f["min_score"] and (intel.get("score") or 0) < float(f["min_score"]):
                return False
            if f["min_compensation"] and (eco.get("advertised") or 0) < float(f["min_compensation"]):
                return False
            if f["max_risks"] and len(x.get("risks") or []) > int(f["max_risks"]):
                return False
        except ValueError:
            raise EngineApiError(400, "A numeric filter is not a number")
        return True
    out = [a for a in items if keep(a)]
    out.sort(key=lambda a: -((a.get("intel") or {}).get("score") or 0))
    return {"items": out, "total": len(out), "states": list(lifecycle.STATES), "automation_classes": list(task_intel.AUTOMATION_CLASSES), "categories": list(control.CATEGORIES)}


def execution_centre(conn) -> dict[str, Any]:
    jobs = ex.list_jobs(conn)
    by = lambda *s: [j for j in jobs if j["status"] in s]                               # noqa: E731
    qa = []
    for j in by("awaiting_review", "qa_failed", "ready_for_submission")[:30]:
        v = qa_gate.verdict(conn, j["id"], write_report=False)
        qa.append({"job_id": j["id"], "title": j["title"], "status": v["status"], "summary": v["summary"], "checks": v["checks"]})
    logs = db.rows(conn.execute("SELECT e.job_id, e.at, e.kind, e.message FROM execution_events e ORDER BY e.id DESC LIMIT 60"))
    return {**orchestrator.overview(conn), "ready_jobs": by("ready", "created", "blocked"), "running_jobs": by("running"),
            "completed_jobs": by("ready_for_submission", "submitted", "accepted", "rejected"), "failed_jobs": by("failed", "qa_failed"),
            "paused_jobs": by("paused", "interrupted"), "qa_results": qa, "execution_logs": logs}


def onboarding(conn) -> list[dict[str, Any]]:
    """What to know before connecting each platform. Display only: opening a sign-in window is a separate, deliberate action of yours."""
    out = []
    for r in platform_manager(conn):
        act = r["automation"]["actions"]
        steps = []
        if not r["identity"]["verified"]:
            steps.append("First confirm which service this is and its official address; nothing is opened until then.")
        elif r["automation"]["disabled_reason"]:
            steps.append("Do not sign up: " + r["automation"]["disabled_reason"] + ". Never use a VPN or a false location.")
        else:
            steps += ["Open the official sign-in window from the Connections tab.", "Sign in yourself; complete any CAPTCHA, code or identity check personally.",
                      "Press \"I'm signed in\" so the connection is recorded (no password or cookie is ever read)."]
            if act["account_monitoring"]["result"] == "permitted":
                steps.append("Authorize the platform's documented API (read-only) so listings can be read for you.")
            else:
                steps.append("Copy task details from your account into Import tasks; they are classified and tracked locally.")
        out.append({"platform_id": r["platform_id"], "name": r["name"], "official_url": r["official_url"], "kenya": r["eligibility"], "account_requirements": r["account_requirements"],
                    "equipment": r["equipment"], "payout": r["payment"], "automation_level": r["automation"]["level"], "automation_label": r["automation"]["label"],
                    "official_api": r["automation"]["official_api"], "connection_state": r["connection"]["state"], "enabled_operations": [k for k, v in act.items() if v["result"] == "permitted"],
                    "steps": steps})
    return out


# ------------------------------------------------------------------------------------------- milestone 1 views
def opportunity_intelligence(conn, limit: int = 200) -> dict[str, Any]:
    """Section B: every recorded opportunity in its 8-way category, ranked, with the reason and public state."""
    rows = []
    for r in conn.execute("SELECT a.id FROM assignments a LEFT JOIN task_intel t ON t.assignment_id=a.id ORDER BY COALESCE(t.score,0) DESC, a.updated_at DESC LIMIT ?", (limit,)):
        a = lifecycle.view(conn, r["id"])
        intel = a.get("intel") or {}
        x, eco = intel.get("extracted") or {}, intel.get("economics") or {}
        job = conn.execute("SELECT status FROM execution_jobs WHERE id=?", (a.get("execution_job_id"),)).fetchone() if a.get("execution_job_id") else None
        rows.append({"assignment_id": a["id"], "platform_id": a["platform_id"], "title": a["title"], "url": a.get("source_url"), "reward": a.get("reward_gross"),
                     "currency": a.get("currency"), "deadline": a.get("deadline"), "category": intel.get("category"), "automation_class": intel.get("automation_class"),
                     "category8": x.get("category8"), "category8_label": x.get("category8_label"), "category8_reason": x.get("category8_reason"),
                     "reasons": intel.get("reasons") or [], "score": intel.get("score"), "hourly_usd": eco.get("hourly_comparable_usd"),
                     "state": lifecycle.spec_state(a, job["status"] if job else None)})
    counts = {k: 0 for k in task_intel.CATEGORY8}
    for r in rows:
        if r["category8"]:
            counts[r["category8"]] += 1
    return {"opportunities": rows, "category_counts": [{"category": k, "label": task_intel.CATEGORY8[k], "count": counts[k]} for k in counts],
            "states": list(lifecycle.SPEC_STATES)}


def system_health(conn) -> dict[str, Any]:
    """Section F: is everything that should be running, running, and what failed last."""
    try:
        from microtask_monitor import email_discovery
    except ImportError:  # pragma: no cover
        import email_discovery
    sch = scheduler.status(conn)
    failing = [{"job": j["name"], "failures": j.get("consecutive_failures"), "error": j.get("last_error")} for j in sch["jobs"] if j.get("consecutive_failures")]
    disc = discovery.health(conn)
    return {"scheduler_alive": sch["alive"], "heartbeat": sch["heartbeat"], "jobs": sch["jobs"], "failing_jobs": failing,
            "discovery": [{"platform_id": k, **v} for k, v in disc.items()], "email_discovery": email_discovery.health(conn),
            "connections": cx.list_states(conn), "resources": control.resources(conn), "stopped": control.kill_state(conn),
            "paused": control.settings(conn)["paused"], "database_version": db.user_version(conn),
            "open_alerts": conn.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]}


# ------------------------------------------------------------------------------------------- routing
def route(api, conn, method: str, rest: list[str], q: dict, body: dict) -> dict[str, Any]:
    n = len(rest)
    head = rest[0] if rest else ""
    yes = body.get("confirmed_by_user") is True
    if method == "GET":
        if n == 0:
            return {"ok": True, **executive(conn)}
        if head == "platforms":
            return {"ok": True, "platforms": platform_manager(conn), "levels": permissions.LEVELS}
        if head == "permissions":
            return {"ok": True, "matrix": permissions.matrix(conn), "levels": permissions.LEVELS, "actions": permissions.ACTIONS,
                    "never_automated": [{"code": c, "label": lbl} for c, lbl, _, _ in permissions.HARD_RESTRICTIONS]}
        if head == "connections":
            return {"ok": True, "connections": cx.list_states(conn), "states": list(cx.CONNECTION_STATES)}
        if head == "onboarding":
            return {"ok": True, "platforms": onboarding(conn)}
        if head == "pipeline":
            return {"ok": True, **pipeline(conn, q)}
        if head == "execution":
            return {"ok": True, **execution_centre(conn)}
        if head == "jobs" and n == 3 and rest[2] == "qa":
            return {"ok": True, "report": qa_gate.verdict(conn, _int(rest[1]))}
        if head == "submissions":
            return {"ok": True, **submission.manager(conn)}
        if head == "ledger":
            return {"ok": True, **ledger.view(conn), "overview": ledger.earnings_overview(conn)}
        if head == "capabilities":
            from microtask_monitor import capabilities as capmod, platform_adapters as pa
            return {"ok": True, "platforms": pa.control_centre(conn), "capabilities": list(capmod.CAPABILITIES), "states": list(capmod.STATES),
                    "outcomes": pa.OUTCOMES}
        if head == "opportunities":
            return {"ok": True, **opportunity_intelligence(conn)}
        if head == "health":
            return {"ok": True, **system_health(conn)}
        if head == "earnings":
            return {"ok": True, **ledger.earnings_overview(conn)}
        if head == "tasks" and n == 3 and rest[2] == "submission":
            from microtask_monitor import platform_adapters as pa
            a = lifecycle.view(conn, _int(rest[1]))
            return {"ok": True, "submission": pa.adapter(conn, a["platform_id"]).prepare_submission(a["id"])}
        if head == "control":
            return {"ok": True, **control.view(conn)}
        if head == "scheduler":
            return {"ok": True, **scheduler.status(conn)}
        if head == "discovery":
            return {"ok": True, "health": discovery.health(conn), "adapters": [a.identify_platform() for a in discovery.default_adapters()]}
        if head == "tasks" and n == 2:
            return {"ok": True, "assignment": lifecycle.view(conn, _int(rest[1]))}
        if head == "outreach":
            from microtask_monitor import outreach
            try:
                ocfg, oerr = outreach.load_config(), None
            except outreach.OutreachError as exc:
                ocfg, oerr = None, str(exc)
            data = outreach.view(conn, ocfg)
            if ocfg and ocfg.get("templates_path"):
                try:
                    data["templates_text"] = Path(ocfg["templates_path"]).read_text(encoding="utf-8")
                except OSError as exc:
                    data["templates_text"], oerr = None, f"Templates file not readable: {exc}"
            return {"ok": True, **data, "config_error": oerr}
        if head == "owner":
            from microtask_monitor import shorts_service
            try:
                cfg = shorts_service.load_config()
            except shorts_service.ServiceError as exc:
                cfg, err = None, str(exc)
            else:
                err = None
            return {"ok": True, **shorts_service.view(conn, cfg, api.store), "config_error": err}
    if method == "PUT" and rest == ["settings"]:
        return {"ok": True, "settings": control.update_settings(conn, body)}
    if method == "POST" and rest == ["email", "import"]:
        from microtask_monitor import email_discovery as ed
        return {"ok": True, "result": ed.import_messages(conn, ed.EmlFolder().fetch(), "eml_folder")}
    if method == "POST":
        if rest == ["stop"]:
            return {"ok": True, "kill_switch": control.engage_stop(conn, str(body.get("reason") or "Stopped by you from the dashboard"))}
        if rest == ["resume"]:
            return {"ok": True, "kill_switch": control.release_stop(conn, yes)}
        if rest == ["tick"]:
            return {"ok": True, **orchestrator.tick(conn, None, api.db_path, api.exec_dir, background=True)}
        if rest == ["discovery", "run"]:
            result = discovery.run(conn, discovery.default_adapters(api.store), force=body.get("force") is True)
            return {"ok": True, **result, "intake": orchestrator.intake(conn)}
        if rest == ["tasks"]:
            pid = str(body.get("platform_id") or "")
            if not permissions.known(pid):
                raise EngineApiError(400, "Choose a registered platform, or 'direct_client' for your own client's work")
            rows = body.get("tasks")
            if not isinstance(rows, list) or not rows or len(rows) > 100:
                raise EngineApiError(400, "Send 1-100 task records in 'tasks'")
            result = discovery.ManualImportAdapter(pid).import_records(conn, rows)
            files = _files(body)
            if files and len(result["assignment_ids"]) == 1:
                orchestrator.stage_inputs(conn, result["assignment_ids"][0], files, api.exec_dir)
                lifecycle.classify(conn, result["assignment_ids"][0])
            return {"ok": True, **result, "assignments": [lifecycle.view(conn, i) for i in result["assignment_ids"]]}
        if head == "tasks" and n == 3:
            aid, action = _int(rest[1]), rest[2]
            if action == "classify":
                lifecycle.check_eligibility(conn, aid)
                return {"ok": True, "assignment": lifecycle.classify(conn, aid)}
            if action == "confirm-eligibility":
                lifecycle.confirm_eligibility(conn, aid, yes, str(body.get("reference", "")))
                return {"ok": True, "assignment": lifecycle.classify(conn, aid)}
            if action == "inputs":
                saved = orchestrator.stage_inputs(conn, aid, _files(body), api.exec_dir)
                return {"ok": True, "saved": saved, "assignment": lifecycle.classify(conn, aid)}
            if action == "pause":
                return {"ok": True, "assignment": lifecycle.pause(conn, aid)}
            if action == "resume":
                return {"ok": True, "assignment": lifecycle.resume(conn, aid)}
            if action == "authorize":
                return {"ok": True, **orchestrator.authorize(conn, lifecycle.view(conn, aid))}
        if head == "permissions" and n == 3:
            if rest[2] == "suspend":
                permissions.suspend(conn, rest[1], str(body.get("reason") or "Suspended by you"), actor="user")
            elif rest[2] == "review":
                permissions.clear_suspension(conn, rest[1], body.get("reviewed_by_user") is True, str(body.get("note", "")))
            else:
                raise EngineApiError(404, "No such permissions endpoint")
            return {"ok": True, "matrix": permissions.matrix(conn)}
        if head == "submissions" and n == 3 and rest[2] == "submit":
            return {"ok": True, **submission.submit(conn, _int(rest[1]))}
        if head == "attempts" and n == 3 and rest[2] == "reconcile":
            return {"ok": True, "attempt": submission.reconcile(conn, _int(rest[1]), None, yes, str(body.get("reference", "")), body.get("not_submitted") is True)}
        if rest == ["owner", "run"]:
            from microtask_monitor import clip_worker, seller, shorts_service
            try:
                cfg = shorts_service.load_config()
                media = clip_worker.Media(Path(cfg["clipper_dir"]), cfg.get("caption_font", "Arial Black"), cfg.get("whisper_model", "small"))
                return {"ok": True, **shorts_service.run(conn, seller.Seller(cfg["account_id"], api.store), media, cfg, budget_seconds=120)}
            except (shorts_service.ServiceError, seller.SellerError, clip_worker.ClipError) as exc:
                raise EngineApiError(409, str(exc))
        if head == "outreach":
            from microtask_monitor import outreach
            try:
                ocfg = outreach.load_config()
                if rest == ["outreach", "approve"]:
                    return {"ok": True, "sha256": outreach.approve_templates(conn, Path(ocfg["templates_path"]).read_text(encoding="utf-8"), yes)}
                if rest == ["outreach", "import-legacy"]:
                    return {"ok": True, **outreach.import_legacy(conn, Path(ocfg["legacy_leads_path"]))}
                if n == 4 and rest[1] == "leads" and rest[3] == "release":
                    return {"ok": True, "lead": outreach.release(conn, _int(rest[2]), yes)}
            except (outreach.OutreachError, OSError, KeyError, ValueError) as exc:
                raise EngineApiError(400, str(exc))
        if rest == ["payout-methods"]:
            return {"ok": True, "methods": ledger.set_owned_methods(conn, body.get("methods"), yes)}
        if rest == ["payout-routes"]:
            return {"ok": True, "route": ledger.set_route(conn, str(body.get("platform_id", "")), str(body.get("method", "")), body.get("account_verified") is True,
                                                           str(body.get("evidence", "")), yes)}
        if head == "scheduler" and n == 3 and rest[2] == "enable":
            return {"ok": True, **scheduler.enable(conn, rest[1], yes)}
    raise EngineApiError(404, "No such engine endpoint")
