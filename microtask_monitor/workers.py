"""Specialist workers and AI tool runners for the execution engine.

Two kinds of worker:
* deterministic local workers (no AI): ``DataWorker`` applies a spec you supply to CSV data;
* agent workers (software, writing, evaluation, content, automation): hand the real instructions to a local AI coding
  tool (Claude Code or Codex CLI, using your own existing sign-in) running inside the job's isolated workspace.

Agent workers are only ever started when AI help is recorded as permitted for the task AND you approved that run.
Nothing here contacts a work platform; the AI tool's own network access is the tool's, not this program's.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class WorkerError(RuntimeError):
    pass


class Stopped(Exception):
    """Raised when you paused or cancelled a running job."""


@dataclass
class RunResult:
    returncode: int
    output: str


# ------------------------------------------------------------------------------------------- runners
def _clean_env() -> dict[str, str]:
    drop = re.compile(r"(GITHUB|GH)_TOKEN|PASSWORD|SECRET|COOKIE|SESSION_TOKEN", re.I)
    # Variables of a Claude Code session that launched this process must not leak into the child tool,
    # or it behaves as a nested session of the parent instead of using your own sign-in.
    session = re.compile(r"^(CLAUDECODE|CLAUDE_CODE_[A-Z_]+|CLAUDE_PID|CLAUDE_AGENT_SDK_[A-Z_]+|CLAUDE_EFFORT|CLAUDE_PREVIEW_[A-Z_]+)$")
    return {k: v for k, v in os.environ.items() if not drop.search(k) and not session.match(k)}


CLAUDE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash(python:*)", "Bash(py:*)", "Bash(node:*)", "Bash(npm test:*)"]


class CliRunner:
    """Runs a local AI CLI non-interactively inside a workspace. The prompt is passed on stdin (no quoting problems)."""

    def __init__(self, kind: str, executable: str):
        self.kind, self.executable = kind, executable
        self.name = kind

    def command(self, cwd: Path) -> list[str]:
        if self.kind == "claude":
            return [self.executable, "-p", "--permission-mode", "acceptEdits", "--output-format", "text", "--allowedTools", *CLAUDE_TOOLS]
        return [self.executable, "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "-C", str(cwd), "-"]

    def run(self, prompt: str, cwd: Path, timeout: int, should_stop: Callable[[], str | None], log: Callable[[str], None]) -> RunResult:
        proc = subprocess.Popen(self.command(cwd), cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=_clean_env(), text=True, encoding="utf-8", errors="replace")
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass
        chunks: list[str] = []
        import threading
        reader = threading.Thread(target=lambda: chunks.extend(iter(proc.stdout.readline, "")), daemon=True)
        reader.start()
        started = time.monotonic()
        while proc.poll() is None:
            if time.monotonic() - started > timeout:
                proc.kill()
                raise WorkerError(f"The {self.kind} run exceeded the {timeout}s time limit and was stopped")
            why = should_stop()
            if why:
                proc.kill()
                raise Stopped(why)
            time.sleep(1.0)
        reader.join(5)
        return RunResult(proc.returncode, "".join(chunks))


def available_runner(preferred: str | None = None) -> CliRunner | None:
    order = [preferred] if preferred in ("claude", "codex") else []
    order += [k for k in ("claude", "codex") if k not in order]
    for kind in order:
        exe = shutil.which(kind)
        if exe:
            return CliRunner(kind, exe)
    return None


# ------------------------------------------------------------------------------------------- workers
RULES = """RULES (non-negotiable):
- Work ONLY inside this folder. Read input/ and INSTRUCTIONS.md; write every deliverable into output/.
- Follow the instructions exactly. If something is ambiguous or impossible, say so in output/NOTES.md instead of guessing.
- Never invent facts, sources, test results, data, credentials, identities or work history. Never claim a test passed unless you ran it.
- Do not contact any work platform, do not log in anywhere, do not submit anything. Do not write secrets into files.
- Finish by writing output/NOTES.md: what you did, assumptions, anything you could not verify, and how to run any tests."""

GUIDE = {
    "software": "Write clean, working code for the task. Include automated tests in output/tests/ (unittest or the language's standard runner) and run them yourself; report the real result in NOTES.md.",
    "writing": "Produce the requested text in Markdown. Match requested length, tone and structure. List every source you actually used with its URL in a 'Sources' section; mark any claim you could not support.",
    "evaluation": "Evaluate each item against the stated rubric. Output a JSON or CSV file with one row per item (id, scores per criterion, short rationale). Be consistent and justify each judgement from the item itself.",
    "content": "Produce the requested scripts/captions/asset lists as plain text or Markdown files that respect every stated limit (length, format, language). List required visual assets explicitly.",
    "automation": "Produce the workflow as an importable file (n8n/Make JSON). Use placeholders for credentials (never real secrets). Describe a safe way to test it in NOTES.md.",
    "data": "Process the supplied data exactly as the instructions describe and write the result files to output/. Record row counts before and after in NOTES.md.",
}


class Worker:
    name = "worker"
    work_types: tuple[str, ...] = ()

    def requires_ai(self, job: dict[str, Any], paths: dict[str, Path]) -> bool:
        return True

    def preflight(self, job: dict[str, Any], paths: dict[str, Path]) -> str | None:
        if job["work_type"] == "unknown":
            return "The work type is unclear: choose one so the right specialist is used."
        if job["ai_policy"] == "prohibited" and self.requires_ai(job, paths):
            return "AI help is prohibited for this work, and this specialist needs AI. Do the work yourself, or use a deterministic spec (data work)."
        if self.requires_ai(job, paths) and job["ai_policy"] == "unknown":
            return "Record whether AI help is permitted (and where the rules say so) before AI can work on this."
        return None

    def run(self, job, paths, runner, log, should_stop) -> dict[str, Any]:
        raise NotImplementedError


class AgentWorker(Worker):
    def __init__(self, name: str, work_type: str):
        self.name, self.work_types = name, (work_type,)

    def prompt(self, job: dict[str, Any], paths: dict[str, Path]) -> str:
        feedback = paths["logs"] / "qa_feedback.txt"
        extra = ""
        if feedback.exists():
            extra = "\n\nA previous attempt FAILED independent quality checks. Fix exactly these problems:\n" + feedback.read_text(encoding="utf-8")[:4000]
        inputs = [p.name for p in sorted(paths["input"].glob("*"))] if paths["input"].exists() else []
        return (f"You are the {self.name} for a paid remote-work assignment.\n\nTitle: {job['title']}\nWork type: {job['work_type']}\n"
                f"Input files: {', '.join(inputs) or 'none'}\n\n{GUIDE.get(job['work_type'], '')}\n\n{RULES}\n\n"
                f"INSTRUCTIONS (from the user, copied from their account):\n{job['instructions']}{extra}\n")

    def run(self, job, paths, runner, log, should_stop) -> dict[str, Any]:
        runner = runner or available_runner()
        if runner is None:
            raise WorkerError("No local AI tool was found. Install and sign in to Claude Code (claude) or Codex (codex), then resume.")
        timeout = int(os.environ.get("AUTOPILOT_EXEC_TIMEOUT", "1800"))
        prompt = self.prompt(job, paths)
        paths["logs"].mkdir(parents=True, exist_ok=True)
        (paths["logs"] / f"prompt-attempt-{job['attempts']}.md").write_text(prompt, encoding="utf-8")
        log(f"Running {getattr(runner, 'name', 'AI tool')} in the workspace (time limit {timeout}s)")
        result = runner.run(prompt, paths["base"], timeout, should_stop, log)
        (paths["logs"] / f"agent-attempt-{job['attempts']}.log").write_text(result.output[-200000:], encoding="utf-8")
        if result.returncode != 0:
            raise WorkerError(f"The AI tool exited with code {result.returncode}. See logs/agent-attempt-{job['attempts']}.log (tail: {result.output[-300:].strip()})")
        made = [p for p in paths["output"].rglob("*") if p.is_file()]
        if not made:
            raise WorkerError("The AI tool finished but wrote nothing to output/")
        return {"summary": f"{getattr(runner, 'name', 'AI tool')} produced {len(made)} file(s)"}


# ---- deterministic data worker -------------------------------------------------------------------
def load_spec(paths: dict[str, Path]) -> dict[str, Any] | None:
    f = paths["input"] / "spec.json"
    if not f.exists():
        return None
    try:
        spec = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return {"_error": "spec.json is not valid JSON"}
    return spec if isinstance(spec, dict) else {"_error": "spec.json must be an object"}


def _case(value: str, mode: str) -> str:
    return {"lower": value.lower(), "upper": value.upper(), "title": value.title()}.get(mode, value)


class DataWorker(Worker):
    name = "data worker"
    work_types = ("data",)

    def requires_ai(self, job, paths) -> bool:
        spec = load_spec(paths)
        return not (spec and "_error" not in spec)

    def preflight(self, job, paths) -> str | None:
        spec = load_spec(paths)
        if spec is None:
            return super().preflight(job, paths) or None
        if "_error" in spec:
            return spec["_error"]
        src = paths["input"] / str(spec.get("input", ""))
        if not spec.get("input") or not src.is_file():
            return f"spec.json names input '{spec.get('input')}' which is not among the uploaded files."
        return None

    def run(self, job, paths, runner, log, should_stop) -> dict[str, Any]:
        spec = load_spec(paths)
        if not spec or "_error" in spec:
            return AgentWorker("data worker (AI)", "data").run(job, paths, runner, log, should_stop)
        src = paths["input"] / spec["input"]
        with open(src, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            fields = reader.fieldnames or []
            rows = list(reader)
        total_in = len(rows)
        log(f"Read {total_in} rows from {src.name}")
        trim, case = spec.get("trim", True), spec.get("case", {})
        required, dedupe = spec.get("required", []), spec.get("dedupe_on", [])
        kept, rejected, seen, dropped_empty, dupes = [], [], set(), 0, 0
        for n, row in enumerate(rows):
            if n % 500 == 0:
                why = should_stop()
                if why:
                    raise Stopped(why)
            clean = {k: ((v or "").strip() if trim else (v or "")) for k, v in row.items() if k is not None}
            if spec.get("drop_empty_rows", True) and not any(clean.values()):
                dropped_empty += 1
                continue
            for col, mode in case.items():
                if col in clean:
                    clean[col] = _case(clean[col], mode)
            missing = [c for c in required if not clean.get(c)]
            if missing:
                rejected.append({**clean, "_reason": "missing " + ",".join(missing)})
                continue
            if dedupe:
                key = tuple(clean.get(c, "").lower() for c in dedupe)
                if key in seen:
                    dupes += 1
                    rejected.append({**clean, "_reason": "duplicate of " + "/".join(dedupe)})
                    continue
                seen.add(key)
            cat = spec.get("categorize")
            if cat:
                text = clean.get(cat.get("from", ""), "").lower()
                clean[cat["column"]] = next((label for label, kws in cat.get("rules", {}).items() if any(k.lower() in text for k in kws)), cat.get("default", "uncategorized"))
            kept.append(clean)
        columns = spec.get("columns") or (fields + ([spec["categorize"]["column"]] if spec.get("categorize") and spec["categorize"]["column"] not in fields else []))
        out = paths["output"] / spec.get("output", "cleaned.csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(kept)
        rej_path = paths["output"] / "rejected.csv"
        with open(rej_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=[*columns, "_reason"], extrasaction="ignore")
            w.writeheader()
            w.writerows(rejected)
        notes = (f"# Notes\n\nDeterministic data processing (no AI) using input/spec.json.\n\n- Rows read: {total_in}\n- Rows kept: {len(kept)}\n"
                 f"- Empty rows dropped: {dropped_empty}\n- Rejected: {len(rejected)} (duplicates: {dupes}) - see rejected.csv\n- Output: {out.name}\n")
        (paths["output"] / "NOTES.md").write_text(notes, encoding="utf-8")
        log(f"Wrote {len(kept)} rows to {out.name}; {len(rejected)} rejected")
        return {"summary": f"Processed {total_in} rows: {len(kept)} kept, {len(rejected)} rejected"}


WORKERS: list[Worker] = [
    AgentWorker("software developer", "software"),
    AgentWorker("writing and research specialist", "writing"),
    DataWorker(),
    AgentWorker("AI evaluation specialist", "evaluation"),
    AgentWorker("content creation specialist", "content"),
    AgentWorker("workflow automation specialist", "automation"),
]
_UNKNOWN = AgentWorker("unassigned", "unknown")


def select(work_type: str) -> Worker:
    for w in WORKERS:
        if work_type in w.work_types:
            return w
    return _UNKNOWN
