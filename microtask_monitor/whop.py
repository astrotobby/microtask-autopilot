"""Whop Workforce (platform 13): bounties through Whop's documented API, with your own OAuth authorization.

Sources (checked 2026-11-10):
  https://docs.whop.com/api-reference/bounties/bounty                  the Bounty object and its fields
  https://docs.whop.com/api-reference/bounties/list-bounties           GET /bounties ("for a user token, the bounties the user can see and work")
  https://docs.whop.com/api-reference/bounty-submissions/bounty-submission   your own attempts and their review status
  https://docs.whop.com/developer/guides/oauth                         OAuth 2.1 + PKCE, 1-hour access tokens, rotating refresh tokens

What this adapter does: reads bounties and YOUR submissions with a user token that carries only ``bounty:basic:read``,
filters them for Kenya, availability and your hardware, routes suitable ones to the execution engine, and follows the
review result. What it never does: open whop.com pages, scrape, start or submit an attempt, like/follow/comment for
you, withdraw money, or call any endpoint that is not in the documentation above. Submitting stays a step you do on Whop.

Tokens live only in Windows Credential Manager (``secret_store``). SQLite holds the client id, scopes and expiry time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from microtask_monitor import assignments as asg, connections as cx, db, execution as ex, notify, policy, secret_store as ss
except ImportError:  # pragma: no cover
    import assignments as asg, connections as cx, db, execution as ex, notify, policy, secret_store as ss

PLATFORM = "whop_workforce"
API_BASE = "https://api.whop.com/api/v1"
AUTHORIZE_URL = "https://api.whop.com/oauth/authorize"
TOKEN_URL = "https://api.whop.com/oauth/token"
COMMUNITY_URL = "https://whop.com/whop-workforce/"
REDIRECT_PATH = "/api/whop/oauth/callback"
READ_SCOPES = ("bounty:basic:read",)                 # the minimum: listing bounties and reading your own submissions
GOAL_TYPES = ("clipping", "post_engagement", "owned_account_growth", "ugc_content", "local_activation", "data_capture", "other")
DELIVERABLE_TYPES = ("content_url", "media", "data_capture")
UNAVAILABLE = ("scheduled", "closed", "completed", "canceled", "cancelled", "unavailable")
LOW_VALUE_NET = 0.50                                 # below this (in the bounty's currency) a bounty is flagged, not hidden
MAX_BYTES = 8 * 1024 * 1024
USER_AGENT = "microtask-autopilot/1.0 (local personal tool; official API only)"
PKCE_TTL = 900

Http = Callable[[str, str, dict, dict | None], tuple[int, dict]]


class WhopError(RuntimeError):
    pass


class NotAuthorized(WhopError):
    """No usable user authorization: the message says exactly what is missing."""


# ------------------------------------------------------------------------------------------- HTTP (official API hosts only)
def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    if not url.startswith("https://api.whop.com/"):
        raise WhopError("Only Whop's documented API host is contacted")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                                         **({"Content-Type": "application/json"} if data else {}), **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(MAX_BYTES + 1), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(MAX_BYTES + 1), exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WhopError(f"Whop API not reachable: {ss.redact(str(exc))[:200]}")
    if len(raw) > MAX_BYTES:
        raise WhopError("Whop API response too large")
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {"data": parsed}


# ------------------------------------------------------------------------------------------- OAuth (PKCE, public client)
_pending: dict[str, tuple[str, float]] = {}          # state -> (code_verifier, created); memory only, never written to disk


def redirect_uri(port: int = 8765) -> str:
    return f"http://localhost:{port}{REDIRECT_PATH}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def oauth_meta(conn) -> dict[str, Any]:
    return db.get_state(conn, "whop_oauth", {}) or {}


def set_client_id(conn, client_id: str) -> dict[str, Any]:
    """The public id of the OAuth app YOU created in Whop's developer dashboard (``app_...``). It is not a secret."""
    client_id = str(client_id or "").strip()
    if not re.fullmatch(r"app_[A-Za-z0-9]{6,40}", client_id):
        raise WhopError("The client id looks like app_xxxxxxxx (copy it from your app in Whop's Developer Dashboard)")
    with db.tx(conn):
        db.set_state(conn, "whop_oauth", {**oauth_meta(conn), "client_id": client_id})
        db.audit(conn, "whop.client_id", "platform", PLATFORM)
    return oauth_status(conn, None)


