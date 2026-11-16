"""The standard PlatformAdapter interface, one instance per registered platform (all 25), without a special case in the
orchestrator for any of them.

Every method returns a structured result: {"supported": bool, "ok": bool, "reason": str, ...}. A method a platform does
not support (or does not permit) says so with the reason; nothing pretends. Every operation first passes
capabilities.check (platform rule, assignment rule, your pre-authorization, your limits).

Where the work is done:
    discovery        discovery.py adapters (OpenTrain public API, Whop authorized API, policy monitor, manual/email import)
    classification   lifecycle.check_eligibility + lifecycle.classify (task_intel)
    execution        orchestrator.dispatch (specialist workers in isolated workspaces)
    verification     qa_gate.verdict (independent of the worker)
    submission       submission.preflight / submission.submit (separate engine, idempotent; human-required by default)
    earnings         ledger (recorded money only) and, where authorized, the platform's own API
To add a platform: register a subclass in ADAPTERS (or rely on the generic adapter); nothing else changes.
"""
from __future__ import annotations

from typing import Any

try:
    from microtask_monitor import (capabilities as capmod, connections as cx, db, discovery, ledger, lifecycle, orchestrator, permissions, policy,
                                   qa_gate, submission, task_intel)
except ImportError:  # pragma: no cover
    import capabilities as capmod, connections as cx, db, discovery, ledger, lifecycle, orchestrator, permissions, policy, qa_gate, submission, task_intel

OUTCOMES = {"A": "Automatic delivery authorised", "B": "Execution authorised, submission restricted (package ready for you to submit)",
            "C": "Task execution prohibited or impossible"}


def result(supported: bool, ok: bool = False, reason: str = "", **data: Any) -> dict[str, Any]:
    return {"supported": supported, "ok": ok and supported, "reason": reason, **data}


