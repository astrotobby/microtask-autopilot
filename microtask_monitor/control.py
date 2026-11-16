"""Your standing instructions to the engine: automation settings, the global stop, pause/resume and local resource limits.

Settings are your pre-authorization. They can only NARROW what the permission engine allows; no setting can enable an
action a platform has not been verified to permit. Everything is stored in SQLite (``sync_state``), never in the browser.
"""
from __future__ import annotations

import ctypes
import os
import shutil
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import db
except ImportError:  # pragma: no cover
    import db

CATEGORIES = ("software_development", "coding_debugging", "data_processing", "spreadsheet_processing", "data_annotation", "image_labeling",
              "video_annotation", "text_classification", "writing", "technical_writing", "ai_evaluation", "llm_evaluation", "prompt_engineering",
              "content_creation", "translation", "transcription", "research", "survey", "usability_testing", "voice_recording", "video_recording",
              "physical_capture", "social_engagement", "administrative", "other")
# Categories that are a person's own experience or presence: they can never be put on the permitted list.
HUMAN_ONLY_CATEGORIES = frozenset({"survey", "usability_testing", "voice_recording", "video_recording", "physical_capture", "social_engagement"})
DEFAULTS: dict[str, Any] = {
    "auto_execute": False,                 # run permitted tasks without asking each time (your standing authorization)
    "ai_runs_preauthorized": False,        # lets AI-based specialists start under that standing authorization
    "auto_approve_clean_qa": False,        # package work whose every QA check passed with nothing left for manual review
    "permitted_categories": ["data_processing", "spreadsheet_processing"],
    "excluded_task_types": [],
    "max_concurrency": 1,
    "max_task_value_usd": 50.0,
    "min_hourly_rate_usd": 0.0,
    "max_daily_execution_minutes": 240,
    "min_free_disk_mb": 1024,
    "min_free_memory_mb": 512,
    "job_timeout_seconds": 1800,
    "retention_days": 30,
    "auto_submit_platforms": {},           # {platform_id: true}; only honoured where the platform is verified at level 5
    "paused": False,
}
_LIMITS = {"max_concurrency": (1, 4), "max_task_value_usd": (0, 100000), "min_hourly_rate_usd": (0, 10000), "max_daily_execution_minutes": (0, 1440),
           "min_free_disk_mb": (100, 1_000_000), "min_free_memory_mb": (64, 1_000_000), "job_timeout_seconds": (60, 14400), "retention_days": (1, 3650)}


class ControlError(ValueError):
    pass


