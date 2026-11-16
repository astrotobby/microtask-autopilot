"""Find podcast buyers through the official YouTube Data API v3 (no page scraping).

A lead is a podcast channel whose Shorts get far fewer views than its full episodes (the "gap"), in an allowed country,
that publishes a business email in its own channel or video descriptions, and that does not say it refuses unsolicited
messages. The API key is free (Google Cloud console → YouTube Data API v3) and lives only in Windows Credential Manager:
    py -3 microtask_monitor/connect.py set-key owner_youtube api_key
Quota (default 10,000 units/day): search.list costs 100, channels/playlistItems/videos.list cost 1 each.
"""
from __future__ import annotations

import json
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

try:
    from microtask_monitor import secret_store as ss
except ImportError:  # pragma: no cover
    import secret_store as ss

API = "https://www.googleapis.com/youtube/v3"
KEY_OWNER, KEY_NAME = "owner_youtube", "api_key"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
BAD_EMAIL = ("example.", "sentry", "wixpress", "youtube.com", "google.com", "noreply", "no-reply", "@2x", ".png", ".jpg")
BUSINESS_HINT = re.compile(r"business|inquir|enquir|collab|partnership|sponsor|booking|contact|advertis|press|media", re.I)
NO_SOLICIT = re.compile(r"no (?:unsolicited|solicitation|cold|spam|marketing)|do not (?:solicit|pitch|send)|not accept(?:ing)? (?:pitches|solicitations)|unsolicited (?:emails?|pitches) (?:will|are)", re.I)
ISO8601 = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
SHORT_MAX_SECONDS = 180
EPISODE_MIN_SECONDS = 15 * 60
Http = Callable[[str], tuple[int, dict]]


class YouTubeError(RuntimeError):
    pass


def http_get(url: str, timeout: int = 30) -> tuple[int, dict]:
    if not url.startswith(API + "/"):
        raise YouTubeError("Only the official YouTube Data API host is contacted")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "microtask-autopilot/1.0 (official API)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read(4_000_000).decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(200_000).decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise YouTubeError(f"YouTube API not reachable: {ss.redact(str(e))[:200]}")


