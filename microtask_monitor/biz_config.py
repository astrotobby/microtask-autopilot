"""Freelance business engine: configuration, the separate business audit trail, and shared helpers.

The business channel sits beside the microtask engine and reuses it: the same database, the same direct-client lane for
execution (workers, isolated workspaces, independent QA, packaging), the same ``payments`` table for money received and
the same Gmail transport for permitted messages. Nothing here replaces or changes the 25-platform engine.

Configuration = business_defaults.json (tracked) deep-merged with data/business_config.json (yours, not committed).
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import db
except ImportError:  # pragma: no cover
    import db

HERE = Path(__file__).resolve().parent
DEFAULTS_PATH = HERE / "business_defaults.json"
SOURCES_PATH = HERE / "business_sources.json"
USER_PATH = db.DATA_DIR / "business_config.json"
PROFILE_PATH = HERE / "profile.json"
OUTREACH_PATH = db.DATA_DIR / "outreach_config.json"


class BizError(ValueError):
    """A business rule refused the action; the message says why."""


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        return {**base, **{k: _merge(base.get(k), v) for k, v in over.items()}}
    return copy.deepcopy(over)


def load(user_path: Path | None = None) -> dict[str, Any]:
    cfg = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    p = Path(user_path or USER_PATH)
    if p.exists():
        try:
            cfg = _merge(cfg, json.loads(p.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise BizError(f"{p.name} is not valid JSON: {exc}")
    cfg["profile"] = profile()
    return cfg


def save_overrides(changes: dict[str, Any], user_path: Path | None = None) -> dict[str, Any]:
    """Persist your overrides (pricing, targets, autonomy, packages). Validated: prices stay positive, autonomy flags stay booleans."""
    if not isinstance(changes, dict):
        raise BizError("Settings must be an object")
    allowed = {"business_name", "targets", "pricing", "capacity", "autonomy", "packages", "testimonials", "contact"}
    bad = set(changes) - allowed
    if bad:
        raise BizError("Unknown business settings: " + ", ".join(sorted(bad)))
    for k, v in (changes.get("pricing") or {}).items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise BizError(f"pricing.{k} must be a non-negative number")
    for k, v in (changes.get("autonomy") or {}).items():
        if k != "max_messages_per_client_per_week" and not isinstance(v, bool):
            raise BizError(f"autonomy.{k} must be true or false")
    for t in (changes.get("testimonials") or []):
        if not (isinstance(t, dict) and t.get("client") and t.get("text") and t.get("evidence")):
            raise BizError("A testimonial needs the client, their words and where they wrote it (evidence): none are ever invented")
    p = Path(user_path or USER_PATH)
    cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    cur = _merge(cur, changes)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return load(p)


def sources() -> list[dict[str, Any]]:
    return json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["sources"]


def source(source_id: str) -> dict[str, Any]:
    for s in sources():
        if s["id"] == source_id:
            return s
    raise BizError(f"Unknown lead source '{source_id}' (fail closed)")


def excluded_domains() -> list[str]:
    return json.loads(SOURCES_PATH.read_text(encoding="utf-8")).get("excluded", [])


def profile() -> dict[str, Any]:
    """Your real profile: skills you stated, and contact details only from your own outreach configuration. Nothing invented."""
    try:
        p = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        p = {}
    contact: dict[str, Any] = {}
    try:
        o = json.loads(OUTREACH_PATH.read_text(encoding="utf-8"))
        contact = {"name": o.get("sender_name"), "email": o.get("from_address")}
    except (OSError, ValueError):
        pass
    skills = sorted({s for group in (p.get("established_skills") or {}).values() for s in group})
    return {"country": p.get("country", "Kenya"), "skills": skills, "not_established": p.get("not_established", []), "contact": contact}


def audit(conn, action: str, entity: str = "", entity_id: Any = "", detail: dict | None = None, permission: str = "", actor: str = "engine") -> None:
    """The business audit trail: every automated decision with the permission it relied on."""
    conn.execute("INSERT INTO biz_audit(at,actor,action,entity,entity_id,permission,detail_json) VALUES(?,?,?,?,?,?,?)",
                 (db.now_iso(), actor, action, entity, str(entity_id), permission[:300],
                  db._redact(json.dumps(detail or {}, ensure_ascii=False, default=str))))


def norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()