def settings(conn) -> dict[str, Any]:
    saved = db.get_state(conn, "autonomy_settings", {}) or {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def update_settings(conn, changes: dict[str, Any]) -> dict[str, Any]:
    """Validate strictly and persist. Unknown keys and out-of-range values are rejected, not silently dropped."""
    if not isinstance(changes, dict):
        raise ControlError("Settings must be an object")
    cur = settings(conn)
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ControlError(f"Unknown setting '{key}'")
        if isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ControlError(f"'{key}' must be true or false")
        elif key in _LIMITS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ControlError(f"'{key}' must be a number")
            lo, hi = _LIMITS[key]
            if not lo <= value <= hi:
                raise ControlError(f"'{key}' must be between {lo} and {hi}")
            value = int(value) if isinstance(DEFAULTS[key], int) else float(value)
        elif key in ("permitted_categories", "excluded_task_types"):
            if not isinstance(value, list) or any(v not in CATEGORIES for v in value):
                raise ControlError(f"'{key}' must be a list of known task categories")
            human = sorted(set(value) & HUMAN_ONLY_CATEGORIES)
            if key == "permitted_categories" and human:
                raise ControlError("These categories need a real person and can never be automated: " + ", ".join(human))
            value = sorted(set(value))
        elif key == "auto_submit_platforms":
            try:
                from microtask_monitor import permissions
            except ImportError:  # pragma: no cover
                import permissions
            if not isinstance(value, dict) or any(not isinstance(v, bool) for v in value.values()):
                raise ControlError("'auto_submit_platforms' must map platform ids to true/false")
            for pid, on in value.items():
                prof = permissions.profile(pid)
                if prof is None:
                    raise ControlError(f"Unknown platform '{pid}'")
                if on and prof["actions"]["auto_submission"]["result"] != "permitted":
                    raise ControlError(f"Automatic submission cannot be enabled for {prof['name']}: it is {prof['actions']['auto_submission']['result'].replace('_', ' ')} there")
        cur[key] = value
    with db.tx(conn):
        db.set_state(conn, "autonomy_settings", cur)
        db.audit(conn, "control.settings", "settings", "autonomy", {k: changes[k] for k in changes})
    return cur


# ------------------------------------------------------------------------------------------- global stop / pause
def kill_state(conn) -> dict[str, Any]:
    return db.get_state(conn, "kill_switch", {}) or {"engaged": False}


def is_stopped(conn) -> bool:
    return bool(kill_state(conn).get("engaged"))


def engage_stop(conn, reason: str = "Stopped by you", actor: str = "user") -> dict[str, Any]:
    """Emergency stop: nothing new starts, running jobs are asked to pause at their next checkpoint, and work claims are released.

    Idempotent. It never deletes anything; press Resume to continue.
    """
    with db.tx(conn):
        db.set_state(conn, "kill_switch", {"engaged": True, "reason": str(reason)[:300], "at": db.now_iso(), "by": actor})
        paused = conn.execute("UPDATE execution_jobs SET pause_requested=1 WHERE status='running'").rowcount
        conn.execute("UPDATE assignments SET claimed_by=NULL, claim_until=NULL WHERE claimed_by IS NOT NULL")
        db.audit(conn, "control.stop", "system", "kill_switch", {"reason": str(reason)[:300], "jobs_asked_to_pause": paused}, actor=actor)
    return {**kill_state(conn), "jobs_asked_to_pause": paused}


def release_stop(conn, confirmed_by_user: bool) -> dict[str, Any]:
    if confirmed_by_user is not True:
        raise ControlError("Only you can release the global stop (confirmed_by_user=true)")
    with db.tx(conn):
        db.set_state(conn, "kill_switch", {"engaged": False, "released_at": db.now_iso()})
        db.audit(conn, "control.resume", "system", "kill_switch")
    return kill_state(conn)


def may_run(conn) -> tuple[bool, str]:
    """Whether the engine may start new work right now."""
    if is_stopped(conn):
        return False, "The global stop is engaged: " + (kill_state(conn).get("reason") or "")
    if settings(conn)["paused"]:
        return False, "Automation is paused in your settings"
    return True, ""


# ------------------------------------------------------------------------------------------- local resources
def free_memory_mb() -> float | None:
    if os.name == "nt":
        class _Mem(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = _Mem()
        m.dwLength = ctypes.sizeof(_Mem)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ullAvailPhys / 1048576
        except (OSError, AttributeError):
            return None
        return None
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1048576
    except (ValueError, OSError, AttributeError):
        return None


def resources(conn, base: Path | None = None, probe: dict[str, float] | None = None) -> dict[str, Any]:
    """Free disk and memory against your limits. ``probe`` lets tests supply readings."""
    s = settings(conn)
    base = Path(base or db.DATA_DIR)
    try:
        disk = (probe or {}).get("disk_mb", shutil.disk_usage(base if base.exists() else base.parent).free / 1048576)
    except OSError:
        disk = None
    mem = (probe or {}).get("memory_mb", free_memory_mb())
    problems = []
    if disk is not None and disk < s["min_free_disk_mb"]:
        problems.append(f"Only {disk:.0f} MB of disk is free (your limit is {s['min_free_disk_mb']} MB)")
    if mem is not None and mem < s["min_free_memory_mb"]:
        problems.append(f"Only {mem:.0f} MB of memory is free (your limit is {s['min_free_memory_mb']} MB)")
    return {"ok": not problems, "free_disk_mb": None if disk is None else round(disk), "free_memory_mb": None if mem is None else round(mem),
            "cpu_count": os.cpu_count(), "problems": problems}


def view(conn) -> dict[str, Any]:
    ok, why = may_run(conn)
    return {"settings": settings(conn), "kill_switch": kill_state(conn), "may_run": ok, "blocked_reason": why or None,
            "resources": resources(conn), "categories": list(CATEGORIES), "human_only_categories": sorted(HUMAN_ONLY_CATEGORIES),
            "limits": {k: list(v) for k, v in _LIMITS.items()}}
