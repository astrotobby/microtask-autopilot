"""Configurable, $0 AI assistance layer.

Default provider is ``rules``: deterministic, fully local, no model and no network. An optional
local Ollama model can polish wording (``AUTOPILOT_AI_PROVIDER=ollama``). The polish is guarded:
it is discarded if it adds numbers, links or length that were not in the original draft, so an
AI can never invent experience for you. No paid API is used or required.

Optional local model hardware note (not required): a 1-3B parameter model (e.g. qwen2.5:1.5b or
llama3.2:1b via Ollama) needs roughly 2-4 GB free RAM and runs on a CPU-only laptop, slowly.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Protocol
from urllib.parse import urlparse

try:
    from microtask_monitor import intelligence as ig, opportunity_common as oc
except ImportError:  # pragma: no cover
    import intelligence as ig, opportunity_common as oc

MAX_PROMPT_CHARS = 6000


class Provider(Protocol):
    name: str
    def generate(self, prompt: str) -> str | None: ...


class RulesProvider:
    name = "rules"

    def generate(self, prompt: str) -> str | None:
        return None                       # no model: callers use their deterministic output


class OllamaProvider:
    """Optional local model. Only loopback URLs are accepted; nothing leaves the machine."""
    name = "ollama"

    def __init__(self, url: str | None = None, model: str | None = None, timeout: int = 120):
        self.url = (url or os.environ.get("AUTOPILOT_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or os.environ.get("AUTOPILOT_OLLAMA_MODEL") or "llama3.2:1b"
        self.timeout = timeout
        host = urlparse(self.url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("The local model URL must be a loopback address")

    def generate(self, prompt: str) -> str | None:
        body = json.dumps({"model": self.model, "prompt": prompt[:MAX_PROMPT_CHARS], "stream": False}).encode()
        request = urllib.request.Request(self.url + "/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return str(json.loads(response.read(2_000_000)).get("response", "")).strip() or None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None                   # model unavailable: fall back to the rules output


def get_provider(name: str | None = None) -> Provider:
    name = (name or os.environ.get("AUTOPILOT_AI_PROVIDER") or "rules").lower()
    if name == "ollama":
        return OllamaProvider()
    return RulesProvider()


# ---------------------------------------------------------------- guard ------------------------------
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_URL = re.compile(r"https?://\S+")


def guard_polish(original: str, polished: str | None) -> str | None:
    """Accept AI-polished text only if it adds no numbers, links or bulk beyond the original."""
    if not polished:
        return None
    if not set(_NUM.findall(polished)) <= set(_NUM.findall(original)):
        return None
    if not set(_URL.findall(polished)) <= set(_URL.findall(original)):
        return None
    ratio = len(polished) / max(1, len(original))
    return polished if 0.5 <= ratio <= 1.8 else None


# ---------------------------------------------------------------- applications -----------------------
def _profile_skills(profile: dict[str, Any]) -> list[str]:
    return [s for group in profile.get("established_skills", {}).values() for s in group]


def relevant_skills(opportunity: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    """Only established skills that the posting actually mentions; never invented."""
    text = " ".join(str(opportunity.get(k) or "") for k in ("title", "summary", "category")).lower()
    text += " " + " ".join(str(s) for s in (opportunity.get("skills") or [])).lower()
    out = []
    for skill in _profile_skills(profile):
        key = skill.lower()
        words = [w for w in re.split(r"[^a-z0-9.+#]+", key) if len(w) > 2 and w not in ("and", "the", "workflows", "development")]
        if key in text or any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text) for w in words):
            out.append(skill)
    return out


def draft_application(opportunity: dict[str, Any], profile: dict[str, Any], provider: Provider | None = None) -> dict[str, Any]:
    """Create a truthful, reviewable draft. Gaps are listed, never papered over."""
    provider = provider or RulesProvider()
    cls = opportunity.get("eligibility_class") or "MANUAL_REVIEW_REQUIRED"
    title = opportunity.get("title") or "this opportunity"
    reasons = {
        ig.KENYA_CONFIRMED: "The listing's own country list includes Kenya.",
        ig.WORLDWIDE_CONFIRMED: "The listing states it is open worldwide. Confirm on the application form that Kenya is accepted.",
        ig.KENYA_UNVERIFIED: "Kenya eligibility is NOT confirmed. Check the country rules on the official page before applying.",
        ig.MANUAL_REVIEW_REQUIRED: "Eligibility needs manual review on the official page before you apply.",
        ig.KENYA_UNSUPPORTED: "Kenya is not eligible for this opportunity. Do not apply.",
        ig.EXPIRED: "This opportunity is no longer available. Do not apply.",
    }
    blocked = cls in (ig.KENYA_UNSUPPORTED, ig.EXPIRED)
    req = ig.extract_requirements(" ".join(str(opportunity.get(k) or "") for k in ("title", "summary")))
    gaps = ig.qualification_gaps(req, profile)
    skills = relevant_skills(opportunity, profile)
    missing = ["Your full name", "Contact details / preferred contact method", "Availability (hours per week and start date)",
               "Rate expectation (if the application asks)"]
    if not skills:
        missing.append("No established skill clearly matches this posting: confirm you are a fit before applying")
    if any("portfolio" in a or "GitHub" in a or "writing sample" in a for a in req["application_requirements"]):
        missing.append("Links to relevant work (portfolio, GitHub or writing samples)")
    missing += [f"Confirm yourself: {g}" for g in gaps]
    if req["application_requirements"]:
        missing.append("The posting also asks for: " + ", ".join(req["application_requirements"]))
    summary_line = re.split(r"(?<=[.!?])\s+", (opportunity.get("summary") or "").strip())[0][:220]
    lines = [f"Subject: Application for {title}", "", "Hello,", "",
             f"I am applying for the {title} opportunity" + (f" on {opportunity.get('source')}." if opportunity.get("source") else ".")]
    if summary_line:
        lines += ["", f"From the listing, I understand the work involves: {summary_line}"]
    if skills:
        lines += ["", "Relevant skills and tools I work with:"] + [f"- {s}" for s in skills]
    lines += ["", "[Add one or two sentences about a real, relevant project you completed, with a link.]", "",
              "[Your availability: hours per week and start date.]", "",
              "Thank you for your consideration.", "[Your name]", "[Contact details]"]
    body = "\n".join(lines)
    polished = guard_polish(body, provider.generate(
        "Improve the wording of this application draft. Do NOT add any experience, numbers, links or claims. "
        "Keep every [placeholder]. Draft:\n" + body)) if not blocked else None
    return {"summary": f"Draft for {title}", "body": polished or body, "missing_info": missing,
            "eligibility_note": reasons.get(cls, reasons[ig.MANUAL_REVIEW_REQUIRED]), "eligibility_class": cls,
            "matched_skills": skills, "qualification_gaps": gaps, "blocked": blocked,
            "provider": provider.name + ("+polish" if polished else ""),
            "apply_url": opportunity.get("apply_url") or opportunity.get("url"),
            "instructions": ["Review and edit every placeholder.", "Approve the draft in the dashboard.",
                             "Open the original application link and submit it yourself.",
                             "Record the submission date here afterwards."]}


# ---------------------------------------------------------------- task assistance --------------------
_IMPERATIVES = ("open", "click", "go", "read", "listen", "watch", "write", "record", "upload", "take", "select", "choose",
                "enter", "fill", "copy", "paste", "check", "review", "rate", "label", "tag", "answer", "translate",
                "transcribe", "submit", "visit", "search", "find", "create", "add", "follow", "complete", "send", "provide",
                "make", "use", "verify", "compare", "identify", "annotate", "describe", "summarize")
_RULE_RX = re.compile(r"\b(must|do not|don't|never|required|only|should not|cannot|prohibited|forbidden|at least|no more than)\b", re.I)
_EVIDENCE_RX = re.compile(r"\b(screenshot|proof|evidence|upload|photo|picture|recording|video|link|url|attach|receipt|submit)\b", re.I)
_DEADLINE_RX = re.compile(r"(within\s+\d+\s*(?:minutes?|hours?|days?)|by\s+\d{1,2}[:/.-]\d{1,2}[^\s,.]*|deadline[^.\n]*|\d{4}-\d{2}-\d{2})", re.I)
_MATERIAL_TERMS = {"account": "an account on the platform", "microphone": "a working microphone", "headphones": "headphones",
                   "camera": "a camera", "phone": "a phone", "laptop": "a laptop/computer", "spreadsheet": "a spreadsheet",
                   "screenshot": "a way to take screenshots", "browser": "a web browser", "email": "an email address",
                   "gmail": "a Gmail account", "word": "a word processor", "excel": "Excel/Sheets"}


def _sentences(text: str) -> list[str]:
    out = []
    for line in re.split(r"[\r\n]+", text or ""):
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        out += [s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]
    return out


def simplify_instructions(text: str) -> dict[str, Any]:
    """Rule-based breakdown of the ORIGINAL instructions. Nothing is added beyond the text itself,
    except clearly-labelled generic quality checks."""
    sentences = _sentences(text)
    numbered = [re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", l).strip() for l in (text or "").splitlines()
                if re.match(r"^\s*(?:\d+[.)]|[-*•])\s+\S", l)]
    steps = numbered or [s for s in sentences if s.split(" ", 1)[0].lower().strip(",:;") in _IMPERATIVES]
    rules = [s for s in sentences if _RULE_RX.search(s)]
    evidence = [s for s in sentences if _EVIDENCE_RX.search(s)]
    deadlines = [m.group(0).strip() for m in _DEADLINE_RX.finditer(text or "")]
    low = (text or "").lower()
    materials = sorted({label for term, label in _MATERIAL_TERMS.items() if re.search(r"\b" + term + r"\b", low)})
    mistakes = []
    for rule in rules:
        m = re.search(r"(?:do not|don't|never|should not|cannot)\s+(.*)", rule, re.I)
        if m:
            mistakes.append("Doing this: " + m.group(1).strip().rstrip(".") + " (the instructions forbid it)")
    words = re.search(r"(\d{2,5})\s*words", low)
    qc = ["Re-read the original instructions once more before submitting.",
          "Confirm each step above is done, in order.",
          "Check spelling, formatting and that the output matches the requested format."]
    if words:
        qc.append(f"Check the length requirement ({words.group(1)} words).")
    if evidence:
        qc.append("Confirm all required evidence is attached and clearly readable.")
    if re.search(r"original|plagiar|ai[- ]generated|copy", low):
        qc.append("Make sure the work is genuinely yours and meets any originality rules.")
    short = " ".join(sentences[:2])[:300]
    return {"summary": short, "steps": steps[:25], "rules": rules[:15], "evidence_requirements": evidence[:10],
            "deadlines": deadlines[:5], "materials": materials, "common_mistakes": mistakes[:10],
            "quality_checks": qc, "generic_note": "Quality checks beyond the text are generic reminders, not platform rules.",
            "needs_human_reading": not steps}


def generate_checklist(guidance: dict[str, Any]) -> list[dict[str, str]]:
    items = [{"text": s, "kind": "step"} for s in guidance.get("steps", [])]
    items += [{"text": f"Evidence: {e}", "kind": "evidence"} for e in guidance.get("evidence_requirements", [])]
    items += [{"text": q, "kind": "quality"} for q in guidance.get("quality_checks", [])]
    if not guidance.get("steps"):
        items.insert(0, {"text": "Read the original instructions carefully (no clear steps could be extracted).", "kind": "step"})
    return items


_ERROR_HELP = (
    ("401", "GitHub requires authentication for artifact downloads; the app uses the public report-data copy instead."),
    ("429", "A service is rate-limiting requests. The app waits and retries; do not run it repeatedly."),
    ("timed out", "Your connection to the service is slow or interrupted. The app retries automatically; try again later."),
    ("getaddrinfo", "DNS lookup failed: you may be offline or the network is unstable. It will retry."),
    ("handshake", "Secure connection setup timed out; the network is unstable. It will retry."),
    ("incomplete download", "The download was cut off. The previous valid report is kept and the app will resume."),
    ("permission", "This action is blocked by the platform capability rules. No workaround is provided on purpose."),
)


def explain_error(message: str) -> str:
    low = (message or "").lower()
    for needle, help_text in _ERROR_HELP:
        if needle in low:
            return help_text
    return "Unrecognised error. Check the log in data/ and the Platform Monitor page; the previous data is unchanged."


def plan_work(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order open tasks: soonest due first, then smallest unfinished checklist."""
    open_tasks = [t for t in tasks if t.get("status") in ("accepted", "in_progress")]
    return sorted(open_tasks, key=lambda t: (t.get("due_at") or "9999", t.get("open_items", 0), t.get("id", 0)))
