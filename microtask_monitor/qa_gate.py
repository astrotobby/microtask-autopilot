"""The independent QA verdict for one execution: PASS, FAIL or MANUAL_CHECK_REQUIRED, with a written quality report.

The worker's own claim of success is never an input. The verdict is computed from the checks that qa.py actually
executed plus integrity checks done here, by re-reading the files on disk:

* every deliverable still has the SHA-256 that was recorded when QA ran (nothing changed afterwards);
* nothing unexpected is present (programs, scripts that could run on a double-click, files written outside output/);
* when a package exists, its manifest and archive match the deliverables exactly.

FAIL blocks packaging and submission. MANUAL_CHECK_REQUIRED is never reported as verified.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

try:
    from microtask_monitor import db, execution as ex, reliability
except ImportError:  # pragma: no cover
    import db, execution as ex, reliability

PASS, FAIL, MANUAL = "PASS", "FAIL", "MANUAL_CHECK_REQUIRED"
UNEXPECTED_SUFFIXES = {".exe", ".dll", ".bat", ".cmd", ".ps1", ".scr", ".msi", ".lnk", ".vbs", ".jar", ".com", ".pif", ".hta"}
WORKSPACE_ALLOWED = {"input", "output", "logs", "tests", "qa", "package", "INSTRUCTIONS.md"}


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check(name: str, status: str, detail: str) -> dict[str, Any]:
    return {"name": name, "status": status, "executed": True, "detail": detail, "source": "qa_gate"}


def integrity_checks(conn, job: dict[str, Any]) -> list[dict[str, Any]]:
    paths = ex.workspace_paths(job)
    out, base = paths["output"], paths["base"]
    files = [p for p in sorted(out.rglob("*")) if p.is_file()] if out.exists() else []
    checks = []
    recorded = {r["rel_path"]: r["sha256"] for r in conn.execute("SELECT rel_path, sha256 FROM execution_files WHERE job_id=? AND role='output'", (job["id"],))}
    current = {p.relative_to(base).as_posix(): _sha(p) for p in files}
    if not recorded:
        checks.append(_check("hashes_recorded", "fail", "No deliverable hashes were recorded for this job"))
    else:
        changed = sorted(k for k in current if k in recorded and recorded[k] != current[k])
        added = sorted(k for k in current if k not in recorded)
        gone = sorted(k for k in recorded if k not in current)
        problems = [f"changed after QA: {', '.join(changed)}"] * bool(changed) + [f"added after QA: {', '.join(added)}"] * bool(added) + [f"removed after QA: {', '.join(gone)}"] * bool(gone)
        checks.append(_check("hashes_recorded", "fail" if problems else "pass", "; ".join(problems) or f"{len(current)} deliverable(s) match their recorded SHA-256"))
    risky = sorted(p.relative_to(out).as_posix() for p in files if p.suffix.lower() in UNEXPECTED_SUFFIXES)
    checks.append(_check("no_unexpected_files", "fail" if risky else "pass", ("Programs or launchers are not deliverables: " + ", ".join(risky)) if risky else "No programs or launchers among the deliverables"))
    stray = sorted(p.name for p in base.iterdir() if p.name not in WORKSPACE_ALLOWED) if base.exists() else []
    checks.append(_check("workspace_contained", "manual" if stray else "pass", ("Files were written outside output/: " + ", ".join(stray[:8])) if stray else "Nothing was written outside the expected folders"))
    manifest_path = paths["package"] / "MANIFEST.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            listed = {f["path"]: f["sha256"] for f in manifest.get("files", [])}
        except (ValueError, KeyError, TypeError):
            listed = None
        now_out = {p.relative_to(out).as_posix(): _sha(p) for p in files}
        if listed is None:
            checks.append(_check("package_matches_deliverables", "fail", "MANIFEST.json could not be read"))
        elif listed != now_out:
            checks.append(_check("package_matches_deliverables", "fail", "The package manifest does not match the deliverables on disk"))
        else:
            archive = paths["package"] / f"deliverables-job-{job['id']}.zip"
            ok, detail = archive.exists(), "The archive is missing"
            if ok:
                with zipfile.ZipFile(archive) as z:
                    names = set(z.namelist())
                    bad = z.testzip()
                missing = [k for k in listed if "deliverables/" + k not in names]
                ok, detail = not bad and not missing, ("Archive is corrupt" if bad else "Archive is missing: " + ", ".join(missing[:5]))
            checks.append(_check("package_matches_deliverables", "pass" if ok else "fail", f"Manifest and archive match {len(listed)} deliverable(s)" if ok else detail))
    return checks


def verdict(conn, job_id: int, write_report: bool = True) -> dict[str, Any]:
    """Compute the verdict for the latest attempt and (by default) write qa/QUALITY_REPORT.json and .md in the workspace."""
    job = dict(conn.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone() or {})
    if not job:
        raise ex.ExecutionError("Job not found")
    rows = db.rows(conn.execute("SELECT check_name AS name, status, executed, detail FROM execution_qa WHERE job_id=? AND attempt=? ORDER BY id", (job_id, job["attempts"])))
    for r in rows:
        r["source"], r["executed"] = "qa", bool(r["executed"])
    checks = rows + integrity_checks(conn, job)
    if not rows:
        status, why = FAIL if job["status"] in ("qa_failed", "failed") else MANUAL, "No quality check has been executed for the latest attempt"
    elif any(c["status"] == "fail" for c in checks):
        status, why = FAIL, "Failed: " + ", ".join(c["name"] for c in checks if c["status"] == "fail")
    elif any(c["status"] == "manual" or not c["executed"] for c in checks):
        status, why = MANUAL, "Needs your own review: " + ", ".join(c["name"] for c in checks if c["status"] == "manual" or not c["executed"])
    else:
        status, why = PASS, f"All {len(checks)} checks were executed and passed"
    paths = ex.workspace_paths(job)
    out = paths["output"]
    report = {"job_id": job_id, "title": job["title"], "attempt": job["attempts"], "work_type": job["work_type"], "worker": job["worker"],
              "status": status, "summary": why, "generated_at": db.now_iso(), "checks": checks,
              "deliverables": [{"path": p.relative_to(out).as_posix(), "sha256": _sha(p), "bytes": p.stat().st_size} for p in sorted(out.rglob("*")) if p.is_file()] if out.exists() else [],
              "note": "PASS means these checks passed. It is not the platform's acceptance, and it is not payment."}
    if write_report and paths["base"].exists():
        qa_dir = paths["base"] / "qa"
        qa_dir.mkdir(parents=True, exist_ok=True)
        reliability.atomic_write_text(qa_dir / "QUALITY_REPORT.json", json.dumps(report, indent=2, ensure_ascii=False))
        lines = [f"# Quality report - job {job_id}: {job['title']}", "", f"**Verdict: {status}** - {why}", "", f"Attempt {job['attempts']}, generated {report['generated_at']}", "",
                 "| Check | Result | Executed | Detail |", "|---|---|---|---|"]
        lines += [f"| {c['name']} | {c['status']} | {'yes' if c['executed'] else 'NO'} | {str(c['detail']).replace('|', '/')[:300]} |" for c in checks]
        lines += ["", "## Deliverables (SHA-256)", ""] + [f"- `{d['path']}` {d['sha256']} ({d['bytes']} bytes)" for d in report["deliverables"]]
        lines += ["", report["note"], ""]
        reliability.atomic_write_text(qa_dir / "QUALITY_REPORT.md", "\n".join(lines))
    return report
