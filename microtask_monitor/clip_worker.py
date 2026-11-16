"""Clip worker for the owner lane: footage link -> chosen moments -> 9:16 captioned clips -> executed QC.

It reuses your existing clipper (``wc/whop-clipper``): faster-whisper transcription, the moment heuristic and the
caption renderer. Following the playbook, a YouTube episode is never downloaded whole: captions are read first, the
moments are chosen from them, and only those sections are downloaded (at 1080p). Google Drive files are downloaded
because Drive offers no section download.

QC checks are executed on the rendered file, never assumed:
    plays           ffprobe finds a 1080x1920 video stream and an audio stream
    duration        20-60 seconds
    hook_start      the clip opens on a complete thought of at least four words, not on filler
    finished_end    the clip ends on a finished sentence, not a dangling word
    captions_match  the rendered audio is re-transcribed and must match the caption words
    frames          first and last frames are not black
    english         the speech is clearly English (Whisper garbles other languages)
A clip ships only if every check passed.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

YOUTUBE_RX = re.compile(r"https://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)[A-Za-z0-9_-]{6,}[^\s<>)\"']*")
DRIVE_RX = re.compile(r"https://drive\.google\.com/[^\s<>)\"']+")
EN_COMMON = {"the", "and", "you", "that", "it", "is", "to", "of", "a", "in", "i", "we", "what", "this", "so", "but", "have", "be", "they", "was", "for", "on", "not", "my", "with"}
FILLER = {"um", "uh", "and", "but", "because", "like", "yeah", "anyway", "so"}
DANGLING = {"a", "an", "the", "and", "or", "but", "of", "to", "if", "because", "that", "your", "my", "is", "are", "was", "with", "for", "in", "on", "at", "so"}


class ClipError(RuntimeError):
    pass


@dataclass
class W:
    start: float
    end: float
    text: str


@dataclass
class ClipResult:
    path: Path
    start: float
    end: float
    hook: str
    caption: str
    hashtags: list[str]
    qc: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.qc) and all(c["status"] == "pass" and c["executed"] for c in self.qc)


def find_link(text: str) -> str | None:
    """The first footage link in a buyer's message: a YouTube video or a Google Drive file/folder."""
    for rx in (YOUTUBE_RX, DRIVE_RX):
        m = rx.search(text or "")
        if m:
            return m.group(0).rstrip(".,;")
    return None


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z']+", (text or "").lower())


def english_ratio(words: list[W]) -> float:
    toks = [t for w in words for t in tokens(w.text)]
    return sum(t in EN_COMMON for t in toks) / max(1, len(toks))


# ------------------------------------------------------------------------------------------- media operations
class Media:
    """Real tools: yt-dlp, gdown, ffmpeg/ffprobe and the clipper package. Tests replace this class."""

    def __init__(self, clipper_dir: Path, font: str = "Arial Black", whisper_model: str = "small"):
        self.clipper_dir, self.font, self.whisper_model = Path(clipper_dir), font, whisper_model
        if not (self.clipper_dir / "clipper" / "render.py").exists():
            raise ClipError(f"Clipper not found at {self.clipper_dir}")
        if str(self.clipper_dir) not in sys.path:
            sys.path.insert(0, str(self.clipper_dir))

    def captions(self, url: str, work: Path) -> list[W]:
        """English captions of a YouTube video as timed words, without downloading the video."""
        work.mkdir(parents=True, exist_ok=True)
        subprocess.run(["yt-dlp", "--no-playlist", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en",
                        "--sub-format", "json3", "-o", str(work / "cap.%(ext)s"), url], check=True, capture_output=True, timeout=300)
        files = sorted(work.glob("cap*.json3"))
        if not files:
            raise ClipError("No English captions are available for this video")
        data = json.loads(files[0].read_text(encoding="utf-8"))
        out: list[W] = []
        for e in data.get("events", []):
            t0 = e.get("tStartMs", 0) / 1000
            segs = [s for s in e.get("segs") or [] if s.get("utf8", "").strip()]
            for k, s in enumerate(segs):
                st = t0 + s.get("tOffsetMs", 0) / 1000
                nxt = t0 + segs[k + 1].get("tOffsetMs", 0) / 1000 if k + 1 < len(segs) else st + 0.5
                for piece in s["utf8"].replace("\n", " ").split():
                    out.append(W(st, max(st + 0.05, nxt), piece))
        return out

    def download_section(self, url: str, start: float, end: float, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["yt-dlp", "--no-playlist", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
                        "--download-sections", f"*{max(0.0, start):.2f}-{end:.2f}", "--force-keyframes-at-cuts", "-o", str(out), url],
                       check=True, capture_output=True, timeout=1800)
        if not out.exists():
            cands = sorted(out.parent.glob(out.stem + "*"))
            if not cands:
                raise ClipError("The section download produced no file")
            return cands[0]
        return out

    def download_drive(self, url: str, work: Path) -> Path:
        from clipper import sources
        files = sources.fetch(url, work, max_files=1)
        if not files:
            raise ClipError("No video file was found at that Google Drive link")
        return files[0]

    def transcribe(self, video: Path) -> list[W]:
        from clipper.transcribe import transcribe
        return [W(w.start, w.end, w.text) for w in transcribe(video, self.whisper_model)]

    def render(self, src: Path, words: list[W], start: float, end: float, out: Path, hook: str) -> Path:
        from clipper.render import render
        from clipper.transcribe import Word
        return render(src, [Word(w.start, w.end, w.text) for w in words], start, end, out, hook=hook, font=self.font)

    def probe(self, path: Path) -> dict[str, Any]:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {"ok": False}
        d = json.loads(r.stdout or "{}")
        streams = d.get("streams") or []
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        return {"ok": True, "video": bool(v), "audio": any(s.get("codec_type") == "audio" for s in streams), "width": v.get("width"),
                "height": v.get("height"), "duration": float((d.get("format") or {}).get("duration") or 0)}

    def frame_luma(self, path: Path, t: float) -> float | None:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(path), "-frames:v", "1", "-vf", "scale=64:-1,format=gray",
                            "-f", "rawvideo", "-"], capture_output=True, timeout=60)
        return (sum(r.stdout) / len(r.stdout)) if r.returncode == 0 and r.stdout else None


