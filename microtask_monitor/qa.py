"""Independent quality assurance for execution jobs.

QA never trusts the worker: it re-inspects the files in ``output/`` and RE-RUNS tests itself. Each check returns
``status`` = pass / fail / manual and ``executed`` (True only if something was actually run or parsed here).
``manual`` means a person must judge it (e.g. factual accuracy); it is never counted as a pass.
A single ``fail`` keeps the job out of review.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import shutil
from pathlib import Path
from typing import Any, Callable

PLACEHOLDER = re.compile(r"\b(lorem ipsum|TODO|FIXME|TBD|placeholder text)\b|\[(?:your|insert|add)[^\]]{0,60}\]", re.I)
SECRET = re.compile(r"(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|"
                    r"(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"])", re.I)
TEST_TIMEOUT = 120


def _r(name: str, status: str, detail: str, executed: bool = True) -> dict[str, Any]:
    return {"name": name, "status": status, "executed": executed, "detail": detail}


def _files(out: Path) -> list[Path]:
    return [p for p in sorted(out.rglob("*")) if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]


def _text(p: Path, limit: int = 400_000) -> str:
    try:
        return p.read_bytes()[:limit].decode("utf-8", "replace")
    except OSError:
        return ""


def _spec(paths: dict[str, Path]) -> dict[str, Any]:
    f = paths["input"] / "spec.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------------------------------- common checks
def common_checks(job: dict[str, Any], paths: dict[str, Path]) -> list[dict[str, Any]]:
    out = paths["output"]
    res = []
    files = [f for f in _files(out)]
    res.append(_r("deliverables_exist", "pass" if files else "fail", f"{len(files)} file(s) in output/" if files else "output/ is empty"))
    empty = [f.name for f in files if f.stat().st_size == 0]
    res.append(_r("no_empty_files", "fail" if empty else "pass", ("Empty files: " + ", ".join(empty)) if empty else "No empty files"))
    notes = out / "NOTES.md"
    res.append(_r("notes_present", "pass" if notes.exists() and notes.stat().st_size > 20 else "fail", "NOTES.md explains what was done" if notes.exists() else "NOTES.md is missing"))
    bad_ph = [f.name for f in files if f.suffix.lower() in (".md", ".txt", ".csv", ".json", ".py", ".js", ".ts") and PLACEHOLDER.search(_text(f)) and f.name != "NOTES.md"]
    res.append(_r("no_placeholders", "fail" if bad_ph else "pass", ("Placeholder text found in: " + ", ".join(bad_ph)) if bad_ph else "No placeholder text"))
    leaked = [f.name for f in files if SECRET.search(_text(f))]
    res.append(_r("no_secrets", "fail" if leaked else "pass", ("Possible secret in: " + ", ".join(leaked)) if leaked else "No secret-like strings"))
    wanted = sorted(set(re.findall(r"\b([\w-]+\.(?:csv|json|md|txt|py|js|ts|xlsx|srt|zip))\b", job["instructions"])))
    wanted = [w for w in wanted if w.lower() not in ("spec.json", "instructions.md", "notes.md")]
    have = {f.name for f in files}
    inputs = {p.name for p in paths["input"].glob("*")} if paths["input"].exists() else set()
    missing = [w for w in wanted if w not in have and w not in inputs]
    if wanted:
        res.append(_r("named_deliverables", "fail" if missing else "pass", ("Instructions name files that were not delivered: " + ", ".join(missing)) if missing else "Every file named in the instructions was delivered"))
    return res


# ------------------------------------------------------------------------------------------- software
def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if not re.search(r"TOKEN|SECRET|PASSWORD", k, re.I)}
    env["PYTHONPATH"] = str(cwd) + os.pathsep + env.get("PYTHONPATH", "")        # delivered code is importable from its own folder
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT, env=env)
        return p.returncode, (p.stdout + p.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {TEST_TIMEOUT}s"
    except OSError as exc:
        return 127, str(exc)


def software_checks(job, paths) -> list[dict[str, Any]]:
    out, res = paths["output"], []
    py = [f for f in _files(out) if f.suffix == ".py"]
    js = [f for f in _files(out) if f.suffix in (".js", ".mjs", ".cjs")]
    if not py and not js and not any(f.suffix == ".ts" for f in _files(out)):
        return [_r("code_present", "fail", "No Python or JavaScript source files were delivered", executed=True)]
    for f in py:
        code, msg = _run([sys.executable, "-m", "py_compile", str(f)], out)
        res.append(_r(f"compile:{f.name}", "pass" if code == 0 else "fail", "Compiles" if code == 0 else msg))
    node = shutil.which("node")
    for f in js:
        if node:
            code, msg = _run([node, "--check", str(f)], out)
            res.append(_r(f"syntax:{f.name}", "pass" if code == 0 else "fail", "Syntax OK" if code == 0 else msg))
        else:
            res.append(_r(f"syntax:{f.name}", "manual", "Node.js is not installed, so this file was not checked", executed=False))
    tests = [f for f in py if f.name.startswith("test_") or f.parent.name == "tests"]
    if tests:
        code, msg = _run([sys.executable, "-m", "unittest", "discover", "-s", str(tests[0].parent)], out)
        ran = re.search(r"Ran (\d+) tests?", msg)
        if code == 0 and ran and int(ran.group(1)) > 0:
            res.append(_r("tests_executed", "pass", f"QA re-ran the tests itself: {ran.group(0)} - OK"))
        elif code == 0:
            res.append(_r("tests_executed", "fail", "The test runner finished but ran no tests"))
        else:
            res.append(_r("tests_executed", "fail", f"Tests failed or errored (exit {code}): {msg}"))
    elif js and (out / "package.json").exists() and node:
        pkg = json.loads(_text(out / "package.json") or "{}")
        if (pkg.get("scripts") or {}).get("test") and shutil.which("npm"):
            code, msg = _run([shutil.which("npm"), "test", "--silent"], out)
            res.append(_r("tests_executed", "pass" if code == 0 else "fail", "npm test passed" if code == 0 else f"npm test failed: {msg}"))
        else:
            res.append(_r("tests_executed", "fail", "No executable tests were delivered for the JavaScript code"))
    else:
        res.append(_r("tests_executed", "fail", "No automated tests were delivered, so correctness is unproven. Ask the worker to add and run tests."))
    return res


# ------------------------------------------------------------------------------------------- writing
def writing_checks(job, paths) -> list[dict[str, Any]]:
    docs = [f for f in _files(paths["output"]) if f.suffix.lower() in (".md", ".txt") and f.name != "NOTES.md"]
    if not docs:
        return [_r("document_present", "fail", "No .md or .txt deliverable found")]
    res = []
    body = "\n".join(_text(f) for f in docs)
    words = len(re.findall(r"\b\w+\b", body))
    m = re.search(r"\b(\d{2,5})\s*(?:\+|-|to)?\s*(\d{2,5})?\s*words?\b", job["instructions"], re.I)
    if m:
        low = int(m.group(1))
        high = int(m.group(2)) if m.group(2) else None
        lo, hi = (low, high) if high else (int(low * 0.9), int(low * 1.15))
        res.append(_r("word_count", "pass" if lo <= words <= hi else "fail", f"{words} words delivered; instructions ask for {lo}-{hi}"))
    paras = [p.strip().lower() for p in re.split(r"\n\s*\n", body) if len(p.strip()) > 60]
    dup = len(paras) - len(set(paras))
    res.append(_r("no_repeated_paragraphs", "fail" if dup else "pass", f"{dup} repeated paragraph(s)" if dup else "No repeated paragraphs"))
    heads = len(re.findall(r"^#{1,4} ", body, re.M))
    res.append(_r("structure", "pass" if heads >= 1 or words < 250 else "fail", f"{heads} heading(s)"))
    if re.search(r"\b(research|sources?|cite|references?|statistics?)\b", job["instructions"], re.I):
        urls = re.findall(r"https?://\S+", body)
        res.append(_r("sources_listed", "pass" if len(set(urls)) >= 2 else "fail", f"{len(set(urls))} distinct source URL(s) listed"))
    claims = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if re.search(r"\b(19|20)\d{2}\b|\b\d+(?:\.\d+)?\s?%|according to|studies show", s)]
    if claims:
        res.append(_r("factual_claims_review", "manual", f"{len(claims)} sentence(s) contain dates, figures or attributions that this program cannot verify. Check them yourself, e.g.: " + " | ".join(c[:110] for c in claims[:3]), executed=False))
    return res


# ------------------------------------------------------------------------------------------- data / annotation
def data_checks(job, paths) -> list[dict[str, Any]]:
    out, spec, res = paths["output"], _spec(paths), []
    csvs = [f for f in _files(out) if f.suffix.lower() == ".csv" and f.name != "rejected.csv"]
    if any(f.suffix.lower() in (".xlsx", ".xls") for f in _files(out)):
        res.append(_r("spreadsheet_format", "manual", "An Excel file was delivered; its formulas and formatting cannot be validated here. Open it and check.", executed=False))
    if not csvs:
        return res + [_r("csv_present", "fail" if not res else "manual", "No CSV deliverable to validate", executed=True)]
    target = out / spec["output"] if spec.get("output") and (out / spec["output"]).exists() else csvs[0]
    try:
        with open(target, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            cols, rows = reader.fieldnames or [], list(reader)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        return res + [_r("csv_parses", "fail", f"{target.name} could not be parsed: {exc}")]
    res.append(_r("csv_parses", "pass", f"{target.name}: {len(rows)} rows, {len(cols)} columns"))
    if spec.get("columns"):
        res.append(_r("columns_match_spec", "pass" if cols == spec["columns"] else "fail", f"columns {cols} vs required {spec['columns']}"))
    inp = paths["input"] / spec["input"] if spec.get("input") else None
    if inp and inp.exists():
        with open(inp, newline="", encoding="utf-8-sig") as fh:
            n_in = sum(1 for _ in csv.DictReader(fh))
        rej = out / "rejected.csv"
        n_rej = 0
        if rej.exists():
            with open(rej, newline="", encoding="utf-8") as rf:
                n_rej = sum(1 for _ in csv.DictReader(rf))
        res.append(_r("no_records_lost", "pass" if len(rows) + n_rej <= n_in and len(rows) > 0 else "fail",
                      f"input {n_in} rows -> {len(rows)} kept + {n_rej} rejected"))
    for col in spec.get("required", []):
        bad = sum(1 for r in rows if not (r.get(col) or "").strip())
        res.append(_r(f"required:{col}", "fail" if bad else "pass", f"{bad} row(s) missing {col}" if bad else f"all rows have {col}"))
    for col, allowed in (spec.get("allowed_values") or {}).items():
        bad = sorted({r.get(col, "") for r in rows if r.get(col, "") not in allowed})
        res.append(_r(f"allowed_values:{col}", "fail" if bad else "pass", f"values outside the allowed set: {bad[:5]}" if bad else f"all {col} values are allowed"))
    if spec.get("dedupe_on"):
        keys = [tuple((r.get(c) or "").lower() for c in spec["dedupe_on"]) for r in rows]
        res.append(_r("no_duplicates", "fail" if len(keys) != len(set(keys)) else "pass", f"{len(keys) - len(set(keys))} duplicate key(s)" if len(keys) != len(set(keys)) else "no duplicate keys"))
    if spec.get("label_column") and spec.get("text_column"):                         # annotation consistency
        by_text: dict[str, set[str]] = {}
        for r in rows:
            by_text.setdefault((r.get(spec["text_column"]) or "").strip().lower(), set()).add(r.get(spec["label_column"], ""))
        conflicts = [t for t, labels in by_text.items() if len(labels) > 1]
        res.append(_r("annotation_consistency", "fail" if conflicts else "pass", f"{len(conflicts)} identical item(s) got different labels" if conflicts else "identical items always share a label"))
    return res


# ------------------------------------------------------------------------------------------- evaluation
def evaluation_checks(job, paths) -> list[dict[str, Any]]:
    out, res = paths["output"], []
    spec = _spec(paths)
    jsons = [f for f in _files(out) if f.suffix == ".json" and f.name != "NOTES.md"]
    tables = [f for f in _files(out) if f.suffix.lower() == ".csv"]
    items: list[dict[str, Any]] = []
    for f in jsons:
        try:
            d = json.loads(_text(f))
        except ValueError:
            res.append(_r(f"json_valid:{f.name}", "fail", "Not valid JSON"))
            continue
        items = d if isinstance(d, list) else d.get("evaluations", []) if isinstance(d, dict) else []
        res.append(_r(f"json_valid:{f.name}", "pass", f"{len(items)} evaluation item(s)"))
    if not jsons and tables:
        with open(tables[0], newline="", encoding="utf-8-sig") as fh:
            items = list(csv.DictReader(fh))
        res.append(_r("table_parses", "pass", f"{len(items)} evaluation row(s)"))
    if not items:
        return res + [_r("evaluations_present", "fail", "No evaluation records found in a JSON or CSV file")]
    no_reason = sum(1 for i in items if not str(i.get("rationale") or i.get("reason") or i.get("justification") or "").strip())
    res.append(_r("each_has_rationale", "fail" if no_reason else "pass", f"{no_reason} item(s) without a rationale" if no_reason else "every item has a rationale"))
    if spec.get("expected_items"):
        res.append(_r("covers_all_items", "pass" if len(items) >= int(spec["expected_items"]) else "fail", f"{len(items)} evaluated; {spec['expected_items']} expected"))
    return res


# ------------------------------------------------------------------------------------------- content
def content_checks(job, paths) -> list[dict[str, Any]]:
    out, spec, res = paths["output"], _spec(paths), []
    docs = [f for f in _files(out) if f.suffix.lower() in (".md", ".txt", ".srt", ".vtt") and f.name != "NOTES.md"]
    if not docs:
        return [_r("content_files_present", "fail", "No script/caption files were delivered")]
    res.append(_r("content_files_present", "pass", f"{len(docs)} content file(s)"))
    limit = spec.get("max_caption_chars")
    if not limit:
        m = re.search(r"(?:under|max(?:imum)?|up to|no more than|within)\s+(\d{2,4})\s+characters", job["instructions"], re.I)
        limit = int(m.group(1)) if m else None
    if limit:
        over = [f"{f.name}:{i + 1}" for f in docs for i, line in enumerate(_text(f).splitlines()) if len(line.strip()) > int(limit)]
        res.append(_r("length_limit", "fail" if over else "pass", f"Lines over {limit} characters: {', '.join(over[:5])}" if over else f"All lines within {limit} characters"))
    wanted_assets = spec.get("required_assets", [])
    body = "\n".join(_text(f) for f in docs).lower()
    miss = [a for a in wanted_assets if a.lower() not in body]
    if wanted_assets:
        res.append(_r("required_assets_listed", "fail" if miss else "pass", f"Not mentioned: {', '.join(miss)}" if miss else "All required assets are listed"))
    res.append(_r("media_files_not_checked", "manual", "Any audio/video/image assets must be played or viewed by you; this program does not judge media.", executed=False))
    return res


# ------------------------------------------------------------------------------------------- automation
def automation_checks(job, paths) -> list[dict[str, Any]]:
    out, res = paths["output"], []
    flows = [f for f in _files(out) if f.suffix == ".json"]
    if not flows:
        return [_r("workflow_file_present", "fail", "No workflow JSON file was delivered")]
    for f in flows:
        try:
            d = json.loads(_text(f))
        except ValueError:
            res.append(_r(f"workflow_json:{f.name}", "fail", "Not valid JSON"))
            continue
        nodes = d.get("nodes") if isinstance(d, dict) else None
        conns = d.get("connections") if isinstance(d, dict) else None
        if not isinstance(nodes, list) or not nodes:
            res.append(_r(f"workflow_nodes:{f.name}", "fail", "No 'nodes' list (not an n8n-style workflow export)"))
            continue
        names = {n.get("name") for n in nodes if isinstance(n, dict)}
        bad = [src for src in (conns or {}) if src not in names]
        for src, outs in (conns or {}).items():
            for branch in (outs or {}).get("main", []) if isinstance(outs, dict) else []:
                bad += [c.get("node") for c in branch or [] if c.get("node") not in names]
        res.append(_r(f"workflow_connections:{f.name}", "fail" if bad else "pass", f"Connections reference missing nodes: {sorted(set(map(str, bad)))}" if bad else f"{len(nodes)} nodes, connections consistent"))
        creds = [n.get("name") for n in nodes if isinstance(n, dict) and any(isinstance(v, str) and len(v) > 12 and SECRET.search(f'"x":"{v}"') for v in json.dumps(n.get("parameters", {})).split('"'))]
        res.append(_r(f"workflow_no_credentials:{f.name}", "fail" if creds else "pass", f"Hard-coded secrets in: {creds}" if creds else "No hard-coded credentials"))
        res.append(_r(f"workflow_has_trigger:{f.name}", "pass" if any("trigger" in str(n.get("type", "")).lower() or "webhook" in str(n.get("type", "")).lower() or "manual" in str(n.get("type", "")).lower() or "schedule" in str(n.get("type", "")).lower() for n in nodes if isinstance(n, dict)) else "fail", "trigger node present" if nodes else "no trigger"))
    res.append(_r("live_run", "manual", "The workflow was validated statically only. Import it into your own n8n/Make account and run the safe test described in NOTES.md.", executed=False))
    return res


CHECKS: dict[str, Callable[[dict[str, Any], dict[str, Path]], list[dict[str, Any]]]] = {
    "software": software_checks, "writing": writing_checks, "data": data_checks, "evaluation": evaluation_checks,
    "content": content_checks, "automation": automation_checks,
}


def run_qa(job: dict[str, Any], paths: dict[str, Path], log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    results = common_checks(job, paths)
    fn = CHECKS.get(job["work_type"])
    if fn:
        results += fn(job, paths)
    else:
        results.append(_r("work_type_checks", "manual", "No specialised checks exist for this work type; review everything yourself.", executed=False))
    if log:
        log(f"QA ran {len(results)} checks: {sum(r['status'] == 'pass' for r in results)} pass, {sum(r['status'] == 'fail' for r in results)} fail, {sum(r['status'] == 'manual' for r in results)} manual")
    return results
