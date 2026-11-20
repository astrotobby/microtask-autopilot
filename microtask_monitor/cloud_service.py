"""Cloud service entry point for an always-on scheduler and a narrowly scoped admin/OAuth surface.

Run only on a paid, single-instance host with a persistent disk mounted at /app/data.
It exposes health + authenticated administration; it does not expose the local dashboard API.
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOG = logging.getLogger("microtask_autopilot.cloud")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_started_at = time.time()
_stop = threading.Event()
_engine = None
_engine_thread: threading.Thread | None = None
_backup_thread: threading.Thread | None = None
_server: ThreadingHTTPServer | None = None
_store = None


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def public_base_url() -> str:
    value = (os.environ.get("AUTOPILOT_PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not value or not value.startswith("https://") or "@" in value:
        return ""
    return value


def _admin_token() -> str:
    token = os.environ.get("AUTOPILOT_ADMIN_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("AUTOPILOT_ADMIN_TOKEN must be at least 32 characters and set as a private host secret")
    return token


def authorize_header(header: str | None, token: str, username: str = "admin") -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        user, supplied = decoded.split(":", 1)
    except Exception:
        return False
    return hmac.compare_digest(user, username) and hmac.compare_digest(supplied, token)


def filter_cloud_jobs(jobs, env=None):
    """Exclude side-effectful prospecting by default; enable only through explicit host config."""
    env = env if env is not None else os.environ
    out = []
    for job in jobs:
        if job.name == "owner_outreach" and str(env.get("AUTOPILOT_ENABLE_OWNER_OUTREACH", "false")).lower() not in {"1", "true", "yes"}:
            continue
        if job.name == "freelance_business" and str(env.get("AUTOPILOT_ENABLE_FREELANCE_BUSINESS", "false")).lower() not in {"1", "true", "yes"}:
            continue
        out.append(job)
    return out


def _prepare_engine():
    global _store
    from microtask_monitor import db, execution, intelligence, permissions, policy, scheduler, secret_store, sync_reports, workers

    os.environ.setdefault("AUTOPILOT_RUNTIME", "cloud")
    data_dir = os.environ.get("AUTOPILOT_DATA_DIR") or os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    with closing(db.connect(db.DB_PATH)) as conn:
        with db.tx(conn):
            policy.sync_to_db(conn)
        permissions.sync(conn)
        with db.tx(conn):
            intelligence.backfill(conn)

    _store = secret_store.default_store()
    runner = workers.available_runner()
    base = scheduler.Scheduler(
        db.DB_PATH,
        sync_fn=sync_reports.sync_once,
        store=_store,
        runner=runner,
        exec_dir=execution.EXEC_DIR,
    )
    jobs = filter_cloud_jobs(base.jobs)
    return scheduler.Scheduler(
        db.DB_PATH,
        jobs=jobs,
        sync_fn=sync_reports.sync_once,
        store=_store,
        runner=runner,
        exec_dir=execution.EXEC_DIR,
    )


def _db_status() -> dict:
    from microtask_monitor import db

    with closing(db.connect(db.DB_PATH)) as conn:
        heartbeat = db.get_state(conn, "scheduler_heartbeat", {}) or {}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for table in ("opportunities", "assignments", "applications", "execution_jobs", "submission_attempts", "platform_capabilities"):
            if table in tables:
                counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        jobs = []
        if "scheduler_jobs" in tables:
            jobs = [dict(row) for row in conn.execute(
                "SELECT name,last_status,last_finished,consecutive_failures,disabled_reason FROM scheduler_jobs ORDER BY name"
            )]
    return {"heartbeat": heartbeat, "counts": counts, "jobs": jobs}


def _restore_migration(bundle: bytes, passphrase: str) -> dict:
    """Stop the scheduler, validate a passphrase-encrypted export, back up, then import operating state."""
    from microtask_monitor import cloud_export, connections, db, policy

    data_dir = Path(db.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    stage_parent = Path(tempfile.mkdtemp(prefix="autopilot-restore-", dir=str(data_dir)))
    stopped = False
    backup = None
    replaced = False
    try:
        manifest = cloud_export.unpack_to_staging(bundle, passphrase, stage_parent / "staged")
        staged_data = stage_parent / "staged" / "data"
        target_db = Path(db.DB_PATH)

        # Validate the incoming database again immediately before stopping the service.
        incoming = sqlite3.connect(str(staged_data / "autopilot.sqlite3"))
        try:
            check = incoming.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise cloud_export.CloudExportError("Imported SQLite database failed its final integrity check")
        finally:
            incoming.close()

        _stop.set()
        if _engine_thread and _engine_thread.is_alive():
            _engine_thread.join(timeout=30)
            if _engine_thread.is_alive():
                raise cloud_export.CloudExportError("Scheduler did not stop safely; migration cancelled")
        if _backup_thread and _backup_thread.is_alive():
            _backup_thread.join(timeout=30)
            if _backup_thread.is_alive():
                raise cloud_export.CloudExportError("Database backup thread did not stop safely; migration cancelled")
        stopped = True

        # Always preserve the pre-import database on the persistent disk.
        if target_db.exists():
            backup_dir = data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / ("pre-restore-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3")
            src = sqlite3.connect(str(target_db))
            dst = sqlite3.connect(str(backup))
            try:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise cloud_export.CloudExportError("Pre-import database backup failed integrity verification")
            finally:
                dst.close()
                src.close()

        os.replace(staged_data / "autopilot.sqlite3", target_db)
        replaced = True
        # Copy only the export format's explicit allowlist. Never import cookies, browser profiles or secrets.enc.
        for name in cloud_export.DATA_FILES:
            source = staged_data / name
            if source.is_file():
                shutil.copy2(source, data_dir / name)
        for name in cloud_export.DATA_DIRS:
            source = staged_data / name
            if source.is_dir():
                destination = data_dir / name
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(source, destination)

        private_profile = stage_parent / "staged" / "private" / "profile.json"
        if private_profile.is_file():
            profile_path = data_dir / "profile.json"
            shutil.copy2(private_profile, profile_path)
            try:
                os.chmod(profile_path, 0o600)
            except OSError:
                pass
            os.environ["AUTOPILOT_PROFILE_PATH"] = str(profile_path)

        # Windows browser sessions are not portable. Mark every formerly-confirmed session as requiring sign-in.
        with closing(db.connect(db.DB_PATH)) as conn:
            statuses = connections.list_status(conn)
            for item in statuses:
                if item.get("status") == "connected_user_confirmed":
                    connections.report_expired(conn, item["platform_id"])
        _prepare_engine()
        return {
            "ok": True,
            "files_imported": manifest["file_count"],
            "database_integrity": "ok",
            "pre_restore_backup": str(backup) if backup else None,
            "browser_sessions": "not imported; all previously confirmed sessions require fresh sign-in",
            "profile_imported": private_profile.is_file(),
            "secrets": "not imported; reconnect Whop and Gmail in cloud admin",
        }
    except Exception:
        # Never leave the worker stopped, and roll back the database swap if the import did not finish.
        if replaced and backup and backup.exists():
            recovery = target_db.with_name(target_db.name + ".rollback")
            shutil.copy2(backup, recovery)
            os.replace(recovery, target_db)
        raise
    finally:
        if stopped:
            _stop.clear()
            try:
                _worker_start()
            except Exception:
                LOG.exception("Could not restart cloud scheduler after migration")
        shutil.rmtree(stage_parent, ignore_errors=True)


def _parse_multipart(content_type: str, raw: bytes) -> tuple[bytes, str]:
    if not content_type.lower().startswith("multipart/form-data;"):
        raise ValueError("Upload must use the multipart form")
    msg = BytesParser(policy=email_policy).parsebytes(
        ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode("ascii") + raw
    )
    archive = None
    passphrase = ""
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if name == "archive":
            archive = payload
        elif name == "passphrase":
            passphrase = payload.decode("utf-8", "strict")
    if not archive or not passphrase:
        raise ValueError("Choose the encrypted migration file and enter its passphrase")
    return archive, passphrase


def _response_json(handler, status: int, value: dict):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _response_html(handler, status: int, text: str):
    raw = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _admin_page() -> str:
    from contextlib import closing
    from microtask_monitor import db, secret_store, whop

    store = _store or secret_store.default_store()
    with closing(db.connect(db.DB_PATH)) as conn:
        status = whop.oauth_status(conn, store)
    base = public_base_url()
    redirect_uri = status["redirect_uri"]
    vault_ready = bool(os.environ.get("AUTOPILOT_SECRET_ENCRYPTION_KEY"))
    worker_alive = bool(_engine_thread and _engine_thread.is_alive())
    whop_state = "Authorized (read-only)" if status.get("authorized") and status.get("has_read_scope") else "Not authorized"
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Microtask Autopilot Cloud</title><style>
body{font:15px system-ui,sans-serif;max-width:820px;margin:36px auto;padding:0 16px;color:#172033}
section{border:1px solid #d7deea;border-radius:10px;padding:18px;margin:16px 0}
input,button{font:inherit;padding:9px;border:1px solid #bcc8d8;border-radius:7px;max-width:100%}
button{cursor:pointer;background:#174f94;color:white;border:0}label{display:block;margin:10px 0}
.muted{color:#637087}.warn{color:#8d5200}.ok{color:#1c6a36}
</style></head><body>
<h1>Microtask Autopilot — Cloud Control</h1>
<p class="ok">Cloud process: """ + ("running" if worker_alive else "not running") + """</p>
<section><h2>Platform discovery and execution</h2>
<p>The public scanner runs independently of your PC. The cloud scheduler processes the persistent queue and only calls adapters whose platform permissions allow the action. Side-effectful prospecting is disabled by default. No withdrawal or prohibited assessment is automated.</p>
<p><a href="/admin/status">View scheduler status and queue counts</a></p></section>
<section><h2>Import your current PC state</h2>
<p>Upload the encrypted bundle created by <code>python -m microtask_monitor.cloud_export</code>. It restores the database, queue, history, permitted account metadata, configuration and work files. Browser sessions and Windows secrets are deliberately excluded and must be reconnected here.</p>
<form method="post" action="/admin/restore" enctype="multipart/form-data">
<label>Encrypted migration file <input type="file" name="archive" accept=".enc,application/octet-stream" required></label>
<label>Migration passphrase <input type="password" name="passphrase" required minlength="12" autocomplete="off"></label>
<button type="submit">Validate and import current engine state</button>
</form>
<p class="muted">The current cloud database is backed up before replacement. Never upload a plaintext database or browser profile.</p>
</section>
<section><h2>Whop read-only connection</h2>
<p>Status: <b>""" + html.escape(whop_state) + """</b></p>
<p class="muted">OAuth redirect URL to add to your Whop developer app:</p><code>""" + html.escape(redirect_uri) + """</code>
<form method="post" action="/admin/whop/client-id">
<label>Whop public app client ID <input name="client_id" required maxlength="64" placeholder="app_..."></label><button type="submit">Save public client ID</button>
</form>
<form method="post" action="/admin/whop/authorize"><button type="submit">Authorize Whop (read-only)</button></form>
<p class="muted">Only the documented <code>bounty:basic:read</code> scope is requested. Task initiation, submissions, social engagement and withdrawals remain human-only.</p>
</section>
<section><h2>Employer email monitoring</h2>
<p>To re-enable the approved Gmail reply monitor on this host, store your Gmail app password here. It is encrypted on the persistent disk and never returned to this page.</p>
<p class=""" + ("ok" if vault_ready else "warn") + """>Encrypted secret vault: """ + ("ready" if vault_ready else "not configured — add AUTOPILOT_SECRET_ENCRYPTION_KEY in the host's private environment settings first") + """</p>
<form method="post" action="/admin/secrets/gmail">
<label>Gmail app password <input type="password" name="app_password" required minlength="8" autocomplete="new-password"></label>
<button type="submit" """ + ("" if vault_ready else "disabled") + """>Save encrypted Gmail credential</button>
</form>
<p class="muted">The existing employer-reply feature remains governed by your imported database settings and its safety holds. Adding a credential does not automatically turn those settings on.</p>
</section>
<section><h2>Security</h2>
<p>Use the host-generated admin token with HTTP Basic Auth (username <code>admin</code>). Do not share it in chat. Browser sessions and Windows Credential Manager entries are not copied to the cloud.</p></section>
</body></html>"""