def begin_oauth(conn, port: int = 8765, scopes: tuple[str, ...] = READ_SCOPES) -> dict[str, Any]:
    """Build the authorize URL. You open it in your own browser and approve on whop.com; no password passes through here."""
    meta = oauth_meta(conn)
    if not meta.get("client_id"):
        raise NotAuthorized("Missing prerequisite: create a free OAuth app in Whop's Developer Dashboard, add the redirect URL "
                            f"{redirect_uri(port)}, enable the scope bounty:basic:read, then save its client id here.")
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v[1] > PKCE_TTL]:
        _pending.pop(k, None)
    verifier, state = _b64(secrets.token_bytes(48)), _b64(secrets.token_bytes(24))
    _pending[state] = (verifier, now)
    query = urllib.parse.urlencode({"response_type": "code", "client_id": meta["client_id"], "redirect_uri": redirect_uri(port),
                                    "scope": " ".join(scopes), "state": state, "nonce": _b64(secrets.token_bytes(16)),
                                    "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"})
    return {"authorize_url": f"{AUTHORIZE_URL}?{query}", "redirect_uri": redirect_uri(port), "scopes": list(scopes)}


def _store_tokens(conn, store, tokens: dict, scopes: list[str] | None = None) -> None:
    access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
    if not access:
        raise WhopError("Whop returned no access token")
    ss.put_long(store, PLATFORM, "access", access)
    if refresh:
        ss.put_long(store, PLATFORM, "refresh", refresh)
    meta = oauth_meta(conn)
    granted = tokens.get("scope")
    meta.update({"expires_at": time.time() + float(tokens.get("expires_in") or 3600), "obtained_at": db.now_iso(),
                 "scopes": (granted.split() if isinstance(granted, str) and granted else scopes or meta.get("scopes") or list(READ_SCOPES))})
    with db.tx(conn):
        db.set_state(conn, "whop_oauth", meta)


def complete_oauth(conn, store, code: str, state: str, port: int = 8765, http: Http = http_json, error: str = "") -> dict[str, Any]:
    """Handle the redirect from Whop: verify state, exchange the single-use code, keep the tokens in Credential Manager."""
    if error:
        raise NotAuthorized(f"Whop did not authorize the app: {ss.redact(error)[:200]}")
    pending = _pending.pop(str(state or ""), None)
    if not pending or time.time() - pending[1] > PKCE_TTL:
        raise NotAuthorized("This authorization was not started from this program or has expired. Start again from the Whop tab.")
    meta = oauth_meta(conn)
    status, body = http("POST", TOKEN_URL, {}, {"grant_type": "authorization_code", "code": str(code or ""), "redirect_uri": redirect_uri(port),
                                              "client_id": meta.get("client_id"), "code_verifier": pending[0]})
    if status != 200:
        raise NotAuthorized(f"Token exchange failed ({status}): {ss.redact(str(body.get('error_description') or body.get('error') or ''))[:200]}")
    _store_tokens(conn, store, body)
    with db.tx(conn):
        db.audit(conn, "whop.oauth.authorized", "platform", PLATFORM, {"scopes": oauth_meta(conn).get("scopes")})
    return verify_connection(conn, store, http)


