"""Service catalogue, case studies and the static business website (an inbound acquisition channel).

Built only from your configuration: your stated skills, your own projects (each labelled "own project", never as client
work), packages you configured, testimonials you recorded WITH evidence, and contact details from your own outreach
settings. Nothing is invented. The site is written to data/business/site (not committed): publishing it on a free static
host is your decision. Its enquiry form posts to the endpoint you configure (contact.form_endpoint), or to this program
when served locally, or falls back to an email with the subject "Project enquiry" that the business inbox picks up.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import biz_config as bc, db
except ImportError:  # pragma: no cover
    import biz_config as bc, db

SITE_DIR = db.DATA_DIR / "business" / "site"


def catalogue(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = cfg or bc.load()
    lines = cfg["service_lines"]
    out = []
    for key, line in sorted(lines.items(), key=lambda kv: kv[1]["priority"]):
        out.append({"service_line": key, "label": line["label"], "priority": line["priority"],
                    "packages": [p for p in cfg["packages"] if p["service_line"] == key],
                    "case_studies": [p for p in cfg["portfolio"] if key in p.get("service_lines", [])]})
    return out


def _e(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def build_site(cfg: dict[str, Any] | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    cfg = cfg or bc.load()
    out = Path(out_dir or SITE_DIR)
    out.mkdir(parents=True, exist_ok=True)
    prof = cfg["profile"]
    contact = {**(prof.get("contact") or {}), **(cfg.get("contact") or {})}
    name = cfg["business_name"]
    email = contact.get("email")
    endpoint = contact.get("form_endpoint") or ""
    services = catalogue(cfg)
    svc_html = "".join(
        f'<section class="svc"><h3>{_e(s["label"])}</h3><ul>' + "".join(
            f'<li><b>{_e(p["name"])}</b> <span class="price">from USD {p["from_usd"]:,}{"/month" if p.get("recurring") else ""}</span>'
            f'<div class="muted">{_e("; ".join(p["includes"]))} · about {p["days"]} days</div></li>' for p in s["packages"]) + "</ul></section>"
        for s in services if s["packages"])
    case_html = "".join(
        f'<article class="case"><h3>{_e(c["title"])}</h3><p class="tag">{_e(c["kind"])}</p><p>{_e(c["summary"])}</p>'
        f'<p class="muted">{_e(", ".join(c["tech"]))}</p>' + (f'<p><a href="{_e(c["url"])}" rel="noopener">View</a></p>' if c.get("url") else "") + "</article>"
        for c in cfg["portfolio"])
    tests = [t for t in cfg.get("testimonials", []) if t.get("evidence")]
    test_html = ("<section><h2>What clients said</h2>" + "".join(f'<blockquote>{_e(t["text"])}<cite>{_e(t["client"])}</cite></blockquote>' for t in tests) + "</section>") if tests else ""
    stack = sorted(set(prof.get("skills") or []))
    form_note = ("" if (endpoint or email) else '<p class="warn">Add contact.email or contact.form_endpoint in your business settings before publishing.</p>')
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(name)}: software, automation and AI video services</title>
<meta name="description" content="{_e(name)} builds web apps, APIs, n8n and Make.com automations and AI video pipelines.">
<style>
:root{{--ink:#152033;--muted:#5b6678;--accent:#1c5caa;--bg:#f6f8fb;--card:#fff;--line:#dde3ec}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.55 system-ui,Segoe UI,Arial,sans-serif;color:var(--ink);background:var(--bg)}}
header{{background:var(--ink);color:#fff;padding:48px 16px}}.wrap{{max-width:1040px;margin:auto;padding:0 16px}}
h1{{font-size:clamp(28px,5vw,42px);margin:0 0 8px}}h2{{margin:40px 0 12px}}.lead{{color:#c7d2e3;max-width:640px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}}
.svc,.case,form{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}}
.svc ul{{padding-left:18px}}.price{{color:var(--accent);white-space:nowrap}}.muted{{color:var(--muted);font-size:14px}}
.tag{{display:inline-block;font-size:12px;background:#eef3fb;color:var(--accent);padding:2px 8px;border-radius:99px;margin:0}}
.stack span{{display:inline-block;border:1px solid var(--line);border-radius:6px;padding:3px 8px;margin:3px;background:#fff;font-size:14px}}
label{{display:block;font-weight:600;margin-top:10px}}input,textarea{{width:100%;padding:9px;border:1px solid var(--line);border-radius:6px;font:inherit}}
button{{margin-top:12px;background:var(--accent);color:#fff;border:0;border-radius:6px;padding:10px 18px;font:inherit;cursor:pointer}}
blockquote{{background:#fff;border-left:4px solid var(--accent);margin:8px 0;padding:10px 14px}}cite{{display:block;color:var(--muted)}}
.warn{{color:#8a1f1f}}footer{{padding:32px 16px;color:var(--muted)}}a{{color:var(--accent)}}
</style></head><body>
<header><div class="wrap"><h1>{_e(name)}</h1><p class="lead">Websites, backends and APIs, business automations (n8n, Make.com, AI workflows) and AI video pipelines,
built with modern AI-assisted development tools, tested, and handed over with documentation. Based in {_e(prof.get("country") or "Kenya")}, working remotely.</p>
<p><a href="#enquire" style="color:#fff">Tell me about your project &rarr;</a></p></div></header>
<main class="wrap">
<h2>Services</h2><div class="grid">{svc_html}</div>
<h2>Projects I have built</h2><p class="muted">These are my own projects, not client engagements.</p><div class="grid">{case_html}</div>
{test_html}
<h2>Tools and technologies</h2><div class="stack">{"".join(f"<span>{_e(s)}</span>" for s in stack)}</div>
<h2 id="enquire">Project enquiry</h2>{form_note}
<form id="f"><label>Your name<input name="name" required maxlength="120"></label><label>Email<input name="email" type="email" required maxlength="200"></label>
<label>Company (optional)<input name="company" maxlength="120"></label><label>Project title<input name="subject" maxlength="200"></label>
<label>Budget in USD (optional)<input name="budget" type="number" min="0" step="1"></label>
<label>What do you need?<textarea name="message" rows="6" required minlength="20" maxlength="8000"></textarea></label>
<button>Send enquiry</button><p id="msg" class="muted"></p></form>
</main><footer class="wrap">{_e(name)}{(" · " + _e(email)) if email else ""}</footer>
<script>
const ENDPOINT = {json.dumps(endpoint)}, EMAIL = {json.dumps(email or "")};
document.getElementById("f").addEventListener("submit", async e => {{
  e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); const msg = document.getElementById("msg");
  const target = ENDPOINT || (location.hostname === "127.0.0.1" ? "/api/business/enquiry" : "");
  if (target) {{ try {{ const r = await fetch(target, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(data)}});
      if (r.ok) {{ msg.textContent = "Thank you: your enquiry was received. I will reply by email."; e.target.reset(); return; }} }} catch (_) {{}} }}
  if (EMAIL) {{ location.href = "mailto:" + EMAIL + "?subject=" + encodeURIComponent("Project enquiry: " + (data.subject || data.name)) +
      "&body=" + encodeURIComponent(data.message + "\\n\\nName: " + data.name + "\\nCompany: " + (data.company || "") + "\\nBudget (USD): " + (data.budget || "") + "\\nReply to: " + data.email); }}
  else msg.textContent = "The enquiry form is not connected yet.";
}});
</script></body></html>"""
    (out / "index.html").write_text(page, encoding="utf-8")
    return {"path": str(out / "index.html"), "services": sum(len(s["packages"]) for s in services), "case_studies": len(cfg["portfolio"]),
            "testimonials": len(tests), "contact_email": bool(email), "form_endpoint": bool(endpoint)}
