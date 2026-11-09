"""Local dashboard server, JSON API and resilient background report synchronizer.

Binds to 127.0.0.1 only. Every request must carry a loopback Host header, and state-changing requests
must be same-origin JSON, which blocks other websites (CSRF) and DNS-rebinding attacks.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from microtask_monitor import api as api_module, daily, db, policy, reliability
from microtask_monitor.sync_reports import DB_PATH, LATEST_REPORT, sync_once

DASHBOARD = ROOT / "dashboard"
HOST = "127.0.0.1"
PORT = 8765
INTERVAL_SECONDS = 900
MAX_BODY_BYTES = 14 * 1024 * 1024
LOG = logging.getLogger("microtask_autopilot.server")

_sync_lock = threading.Lock()
_sync_running = threading.Event()


def run_sync_guarded() -> bool:
    """Run one sync unless one is already running. Returns True if this call performed it."""
    if not _sync_lock.acquire(blocking=False):
        return False
    _sync_running.set()
    try:
        result = sync_once()
        LOG.info("Report sync: %s", result)
        return True
    finally:
        _sync_running.clear()
        _sync_lock.release()


def trigger_sync_async() -> bool:
    if _sync_running.is_set():
        return False
    def work():
        try:
            run_sync_guarded()
        except Exception:                           # noqa: BLE001 - failure is recorded in sync_health
            LOG.exception("Manual sync failed")
    threading.Thread(target=work, daemon=True, name="manual-sync").start()
    return True


def next_delay(consecutive_failures: int, interval: int = INTERVAL_SECONDS) -> int:
    """Seconds to wait before the next sync: the normal interval after success, exponential backoff after failures."""
    if consecutive_failures <= 0:
        return interval
    return int(min(interval, 60 * (2 ** (consecutive_failures - 1))))


def background_sync(stop: threading.Event | None = None) -> None:
    failures = 0
    while not (stop and stop.is_set()):
        try:
            run_sync_guarded()
            failures = 0
        except Exception:                           # noqa: BLE001
            failures += 1
            LOG.exception("Background report sync failed (%s in a row)", failures)
        delay = next_delay(failures)
        if stop:
            if stop.wait(delay):
                return
        else:
            time.sleep(delay)


SCHEDULER_INTERVAL_SECONDS = 600


def maintain(db_path: Path) -> None:
    """Cheap local upkeep: due reminders (and their notifications) and today's action list."""
    from microtask_monitor import actions, reminders
    with closing(db.connect(db_path)) as conn:
        now = datetime.now(timezone.utc)
        with db.tx(conn):
            reminders.generate(conn, now)
        reminders.notify_due(conn, now)
        actions.persist_today(conn, now)


def daily_scheduler(stop: threading.Event | None = None, interval: int = SCHEDULER_INTERVAL_SECONDS,
                    db_path: Path | None = None) -> None:
    """Checks every 10 minutes whether the 08:00 East Africa Time analysis is due (at most one per Nairobi day).

    It only reads the local database, never contacts a platform, and a laptop that was off at 08:00 runs the
    missed analysis as soon as it is back on. The cloud scan keeps producing its own shortlist meanwhile.
    """
    while not (stop and stop.is_set()):
        try:
            maintain(db_path or DB_PATH)
        except Exception:                           # noqa: BLE001
            LOG.exception("Reminder/action maintenance failed")
        try:
            outcome = daily.run_if_due(db_path or DB_PATH)
            if outcome:
                LOG.info("Scheduled daily analysis: %s", outcome.get("skipped") or f"analysis {outcome.get('analysis_id')} completed")
        except Exception:                           # noqa: BLE001 - logged; the next check retries
            LOG.exception("Scheduled daily analysis failed")
        if stop:
            if stop.wait(interval):
                return
        else:
            time.sleep(interval)


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def request_allowed(method: str, headers, port: int) -> tuple[bool, str]:
    host = (headers.get("Host") or "").lower()
    if host not in allowed_hosts(port):
        return False, "Host not allowed"
    if method in ("POST", "PUT", "DELETE"):
        origin = headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() not in allowed_hosts(port):
            return False, "Cross-origin requests are not allowed"
        if "application/json" not in (headers.get("Content-Type") or "").lower():
            return False, "Content-Type must be application/json"
    return True, ""


def make_api(db_path: Path = DB_PATH, latest: Path = LATEST_REPORT, evidence_dir: Path | None = None) -> api_module.Api:
    return api_module.Api(db_path, latest, trigger_sync=trigger_sync_async, sync_running=_sync_running.is_set,
                          evidence_dir=evidence_dir)


