"""JSON API over the persistent backend. Pure functions of (method, path, query, body), so it is testable
without HTTP. The HTTP wiring (and its Host/Origin protection) lives in local_server.py.
"""
from __future__ import annotations

import base64
import shutil
import binascii
import json
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from microtask_monitor import (actions, assistant, autonomy, connections as cx, execution as ex, workers as exec_workers, daily, db, guidance, intelligence as ig, notify, packets, policy, profile_answers as pa,
                                   reminders, workflow as wf)
except ImportError:  # pragma: no cover
    import actions, assistant, autonomy, connections as cx, execution as ex, workers as exec_workers, daily, db, guidance, intelligence as ig, notify, packets, policy, profile_answers as pa, reminders, workflow as wf

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS_PATH = ROOT / "microtask_monitor" / "platforms.json"
MAX_LIMIT = 200
SORTS = {"score": "relevance_score DESC, last_seen_utc DESC", "date": "first_seen_utc DESC",
         "published": "published DESC", "pay": "CASE WHEN COALESCE(payment_usd_per_hour,0) > 150 THEN 1 ELSE 0 END, COALESCE(payment_usd_per_hour,0) DESC, relevance_score DESC",
         "title": "title COLLATE NOCASE ASC"}
LOCAL_STATUS_MAP = {"saved": "draft", "reviewing": "draft", "applied": "submitted", "assessment": "submitted",
                    "interview": "interview", "accepted": "accepted", "rejected": "rejected", "withdrawn": "withdrawn"}


SECRET_FIELD = re.compile(r"pass(word|wd)?$|^pwd$|cookie|otp|one_time|mfa_code|token|api_?key|secret|session_id|credential", re.I)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _int(value: Any, default: int, lo: int = 0, hi: int = 10**9) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _first(query: dict, key: str, default: str = "") -> str:
    v = query.get(key, default)
    return (v[0] if isinstance(v, list) else v) or default