class PlatformAdapter:
    platform_id = ""
    discovery_channel = "manual_import"

    def __init__(self, conn, platform_id: str | None = None, store: Any = None):
        self.conn, self.store = conn, store
        if platform_id:
            self.platform_id = platform_id
        if not permissions.known(self.platform_id):
            raise policy.PermissionDenied(f"Blocked: no capability record for '{self.platform_id}'")

    # ---------------------------------------------------------------- facts
    def _discovery_adapter(self) -> discovery.Adapter:
        return discovery.adapter_for(self.platform_id)

    def get_platform_info(self) -> dict[str, Any]:
        reg = discovery.registry().get(self.platform_id, {})
        prof = capmod.profile(self.platform_id)
        ad = self._discovery_adapter()
        h = discovery.health(self.conn).get(self.platform_id, {})
        return result(True, True, "", platform_id=self.platform_id, name=reg.get("name", self.platform_id), official_url=reg.get("official_url"),
                      platform_type=reg.get("platform_type"), registered=True, identity=ad.verify_platform_identity(), discovery_channel=ad.mode,
                      last_scan=h.get("last_run"), last_success=h.get("last_success"), adapter_status=h.get("status", "never_run"), adapter_error=h.get("error"),
                      capabilities=prof["capabilities"], reasons=prof["reasons"], permission_evidence=prof["permission_evidence"],
                      submission_mode=prof["submission_mode"], level=prof["level"])

    def check_connection(self) -> dict[str, Any]:
        st = cx.connection_state(self.conn, self.platform_id)
        return result(True, st["session"] == "CONNECTED", st.get("required_action") or "", state=st["state"], session=st["session"],
                      automation=st["automation"], last_success_at=st.get("last_success_at"), verified_by=st.get("verified_by"))

    # ---------------------------------------------------------------- discovery
    def discover_opportunities(self) -> dict[str, Any]:
        ad = self._discovery_adapter()
        if ad.mode == "manual_import":
            return result(False, reason="No automated discovery is permitted or available: import task details yourself (or from notification emails)",
                          channel="manual_import")
        out = discovery.run(self.conn, [ad], force=True)["adapters"].get(self.platform_id, {})
        return result(True, out.get("status") in ("ok", "ok_skipped"), out.get("message") or out.get("error") or "", channel=ad.mode,
                      status=out.get("status"), stats=out.get("stats"))

    def retrieve_opportunity(self, opportunity_id: Any) -> dict[str, Any]:
        r = self.conn.execute("SELECT id FROM assignments WHERE platform_id=? AND (external_id=? OR id=?)", (self.platform_id, str(opportunity_id), opportunity_id)).fetchone()
        if r:
            return result(True, True, "", kind="assignment", record=lifecycle.view(self.conn, r["id"]))
        o = self.conn.execute("SELECT url,title,status,eligibility_class,payment_text,deadline,relevance_score FROM opportunities WHERE platform_id=? AND (external_id=? OR url=?)",
                              (self.platform_id, str(opportunity_id), str(opportunity_id))).fetchone()
        if o:
            return result(True, True, "", kind="listing", record=dict(o))
        return result(True, False, "Not found among this platform's recorded opportunities")

    # ---------------------------------------------------------------- intelligence
    def classify_opportunity(self, assignment_id: int) -> dict[str, Any]:
        lifecycle.check_eligibility(self.conn, assignment_id)
        v = lifecycle.classify(self.conn, assignment_id)
        intel = v.get("intel") or {}
        return result(True, True, "", state=v["state"], automation_class=intel.get("automation_class"), category8=intel.get("extracted", {}).get("category8"),
                      reasons=intel.get("reasons"), score=intel.get("score"))

    def prepare_task(self, assignment_id: int, inputs: dict[str, bytes] | None = None, exec_dir: Any = None) -> dict[str, Any]:
        gate = capmod.check(self.conn, self.platform_id, "task_preparation")
        if not gate["allowed"]:
            return result(True, False, "; ".join(gate["reasons"]), checks=gate["checks"])
        if inputs:
            orchestrator.stage_inputs(self.conn, assignment_id, inputs, exec_dir)
        v = lifecycle.classify(self.conn, assignment_id)
        return result(True, True, "Prepared locally; nothing was sent to the platform", state=v["state"])

    # ---------------------------------------------------------------- execution, verification
    def _task(self, assignment_id: int) -> dict[str, Any]:
        a = lifecycle.view(self.conn, assignment_id)
        cls = (a.get("intel") or {}).get("automation_class")
        return {**a, "category": (a.get("intel") or {}).get("category"), "needs_ai": cls != "DETERMINISTIC_LOCAL_PROCESSING"}

    def execute_task(self, assignment_id: int, runner: Any = None, db_path: Any = None, exec_dir: Any = None, probe: dict | None = None) -> dict[str, Any]:
        a = self._task(assignment_id)
        gate = capmod.check(self.conn, self.platform_id, "ai_execution", a)
        if not gate["allowed"]:
            return result(True, False, "; ".join(gate["reasons"]), checks=gate["checks"], outcome="C" if not gate["checks"].get("platform") or not gate["checks"].get("assignment") else None)
        out = orchestrator.dispatch(self.conn, assignment_id, runner, db_path, exec_dir, probe=probe)
        return result(True, bool(out.get("started")), "; ".join(out.get("reasons") or []), checks=gate["checks"], run=out)

    def verify_result(self, assignment_id: int) -> dict[str, Any]:
        a = lifecycle.view(self.conn, assignment_id)
        if not a.get("execution_job_id"):
            return result(True, False, "No execution job exists for this assignment")
        r = qa_gate.verdict(self.conn, a["execution_job_id"])
        return result(True, r["status"] == qa_gate.PASS, r["summary"], verdict=r["status"], checks=r["checks"])

    # ---------------------------------------------------------------- submission, earnings
    def prepare_submission(self, assignment_id: int) -> dict[str, Any]:
        """Outcome A, B or C with the exact reasons. Never clicks anything."""
        a = self._task(assignment_id)
        intel = a.get("intel") or {}
        cls = intel.get("automation_class")
        if cls not in task_intel.EXECUTABLE or a.get("hold") == "policy_blocked":
            why = intel.get("reasons") or [a.get("hold_reason") or ("not classified yet" if not cls else cls)]
            return result(True, False, "; ".join(why), outcome="C", outcome_label=OUTCOMES["C"], automation_class=cls,
                          note="The engine may not execute this task, so nothing is prepared for submission. Do it yourself where the platform allows.")
        pre = submission.preflight(self.conn, assignment_id)
        gate = capmod.check(self.conn, self.platform_id, "automatic_submission", a)
        if pre["ok"] and gate["allowed"]:
            return result(True, True, "Every submission condition passed", outcome="A", outcome_label=OUTCOMES["A"], checks=pre["checks"])
        ready = a["stage"] == "ready_for_delivery"
        return result(True, ready, "Package ready: submit it yourself, then record the reference" if ready else "Not packaged yet",
                      outcome="B", outcome_label=OUTCOMES["B"], blocked_by=pre["blocked_by"] + [r for r in gate["reasons"] if r not in pre["blocked_by"]],
                      checks=pre["checks"])

    def submit_result(self, assignment_id: int) -> dict[str, Any]:
        prep = self.prepare_submission(assignment_id)
        if prep.get("outcome") != "A":
            return result(True, False, "Automatic submission is not permitted here: " + (prep["reason"] or ""), outcome=prep.get("outcome"),
                          blocked_by=prep.get("blocked_by"))
        out = submission.submit(self.conn, assignment_id)
        return result(True, out.get("submitted") is True, out.get("reason") or "", outcome="A", submission=out)

    def retrieve_earnings(self) -> dict[str, Any]:
        rows = [r for r in ledger.by_platform(self.conn) if r["platform_id"] == self.platform_id]
        prof = capmod.profile(self.platform_id)
        return result(True, True, "Recorded payments only; advertised or pending rewards are never counted as earned", received=rows,
                      api_monitoring=prof["capabilities"]["earnings_monitoring"])


