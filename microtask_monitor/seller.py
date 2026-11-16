"""Seller-side Whop client for YOUR OWN business (owner lane).

Authenticated with an account API key you create on Whop and type into a hidden prompt:
    py -3 microtask_monitor/connect.py set-key owner_whop api_key
The key lives only in Windows Credential Manager. It is never written to SQLite, logs, reports or API responses.

Only these documented operations are used (https://docs.whop.com/api-reference):
    GET  /payments            sales of the account (payment:basic:read)
    POST /channels            kind=support with the buyer: opens or returns the 1:1 support chat
    GET  /messages            read the support chat (support_chat:read)
    POST /messages            write in the support chat, with an Idempotency-Key (support_chat:message:create)
    POST /files, GET /files/{id} and a PUT to the presigned upload URL   (deliverable clips)
Nothing here can refund, withdraw, change prices or edit products.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    from microtask_monitor import secret_store as ss
except ImportError:  # pragma: no cover
    import secret_store as ss

API_BASE = "https://api.whop.com/api/v1"
API_VERSION = "2026-11-14-1"
KEY_OWNER, KEY_NAME = "owner_whop", "api_key"
USER_AGENT = "microtask-autopilot-owner/1.0 (your own business; official API only)"
MAX_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
Http = Callable[..., tuple[int, dict]]


class SellerError(RuntimeError):
    pass


class NotConfigured(SellerError):
    """No account API key stored yet: the message says how to add it."""


def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 45) -> tuple[int, dict]:
    if not url.startswith(API_BASE + "/"):
        raise SellerError("Only Whop's documented API host is contacted")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                                         **({"Content-Type": "application/json"} if data else {}), **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(MAX_BYTES + 1), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(MAX_BYTES + 1), exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SellerError(f"Whop API not reachable: {ss.redact(str(exc))[:200]}")
    if len(raw) > MAX_BYTES:
        raise SellerError("Whop API response too large")
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {"data": parsed}


def _upload_host_ok(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return urlparse(url).scheme == "https" and (host == "whop.com" or host.endswith(".whop.com") or host.endswith(".amazonaws.com"))


def http_put(url: str, headers: dict, path: Path, timeout: int = 600) -> int:
    """PUT the bytes to the presigned upload URL Whop returned (its storage host; no Whop credential is sent)."""
    if not _upload_host_ok(url):
        raise SellerError("Refusing to upload to a host that is not Whop's file storage")
    data = Path(path).read_bytes()
    if len(data) > MAX_UPLOAD_BYTES:
        raise SellerError("File too large for a single upload")
    req = urllib.request.Request(url, data=data, method="PUT", headers={k: str(v) for k, v in (headers or {}).items()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SellerError(f"Upload failed: {ss.redact(str(exc))[:200]}")


class Seller:
    """Thin client. ``http`` and ``put`` are injectable so tests never touch the network."""

    def __init__(self, account_id: str, store: Any = None, http: Http = http_json, put: Callable[..., int] = http_put, key: str | None = None):
        if not str(account_id or "").startswith("biz_"):
            raise SellerError("The business id looks like biz_xxxxxxxx")
        self.account_id, self.http, self.put = account_id, http, put
        self._key = key
        self._store = store

    def _token(self) -> str:
        if self._key:
            return self._key
        store = self._store or ss.default_store()
        key = store.get(KEY_OWNER, KEY_NAME)
        if not key:
            raise NotConfigured("No Whop account API key stored for your business. Create one on Whop (Developer → API keys) with the "
                                "permissions listed in the dashboard, then run: py -3 microtask_monitor/connect.py set-key owner_whop api_key")
        return key

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None, idem: str | None = None) -> dict:
        query = urllib.parse.urlencode([(k, x) for k, v in (params or {}).items() if v not in (None, "")
                                        for x in (v if isinstance(v, (list, tuple)) else [v])])
        url = f"{API_BASE}{path}" + (f"?{query}" if query else "")
        headers = {"Authorization": f"Bearer {self._token()}", "Api-Version-Date": API_VERSION}
        if idem:
            headers["Idempotency-Key"] = idem
        status, data = self.http(method, url, headers, body)
        if status in (401, 403):
            raise NotConfigured(f"Whop refused the key for {path} ({status}): it lacks a permission or was revoked. "
                                f"{ss.redact(str(data.get('message') or data.get('error') or ''))[:200]}")
        if status == 429:
            raise SellerError("Whop rate limit reached; the next run retries")
        if status not in (200, 201):
            raise SellerError(f"Whop API error {status} on {method} {path}: {ss.redact(str(data.get('message') or data.get('error') or ''))[:200]}")
        return data

    # ---------------------------------------------------------------- reads
    def paid_payments(self, product_ids: list[str], since: str | None = None, max_pages: int = 5) -> list[dict]:
        out, after = [], None
        for _ in range(max_pages):
            data = self._call("GET", "/payments", {"account_id": self.account_id, "product_ids": product_ids, "status": "paid",
                                                  "created_after": since, "order": "created_at", "direction": "asc", "first": 50, "after": after})
            out += [p for p in data.get("data") or [] if isinstance(p, dict) and p.get("id")]
            info = data.get("page_info") or {}
            if not info.get("has_next_page") or not info.get("end_cursor"):
                break
            after = info["end_cursor"]
        return out

    def payment(self, payment_id: str) -> dict:
        return self._call("GET", f"/payments/{urllib.parse.quote(payment_id)}")

    def support_channel(self, buyer_user_id: str) -> str:
        data = self._call("POST", "/channels", body={"kind": "support", "account_id": self.account_id, "customer_user_id": buyer_user_id},
                          idem=f"support-{self.account_id}-{buyer_user_id}")
        cid = data.get("id")
        if not cid:
            raise SellerError("Whop returned no support channel id")
        return cid

    def messages(self, channel_id: str, first: int = 50) -> list[dict]:
        data = self._call("GET", "/messages", {"channel_id": channel_id, "first": first, "direction": "desc"})
        return [m for m in data.get("data") or [] if isinstance(m, dict)]

    # ---------------------------------------------------------------- writes (your own support chat and files only)
    def send(self, channel_id: str, content: str, idem: str, file_ids: list[str] | None = None) -> dict:
        body: dict[str, Any] = {"channel_id": channel_id, "content": content}
        if file_ids:
            body["attachments"] = [{"id": f} for f in file_ids]
        return self._call("POST", "/messages", body=body, idem=idem)

    def upload(self, path: Path, idem: str) -> dict:
        """Create a public file record (unguessable CDN URL, as the playbook uses), PUT the bytes, return the record."""
        rec = self._call("POST", "/files", body={"filename": Path(path).name, "visibility": "public"}, idem=idem)
        if rec.get("upload_status") == "ready":
            return rec
        if not rec.get("upload_url"):
            raise SellerError("Whop returned no upload URL")
        status = self.put(rec["upload_url"], rec.get("upload_headers") or {}, path)
        if status not in (200, 201, 204):
            raise SellerError(f"Upload to Whop storage failed ({status})")
        return rec

    def file(self, file_id: str) -> dict:
        return self._call("GET", f"/files/{urllib.parse.quote(file_id)}")
