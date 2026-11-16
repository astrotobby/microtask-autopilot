"""Adapter-based opportunity discovery.

Each adapter implements only what its platform has been verified to permit, and the engine asks the permission
engine before every run. There is no universal browser bot:

* ``OpenTrainAdapter``    documented public jobs API (level 1), read-only, rate limited, cached.
* ``WhopAdapter``         documented API with your own OAuth authorization (level 2), read-only.
* ``PolicyMonitorAdapter`` reports the published-eligibility monitor that the cloud scan runs (Mindrift).
* ``ManualImportAdapter`` every other platform: no network access at all. It only accepts task records you provide.

Results are normalized, de-duplicated by (platform, external id) and stored through ``intelligence.ingest`` or as
assignments. Failures degrade one adapter, never the engine; repeated failures open a circuit breaker for that adapter.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from microtask_monitor import assignments as asg, connections as cx, db, intelligence as ig, lifecycle, opportunity_common as oc, permissions, policy
except ImportError:  # pragma: no cover
    import assignments as asg, connections as cx, db, intelligence as ig, lifecycle, opportunity_common as oc, permissions, policy

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS_PATH = ROOT / "microtask_monitor" / "platforms.json"
BREAKER_FAILURES = 4                 # consecutive failures before an adapter is stopped automatically
BREAKER_HOURS = 6
FRESH_CLOUD_REPORT_MINUTES = 90      # the hourly cloud scan already read the public API: do not read it twice
AUTO_TRACK_LIMIT = 25


def registry() -> dict[str, dict[str, Any]]:
    try:
        return {p["id"]: p for p in json.loads(PLATFORMS_PATH.read_text(encoding="utf-8"))["platforms"]}
    except (OSError, ValueError, KeyError):
        return {}


class Adapter:
    """What every adapter can do. Discovery methods exist only on adapters whose platform permits them."""
    platform_id = ""
    mode = "manual_import"           # public_api | authorized_api | policy_monitor | manual_import
    action: str | None = None        # the permission the adapter needs before it may run (None = it never contacts the platform)
    min_interval_seconds = 3600

    def __init__(self, platform_id: str | None = None):
        if platform_id:
            self.platform_id = platform_id

    def identify_platform(self) -> dict[str, Any]:
        p = registry().get(self.platform_id, {})
        return {"platform_id": self.platform_id, "name": p.get("name", self.platform_id), "official_url": p.get("official_url"),
                "platform_type": p.get("platform_type"), "mode": self.mode}

    def verify_platform_identity(self) -> dict[str, Any]:
        """Identity comes from the reviewed registry: an https official URL and a recorded identity status. Never from a guess."""
        p = registry().get(self.platform_id)
        if not p:
            return {"verified": False, "reason": "Not in the platform registry"}
        url, status = p.get("official_url"), str(p.get("identity_status") or "")
        if not (isinstance(url, str) and url.startswith("https://")):
            return {"verified": False, "reason": "The exact service and official URL have not been confirmed"}
        if status.startswith("needs_"):
            return {"verified": False, "reason": f"Identity status: {status}"}
        return {"verified": True, "official_url": url, "identity_status": status}

    def check_policy(self, conn) -> dict[str, Any]:
        if self.action is None:
            return {"allowed": True, "reasons": [], "result": "local_only", "level": (permissions.profile(self.platform_id) or {}).get("level", 0)}
        return permissions.decide(conn, self.platform_id, self.action)

    def parse_opportunity(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        return raw if isinstance(raw, dict) and raw.get("title") else None

    def report_adapter_health(self, conn) -> dict[str, Any]:
        return (db.get_state(conn, "discovery_health", {}) or {}).get(self.platform_id, {"status": "never_run"})


class OpenTrainAdapter(Adapter):
    platform_id, mode, action = "opentrain_ai", "public_api", "public_discovery"

    def __init__(self, fetch: Callable | None = None, cache_path: Path | None = None):
        super().__init__()
        self.fetch, self.cache_path = fetch, cache_path

    def discover_public_opportunities(self, conn, now: datetime) -> dict[str, Any]:
        last = db.get_state(conn, "last_report", {}) or {}
        seen = oc.parse_time(last.get("checked_at_utc"))
        if self.fetch is None and seen and now - seen < timedelta(minutes=FRESH_CLOUD_REPORT_MINUTES):
            return {"items": [], "skipped": f"The cloud scan read this API at {last.get('checked_at_utc')}; its listings are already synced"}
        try:
            from microtask_monitor import opentrain
        except ImportError:  # pragma: no cover
            import opentrain
        kw = {"cache_path": self.cache_path or (db.DATA_DIR / "cache" / "opentrain_jobs.json"), "now": now}
        if self.fetch is not None:
            kw.update(fetch=self.fetch, sleep=lambda s: None)
        result = opentrain.scan(**kw)
        health = result["health"]
        if health["status"] == "error":
            raise RuntimeError(health.get("error") or "OpenTrain scan failed")
        return {"items": [i for i in (self.parse_opportunity(r) for r in result["items"]) if i], "complete": health["status"] == "ok", "detail": health["status"]}


class WhopAdapter(Adapter):
    platform_id, mode, action = "whop_workforce", "authorized_api", "account_monitoring"
    min_interval_seconds = 1800

    def __init__(self, store: Any = None, http: Callable | None = None):
        super().__init__()
        self.store, self.http = store, http

    def discover_authorized_account_opportunities(self, conn, now: datetime) -> dict[str, Any]:
        try:
            from microtask_monitor import secret_store, whop
        except ImportError:  # pragma: no cover
            import secret_store, whop
        store = self.store or secret_store.default_store()
        if not whop.oauth_status(conn, store)["authorized"]:
            return {"items": [], "skipped": "Whop is not authorized yet: connect it yourself on the Whop tab", "needs_user": True}
        result = whop.sync(conn, store, self.http or whop.http_json)
        tracked = 0
        for r in conn.execute("SELECT id FROM whop_bounties WHERE status='open' AND decision IN ('executable','human_only') ORDER BY net_reward DESC LIMIT ?", (AUTO_TRACK_LIMIT,)).fetchall():
            before = conn.execute("SELECT COUNT(*) FROM assignments WHERE platform_id='whop_workforce'").fetchone()[0]
            whop.track(conn, r["id"])
            tracked += conn.execute("SELECT COUNT(*) FROM assignments WHERE platform_id='whop_workforce'").fetchone()[0] - before
        return {"items": [], "detail": f"bounties {result['bounties']}", "assignments_created": tracked, "stored_by_adapter": True}


class PolicyMonitorAdapter(Adapter):
    mode, action = "policy_monitor", "policy_monitor"

    def discover_public_opportunities(self, conn, now: datetime) -> dict[str, Any]:
        m = (db.get_state(conn, "eligibility_monitors", {}) or {}).get(self.platform_id)
        return {"items": [], "detail": "Eligibility policy monitor (run by the cloud scan): " + (json.dumps(m)[:200] if m else "no result synced yet")}


class ManualImportAdapter(Adapter):
    """No network access. Accepts task records you copied from your own account and runs them through the same pipeline."""
    mode, action = "manual_import", None

    def import_records(self, conn, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not permissions.known(self.platform_id):
            raise policy.PermissionDenied(f"Blocked: no capability record for '{self.platform_id}'")
        prof = permissions.profile(self.platform_id)
        if prof["disabled_reason"]:
            raise policy.PermissionDenied(f"Blocked: {prof['disabled_reason']}")
        created, skipped, ids = 0, [], []
        for row in rows if isinstance(rows, list) else []:
            rec = normalize_task(self.platform_id, row)
            if isinstance(rec, str):
                skipped.append(rec)
                continue
            before = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
            a = asg.create(conn, self.platform_id, rec["external_id"], rec["title"], "direct" if self.platform_id == permissions.DIRECT_LANE else "manual",
                           rec["instructions"], rec["source_url"], rec["deadline"], rec["reward_gross"], rec["reward_net"], rec["currency"], rec["requirements"],
                           ai_use=rec["ai_use"], ai_use_source=rec["ai_use_source"], listing_ref=f"user_import:{rec['external_id']}")
            created += conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] - before
            ids.append(a["id"])
            lifecycle.check_eligibility(conn, a["id"])
            lifecycle.classify(conn, a["id"])
        return {"created": created, "duplicates": len(ids) - created, "skipped": skipped, "assignment_ids": ids}


def normalize_task(platform_id: str, row: Any) -> dict[str, Any] | str:
    """Strictly validate one user-provided task record. Returns the record, or the reason it was refused."""
    if not isinstance(row, dict):
        return "Not an object"
    title, instructions = str(row.get("title") or "").strip(), str(row.get("instructions") or "").strip()
    if not title:
        return "A task needs a title"
    url = str(row.get("url") or "").strip()
    if url and not url.startswith("https://"):
        return f"'{title[:40]}': the link must be https"
    ext = str(row.get("id") or "").strip()
    if not ext:
        import hashlib
        ext = "import-" + hashlib.sha256(f"{platform_id}|{title}|{url}|{instructions[:500]}".encode()).hexdigest()[:16]

    def money(key: str) -> float | None | str:
        v = row.get(key)
        if v in (None, ""):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "bad"
        return f if 0 <= f < 1_000_000 else "bad"
    gross, net = money("reward"), money("reward_net")
    if gross == "bad" or net == "bad":
        return f"'{title[:40]}': the reward must be a non-negative number"
    currency = str(row.get("currency") or "").strip().upper()
    if currency and not (len(currency) == 3 and currency.isalpha()):
        return f"'{title[:40]}': currency must be a 3-letter code"
    ai_use = str(row.get("ai_use") or "unknown")
    if ai_use not in db.AI_USE:
        return f"'{title[:40]}': unknown AI-use value"
    ai_source = str(row.get("ai_source") or "").strip()
    if ai_use == "allowed" and not ai_source:
        return f"'{title[:40]}': to mark AI as allowed, say where the task or platform rules permit it"
    countries = row.get("allowed_countries")
    req: dict[str, Any] = {}
    if isinstance(countries, list):
        req["allowed_countries"] = [str(c).upper()[:2] for c in countries if isinstance(c, str)][:60]
    if row.get("deterministic_spec") is True:
        req["deterministic_spec"] = True
    return {"external_id": ext[:120], "title": title, "instructions": instructions, "source_url": url, "deadline": str(row.get("deadline") or "").strip() or None,
            "reward_gross": gross, "reward_net": net, "currency": currency or None, "requirements": req, "ai_use": ai_use, "ai_use_source": ai_source}


def default_adapters(store: Any = None) -> list[Adapter]:
    """One adapter per registered platform: the least resource-intensive permitted method for each."""
    caps, out = policy.load_capabilities(), []
    for pid in caps:
        if pid == "opentrain_ai":
            out.append(OpenTrainAdapter())
        elif pid == "whop_workforce":
            out.append(WhopAdapter(store))
        elif "policy_monitor" in (caps[pid].get("permitted_automation") or []):
            out.append(PolicyMonitorAdapter(pid))
        else:
            out.append(ManualImportAdapter(pid))
    return out


def adapter_for(platform_id: str) -> Adapter:
    if platform_id == permissions.DIRECT_LANE:
        return ManualImportAdapter(platform_id)
    for a in default_adapters():
        if a.platform_id == platform_id:
            return a
    raise policy.PermissionDenied(f"Blocked: no capability record for '{platform_id}'")


def _save_health(conn, pid: str, **fields: Any) -> dict[str, Any]:
    with db.tx(conn):
        health = db.get_state(conn, "discovery_health", {}) or {}
        health[pid] = {**health.get(pid, {}), **fields}
        db.set_state(conn, "discovery_health", health)
    return health[pid]


def run(conn, adapters: list[Adapter] | None = None, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """One discovery pass over every adapter that may run. Returns per-adapter outcomes and whether anything meaningful changed."""
    now = now or datetime.now(timezone.utc)
    adapters = default_adapters() if adapters is None else adapters
    outcomes, changed = {}, False
    for ad in adapters:
        pid, prev = ad.platform_id, ad.report_adapter_health(conn)
        base = {"mode": ad.mode, "last_attempt": now.isoformat()}
        if ad.mode == "manual_import":
            outcomes[pid] = _save_health(conn, pid, mode=ad.mode, status="manual_import_only",
                                         message="Nothing is read automatically. Import task details you copied from your own account.")
            continue
        identity = ad.verify_platform_identity()
        if not identity["verified"]:
            outcomes[pid] = _save_health(conn, pid, **base, status="blocked", message=identity["reason"])
            continue
        decision = ad.check_policy(conn)
        if not decision["allowed"]:
            outcomes[pid] = _save_health(conn, pid, **base, status="blocked", message="; ".join(decision["reasons"]))
            continue
        until = oc.parse_time(prev.get("circuit_open_until"))
        if until and until > now and not force:
            outcomes[pid] = {**prev, "status": "stopped_after_failures"}
            continue
        last = oc.parse_time(prev.get("last_run"))
        if last and not force and (now - last).total_seconds() < ad.min_interval_seconds:
            outcomes[pid] = {**prev, "skipped": "rate limit: too soon since the last run"}
            continue
        try:
            fn = getattr(ad, "discover_public_opportunities", None) or getattr(ad, "discover_authorized_account_opportunities")
            result = fn(conn, now)
            stats = {"new": 0, "updated": 0, "changed": 0, "expired": 0, "duplicates": 0}
            if result.get("items"):
                with db.tx(conn):
                    stats = ig.ingest(conn, list(oc.dedupe(result["items"])), now=now, complete_platforms={pid} if result.get("complete") else set())
            changed = changed or bool(stats["new"] or stats["changed"] or result.get("assignments_created"))
            status = "needs_you" if result.get("needs_user") else ("ok_skipped" if result.get("skipped") else "ok")
            outcomes[pid] = _save_health(conn, pid, **base, status=status, last_run=now.isoformat(),
                                         last_success=prev.get("last_success") if result.get("needs_user") else now.isoformat(),
                                         consecutive_failures=0, circuit_open_until=None, error=None,
                                         message=result.get("skipped") or result.get("detail") or "", stats=stats)
        except (policy.PermissionDenied, cx.SessionRequired) as exc:
            outcomes[pid] = _save_health(conn, pid, **base, status="blocked", message=str(exc)[:300])
        except Exception as exc:                                   # noqa: BLE001 - one adapter failing must not stop the others
            fails = int(prev.get("consecutive_failures") or 0) + 1
            fields = {"status": "error", "last_run": now.isoformat(), "consecutive_failures": fails, "error": str(exc)[:300]}
            if fails >= BREAKER_FAILURES:
                fields.update(status="stopped_after_failures", circuit_open_until=(now + timedelta(hours=BREAKER_HOURS)).isoformat(),
                              message=f"Stopped automatically after {fails} failures in a row; it is retried after {BREAKER_HOURS} hours")
                with db.tx(conn):
                    db.audit(conn, "discovery.circuit_open", "platform", pid, {"failures": fails}, actor="system")
            outcomes[pid] = _save_health(conn, pid, **base, **fields)
    return {"ran_at": now.isoformat(), "changed": changed, "adapters": outcomes}


def stale_check(conn, now: datetime | None = None, max_age_days: int = 14) -> dict[str, int]:
    """Listings nobody has re-verified for a while stop counting as open. Feed items are not touched (the sync owns them)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=max_age_days)).isoformat()
    with db.tx(conn):
        n = conn.execute("UPDATE opportunities SET status='expired', expired_utc=? WHERE status='open' AND platform_id IS NOT NULL AND COALESCE(last_verified_utc, last_seen_utc) < ?",
                         (now.isoformat(), cutoff)).rowcount
    return {"expired_listings": n, "expired_assignments": lifecycle.expire_due(conn, now)}


def health(conn) -> dict[str, Any]:
    return db.get_state(conn, "discovery_health", {}) or {}
