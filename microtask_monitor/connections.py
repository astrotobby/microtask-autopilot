"""Local platform connections: dedicated browser profiles you sign in to yourself, and a status registry with NO secrets.

How connecting works:
1. ``open_login`` starts Microsoft Edge or Google Chrome with a dedicated profile folder for that platform
   (``data/browser-profiles/<platform>``) at the platform's own sign-in page. No remote-debugging port is opened and
   this program never reads the profile's cookies or passwords: the browser keeps them in its OS-encrypted store.
2. You sign in yourself, completing any CAPTCHA, MFA, identity check or KYC personally.
3. You press "I'm signed in". The status is "connected (confirmed by you)": this program cannot and does not verify a
   signed-in session for platforms whose terms do not permit automated access.
4. After ``SESSION_CHECK_DAYS`` without a fresh confirmation, the connection shows as needing a check. If a session
   expires or a human step appears, the status becomes ``needs_user_action`` and any authenticated adapter must stop.

Whether the program may DO anything in a connected account is decided by ``policy.check_action`` and the platform's
verified terms (``access_class``). Today no platform is classified ``browser_automation_permitted``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import db, policy
    from microtask_monitor.secret_store import redact
except ImportError:  # pragma: no cover
    import db, policy
    from secret_store import redact

ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = ROOT / "data" / "browser-profiles"
PLATFORMS_PATH = ROOT / "microtask_monitor" / "platforms.json"
SESSION_CHECK_DAYS = 7
HUMAN_STEPS = {"captcha": "CAPTCHA", "mfa": "two-factor / one-time code", "identity_verification": "identity verification",
               "kyc": "KYC", "terms": "terms or agreement", "other": "a required human step"}
LOGIN_FALLBACK = {"handshake_ai": "https://joinhandshake.com/ai/opportunities/"}


class ConnectionError_(ValueError):
    pass


class SessionRequired(RuntimeError):
    """Raised when an authenticated step needs a live, user-confirmed session (or a human step) first."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def login_url(platform_id: str) -> str | None:
    try:
        data = json.loads(PLATFORMS_PATH.read_text(encoding="utf-8"))
        for p in data["platforms"]:
            if p["id"] == platform_id and p.get("official_url"):
                return p["official_url"]
    except (OSError, ValueError, KeyError):
        pass
    return LOGIN_FALLBACK.get(platform_id)