# ------------------------------------------------------------------------------------------- moment selection
def _sentences(words: list[W]) -> list[W]:
    out, buf = [], []
    for k, w in enumerate(words):
        buf.append(w)
        nxt = words[k + 1] if k + 1 < len(words) else None
        if re.search(r"[.?!]$", w.text) or len(buf) >= 30 or (nxt is not None and nxt.start - w.end > 0.8):
            out.append(W(buf[0].start, buf[-1].end, " ".join(x.text for x in buf)))
            buf = []
    if buf:
        out.append(W(buf[0].start, buf[-1].end, " ".join(x.text for x in buf)))
    return out


def _score(chunk: list[W]) -> float:
    text = " ".join(s.text for s in chunk)
    toks, first, last = tokens(text), tokens(chunk[0].text), tokens(chunk[-1].text)
    dur = max(1.0, chunk[-1].end - chunk[0].start)
    sc = len(toks) / dur * 4
    sc += 3 * ("?" in chunk[0].text or "!" in chunk[0].text)
    sc += 2 * bool(re.search(r"[.?!]$", chunk[-1].text))
    sc -= 5 * bool(last and last[-1] in DANGLING)
    sc -= 6 * bool(first[:1] and first[0] in FILLER)
    sc -= 8 * (len(first) < 4)
    return sc


def pick_moments(words: list[W], n: int, lo: float, hi: float, exclude: list[tuple[float, float]] | None = None) -> list[tuple[float, float, str]]:
    """Up to ``n`` non-overlapping windows of whole sentences, best first: (start, end, text)."""
    sents = _sentences(words)
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            dur = sents[j].end - sents[i].start
            if dur > hi:
                break
            if dur >= lo:
                cands.append((_score(sents[i:j + 1]), sents[i].start, sents[j].end, " ".join(s.text for s in sents[i:j + 1])))
    used = list(exclude or [])
    out = []
    for sc, a, b, text in sorted(cands, key=lambda c: -c[0]):
        if any(a < ue and b > us for us, ue in used):
            continue
        used.append((a, b))
        out.append((a, b, text))
        if len(out) >= n:
            break
    return sorted(out)


def copy_for(text: str, show: str = "") -> dict[str, Any]:
    """Hook, caption and hashtags from the clip's own words (nothing invented)."""
    parts = [p for p in re.split(r"(?<=[.?!])\s", text.strip()) if len(p.split()) >= 5] or [text.strip()]
    first = re.sub(r"^(okay|ok|so|alright)[,]?\s+", "", parts[0], flags=re.I)
    if len(first) > 90:
        first = first[:90].rsplit(" ", 1)[0].rstrip(",;") + "…"
    first = first[:1].upper() + first[1:]
    counts: dict[str, int] = {}
    for t in re.findall(r"[a-z]{5,}", text.lower()):
        counts[t] = counts.get(t, 0) + 1
    tags = [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3]] + ["podcast", "shorts"]
    return {"hook": " ".join(first.rstrip("…").split()[:7]), "caption": first + (f"\n\nFrom {show}" if show else ""), "hashtags": tags[:5]}