class WhopAdapter(PlatformAdapter):
    platform_id = "whop_workforce"

    def retrieve_earnings(self) -> dict[str, Any]:
        base = super().retrieve_earnings()
        subs = db.rows(self.conn.execute("SELECT status, COUNT(*) n FROM whop_submissions GROUP BY status"))
        return {**base, "submission_outcomes_from_api": subs, "reason": base["reason"] + ". Submission outcomes are read from Whop's API; withdrawals stay with you."}


ADAPTERS: dict[str, type[PlatformAdapter]] = {"whop_workforce": WhopAdapter}


def adapter(conn, platform_id: str, store: Any = None) -> PlatformAdapter:
    return ADAPTERS.get(platform_id, PlatformAdapter)(conn, platform_id, store)


def all_adapters(conn, store: Any = None) -> list[PlatformAdapter]:
    return [adapter(conn, pid, store) for pid in policy.load_capabilities()]


def control_centre(conn) -> list[dict[str, Any]]:
    """The Platform Control Centre: every registered platform, from real records only."""
    counts = {r["platform_id"]: r["n"] for r in conn.execute("SELECT platform_id, COUNT(*) n FROM opportunities WHERE status='open' AND duplicate_of IS NULL AND platform_id IS NOT NULL GROUP BY platform_id")}
    tasks = {r["platform_id"]: r["n"] for r in conn.execute("SELECT platform_id, COUNT(*) n FROM assignments GROUP BY platform_id")}
    out = []
    for ad in all_adapters(conn):
        info, connection = ad.get_platform_info(), ad.check_connection()
        out.append({k: info[k] for k in ("platform_id", "name", "official_url", "platform_type", "registered", "discovery_channel", "last_scan", "last_success",
                                         "adapter_status", "adapter_error", "capabilities", "reasons", "submission_mode", "level", "permission_evidence")}
                   | {"connection": connection["state"], "session": connection["session"], "automation": connection["automation"],
                      "opportunities": counts.get(ad.platform_id, 0) + tasks.get(ad.platform_id, 0)})
    return out