def _row(conn, pid: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM platform_connections WHERE platform_id=?", (pid,)).fetchone()
    return dict(r) if r else {"platform_id": pid, "status": "not_connected", "required_action": None, "human_step": None,
                               "last_checked_at": None, "last_success_at": None, "profile_dir": None, "note": None}


def _save(conn, pid: str, **fields: Any) -> None:
    cur = _row(conn, pid)
    cur.update({k: (redact(v) if isinstance(v, str) and k in ("required_action", "note", "human_step") else v) for k, v in fields.items()})
    cur["updated_at"] = _now().isoformat()
    conn.execute("""INSERT INTO platform_connections(platform_id,status,required_action,human_step,last_checked_at,last_success_at,profile_dir,note,updated_at)
                    VALUES(:platform_id,:status,:required_action,:human_step,:last_checked_at,:last_success_at,:profile_dir,:note,:updated_at)
                    ON CONFLICT(platform_id) DO UPDATE SET status=excluded.status, required_action=excluded.required_action, human_step=excluded.human_step,
                    last_checked_at=excluded.last_checked_at, last_success_at=excluded.last_success_at, profile_dir=excluded.profile_dir,
                    note=excluded.note, updated_at=excluded.updated_at""", cur)


def _record(pid: str, caps: dict | None = None) -> dict[str, Any]:
    caps = policy.load_capabilities() if caps is None else caps
    rec = caps.get(pid)
    if not rec:
        raise ConnectionError_(f"Unknown platform '{pid}'")
    return rec


def blocked_reason(rec: dict[str, Any]) -> str | None:
    if (rec.get("country_eligibility") or {}).get("kenya") == "unsupported":
        return "Kenya is listed as unsupported by this platform: do not connect or apply."
    return None


def automation_level(rec: dict[str, Any]) -> str:
    access = rec.get("access_class") or "prohibited_or_unverified"
    allowed = [a for a in sorted(policy.AUTHENTICATED) if a in rec.get("permitted_automation", [])] if access == "browser_automation_permitted" else []
    return {"official_api_permitted": "Official public API (read-only listings). Everything in your account is done by you.",
            "browser_automation_permitted": "Browser automation permitted for: " + (", ".join(allowed) or "nothing yet"),
            "human_assisted_only": "Human-assisted only: you act in your account; this program prepares and tracks.",
            "prohibited_or_unverified": "Automation prohibited or not verified: manual use only."}[access]


def status(conn, pid: str, caps: dict | None = None, now: datetime | None = None) -> dict[str, Any]:
    rec = _record(pid, caps)
    row = _row(conn, pid)
    now = now or _now()
    shown, action = row["status"], row["required_action"]
    blocked = blocked_reason(rec)
    if blocked:
        shown, action = "blocked", blocked
    elif shown == "connected_user_confirmed":
        last = datetime.fromisoformat(row["last_success_at"]) if row["last_success_at"] else None
        if not last or now - last > timedelta(days=SESSION_CHECK_DAYS):
            shown, action = "check_due", f"Open the login window and confirm you are still signed in (last confirmed {(row['last_success_at'] or 'never')[:10]})."
    elif shown == "not_connected":
        action = action or "Open the login window and sign in yourself."
    return {"platform_id": pid, "name": rec.get("name", pid), "status": shown, "stored_status": row["status"], "required_action": action,
            "human_step": row["human_step"], "last_checked_at": row["last_checked_at"], "last_success_at": row["last_success_at"],
            "access_class": rec.get("access_class"), "access_note": rec.get("access_note"), "automation_level": automation_level(rec),
            "login_url": login_url(pid), "profile_exists": bool(row["profile_dir"] and Path(row["profile_dir"]).exists()),
            "verified_evidence": "user_confirmation" if row["status"] == "connected_user_confirmed" else None}


def list_status(conn, caps: dict | None = None) -> list[dict[str, Any]]:
    caps = policy.load_capabilities() if caps is None else caps
    return [status(conn, pid, caps) for pid in caps]


# ------------------------------------------------------------------------------------------- browser profile
def find_browser() -> str | None:
    candidates = [shutil.which("msedge"), shutil.which("chrome"),
                  os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                  os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                  os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def browser_command(browser: str, profile: Path, url: str) -> list[str]:
    """A normal browser window on a dedicated profile. Deliberately NO remote-debugging/automation flags."""
    if not url.startswith("https://"):
        raise ConnectionError_("Only https sign-in pages are opened")
    return [browser, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", url]


def open_login(conn, pid: str, caps: dict | None = None, profiles_dir: Path | None = None, launcher=subprocess.Popen,
               browser: str | None = None) -> dict[str, Any]:
    rec = _record(pid, caps)
    if blocked_reason(rec):
        raise ConnectionError_(blocked_reason(rec))
    url = login_url(pid)
    if not url:
        raise ConnectionError_("No official sign-in URL is recorded for this platform")
    browser = browser or find_browser()
    if not browser:
        raise ConnectionError_("Microsoft Edge or Google Chrome was not found")
    profile = Path(profiles_dir or PROFILES_DIR) / pid
    profile.mkdir(parents=True, exist_ok=True)
    launcher(browser_command(browser, profile, url))
    with db.tx(conn):
        _save(conn, pid, status="login_in_progress", profile_dir=str(profile), last_checked_at=_now().isoformat(),
              required_action="Sign in yourself in the window that opened (complete any CAPTCHA/MFA yourself), then press \"I'm signed in\".", human_step=None)
        db.audit(conn, "connection.open_login", "platform", pid)
    return status(conn, pid, caps)


def confirm_signed_in(conn, pid: str, confirmed_by_user: bool, caps: dict | None = None) -> dict[str, Any]:
    rec = _record(pid, caps)
    if confirmed_by_user is not True:
        raise ConnectionError_("Only you can confirm that you are signed in (confirmed_by_user=true)")
    if blocked_reason(rec):
        raise ConnectionError_(blocked_reason(rec))
    stamp = _now().isoformat()
    with db.tx(conn):
        _save(conn, pid, status="connected_user_confirmed", last_success_at=stamp, last_checked_at=stamp, required_action=None, human_step=None,
              note="Confirmed by you; not verified automatically.")
        db.audit(conn, "connection.confirmed", "platform", pid)
    return status(conn, pid, caps)


def report_human_step(conn, pid: str, kind: str, caps: dict | None = None) -> dict[str, Any]:
    _record(pid, caps)
    if kind not in HUMAN_STEPS:
        raise ConnectionError_(f"Unknown human step '{kind}'")
    with db.tx(conn):
        _save(conn, pid, status="needs_user_action", human_step=kind, last_checked_at=_now().isoformat(),
              required_action=f"Complete the {HUMAN_STEPS[kind]} yourself in the browser window. This program never bypasses it. Then confirm you are signed in.")
        db.audit(conn, "connection.human_step", "platform", pid, {"kind": kind})
    return status(conn, pid, caps)


def report_expired(conn, pid: str, caps: dict | None = None) -> dict[str, Any]:
    _record(pid, caps)
    with db.tx(conn):
        _save(conn, pid, status="needs_user_action", last_checked_at=_now().isoformat(), human_step=None,
              required_action="Your session expired. Open the login window and sign in again; paused work resumes after you confirm.")
        db.audit(conn, "connection.expired", "platform", pid)
    return status(conn, pid, caps)


def disconnect(conn, pid: str, caps: dict | None = None) -> dict[str, Any]:
    _record(pid, caps)
    with db.tx(conn):
        _save(conn, pid, status="not_connected", last_success_at=None, human_step=None,
              required_action="Disconnected. To remove the saved browser session too, sign out in the login window or delete the profile folder.")
        db.audit(conn, "connection.disconnect", "platform", pid)
    return status(conn, pid, caps)


def require_session(conn, pid: str, action: str, user_authorized: bool, caps: dict | None = None) -> dict[str, Any]:
    """The single gate every authenticated adapter must pass: policy permission, your authorization, then a live session."""
    caps = policy.load_capabilities() if caps is None else caps
    decision = policy.check_action(pid, action, caps=caps, user_authorized=user_authorized)
    if not decision.allowed:
        raise policy.PermissionDenied(f"Blocked: {decision.reason}")
    st = status(conn, pid, caps)
    if st["status"] != "connected_user_confirmed":
        raise SessionRequired(st["required_action"] or "Sign in first")
    return st