class Handler(SimpleHTTPRequestHandler):
    api: api_module.Api = None          # set by serve()
    port: int = PORT
    db_path: Path = DB_PATH
    latest_path: Path = LATEST_REPORT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD), **kwargs)

    # -- helpers
    def send_json(self, payload, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _guard(self, method: str) -> bool:
        ok, why = request_allowed(method, self.headers, self.port)
        if not ok:
            if method in ("POST", "PUT"):           # drain a small rejected body so the client sees the 403, not a connection reset
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if 0 < n <= MAX_BODY_BYTES:
                        self.rfile.read(n)
                except (ValueError, OSError):
                    pass
            self.send_json({"ok": False, "error": why}, 403)
        return ok

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self.send_json({"ok": False, "error": "Request body too large or invalid"}, 413)
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self.send_json({"ok": False, "error": "Body must be valid JSON"}, 400)
            return None
        if not isinstance(data, dict):
            self.send_json({"ok": False, "error": "JSON body must be an object"}, 400)
            return None
        return data

    # -- verbs
    def do_GET(self):
        if not self._guard("GET"):
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            return self.send_json(self.legacy_status())
        if parsed.path.startswith("/api/"):
            status, payload = self.api.handle("GET", parsed.path, parse_qs(parsed.query))
            return self.send_json(payload, status)
        return super().do_GET()

    def do_POST(self):
        self._mutate("POST")

    def do_PUT(self):
        self._mutate("PUT")

    def _mutate(self, method: str) -> None:
        if not self._guard(method):
            return
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self.send_json({"ok": False, "error": "Not found"}, 404)
        body = self._read_body()
        if body is None:
            return
        status, payload = self.api.handle(method, parsed.path, parse_qs(parsed.query), body)
        self.send_json(payload, status)

    def do_DELETE(self):
        if self._guard("GET"):              # host check only; deleting is simply not offered
            self.send_json({"ok": False, "error": "Deleting records is not supported"}, 405)

    def legacy_status(self) -> dict:
        """Backward-compatible /api/status used by the earlier dashboard and run-windows-checks."""
        status = {"ok": True, "database": self.db_path.exists(), "latest_report": self.latest_path.exists(),
                  "sync_interval_seconds": INTERVAL_SECONDS, "opportunity_count": 0, "report_count": 0}
        if self.db_path.exists():
            try:
                with closing(db.connect(self.db_path)) as conn:
                    status["opportunity_count"] = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
                    status["report_count"] = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
                    health = db.get_state(conn, "sync_health", {}) or {}
                    status["last_sync_success_utc"] = health.get("last_success_utc")
                    status["consecutive_sync_failures"] = health.get("consecutive_failures", 0)
            except Exception as exc:                 # noqa: BLE001
                status.update(ok=False, database_error=str(exc))
        if self.latest_path.exists():
            try:
                data = json.loads(self.latest_path.read_text(encoding="utf-8"))
                status["synced_at_utc"] = data.get("synced_at_utc")
                status["report_checked_at_utc"] = data.get("report", {}).get("checked_at_utc")
                status["run_url"] = data.get("sync", {}).get("run_url")
            except (OSError, ValueError):
                status["report_error"] = "Latest report metadata could not be read"
        return status

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)


def serve(host: str = HOST, port: int = PORT, db_path: Path = DB_PATH, latest: Path = LATEST_REPORT,
          evidence_dir: Path | None = None) -> ThreadingHTTPServer:
    """Create the server (used by main() and by tests with an ephemeral port)."""
    handler = type("BoundHandler", (Handler,), {"api": make_api(db_path, latest, evidence_dir), "port": port,
                                                "db_path": Path(db_path), "latest_path": Path(latest)})
    server = ThreadingHTTPServer((host, port), handler)
    handler.port = server.server_address[1]
    return server


def prepare_backend() -> None:
    """Upgrade the database and mirror capability records before serving."""
    with closing(db.connect(DB_PATH)) as conn:
        with db.tx(conn):
            policy.sync_to_db(conn)
        from microtask_monitor import intelligence
        with db.tx(conn):
            n = intelligence.backfill(conn)
        if n:
            LOG.info("Classified %s existing opportunities with the new eligibility engine", n)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DASHBOARD.mkdir(parents=True, exist_ok=True)
    try:
        prepare_backend()
    except Exception:                               # noqa: BLE001 - the server must still start; the error is logged
        LOG.exception("Backend preparation failed; continuing with the existing database")
    threading.Thread(target=background_sync, daemon=True, name="github-report-sync").start()
    threading.Thread(target=daily_scheduler, daemon=True, name="daily-analysis-scheduler").start()
    server = serve()
    LOG.info("Dashboard: http://%s:%s", HOST, PORT)
    LOG.info("Local-only binding; not exposed to your network")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Stopping local dashboard server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