def _backup_database_once() -> dict:
    """Create an atomic SQLite online backup and retain the last 7 daily snapshots."""
    from microtask_monitor import db

    data_dir = Path(db.DATA_DIR)
    source_path = Path(db.DB_PATH)
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    final_path = backup_dir / ("daily-" + stamp.strftime("%Y%m%d") + ".sqlite3")
    temp_path = final_path.with_suffix(".sqlite3.tmp")
    source = sqlite3.connect(str(source_path), timeout=30)
    target = sqlite3.connect(str(temp_path), timeout=30)
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("Daily SQLite backup failed integrity_check")
        target.commit()
    finally:
        target.close()
        source.close()
    try:
        os.chmod(temp_path, 0o600)
    except OSError:
        pass
    os.replace(temp_path, final_path)
    old = sorted(backup_dir.glob("daily-*.sqlite3"))
    for path in old[:-7]:
        path.unlink(missing_ok=True)
    return {"path": str(final_path), "integrity": "ok"}


def _backup_loop():
    """Take a consistent database backup once per UTC day, with cancellation on shutdown."""
    from datetime import timedelta
    while not _stop.is_set():
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=2, minute=15, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        if _stop.wait(max(1.0, (next_run - now).total_seconds())):
            return
        try:
            result = _backup_database_once()
            LOG.info("Daily database backup completed: %s", Path(result["path"]).name)
        except Exception:
            LOG.exception("Daily database backup failed")