class Api:
    def __init__(self, db_path: Path | str, latest_report_path: Path | None = None,
                 trigger_sync: Callable[[], bool] | None = None, evidence_dir: Path | None = None,
                 sync_running: Callable[[], bool] | None = None, launcher: Callable | None = None, profiles_dir: Path | None = None):
        self.db_path = Path(db_path)
        self.latest_report_path = latest_report_path
        self.trigger_sync = trigger_sync
        self.sync_running = sync_running or (lambda: False)
        self.evidence_dir = evidence_dir or wf.EVIDENCE_DIR
        self.exec_dir = (Path(evidence_dir).parent / "executions") if evidence_dir else ex.EXEC_DIR
        self.launcher = launcher
        self.profiles_dir = profiles_dir or ((Path(evidence_dir).parent / "browser-profiles") if evidence_dir else cx.PROFILES_DIR)

    # ----------------------------------------------------------------------------- dispatch
    def handle(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> tuple[int, Any]:
        query, body = query or {}, body or {}
        bad = [k for k in body if SECRET_FIELD.search(str(k))]
        if bad:                                   # credentials never travel through this API, not even to localhost
            return 400, {"ok": False, "error": "Never send passwords, codes, cookies or tokens to this program. Sign in yourself in the browser window; store API keys with connect.py."}
        try:
            with closing(db.connect(self.db_path)) as conn:
                return 200 if method == "GET" else 200, self._route(conn, method, path.rstrip("/") or "/", query, body)
        except ApiError as exc:
            return exc.status, {"ok": False, "error": exc.message}
        except wf.Blocked as exc:
            return 409, {"ok": False, "error": str(exc), "blocked": True}
        except policy.PermissionDenied as exc:
            return 403, {"ok": False, "error": str(exc), "blocked": True}
        except cx.SessionRequired as exc:
            return 409, {"ok": False, "error": str(exc), "needs_user_action": True}
        except (wf.WorkflowError, ex.ExecutionError, cx.ConnectionError_) as exc:
            return 400, {"ok": False, "error": str(exc)}

    def _route(self, conn, method: str, path: str, q: dict, body: dict) -> Any:
        parts = [p for p in path.split("/") if p][1:]            # drop "api"
        key = (method, parts[0] if parts else "")
        n = len(parts)
        if key == ("GET", "health"):
            return self.health(conn)
        if key == ("GET", "overview"):
            return self.overview(conn)
        if key == ("GET", "opportunities"):
            if n == 2 and parts[1] == "detail":
                return self.opportunity_detail(conn, _first(q, "url"))
            return self.list_opportunities(conn, q)
        if key == ("GET", "platforms"):
            return self.platforms(conn)
        if key == ("GET", "autonomy"):
            return self.autonomy_status()
        if parts[:1] == ["applications"]:
            return self.applications(conn, method, parts[1:], body)
        if method == "PUT" and parts[:1] == ["drafts"] and n == 2:
            return {"ok": True, "application": wf.edit_draft(conn, _int(parts[1], 0), str(body.get("body", "")))}
        if parts[:1] == ["tasks"]:
            return self.tasks(conn, method, parts[1:], body)
        if key == ("GET", "earnings"):
            return {"ok": True, **wf.list_earnings(conn)}
        if key == ("POST", "earnings"):
            return {"ok": True, "earning": wf.add_earning(conn, body.get("amount"), body.get("currency"), body.get("status", "expected"),
                                                          body.get("platform_id"), body.get("task_id"), str(body.get("description", "")),
                                                          body.get("expected_date"))}
        if key == ("POST", "payments"):
            return {"ok": True, "payment": wf.record_payment(conn, body.get("amount"), body.get("currency"), body.get("received_date"),
                                                             body.get("earning_id"), body.get("platform_id"), str(body.get("method", "")),
                                                             str(body.get("reference", "")))}
        if parts[:1] == ["daily"]:
            return self.daily_endpoints(conn, method, parts[1:], body)
        if parts[:1] == ["actions"]:
            return self.action_endpoints(conn, method, parts[1:], body)
        if parts[:1] == ["reminders"]:
            return self.reminder_endpoints(conn, method, parts[1:], body)
        if parts[:1] == ["connections"]:
            return self.connection_endpoints(conn, method, parts[1:], body)
        if parts[:1] == ["executions"]:
            return self.execution_endpoints(conn, method, parts[1:], body)
        if parts[:1] == ["profile-answers"]:
            return self.profile_endpoints(conn, method, parts[1:], body)
        if key == ("GET", "guidance") and n == 2:
            return {"ok": True, "guidance": guidance.for_platform(parts[1])}
        if parts[:1] == ["notifications"]:
            return self.notifications(conn, method, parts[1:], q, body)
        if key == ("POST", "sync"):
            if self.trigger_sync is None:
                raise ApiError(501, "Synchronization is not available in this process")
            started = self.trigger_sync()
            return {"ok": True, "started": bool(started), "message": "Sync started" if started else "A sync is already running"}
        if key == ("POST", "import-report"):
            return self.import_report_file(body)
        if key == ("GET", "export"):
            return self.export_all(conn)
        if key == ("POST", "import-local-storage"):
            return self.import_local_storage(conn, body)
        if key == ("POST", "assist") and n == 2:
            return self.assist(parts[1], body)
        if key == ("GET", "report"):
            return self.report()
        raise ApiError(404, f"No such endpoint: {method} {path}")

    # ----------------------------------------------------------------------------- reads
    def health(self, conn) -> dict[str, Any]:
        sync = db.get_state(conn, "sync_health", {}) or {}
        last = db.get_state(conn, "last_report", {}) or {}
        return {"ok": True, "database_version": db.user_version(conn), "sync": {**sync, "running": bool(self.sync_running())},
                "last_report": last, "opportunity_count": conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0],
                "capabilities": conn.execute("SELECT COUNT(*) FROM platform_capabilities").fetchone()[0],
                "explanation": assistant.explain_error(sync.get("last_error") or "") if sync.get("last_error") else None}

    def autonomy_status(self) -> dict[str, Any]:
        """Report whether any source is explicitly authorized for unattended earning."""
        result = autonomy.assess(policy.load_capabilities())
        return {"ok": True, **result}

    def overview(self, conn) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        classes = {r["eligibility_class"] or "UNCLASSIFIED": r["n"] for r in conn.execute(
            "SELECT eligibility_class, COUNT(*) n FROM opportunities WHERE duplicate_of IS NULL AND status='open' GROUP BY eligibility_class")}
        def one(sql, *a):
            return conn.execute(sql, a).fetchone()[0]
        sync = db.get_state(conn, "sync_health", {}) or {}
        last = db.get_state(conn, "last_report", {}) or {}
        return {"ok": True,
                "opportunities": {"total": one("SELECT COUNT(*) FROM opportunities WHERE duplicate_of IS NULL"),
                                  "open": one("SELECT COUNT(*) FROM opportunities WHERE duplicate_of IS NULL AND status='open'"),
                                  "by_class": classes,
                                  "kenya_confirmed": classes.get(ig.KENYA_CONFIRMED, 0),
                                  "requiring_verification": classes.get(ig.KENYA_UNVERIFIED, 0) + classes.get(ig.MANUAL_REVIEW_REQUIRED, 0),
                                  "new_last_24h": one("SELECT COUNT(*) FROM opportunities WHERE duplicate_of IS NULL AND first_seen_utc>=?",
                                                      (now - timedelta(hours=24)).isoformat())},
                "applications": {"in_progress": one("SELECT COUNT(*) FROM applications WHERE status IN ('draft','approved','submitted','interview')"),
                                 "total": one("SELECT COUNT(*) FROM applications")},
                "tasks": {"accepted": one("SELECT COUNT(*) FROM tasks WHERE status IN ('accepted','in_progress')"),
                          "completed": one("SELECT COUNT(*) FROM tasks WHERE status IN ('completed','submitted','approved','paid')")},
                "earnings": wf.earnings_summary(conn),
                "unread_notifications": one("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL"),
                "last_cloud_scan": last.get("checked_at_utc"), "last_local_sync": sync.get("last_success_utc"),
                "sync": {"consecutive_failures": sync.get("consecutive_failures", 0), "last_error": sync.get("last_error")}}

    def list_opportunities(self, conn, q: dict) -> dict[str, Any]:
        where, args = ["1=1"], []
        if not _first(q, "include_duplicates"):
            where.append("duplicate_of IS NULL")
        status = _first(q, "status", "open")
        if status != "all":
            where.append("status=?"); args.append(status)
        if _first(q, "platform"):
            if _first(q, "platform") == "_feeds":
                where.append("platform_id IS NULL")
            else:
                where.append("platform_id=?"); args.append(_first(q, "platform"))
        if _first(q, "eligibility"):
            classes = [c for c in _first(q, "eligibility").split(",") if c in ig.CLASSES]
            if not classes:
                raise ApiError(400, "Unknown eligibility class")
            where.append(f"eligibility_class IN ({','.join('?' * len(classes))})"); args += classes
        if _first(q, "q"):
            like = "%" + _first(q, "q").replace("%", "").replace("_", " ") + "%"
            where.append("(title LIKE ? OR summary LIKE ? OR source LIKE ?)"); args += [like] * 3
        if _first(q, "skill"):
            where.append("skills_json LIKE ?"); args.append('%"' + re.sub(r"[^a-z_]", "", _first(q, "skill").lower()) + '"%')
        if _first(q, "category"):
            where.append("category=?"); args.append(_first(q, "category"))
        if _first(q, "min_rate"):
            try:
                where.append("payment_usd_per_hour>=?"); args.append(float(_first(q, "min_rate")))
            except ValueError:
                raise ApiError(400, "min_rate must be a number")
        if _first(q, "payment") == "disclosed":
            where.append("payment_text IS NOT NULL AND payment_text<>'Not disclosed'")
        elif _first(q, "payment") == "undisclosed":
            where.append("(payment_text IS NULL OR payment_text='Not disclosed')")
        sort = SORTS.get(_first(q, "sort", "score"))
        if sort is None:
            raise ApiError(400, "Unknown sort")
        limit, offset = _int(_first(q, "limit"), 50, 1, MAX_LIMIT), _int(_first(q, "offset"), 0)
        clause = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) FROM opportunities WHERE {clause}", args).fetchone()[0]
        rows = conn.execute(f"SELECT url,title,source,summary,category,platform_id,eligibility_class,eligibility_confidence,"
                            f"eligibility_reason,status,deadline,payment_text,payment_usd_per_hour,skills_json,equipment,effort,"
                            f"relevance_score,priority,published,first_seen_utc,last_seen_utc,last_verified_utc,apply_url,duplicate_of,"
                            f"risk_label,risk_score,risk_flags_json,risk_evidence_json "
                            f"FROM opportunities WHERE {clause} ORDER BY {sort} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["skills"] = json.loads(d.pop("skills_json") or "[]")
            d["risk_flags"] = json.loads(d.pop("risk_flags_json") or "[]")
            d["risk_evidence"] = json.loads(d.pop("risk_evidence_json") or "[]")
            items.append(d)
        return {"ok": True, "total": total, "limit": limit, "offset": offset, "items": items}

    def opportunity_detail(self, conn, url: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM opportunities WHERE url=?", (ig.canonical_url(url),)).fetchone()
        if not row:
            raise ApiError(404, "Opportunity not found")
        d = dict(row)
        d["breakdown"] = json.loads(d.pop("score_breakdown_json") or "{}")
        d["requirements"] = json.loads(d.pop("requirements_json") or "{}")
        d["skills"] = json.loads(d.pop("skills_json") or "[]")
        d.pop("raw_json", None)
        d["changes"] = db.rows(conn.execute("SELECT changed_at,field,old_value,new_value FROM opportunity_changes WHERE url=? ORDER BY id DESC LIMIT 50", (d["url"],)))
        d["applications"] = db.rows(conn.execute("SELECT id,status FROM applications WHERE opportunity_url=?", (d["url"],)))
        return {"ok": True, "opportunity": d}

    def platforms(self, conn) -> dict[str, Any]:
        registry = json.loads(PLATFORMS_PATH.read_text(encoding="utf-8")).get("platforms", [])
        caps = {r["platform_id"]: json.loads(r["record_json"]) for r in conn.execute("SELECT * FROM platform_capabilities")} or policy.load_capabilities()
        integrations = db.get_state(conn, "integrations", {}) or {}
        monitors = db.get_state(conn, "eligibility_monitors", {}) or {}
        counts = {r["platform_id"]: dict(r) for r in conn.execute(
            "SELECT platform_id, COUNT(*) total, SUM(CASE WHEN eligibility_class='KENYA_CONFIRMED' THEN 1 ELSE 0 END) kenya_confirmed, "
            "SUM(CASE WHEN eligibility_class='WORLDWIDE_CONFIRMED' THEN 1 ELSE 0 END) worldwide "
            "FROM opportunities WHERE status='open' AND duplicate_of IS NULL AND platform_id IS NOT NULL GROUP BY platform_id")}
        out = []
        for p in registry:
            cap = caps.get(p["id"], {})
            integ = integrations.get(p["id"], {})
            out.append({"id": p["id"], "name": p["name"], "platform_type": p.get("platform_type"), "official_url": p.get("official_url"),
                        "kenya_status": cap.get("country_eligibility", {}).get("kenya", "unverified"),
                        "kenya_note": cap.get("country_eligibility", {}).get("note"), "levels": cap.get("levels", ["UNVERIFIED"]),
                        "capabilities": cap, "integration": {"status": integ.get("status") or (cap.get("levels") or ["UNVERIFIED"])[0],
                                                             "last_successful_scan": integ.get("last_successful_scan"), "error": integ.get("error"),
                                                             "message": integ.get("message")},
                        "eligibility_monitor": monitors.get(p["id"]), "opportunity_counts": counts.get(p["id"], {"total": 0}),
                        "required_manual_action": ("Everything on this platform is manual" if "MANUAL_ONLY" in cap.get("levels", []) else
                                                   "Applications, interviews and contracts are manual"),
                        "restrictions": cap.get("prohibited_automation", []), "equipment": cap.get("equipment")})
        return {"ok": True, "platforms": out}

    def report(self) -> dict[str, Any]:
        if not self.latest_report_path or not Path(self.latest_report_path).exists():
            raise ApiError(404, "No report has synced yet")
        return {"ok": True, **json.loads(Path(self.latest_report_path).read_text(encoding="utf-8"))}

    # ----------------------------------------------------------------------------- writes
    def applications(self, conn, method: str, rest: list[str], body: dict) -> dict[str, Any]:
        if not rest:
            if method == "GET":
                return {"ok": True, "applications": wf.list_applications(conn)}
            if method == "POST":
                return {"ok": True, "application": wf.create_application(conn, str(body.get("opportunity_url", "")))}
        elif method == "GET" and len(rest) == 1:
            return {"ok": True, "application": wf.get_application(conn, _int(rest[0], 0))}
        elif method == "GET" and len(rest) == 2 and rest[1] == "packet":
            app_id = _int(rest[0], 0)
            try:
                latest = packets.latest_packet(conn, app_id)       # always show the LIVE packet (profile/draft may have changed since the snapshot)
                fresh = packets.build_packet(conn, app_id)
                return {"ok": True, "packet": {**fresh, "text": packets.text_export(fresh)}, "saved": bool(latest),
                        "saved_version": latest["version"] if latest else None}
            except packets.PacketError as exc:
                raise ApiError(404, str(exc))
        elif method == "POST" and len(rest) == 2:
            app_id, action = _int(rest[0], 0), rest[1]
            if action == "packet":
                try:
                    return {"ok": True, "packet": packets.save_packet(conn, app_id), "saved": True}
                except packets.PacketError as exc:
                    raise ApiError(404, str(exc))
            if action == "events":
                return {"ok": True, "application": wf.log_event(conn, app_id, str(body.get("kind", "")), str(body.get("detail", "")), body.get("event_date"),
                                                                body.get("next_followup_at"), body.get("decision"))}
            if action == "regenerate":
                return {"ok": True, "application": wf.regenerate_draft(conn, app_id)}
            if action == "approve":
                return {"ok": True, "application": wf.approve_draft(conn, _int(body.get("draft_id"), 0), body.get("approved_by_user"))}
            if action == "submit":
                return {"ok": True, "application": wf.mark_submitted(conn, app_id, body.get("submitted_at"), body.get("confirmed_by_user") is True)}
            if action == "response":
                return {"ok": True, "application": wf.record_response(conn, app_id, str(body.get("status", "")), str(body.get("text", "")), body.get("response_at"))}
        raise ApiError(404, "No such applications endpoint")

    def tasks(self, conn, method: str, rest: list[str], body: dict) -> dict[str, Any]:
        if not rest:
            if method == "GET":
                tasks = wf.list_tasks(conn)
                return {"ok": True, "tasks": tasks, "plan": [t["id"] for t in assistant.plan_work(tasks)]}
            if method == "POST":
                return {"ok": True, "task": wf.create_task(conn, str(body.get("title", "")), str(body.get("instructions", "")),
                                                           body.get("application_id"), body.get("opportunity_url"), body.get("platform_id"), body.get("due_at"))}
        elif method == "GET" and len(rest) == 1:
            return {"ok": True, "task": wf.get_task(conn, _int(rest[0], 0))}
        elif method == "POST" and len(rest) >= 2:
            tid, action = _int(rest[0], 0), rest[1]
            if action == "status":
                return {"ok": True, "task": wf.set_task_status(conn, tid, str(body.get("status", "")))}
            if action == "checklist" and len(rest) == 3:
                return {"ok": True, "task": wf.toggle_item(conn, _int(rest[2], 0), bool(body.get("done", True)))}
            if action == "session" and len(rest) == 3 and rest[2] in ("start", "stop"):
                fn = wf.start_session if rest[2] == "start" else (lambda c, t: wf.stop_session(c, t, str(body.get("notes", ""))))
                return {"ok": True, "task": fn(conn, tid)}
            if action == "evidence":
                content = None
                if body.get("content_base64"):
                    try:
                        content = base64.b64decode(str(body["content_base64"]), validate=True)
                    except (binascii.Error, ValueError):
                        raise ApiError(400, "content_base64 is not valid base64")
                return {"ok": True, "task": wf.add_evidence(conn, tid, str(body.get("kind", "note")), str(body.get("description", "")),
                                                            body.get("file_name"), content, self.evidence_dir)}
            if action == "submission":
                return {"ok": True, "task": wf.record_submission(conn, tid, body.get("confirmed_by_user") is True, str(body.get("platform_reference", "")),
                                                                  str(body.get("notes", "")), body.get("submitted_at"))}
        raise ApiError(404, "No such tasks endpoint")

    def notifications(self, conn, method: str, rest: list[str], q: dict, body: dict) -> dict[str, Any]:
        if method == "GET" and not rest:
            return {"ok": True, "notifications": notify.list_notifications(conn, bool(_first(q, "unread")), _int(_first(q, "limit"), 100, 1, 500))}
        if method == "POST" and rest == ["read"]:
            ids = [i for i in (body.get("ids") or []) if isinstance(i, int)]
            with db.tx(conn):
                return {"ok": True, "marked": notify.mark_read(conn, ids or None)}
        raise ApiError(404, "No such notifications endpoint")

    def daily_endpoints(self, conn, method: str, rest: list[str], body: dict) -> dict[str, Any]:
        """DAILY EARNING OPPORTUNITIES: read the saved shortlist, run an analysis now, set a review status."""
        if method == "GET" and not rest:
            data = daily.load_shortlist(conn)
            return {"ok": True, **data, "cloud": db.get_state(conn, "cloud_daily", None),
                    "priority_order": daily.PLATFORM_PRIORITY, "review_statuses": list(db.REVIEW_STATUSES),
                    "schedule": "Runs after each sync that brings changes, and once a day from 08:00 East Africa Time; "
                                "the cloud scan also prepares a shortlist while this PC is off. GitHub may delay scheduled runs.",
                    "running": bool(conn.execute("SELECT 1 FROM analysis_runs WHERE status='running'").fetchone())}
        if method == "POST" and rest == ["run"]:
            now = datetime.now(timezone.utc)
            out = daily.run_analysis(conn, now, "manual", f"manual:{now.isoformat()}", force=True)
            return {"ok": True, "started": "stats" in out, **{k: out[k] for k in out if k != "stats"}, "stats": out.get("stats")}
        if method == "POST" and rest == ["review"]:
            try:
                return {"ok": True, **daily.set_review(conn, str(body.get("url", "")), str(body.get("status", "")), str(body.get("notes", "")))}
            except ValueError as exc:
                raise ApiError(400, str(exc))
            except LookupError as exc:
                raise ApiError(404, str(exc))
        raise ApiError(404, "No such daily endpoint")

    def connection_endpoints(self, conn, method: str, rest: list[str], body: dict) -> dict[str, Any]:
        """Connection status (no secrets ever). Signing in happens in a browser window you control."""
        if method == "GET" and not rest:
            return {"ok": True, "connections": cx.list_status(conn), "session_check_days": cx.SESSION_CHECK_DAYS,
                    "browser_found": bool(cx.find_browser()), "access_classes": list(policy.ACCESS_CLASSES)}
        if method == "POST" and len(rest) == 2:
            pid, action = rest
            if action == "open":
                kw = {"launcher": self.launcher, "browser": "test-browser"} if self.launcher else {}
                return {"ok": True, "connection": cx.open_login(conn, pid, profiles_dir=self.profiles_dir, **kw)}
            if action == "confirm":
                return {"ok": True, "connection": cx.confirm_signed_in(conn, pid, body.get("confirmed_by_user") is True)}
            if action == "human-step":
                return {"ok": True, "connection": cx.report_human_step(conn, pid, str(body.get("kind", "")))}
            if action == "expired":
                return {"ok": True, "connection": cx.report_expired(conn, pid)}
            if action == "disconnect":
                return {"ok": True, "connection": cx.disconnect(conn, pid)}
        raise ApiError(404, "No such connections endpoint")

    def execution_endpoints(self, conn, method: str, rest: list[str], body: dict) -> dict[str, Any]:
        """Execution Centre: jobs built from instructions YOU supply. Nothing here contacts a platform."""
        if method == "GET" and not rest:
            ex.recover_interrupted(conn) if not getattr(self, "_recovered", False) else 0
            self._recovered = True
            accepted = db.rows(conn.execute("""SELECT a.id, a.title, a.platform_id, a.apply_url, a.updated_at FROM applications a
                WHERE a.status='accepted' AND NOT EXISTS (SELECT 1 FROM execution_jobs j WHERE j.application_id=a.id)"""))
            caps = policy.load_capabilities()
            return {"ok": True, "jobs": ex.list_jobs(conn), "summary": ex.summary(conn), "qualified": accepted, "work_types": list(ex.WORK_TYPES),
                    "ai_tools": {k: bool(shutil.which(k)) for k in ("claude", "codex")},
                    "platform_rules": {pid: c.get("execution") for pid, c in caps.items() if c.get("execution")}}
        if method == "POST" and not rest:
            inputs = {}
            for f in (body.get("inputs") or [])[:10]:
                try:
                    inputs[str(f.get("name", "input"))] = base64.b64decode(str(f.get("content_base64", "")), validate=True)
                except (binascii.Error, ValueError, AttributeError):
                    raise ApiError(400, "An input file is not valid base64")
            job = ex.create_job(conn, str(body.get("title", "")), str(body.get("instructions", "")), body.get("platform_id") or None, body.get("application_id"),
                                body.get("deadline") or None, body.get("work_type") or None, body.get("ai_policy") or None, str(body.get("ai_source", "")),
                                inputs, self.exec_dir, "user_file" if body.get("from_file") else "user_provided")
            return {"ok": True, "job": job}
        jid = _int(rest[0], 0) if rest else 0
        if method == "GET" and len(rest) == 1:
            return {"ok": True, "job": ex.get_job(conn, jid)}
        if method == "GET" and len(rest) == 3 and rest[1] == "files":
            return {"ok": True, **ex.read_file(conn, jid, _int(rest[2], 0))}
        if method == "POST" and len(rest) == 2:
            action = rest[1]
            if action == "ai-permission":
                return {"ok": True, "job": ex.set_ai_permission(conn, jid, str(body.get("status", "")), str(body.get("source", "")))}
            if action == "start":
                return {"ok": True, "job": ex.start(conn, jid, body.get("approve_ai_run") is True, None, self.db_path, background=True)}
            if action == "pause":
                return {"ok": True, "job": ex.pause(conn, jid)}
            if action == "cancel":
                return {"ok": True, "job": ex.cancel(conn, jid)}
            if action == "approve":
                return {"ok": True, "job": ex.approve_review(conn, jid, body.get("approved_by_user") is True, body.get("acknowledged_manual") is True)}
            if action == "submitted":
                return {"ok": True, "job": ex.record_submitted(conn, jid, body.get("confirmed_by_user") is True, str(body.get("reference", "")), body.get("submitted_at"))}
            if action == "outcome":
                return {"ok": True, "job": ex.record_outcome(conn, jid, str(body.get("outcome", "")))}
        raise ApiError(404, "No such executions endpoint")

    def action_endpoints(self, conn, method: str, rest: list[str], body: dict) -> dict[str, Any]:
        """The daily action list (what to do next), persisted per Nairobi day."""
        if method == "GET" and not rest:
            items = actions.persist_today(conn)
            return {"ok": True, "date": daily.analysis_date(datetime.now(timezone.utc)), "items": items, "summary": actions.summary(items)}
        if method == "POST" and len(rest) == 2 and rest[1] == "status":
            try:
                return {"ok": True, "action": actions.set_status(conn, _int(rest[0], 0), str(body.get("status", "")))}
            except ValueError as exc:
                raise ApiError(400, str(exc))
            except LookupError as exc:
                raise ApiError(404, str(exc))
        raise ApiError(404, "No such actions endpoint")

    def reminder_endpoints(self, conn, method: str, rest: list[str], body: dict) -> dict[str, Any]:
        if method == "GET" and not rest:
            return {"ok": True, "reminders": reminders.list_reminders(conn)}
        if method == "POST" and rest == ["generate"]:
            with db.tx(conn):
                made = reminders.generate(conn)
            return {"ok": True, "created": made}
        if method == "POST" and len(rest) == 2 and rest[1] == "status":
            try:
                return {"ok": True, "reminder": reminders.mark(conn, _int(rest[0], 0), str(body.get("status", "")))}
            except ValueError as exc:
                raise ApiError(400, str(exc))
            except LookupError as exc:
                raise ApiError(404, str(exc))
        raise ApiError(404, "No such reminders endpoint")

    def profile_endpoints(self, conn, method: str, rest: list[str], body: dict) -> dict[str, Any]:
        if method == "GET" and not rest:
            with db.tx(conn):
                pa.ensure_defaults(conn)
            return {"ok": True, "answers": pa.list_answers(conn), "missing_required": [a["key"] for a in pa.missing_required(conn)],
                    "warning": "Write only true facts. Never enter passwords, one-time codes, ID numbers or payment details."}
        if method == "PUT" and len(rest) == 1:
            try:
                with db.tx(conn):
                    pa.ensure_defaults(conn)
                return {"ok": True, "answer": pa.set_answer(conn, rest[0], str(body.get("answer", "")))}
            except pa.AnswerError as exc:
                raise ApiError(400, str(exc))
        raise ApiError(404, "No such profile endpoint")

    def assist(self, kind: str, body: dict) -> dict[str, Any]:
        """Stateless local helpers (no platform contact): simplify instructions, explain an error."""
        if kind == "simplify":
            g = assistant.simplify_instructions(str(body.get("text", ""))[:20000])
            return {"ok": True, "guidance": g, "checklist": assistant.generate_checklist(g)}
        if kind == "explain-error":
            return {"ok": True, "explanation": assistant.explain_error(str(body.get("message", "")))}
        raise ApiError(404, "Unknown assistant function")

    def import_report_file(self, body: dict) -> dict[str, Any]:
        """Fallback when cloud sync is blocked: import a report JSON file you downloaded yourself (fully validated)."""
        import hashlib
        from microtask_monitor import sync_reports
        report = body.get("report")
        if not isinstance(report, dict):
            raise ApiError(400, "Body must contain a 'report' object")
        digest = hashlib.sha256(json.dumps(report, sort_keys=True, default=str).encode()).hexdigest()[:16]
        try:
            result = sync_reports.import_report(report, {"run_id": f"manual-{digest}", "artifact_id": "manual-import", "run_url": "",
                                                         "report_file": "manual import"}, self.db_path,
                                                self.latest_report_path or sync_reports.LATEST_REPORT)
        except ValueError as exc:
            raise ApiError(400, f"Not a valid Microtask Autopilot report: {exc}")
        return {"ok": True, **result}

    def export_all(self, conn) -> dict[str, Any]:
        """Everything you entered (no secrets exist in the database), for your own backups."""
        return {"ok": True, "exported_at": db.now_iso(), "applications": [wf.get_application(conn, a["id"]) for a in wf.list_applications(conn)],
                "tasks": wf.list_tasks(conn), **wf.list_earnings(conn)}

    def import_local_storage(self, conn, body: dict) -> dict[str, Any]:
        """One-time, idempotent import of records the old dashboard kept only in this browser's localStorage."""
        done = set(db.get_state(conn, "local_import_ids", []) or [])
        apps, earns = body.get("applications") or [], body.get("earnings") or []
        imported = {"applications": 0, "payments": 0, "skipped": 0}
        now = db.now_iso()
        with db.tx(conn):
            for a in apps if isinstance(apps, list) else []:
                lid = f"app:{a.get('id')}"
                title = str(a.get("title") or "").strip()
                if not a.get("id") or not title or lid in done:
                    imported["skipped"] += 1
                    continue
                status = LOCAL_STATUS_MAP.get(str(a.get("status", "")).strip().lower(), "draft")
                when = str(a.get("date") or "")[:10] or None
                url = a.get("url") if isinstance(a.get("url"), str) and a["url"].startswith("https://") else None
                conn.execute("INSERT INTO applications(opportunity_url,platform_id,title,apply_url,status,created_at,updated_at,submitted_at,notes) VALUES(?,?,?,?,?,?,?,?,?)",
                             (url, a.get("platform") or None, title[:200], url, status, now, now,
                              when if status != "draft" else None, ("Imported from browser storage. " + str(a.get("notes", "")))[:500]))
                done.add(lid); imported["applications"] += 1
            for e in earns if isinstance(earns, list) else []:
                lid = f"earn:{e.get('id')}"
                try:
                    amount = float(e.get("amount"))
                    day = datetime.fromisoformat(str(e.get("date"))[:10]).date().isoformat()
                except (TypeError, ValueError):
                    imported["skipped"] += 1
                    continue
                currency = str(e.get("currency") or "").upper()
                if not e.get("id") or lid in done or not amount > 0 or not re.fullmatch(r"[A-Z]{3}", currency):
                    imported["skipped"] += 1
                    continue
                conn.execute("INSERT INTO payments(platform_id,amount,currency,received_date,method,reference,created_at) VALUES(?,?,?,?,?,?,?)",
                             (e.get("platform") or None, amount, currency, day, "imported", str(e.get("notes", ""))[:120], now))
                done.add(lid); imported["payments"] += 1
            db.set_state(conn, "local_import_ids", sorted(done))
            db.audit(conn, "local_storage.import", "system", "", imported)
        return {"ok": True, **imported}
