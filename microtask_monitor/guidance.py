"""Platform-specific application steps, assessment notes and task quality-control checklists.

Entries marked verified cite the platform's own documentation and the date it was read. Platforms that have
not been verified get the generic guidance and are labelled as unverified, never presented as official.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GUIDANCE_PATH = ROOT / "microtask_monitor" / "platform_guidance.json"
SECTIONS = ("apply_steps", "assessments", "after_applying", "task_qc", "payout", "common_mistakes")


def load(path: Path = GUIDANCE_PATH) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"generic": {"verified": False, "sources": []}, "platforms": {}}


def for_platform(platform_id: str | None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Guidance for a platform; unverified platforms fall back to generic text with verified=False."""
    data = data or load()
    generic = data.get("generic", {})
    specific = (data.get("platforms") or {}).get(platform_id or "")
    out: dict[str, Any] = {"platform_id": platform_id, "verified": False, "verified_on": None, "sources": [], "specific": bool(specific)}
    for section in SECTIONS:
        out[section] = list(generic.get(section, []))
    if specific:
        for section in SECTIONS:
            if specific.get(section):
                out[section] = list(specific[section])
        out.update(verified=bool(specific.get("verified")), verified_on=specific.get("verified_on"), sources=list(specific.get("sources", [])))
        if specific.get("notes"):
            out["notes"] = specific["notes"]
    out["label"] = ("Verified from the platform's own documentation on " + str(out["verified_on"])) if out["verified"] else \
        "UNVERIFIED: generic guidance only. Read this platform's own terms and task rules before relying on it."
    return out


def task_checklist(platform_id: str | None, data: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Checklist rows ({text, kind}) added to a new task for this platform."""
    g = for_platform(platform_id, data)
    prefix = "" if g["verified"] else "(generic) "
    rows = [{"text": prefix + t, "kind": "platform"} for t in g["task_qc"]]
    if not g["verified"]:
        rows.insert(0, {"text": "This platform's terms and task rules are NOT verified here: read them before working.", "kind": "platform"})
    return rows