def _worker_start():
    global _engine, _engine_thread, _backup_thread
    _engine = _prepare_engine()

    def run():
        LOG.info("Cloud scheduler started with %s jobs", len(_engine.jobs))
        _engine.loop(_stop, tick=15)

    _engine_thread = threading.Thread(target=run, name="autopilot-cloud-scheduler", daemon=True)
    _backup_thread = threading.Thread(target=_backup_loop, name="autopilot-daily-db-backup", daemon=True)
    _engine_thread.start()
    _backup_thread.start()


class Handler(BaseHTTPRequestHandler):
    server_version = "MicrotaskAutopilotCloud/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Never log URL query strings (OAuth authorization codes) or request bodies/headers.
        LOG.info("HTTP %s %s", self.command, urlparse(self.path).path)

    def _authorized(self) -> bool:
        try:
            return authorize_header(
                self.headers.get("Authorization"),
                _admin_token(),
                os.environ.get("AUTOPILOT_ADMIN_USER", "admin"),
            )
        except RuntimeError:
            return False

    def _origin_ok(self) -> bool:
        base = public_base_url()
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if not base or origin != base:
            return False
        if os.environ.get("AUTOPILOT_RUNTIME", "cloud").lower() == "cloud":
            return self.headers.get("X-Forwarded-Proto", "https").lower() == "https"
        return True

    def _admin_required(self) -> bool:
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Microtask Autopilot Cloud"')
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def _body(self, limit: int = 8192) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > limit:
            raise ValueError("Request body too large")
        return self.rfile.read(length)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            alive = bool(_engine_thread and _engine_thread.is_alive())
            _response_json(self, 200 if alive else 503, {"ok": alive, "worker_alive": alive, "uptime_seconds": int(time.time() - _started_at)})
            return
        if path == "/":
            self.send_response(302); self.send_header("Location", "/admin"); self.send_header("Content-Length", "0"); self.end_headers()
            return
        if path == "/api/whop/oauth/callback":
            self._whop_callback()
            return
        if path in ("/admin", "/admin/"):
            if self._admin_required():
                _response_html(self, 200, _admin_page())
            return
        if path == "/admin/status":
            if not self._admin_required():
                return
            try:
                _response_json(self, 200, {"ok": True, "worker_alive": bool(_engine_thread and _engine_thread.is_alive()), **_db_status()})
            except Exception as exc:
                LOG.exception("Could not read cloud status")
                _response_json(self, 503, {"ok": False, "error": str(exc)[:160]})
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/admin/whop/client-id", "/admin/whop/authorize", "/admin/secrets/gmail", "/admin/restore"):
            self.send_error(404)
            return
        if not self._admin_required():
            return
        if not self._origin_ok():
            self.send_error(403, "Same-origin HTTPS request required")
            return
        try:
            if path == "/admin/restore":
                from microtask_monitor import cloud_export
                raw_upload = self._body(limit=cloud_export.MAX_UPLOAD_BYTES + 1024 * 1024)
                bundle, passphrase = _parse_multipart(self.headers.get("Content-Type", ""), raw_upload)
                result = _restore_migration(bundle, passphrase)
                _response_html(self, 200, "<!doctype html><title>Migration complete</title><h1>Cloud engine state imported</h1><pre>" +
                               html.escape(json.dumps(result, indent=2)) + "</pre><p>Reconnect Whop and Gmail, then review /admin/status.</p><p><a href='/admin'>Return to cloud control</a></p>")
                return
            from contextlib import closing
            from microtask_monitor import db, secret_store, whop
            raw = self._body()
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            with closing(db.connect(db.DB_PATH)) as conn:
                if path == "/admin/whop/client-id":
                    value = (fields.get("client_id") or [""])[0].strip()
                    whop.set_client_id(conn, value)
                    self._redirect("/admin")
                    return
                if path == "/admin/whop/authorize":
                    out = whop.begin_oauth(conn)
                    self.send_response(302); self.send_header("Location", out["authorize_url"])
                    self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", "0"); self.end_headers()
                    return
                value = (fields.get("app_password") or [""])[0].replace(" ", "").strip()
                if len(value) < 8:
                    self.send_error(400, "Gmail app password looks too short")
                    return
                store = _store or secret_store.default_store()
                secret_store.put_long(store, "owner_gmail", "app_password", value)
                self._redirect("/admin")
        except Exception as exc:
            LOG.exception("Cloud admin action failed")
            _response_html(self, 400, "<!doctype html><title>Request failed</title><p>" + html.escape(str(exc)[:200]) + "</p><p><a href='/admin'>Back</a></p>")

    def _whop_callback(self):
        from contextlib import closing
        from microtask_monitor import db, secret_store, whop
        query = parse_qs(urlparse(self.path).query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        error = (query.get("error_description") or query.get("error") or [""])[0]
        try:
            store = _store or secret_store.default_store()
            with closing(db.connect(db.DB_PATH)) as conn:
                status = whop.complete_oauth(conn, store, code, state, error=error)
            message = "Whop read-only connection verified." if status.get("authorized") and status.get("has_read_scope") else "Whop authorization finished; review the dashboard."
            _response_html(self, 200, "<!doctype html><title>Whop connection</title><h1>" + html.escape(message) + "</h1><p><a href='/admin'>Return to cloud control</a></p>")
        except Exception as exc:
            LOG.warning("Whop OAuth callback failed: %s", str(exc)[:160])
            _response_html(self, 400, "<!doctype html><title>Whop connection</title><h1>Authorization was not completed</h1><p>" + html.escape(str(exc)[:200]) + "</p><p><a href='/admin'>Return to cloud control</a></p>")

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    global _server
    os.environ.setdefault("AUTOPILOT_RUNTIME", "cloud")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not public_base_url() and os.environ.get("AUTOPILOT_RUNTIME", "").lower() == "cloud":
        LOG.warning("No public HTTPS base URL yet; Whop cloud OAuth stays disabled until RENDER_EXTERNAL_URL is available")
    _admin_token()
    _worker_start()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "10000"))
    _server = ThreadingHTTPServer((host, port), Handler)
    _server.daemon_threads = True
    LOG.info("Cloud control service listening on %s:%s", host, port)
    try:
        _server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        _server.server_close()
        if _engine_thread:
            _engine_thread.join(timeout=20)
        if _backup_thread:
            _backup_thread.join(timeout=20)


if __name__ == "__main__":
    main()