# ------------------------------------------------------------------------------------------- QC
def qc(media: Any, clip: Path, words: list[W], start: float, end: float) -> list[dict[str, Any]]:
    res = []

    def add(name: str, ok: bool, detail: str) -> None:
        res.append({"name": name, "status": "pass" if ok else "fail", "executed": True, "detail": detail})

    p = media.probe(clip)
    add("plays", bool(p.get("ok") and p.get("video") and p.get("audio") and (p.get("width"), p.get("height")) == (1080, 1920)),
        f"video={p.get('video')} audio={p.get('audio')} size={p.get('width')}x{p.get('height')}")
    dur = float(p.get("duration") or 0)
    add("duration", 20 <= dur <= 60, f"{dur:.1f}s (allowed 20-60s)")
    inside = [w for w in words if w.start >= start - 0.01 and w.end <= end + 0.01]      # words fully inside the clip, not its padding
    sents = _sentences(inside)
    first = tokens(sents[0].text) if sents else []
    add("hook_start", len(first) >= 4 and first[0] not in FILLER, f"opens with: {(sents[0].text if sents else '')[:80]!r}")
    last_word = inside[-1].text if inside else ""
    add("finished_end", bool(re.search(r"[.?!]$", last_word)) or (bool(tokens(last_word)) and tokens(last_word)[-1] not in DANGLING),
        f"ends with: {last_word!r}")
    heard = media.transcribe(clip)
    ratio = difflib.SequenceMatcher(None, [t for w in inside for t in tokens(w.text)], [t for w in heard for t in tokens(w.text)]).ratio()
    add("captions_match", ratio >= 0.6, f"caption words vs re-transcribed audio similarity {ratio:.2f} (needs 0.60)")
    lumas = [media.frame_luma(clip, 0.2), media.frame_luma(clip, max(0.0, dur - 0.3))]
    add("frames", all(v is not None and v > 16 for v in lumas), f"first/last frame brightness {lumas}")
    er = english_ratio(inside)
    add("english", er >= 0.18, f"common-English-word share {er:.2f} (needs 0.18)")
    return res


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------- one clip
def plan_moments(media: Any, url: str, work: Path, n: int, lo: float, hi: float, exclude: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Choose moments for ``n`` clips. YouTube: from captions only. Drive: from a full transcript."""
    if YOUTUBE_RX.match(url):
        words = media.captions(url, work)
    elif DRIVE_RX.match(url):
        words = media.transcribe(media.download_drive(url, work / "source"))
    else:
        raise ClipError("Only YouTube or Google Drive links are supported")
    if english_ratio(words) < 0.18:
        raise ClipError("The speech does not look clearly English: a person should check this order")
    found = pick_moments(words, n, lo, hi, exclude)
    if not found:
        raise ClipError("No usable moment of the right length was found in this footage")
    return [(a, b) for a, b, _ in found]


def make_clip(media: Any, url: str, start: float, end: float, work: Path, out: Path, lo: float, hi: float, show: str = "") -> ClipResult:
    """Render one clip for a planned moment and QC it. Exact boundaries come from a fresh transcript of the footage itself."""
    pad = 2.0
    if YOUTUBE_RX.match(url):
        src = media.download_section(url, start - pad, end + pad, work / f"sec-{int(start)}-{int(end)}.mp4")
        offset = max(0.0, start - pad)
    else:
        src = next(iter(sorted((work / "source").glob("*"))), None) or media.download_drive(url, work / "source")
        offset = 0.0
    words = media.transcribe(src)
    if offset:                                   # the section's words, re-picked with exact timing
        best = pick_moments(words, 1, lo - 3, hi + 3)
    else:
        best = pick_moments([w for w in words if w.end > start - 1 and w.start < end + 1], 1, lo - 3, hi + 3)
    if not best:
        raise ClipError("The downloaded section had no complete sentences of the right length")
    a, b, text = best[0]
    a, b = max(0.0, a - 0.15), b + 0.3
    copy = copy_for(text, show)
    media.render(src, words, a, b, out, copy["hook"])
    return ClipResult(out, offset + a, offset + b, copy["hook"], copy["caption"], copy["hashtags"], qc(media, out, words, a, b))
