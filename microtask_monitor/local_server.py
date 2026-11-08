"""Local dashboard server and background GitHub report synchronizer."""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from microtask_monitor.sync_reports import DB_PATH, LATEST_REPORT, sync_once
DASHBOARD = ROOT / "dashboard"
HOST = "127.0.0.1"
PORT = 8765
INTERVAL_SECONDS = 900
LOG = logging.getLogger("microtask_autopilot.server")


def background_sync() -> None:
    while True:
        try:
            result = sync_once()
            LOG.info("Background report sync: %s", result)
        except Exception:
            LOG.exception("Background report sync failed")
        time.sleep(INTERVAL_SECONDS)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD), **kwargs)

    def send_json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/report":
            if not LATEST_REPORT.exists():
                return self.send_json({"ok": False, "error": "No report has synced yet"}, 404)
            try:
                payload = json.loads(LATEST_REPORT.read_text(encoding="utf-8"))
                return self.send_json({"ok": True, **payload})
            except (OSError, json.JSONDecodeError) as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 500)
        if path == "/api/status":
            status = {"ok": True, "database": DB_PATH.exists(),
                      "latest_report": LATEST_REPORT.exists(),
                      "sync_interval_seconds": INTERVAL_SECONDS}
            if DB_PATH.exists():
                with sqlite3.connect(DB_PATH) as conn:
                    status["opportunity_count"] = conn.execute(
                        "SELECT COUNT(*) FROM opportunities").fetchone()[0]
                    status["report_count"] = conn.execute(
                        "SELECT COUNT(*) FROM reports").fetchone()[0]
            else:
                status["opportunity_count"] = 0
                status["report_count"] = 0
            if LATEST_REPORT.exists():
                try:
                    data = json.loads(LATEST_REPORT.read_text(encoding="utf-8"))
                    status["synced_at_utc"] = data.get("synced_at_utc")
                    status["report_checked_at_utc"] = data.get("report", {}).get("checked_at_utc")
                    status["run_url"] = data.get("sync", {}).get("run_url")
                except (OSError, json.JSONDecodeError):
                    status["report_error"] = "Latest report metadata could not be read"
            return self.send_json(status)
        return super().do_GET()

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DASHBOARD.mkdir(parents=True, exist_ok=True)
    worker = threading.Thread(target=background_sync, daemon=True, name="github-report-sync")
    worker.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
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