def access_token(conn, store, http: Http = http_json, force_refresh: bool = False) -> str:
    """A valid access token, refreshed when it is about to expire. Refresh tokens rotate, so the new pair is stored each time."""
    meta = oauth_meta(conn)
    token = ss.get_long(store, PLATFORM, "access")
    if token and not force_refresh and time.time() < float(meta.get("expires_at") or 0) - 300:
        return token
    refresh = ss.get_long(store, PLATFORM, "refresh")
    if not refresh or not meta.get("client_id"):
        raise NotAuthorized("Whop is not authorized yet (or the authorization expired). Open the Whop tab and press Authorize.")
    status, body = http("POST", TOKEN_URL, {}, {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": meta["client_id"]})
    if status != 200:
        if status in (400, 401) or body.get("error") == "invalid_grant":
            disconnect(conn, store, "Your Whop authorization expired or was revoked. Press Authorize again.")
        raise NotAuthorized(f"Whop session expired ({status}). Authorize again from the Whop tab.")
    _store_tokens(conn, store, body)
    return body["access_token"]


def disconnect(conn, store, reason: str = "Disconnected by you.") -> dict[str, Any]:
    ss.delete_long(store, PLATFORM, "access")
    ss.delete_long(store, PLATFORM, "refresh")
    meta = oauth_meta(conn)
    with db.tx(conn):
        db.set_state(conn, "whop_oauth", {"client_id": meta.get("client_id")} if meta.get("client_id") else {})
    cx.clear_verification(conn, PLATFORM, reason)
    return oauth_status(conn, store)


def oauth_status(conn, store, port: int = 8765) -> dict[str, Any]:
    meta = oauth_meta(conn)
    has_token = bool(store and (ss.get_long(store, PLATFORM, "access") or ss.get_long(store, PLATFORM, "refresh")))
    if not meta.get("client_id"):
        missing = ("Create an OAuth app (free) at https://whop.com/dashboard/developer, add the redirect URL below, enable only the "
                   "scope bounty:basic:read, then paste the app's client id (app_...) here.")
    elif not has_token:
        missing = "Press Authorize and approve the app on whop.com in your own browser."
    else:
        missing = None
    return {"client_id_saved": bool(meta.get("client_id")), "authorized": has_token, "scopes": meta.get("scopes") or [],
            "required_scopes": list(READ_SCOPES), "has_read_scope": READ_SCOPES[0] in (meta.get("scopes") or []),
            "obtained_at": meta.get("obtained_at"), "redirect_uri": redirect_uri(port), "missing_prerequisite": missing,
            "developer_dashboard": "https://whop.com/dashboard/developer", "token_storage": "Windows Credential Manager (never SQLite, logs or this page)"}


# ------------------------------------------------------------------------------------------- API reads
def _get(conn, store, path: str, params: dict[str, Any], http: Http) -> dict:
    url = f"{API_BASE}{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    token = access_token(conn, store, http)
    status, body = http("GET", url, {"Authorization": f"Bearer {token}"}, None)
    if status == 401:                                                   # expired early: refresh once
        token = access_token(conn, store, http, force_refresh=True)
        status, body = http("GET", url, {"Authorization": f"Bearer {token}"}, None)
    if status == 403:
        raise NotAuthorized("Whop refused the request: this authorization lacks the scope bounty:basic:read. Enable it on your app and authorize again.")
    if status == 429:
        raise WhopError("Whop rate limit reached; try again later")
    if status != 200:
        raise WhopError(f"Whop API error {status}: {ss.redact(str(body.get('message') or body.get('error') or ''))[:200]}")
    return body


def _paged(conn, store, path: str, params: dict[str, Any], http: Http, max_pages: int) -> tuple[list[dict], bool]:
    """Cursor pagination as documented (``first`` / ``after`` with ``page_info``). Returns (items, complete)."""
    items, after, seen = [], None, set()
    for _ in range(max_pages):
        body = _get(conn, store, path, {**params, "first": 100, "after": after}, http)
        for row in body.get("data") or []:
            if isinstance(row, dict) and row.get("id") and row["id"] not in seen:
                seen.add(row["id"])
                items.append(row)
        info = body.get("page_info") or {}
        if not info.get("has_next_page") or not info.get("end_cursor") or info["end_cursor"] == after:
            return items, True
        after = info["end_cursor"]
    return items, False


def fetch_bounties(conn, store, http: Http = http_json, country: str = "KE", max_pages: int = 20) -> tuple[list[dict], bool]:
    return _paged(conn, store, "/bounties", {"status": "open", "country": country}, http, max_pages)


def fetch_my_submissions(conn, store, http: Http = http_json, max_pages: int = 10) -> tuple[list[dict], bool]:
    return _paged(conn, store, "/bounty_submissions", {}, http, max_pages)


def verify_connection(conn, store, http: Http = http_json) -> dict[str, Any]:
    """A connection counts as verified only when the documented API answers this token. Nothing is assumed from a redirect."""
    body = _get(conn, store, "/bounties", {"first": 1, "status": "open"}, http)
    if not isinstance(body.get("data"), list):
        raise WhopError("Unexpected reply from the Whop bounties API")
    return cx.mark_api_verified(conn, PLATFORM, "GET /bounties answered 200 for your user token", oauth_meta(conn).get("scopes") or list(READ_SCOPES))


# ------------------------------------------------------------------------------------------- evaluation
_IOS = re.compile(r"\b(iphone|ios app|ios only|whop ios|apple device|testflight)\b", re.I)
_HEAD_MOUNT = re.compile(r"\bhead[- ]?(mount|strap|cam)", re.I)
_OTHER_COUNTRY_ONLY = re.compile(r"\b(us|u\.s\.|usa|american?s?|uk|canada|canadian)\b[\w\s,&/]{0,25}\bonly\b|\bonly\b[\w\s]{0,12}\b(us|usa|american?s?)\b", re.I)
_RESULT_DEPENDENT = re.compile(r"\b(paid sale|per sale|generate \d+ (?:paid )?sales?|get (?:a )?sale|refer \d+|referr?al link|affiliate link|friends? to sign up|drive \d+)\b", re.I)
_UPFRONT = re.compile(r"\b(pay|deposit|purchase|buy|subscribe)\b[^.\n]{0,40}\b(first|upfront|to (?:join|start|unlock|qualify|participate))\b|\b(seed phrase|private key|send (?:us )?your (?:password|login))\b", re.I)
_ENGAGEMENT = re.compile(r"\b(like|follow|comment|retweet|repost|upvote|engage|enage|share|join|sign[- ]?up|post)\b", re.I)
_FACE = re.compile(r"\b(selfie|show your face|on camera|testimonial|film yourself|record yourself)\b", re.I)

ROUTES = {
    "clipping": ("content", "assisted", "The engine can plan clips, timestamps, captions and a format checklist. Cutting and exporting real video from the real source is done by you; no footage is invented."),
    "ugc_content": ("content", "assisted", "The engine can prepare a script and shot list. The recording must be genuinely yours; nothing is synthesised to look like a real person or review."),
    "owned_account_growth": ("content", "assisted", "The engine can draft compliant posts and disclosure wording. Posting from your own accounts, and any sign-ups by other people, are real-world actions it does not perform."),
    "post_engagement": (None, "human_only", "Likes, follows, comments and shares are never automated or simulated. If you choose to do this bounty you do it personally from your own real account."),
    "local_activation": (None, "human_only", "Needs a real physical presence: confirm the required location before taking it. Location is never faked."),
    "data_capture": (None, "human_only", "Footage is recorded in the Whop app on a supported device. Recordings are never simulated."),
}


def device_os(profile: dict | None = None) -> str:
    devices = (profile or {}).get("devices") or {}
    if devices.get("ios_available") is True or "iphone" in str(devices.get("phone", "")).lower():
        return "ios"
    return "android"


def evaluate(b: dict[str, Any], country: str = "KE", os_name: str = "android", now: datetime | None = None) -> dict[str, Any]:
    """Decide what a bounty is for THIS user. Returns decision (excluded / review / human_only / executable), reasons, flags, routing."""
    now = now or datetime.now(timezone.utc)
    text = f"{b.get('title') or ''}\n{b.get('description') or ''}"
    countries = [str(c).upper() for c in (b.get("allowed_country_codes") or [])]
    deliverables = list(b.get("accepted_deliverable_types") or [])
    goal = b.get("business_goal_type") if b.get("business_goal_type") in GOAL_TYPES else None
    excluded, review, flags = [], [], []

    status = str(b.get("status") or "").lower()
    if status != "open":
        excluded.append(f"Not open for new submissions (status: {status or 'unknown'})")
    sched = b.get("scheduled_publish_at")
    if sched:
        try:
            if datetime.fromisoformat(str(sched).replace("Z", "+00:00")) > now:
                excluded.append("Not published yet")
        except ValueError:
            pass
    if b.get("submissions_closed_at") or b.get("cancel_requested_at"):
        excluded.append("Submissions have been stopped by the poster")
    if int(b.get("spots_remaining") or 0) <= 0:
        excluded.append("No winner slots remain")
    per_user = int(b.get("accepted_submissions_per_user_limit") or 1)
    if int(b.get("viewer_accepted_submissions_count") or 0) >= per_user:
        excluded.append("You have already used your allowance on this bounty")
    if countries and country not in countries:
        excluded.append(f"Kenya ({country}) is not in the bounty's allowed countries")
    kenya = "worldwide" if not countries else ("listed" if country in countries else "not_listed")

    capture = goal == "data_capture" or "data_capture" in deliverables or bool(b.get("capture_spec")) or bool(b.get("min_total_verified_duration_seconds"))
    hardware_ok, hardware_note = True, "No special device stated"
    if capture or _IOS.search(text):
        hardware_note = "Requires an iPhone / the Whop iOS app" + ("; a head mount is also required" if _HEAD_MOUNT.search(text) else "")
        if os_name != "ios":
            hardware_ok = False
            excluded.append(f"{hardware_note}: your phone is Android (Samsung Galaxy A05), so this is not compatible")
    elif _HEAD_MOUNT.search(text):
        hardware_note = "Mentions a head mount: confirm you own one before taking it"
        review.append(hardware_note)

    if _OTHER_COUNTRY_ONLY.search(text) and kenya != "not_listed":
        review.append("The text says another country only, although the country list allows Kenya: ask the poster before doing any work")
    if _UPFRONT.search(text):
        flags.append("suspicious")
        review.append("Asks for a payment, purchase or credentials: treat as suspicious")
    if _RESULT_DEPENDENT.search(text):
        flags.append("result_dependent")
        review.append("The reward depends on other people buying or signing up, not on work you can complete yourself")
    if len((b.get("description") or "").strip()) < 120:
        flags.append("poorly_specified")
        review.append("The instructions are too short to know what counts as done")
    net = b.get("net_reward_amount")
    gross = b.get("gross_reward_amount")
    if net is None or gross is None:
        flags.append("reward_missing")
        review.append("The reward is not stated by the API")
    elif float(net) < LOW_VALUE_NET:
        flags.append("low_value")

    engagement_title = bool(_ENGAGEMENT.search(str(b.get("title") or "")))
    if goal in ROUTES and not (engagement_title and goal == "owned_account_growth"):
        worker, mode, note = ROUTES[goal]
    else:
        cls = ex.classify(str(b.get("title") or ""), str(b.get("description") or ""))
        if engagement_title:                       # posting, sharing, following, joining: personal actions, whatever the declared goal
            worker, mode, note = None, "human_only", ROUTES["post_engagement"][2]
        elif cls["work_type"] == "unknown":
            worker, mode, note = None, "review", "The instructions do not clearly describe one kind of work: read them before deciding."
        else:
            worker, mode, note = cls["work_type"], "assisted", f"Routed from the instruction text ({cls['reasons'][0]})."
    human_parts = []
    if _FACE.search(text):
        human_parts.append("you on camera")
    if "content_url" in deliverables and goal in ("clipping", "ugc_content", "owned_account_growth", "post_engagement"):
        human_parts.append("posting from your own account")
    ai_rule = ex.detect_ai_rules(b.get("description") or "")

    if excluded:
        decision = "excluded"
    elif review or mode == "review":
        decision = "review"
    elif mode == "human_only":
        decision = "human_only"
    else:
        decision = "executable"
    return {"decision": decision, "reasons": excluded or review or [note], "flags": flags, "kenya": kenya, "hardware_ok": hardware_ok,
            "hardware_note": hardware_note,
            "routing": {"goal": goal or "unspecified", "worker": worker, "mode": mode, "note": note, "human_parts": human_parts,
                        "deliverables": deliverables, "ai_rule": ai_rule["status"], "ai_rule_evidence": ai_rule["evidence"]},
            "reward": {"currency": str(b.get("currency") or "").upper() or None, "gross": gross, "net": net,
                       "fee": round(float(gross) - float(net), 2) if gross is not None and net is not None else None,
                       "per_user_limit": per_user, "note": "Net is Whop's quote after the standard platform fee; the amount actually credited can differ."}}


# ------------------------------------------------------------------------------------------- persistence
def bounty_url(b: dict[str, Any]) -> str:
    route = (b.get("hosting_account") or {}).get("route") or b.get("hosting_route")
    return f"https://whop.com/{route}/" if route and re.fullmatch(r"[A-Za-z0-9_-]+", route) else COMMUNITY_URL


def upsert_bounties(conn, bounties: list[dict[str, Any]], source: str, complete: bool = False, profile: dict | None = None,
                    now: datetime | None = None, notify_new: bool = True) -> dict[str, Any]:
    """Store bounties returned by the official API. ``source`` records how they were obtained. Re-importing never duplicates."""
    if source not in ("oauth_api", "official_connector"):
        raise WhopError("Bounties are only stored from Whop's official API")
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    os_name = device_os(profile if profile is not None else _profile())
    counts = {"received": 0, "new": 0, "excluded": 0, "review": 0, "human_only": 0, "executable": 0, "assignments_created": 0}
    for b in bounties:
        bid = str(b.get("id") or "")
        if not re.fullmatch(r"bnty_[A-Za-z0-9]+", bid) or not b.get("title"):
            continue
        ev = evaluate(b, os_name=os_name, now=now)
        counts["received"] += 1
        counts[ev["decision"]] += 1
        host, disc = b.get("hosting_account") or {}, {k: b.get(k) for k in ("discussion_experience_id", "discussion_feed_id", "discussion_post_id")}
        is_new = conn.execute("SELECT 1 FROM whop_bounties WHERE id=?", (bid,)).fetchone() is None
        with db.tx(conn):
            conn.execute("""INSERT INTO whop_bounties(id,title,description,status,currency,gross_reward,net_reward,spots_remaining,accepted_limit,per_user_limit,
                viewer_accepted,business_goal_type,deliverable_types_json,allowed_countries_json,min_verified_seconds,hosting_route,hosting_title,poster_username,
                experience_id,discussion_json,scheduled_publish_at,remote_created_at,remote_updated_at,decision,reasons_json,flags_json,routing_json,hardware_ok,
                source,first_seen_at,last_verified_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description, status=excluded.status, currency=excluded.currency,
                gross_reward=excluded.gross_reward, net_reward=excluded.net_reward, spots_remaining=excluded.spots_remaining, accepted_limit=excluded.accepted_limit,
                per_user_limit=excluded.per_user_limit, viewer_accepted=excluded.viewer_accepted, business_goal_type=excluded.business_goal_type,
                deliverable_types_json=excluded.deliverable_types_json, allowed_countries_json=excluded.allowed_countries_json,
                min_verified_seconds=excluded.min_verified_seconds, hosting_route=excluded.hosting_route, hosting_title=excluded.hosting_title,
                poster_username=excluded.poster_username, experience_id=excluded.experience_id, discussion_json=excluded.discussion_json,
                scheduled_publish_at=excluded.scheduled_publish_at, remote_updated_at=excluded.remote_updated_at, decision=excluded.decision,
                reasons_json=excluded.reasons_json, flags_json=excluded.flags_json, routing_json=excluded.routing_json, hardware_ok=excluded.hardware_ok,
                source=excluded.source, last_verified_at=excluded.last_verified_at, raw_json=excluded.raw_json""",
                         (bid, str(b["title"])[:300], b.get("description"), b.get("status"), str(b.get("currency") or "").upper() or None, b.get("gross_reward_amount"),
                          b.get("net_reward_amount"), b.get("spots_remaining"), b.get("accepted_submissions_limit"), b.get("accepted_submissions_per_user_limit"),
                          b.get("viewer_accepted_submissions_count"), b.get("business_goal_type"), json.dumps(b.get("accepted_deliverable_types") or []),
                          json.dumps(b.get("allowed_country_codes") or []), b.get("min_total_verified_duration_seconds"), host.get("route"), host.get("title"),
                          (b.get("poster") or {}).get("username"), b.get("experience_id"), json.dumps(disc), b.get("scheduled_publish_at"), b.get("created_at"),
                          b.get("updated_at"), ev["decision"], json.dumps(ev["reasons"]), json.dumps(ev["flags"]), json.dumps({**ev["routing"], "kenya": ev["kenya"],
                          "hardware_note": ev["hardware_note"], "reward": ev["reward"]}), 1 if ev["hardware_ok"] else 0, source, stamp, stamp,
                          json.dumps({k: v for k, v in b.items() if k != "active_proof_livestream_feeds"}, ensure_ascii=False)))
        counts["new"] += 1 if is_new else 0
        if ev["decision"] == "executable":
            before = _attempts(conn, bid)
            track(conn, bid)
            counts["assignments_created"] += 0 if before else 1
            if is_new and notify_new:
                with db.tx(conn):
                    notify.create(conn, "new_opportunity", f"Whop bounty you can work on: {str(b['title'])[:120]}",
                                  f"{ev['reward']['currency']} {ev['reward']['net']} net per accepted submission (quoted). {ev['routing']['note']}",
                                  bounty_url(b), key=f"whop:{bid}", channels={})
    if complete:                                                       # a full listing: anything open that Whop no longer returns is gone
        with db.tx(conn):
            conn.execute("UPDATE whop_bounties SET status='unavailable', decision='excluded', reasons_json=? WHERE status='open' AND last_verified_at<?",
                         (json.dumps(["No longer returned by Whop's open-bounty list"]), stamp))
    with db.tx(conn):
        db.set_state(conn, "whop_last_sync", {"at": stamp, "source": source, "complete": complete, **counts})
    return counts


def _profile() -> dict:
    try:
        return json.loads((policy.ROOT / "microtask_monitor" / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _attempts(conn, bounty_id: str) -> list[dict[str, Any]]:
    """Assignments for one bounty, oldest first. A new attempt after a rejection is its own assignment (``bnty_x#2``)."""
    return db.rows(conn.execute("SELECT id, external_id, stage FROM assignments WHERE platform_id=? AND (external_id=? OR external_id LIKE ?) ORDER BY id",
                                (PLATFORM, bounty_id, bounty_id + "#%")))


def track(conn, bounty_id: str, new_attempt: bool = False) -> dict[str, Any]:
    """Turn a stored bounty into an assignment (once). Only bounties that passed every exclusion check are qualified.
    ``new_attempt`` starts a fresh assignment after an earlier one was rejected or cancelled, while the bounty is still workable."""
    row = conn.execute("SELECT * FROM whop_bounties WHERE id=?", (bounty_id,)).fetchone()
    if not row:
        raise WhopError("Unknown bounty: sync from Whop first")
    if row["decision"] == "excluded":
        raise WhopError("This bounty is excluded: " + "; ".join(json.loads(row["reasons_json"] or "[]")))
    routing = json.loads(row["routing_json"] or "{}")
    ai_use = "human_only" if routing.get("mode") == "human_only" else ("prohibited" if routing.get("ai_rule") == "prohibited" else "unknown")
    ai_source = "The bounty's own instructions" if ai_use != "unknown" else ""
    if ai_use == "unknown" and routing.get("ai_rule") == "hint_allowed":
        ai_use, ai_source = "allowed", f"The bounty's own instructions say: \"{routing.get('ai_rule_evidence')}\""
    existing = _attempts(conn, bounty_id)
    external_id = row["id"]
    if existing:
        last = existing[-1]
        if not new_attempt or last["stage"] not in ("rejected", "cancelled"):
            if new_attempt:
                raise WhopError("A new attempt is only for a bounty whose last attempt was rejected or cancelled")
            return asg.get(conn, last["id"])
        external_id = f"{row['id']}#{len(existing) + 1}"
    a = asg.create(conn, PLATFORM, external_id, row["title"], "whop_bounty", row["description"] or "", bounty_url(dict(row)), None, row["gross_reward"],
                   row["net_reward"], row["currency"], {"goal": routing.get("goal"), "deliverables": routing.get("deliverables"), "worker": routing.get("worker"),
                                                        "mode": routing.get("mode"), "human_parts": routing.get("human_parts"), "hardware": routing.get("hardware_note"),
                                                        "kenya": routing.get("kenya"), "spots_remaining": row["spots_remaining"]},
                   ai_use=ai_use, ai_use_source=ai_source, listing_ref=f"whop_api:{row['id']}")
    if row["decision"] in ("executable", "human_only"):
        a = asg.qualify(conn, a["id"], True, [f"Open, slots remaining: {row['spots_remaining']}", f"Kenya: {routing.get('kenya')}", routing.get("hardware_note") or ""],
                        f"whop_api:{row['id']}@{row['last_verified_at']}")
    return a


def apply_submissions(conn, submissions: list[dict[str, Any]], source: str) -> dict[str, int]:
    """Follow YOUR real attempts on Whop. Stages move only on what the platform record says."""
    counts = {"received": 0, "accepted": 0, "submitted": 0, "approved": 0, "denied": 0}
    stamp = db.now_iso()
    for s in submissions:
        sid, bid, status = str(s.get("id") or ""), str(s.get("bounty_id") or ""), s.get("status")
        if not re.fullmatch(r"btys_[A-Za-z0-9]+", sid) or not bid:
            continue
        counts["received"] += 1
        with db.tx(conn):
            conn.execute("""INSERT INTO whop_submissions(id,bounty_id,status,claimed_at,submitted_at,resolved_at,denial_reason,deliverable_json,source,last_checked_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, submitted_at=excluded.submitted_at, resolved_at=excluded.resolved_at,
                denial_reason=excluded.denial_reason, deliverable_json=excluded.deliverable_json, last_checked_at=excluded.last_checked_at""",
                         (sid, bid, status, s.get("claimed_at"), s.get("submitted_at"), s.get("resolved_at"), s.get("denial_reason"),
                          json.dumps({"urls": [u for u in (s.get("deliverable_urls") or []) if isinstance(u, str) and "X-Amz-" not in u][:10], "content": s.get("content")}),
                          source, stamp))
        if not conn.execute("SELECT 1 FROM whop_bounties WHERE id=?", (bid,)).fetchone() or status not in ("in_progress", "submitted", "approved", "denied"):
            continue
        ref = f"whop_submission:{sid}"
        attempts = _attempts(conn, bid)
        known = conn.execute("SELECT assignment_id FROM assignment_events WHERE evidence_ref=? LIMIT 1", (ref,)).fetchone()
        claimed = {r["assignment_id"] for r in conn.execute("SELECT DISTINCT assignment_id FROM assignment_events WHERE evidence_ref LIKE 'whop_submission:%'")}
        free = [x for x in attempts if x["id"] not in claimed and x["stage"] not in ("rejected", "cancelled", "paid")]
        if known:
            aid = known["assignment_id"]
        elif free:
            aid = free[-1]["id"]                                       # the attempt you were preparing here is the one Whop now shows
        else:
            b = conn.execute("SELECT * FROM whop_bounties WHERE id=?", (bid,)).fetchone()
            aid = asg.create(conn, PLATFORM, bid if not attempts else f"{bid}#{len(attempts) + 1}", b["title"], "whop_bounty", b["description"] or "",
                             bounty_url(dict(b)), None, b["gross_reward"], b["net_reward"], b["currency"], listing_ref=f"whop_api:{bid}")["id"]
        try:
            asg.advance(conn, aid, "qualified", "eligibility_check", ref, "You hold an attempt on Whop for this bounty")
            asg.advance(conn, aid, "ready", "requirements_check", ref, "Attempt exists on Whop")
            asg.advance(conn, aid, "accepted", "platform_record", ref, f"Whop shows your attempt ({status})", actor="platform")
            counts["accepted"] += 1
            if status in ("submitted", "approved", "denied"):
                asg.advance_through(conn, aid, "awaiting_review", "platform_record", ref, f"Whop shows the proof was submitted on {str(s.get('submitted_at') or '')[:10]}", actor="platform")
                counts["submitted"] += 1
            if status == "approved":
                asg.advance(conn, aid, "work_accepted", "platform_record", ref, "Whop shows the submission approved", actor="platform")
                counts["approved"] += 1
            elif status == "denied":
                asg.advance(conn, aid, "rejected", "platform_record", ref, "Denied: " + str(s.get("denial_reason") or "no reason given")[:300], actor="platform")
                counts["denied"] += 1
        except asg.AssignmentError:
            continue                                                 # e.g. already terminal: the record above is still stored
    return counts


def sync(conn, store, http: Http = http_json) -> dict[str, Any]:
    """One authorized pass: open bounties workable from Kenya, then your own submissions."""
    policy.require(PLATFORM, "api_read")
    bounties, complete = fetch_bounties(conn, store, http)
    counts = upsert_bounties(conn, bounties, "oauth_api", complete)
    subs, _ = fetch_my_submissions(conn, store, http)
    cx.mark_api_verified(conn, PLATFORM, f"Sync read {len(bounties)} bounties and {len(subs)} of your submissions", oauth_meta(conn).get("scopes") or list(READ_SCOPES))
    return {"bounties": counts, "submissions": apply_submissions(conn, subs, "oauth_api"), "complete": complete}


def import_official_response(conn, payload: dict[str, Any], kind: str = "bounties") -> dict[str, Any]:
    """Load a response body that came from Whop's official API through an already-authorized official connector.
    Used for the first real import before this program has its own OAuth app; never marks the connection as verified."""
    data = payload.get("data")
    if not isinstance(data, list):
        raise WhopError("Not a Whop API list response")
    if kind == "submissions":
        return apply_submissions(conn, data, "official_connector")
    return upsert_bounties(conn, data, "official_connector", complete=False)


# ------------------------------------------------------------------------------------------- reads for the dashboard
def list_bounties(conn, decision: str | None = None) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM whop_bounties", []
    if decision:
        sql += " WHERE decision=?"
        args.append(decision)
    out = []
    for r in conn.execute(sql + " ORDER BY CASE decision WHEN 'executable' THEN 0 WHEN 'human_only' THEN 1 WHEN 'review' THEN 2 ELSE 3 END, net_reward DESC", args):
        d = dict(r)
        d.pop("raw_json")
        for k in ("deliverable_types", "allowed_countries", "discussion", "reasons", "flags", "routing"):
            d[k] = json.loads(d.pop(k + "_json") or "null")
        d["url"] = bounty_url(d)
        attempts = _attempts(conn, d["id"])
        d["assignment"] = attempts[-1] if attempts else None
        d["attempts"] = len(attempts)
        d["can_retry"] = bool(attempts) and attempts[-1]["stage"] in ("rejected", "cancelled") and d["decision"] != "excluded"
        s = conn.execute("SELECT id,status,submitted_at,resolved_at,denial_reason FROM whop_submissions WHERE bounty_id=? ORDER BY last_checked_at DESC LIMIT 1", (d["id"],)).fetchone()
        d["submission"] = dict(s) if s else None
        out.append(d)
    return out


def view(conn, store, port: int = 8765) -> dict[str, Any]:
    rows = list_bounties(conn)
    by = {}
    for r in rows:
        by[r["decision"]] = by.get(r["decision"], 0) + 1
    return {"platform_id": PLATFORM, "community_url": COMMUNITY_URL, "oauth": oauth_status(conn, store, port), "connection": cx.status(conn, PLATFORM),
            "last_sync": db.get_state(conn, "whop_last_sync", None), "counts": by, "bounties": rows, "device": _profile().get("devices") or {},
            "payout": {"status": "unverified", "note": "Whop's payout terms name ACH, push-to-card, wire and cheque 'subject to jurisdiction' and list no countries. "
                       "Check the payout methods offered in YOUR Whop account from Kenya and finish any identity check before relying on a reward. "
                       "An approved bounty credits your Whop balance; it is money received only after a withdrawal you made arrives.",
                       "sources": ["https://whop.com/earnings-terms/", "https://whop.com/payout-terms/"]}}