def seconds(iso: str) -> int:
    m = ISO8601.fullmatch(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def public_email(text: str) -> tuple[str | None, str]:
    """A business email written in the channel's own text, with the words around it as evidence."""
    for m in EMAIL_RE.finditer(text or ""):
        e = m.group(0).strip(".").lower()
        if any(b in e for b in BAD_EMAIL):
            continue
        ctx = text[max(0, m.start() - 90):m.end() + 20].replace("\n", " ")
        if BUSINESS_HINT.search(ctx) or BUSINESS_HINT.search(text[:400]):
            return e, ctx.strip()
    return None, ""


class YouTube:
    def __init__(self, key: str | None = None, store: Any = None, http: Http = http_get):
        self._key, self._store, self.http, self.units = key, store, http, 0

    def _k(self) -> str:
        if self._key:
            return self._key
        k = (self._store or ss.default_store()).get(KEY_OWNER, KEY_NAME)
        if not k:
            raise YouTubeError("No YouTube Data API key stored. Create a free key, then run: py -3 microtask_monitor/connect.py set-key owner_youtube api_key")
        return k

    def _get(self, path: str, cost: int, **params: Any) -> dict:
        q = urllib.parse.urlencode({**{k: v for k, v in params.items() if v not in (None, "")}, "key": self._k()})
        status, data = self.http(f"{API}/{path}?{q}")
        self.units += cost
        if status == 403 and "quota" in json.dumps(data).lower():
            raise YouTubeError("YouTube API daily quota used up; it resets at midnight Pacific time")
        if status != 200:
            raise YouTubeError(f"YouTube API {status} on {path}: {ss.redact(str((data.get('error') or {}).get('message') or ''))[:200]}")
        return data

    def search_channels(self, query: str, region: str | None = None, max_results: int = 25) -> list[str]:
        d = self._get("search", 100, part="snippet", type="video", q=query, videoDuration="long", maxResults=max_results,
                      regionCode=region, relevanceLanguage="en")
        return list(dict.fromkeys(i["snippet"]["channelId"] for i in d.get("items", []) if i.get("snippet", {}).get("channelId")))

    def channels(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            out += self._get("channels", 1, part="snippet,statistics,contentDetails,brandingSettings", id=",".join(ids[i:i + 50]), maxResults=50).get("items", [])
        return out

    def recent_videos(self, uploads_playlist: str, n: int = 50) -> list[dict]:
        ids = [i["contentDetails"]["videoId"] for i in self._get("playlistItems", 1, part="contentDetails", playlistId=uploads_playlist, maxResults=min(50, n)).get("items", [])]
        if not ids:
            return []
        return self._get("videos", 1, part="snippet,contentDetails,statistics", id=",".join(ids), maxResults=50).get("items", [])


def assess(ch: dict, videos: list[dict], allowed_countries: list[str], min_gap: float = 5.0) -> dict[str, Any]:
    """Qualify one channel. Returns {"ok", "reason", ...lead fields}. Every rejection says why."""
    sn, st = ch.get("snippet", {}), ch.get("statistics", {})
    country = (sn.get("country") or "").upper() or None
    lead: dict[str, Any] = {"channel_id": ch.get("id"), "name": sn.get("title") or "", "channel_url": f"https://www.youtube.com/channel/{ch.get('id')}",
                            "country": country, "subscribers": int(st.get("subscriberCount") or 0) if not st.get("hiddenSubscriberCount") else None}
    if country not in allowed_countries:
        return {**lead, "ok": False, "reason": f"country {country or 'not stated'} is outside {', '.join(allowed_countries)}"}
    shorts, episodes = [], []
    for v in videos:
        dur, views = seconds(v.get("contentDetails", {}).get("duration", "")), int(v.get("statistics", {}).get("viewCount") or 0)
        (shorts if 0 < dur <= SHORT_MAX_SECONDS else episodes if dur >= EPISODE_MIN_SECONDS else []).append((v, views))
    if len(episodes) < 3:
        return {**lead, "ok": False, "reason": "fewer than 3 recent full episodes"}
    if len(shorts) < 3:
        return {**lead, "ok": False, "reason": "fewer than 3 recent Shorts to compare"}
    ep_med, sh_med = statistics.median(v for _, v in episodes[:10]), statistics.median(v for _, v in shorts[:10])
    gap = ep_med / max(1.0, sh_med)
    lead.update(episodes_median=int(ep_med), shorts_median=int(sh_med), gap=round(gap, 1))
    if gap < min_gap:
        return {**lead, "ok": False, "reason": f"Shorts already do fine (gap {gap:.1f}x < {min_gap}x)"}
    texts = [sn.get("description") or ""] + [v.get("snippet", {}).get("description") or "" for v, _ in episodes[:5]]
    if any(NO_SOLICIT.search(t) for t in texts):
        return {**lead, "ok": False, "reason": "the channel says it does not want unsolicited messages"}
    email, ctx = next(((e, c) for e, c in (public_email(t) for t in texts) if e), (None, ""))
    if not email:
        return {**lead, "ok": False, "reason": "no business email published in the channel's own descriptions"}
    latest = episodes[0][0]
    lead.update(email=email, email_source=ctx[:240], episode_id=latest.get("id"), episode_title=latest.get("snippet", {}).get("title"))
    return {**lead, "ok": True, "reason": f"gap {gap:.1f}x, business email published"}


def discover(yt: YouTube, queries: list[str], allowed_countries: list[str], known: set[str], max_channels: int = 40, min_gap: float = 5.0) -> list[dict[str, Any]]:
    """Search, then qualify channels not seen before. Stops after ``max_channels`` new channels (quota guard)."""
    ids: list[str] = []
    for q in queries:
        for region in allowed_countries[:1] or [None]:
            ids += [c for c in yt.search_channels(q, region) if c not in known and c not in ids]
        if len(ids) >= max_channels:
            break
    results = []
    for ch in yt.channels(ids[:max_channels]):
        uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        results.append(assess(ch, yt.recent_videos(uploads) if uploads else [], allowed_countries, min_gap))
    return results
