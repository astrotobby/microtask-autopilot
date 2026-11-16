# Execution Engine Report

Covers orchestration, specialist workers, isolated workspaces, independent QA and controlled submission. Status labels are defined in `AUTONOMY_ARCHITECTURE.md`.

## What runs without asking, and what does not

| Step | Direct-client lane (your own client's work) | A registered platform |
|---|---|---|
| Record the task | You add it (Task Pipeline → Import a task) | You import it, or OpenTrain/Whop adapters read listings |
| Eligibility + classification | Automatic | Automatic |
| Taking the task | Not applicable | **You**, on the platform (BLOCKED for the engine) |
| Execution | Automatic if your settings allow it | BLOCKED today: no platform has verified permission (level 4) |
| QA + quality report | Automatic and independent | Same code path (TESTED with a simulated permitted platform) |
| Packaging | Automatic only when every check passed and you enabled it | Same |
| Submission | **You** deliver to your client | BLOCKED today: level 5 on no platform; you submit and record it |

Defaults are conservative: `auto_execute`, `ai_runs_preauthorized` and `auto_approve_clean_qa` are **off** until you switch them on in Automation Settings. Default permitted categories: data processing and spreadsheet processing.

## Specialist workers (existing `workers.py`, dispatched by the orchestrator)

| Spec worker | Implementation | Needs AI | Status |
|---|---|---|---|
| Software development | AgentWorker "software" (Claude Code / Codex CLI in the job folder) | yes | IMPLEMENTED, TESTED with a fake runner; real AI run UNVERIFIED in this session |
| Data processing | DataWorker — deterministic with `spec.json` (trim, case, dedupe, required, categorize, allowed values) | no (with spec) | IMPLEMENTED, TESTED end to end through the orchestrator |
| Writing | AgentWorker "writing" | yes | IMPLEMENTED, TESTED with a fake runner |
| AI evaluation | AgentWorker "evaluation" | yes | IMPLEMENTED, TESTED (QA checks) |
| Content | AgentWorker "content" | yes | IMPLEMENTED, TESTED (QA checks) |
| Automation (n8n / Make / scripts) | AgentWorker "automation" | yes | IMPLEMENTED, TESTED (QA checks) |

No paid model API is used. AI workers start the `claude` or `codex` command already installed and signed in on this PC; `workers.claude_env` strips inherited `ANTHROPIC_*` overrides. Claude Code 2.1.319 is installed (LIVE-VERIFIED: `claude --version`). Whether that CLI is currently signed in for worker runs was **not** checked in this session (UNVERIFIED) — run `tools/claude-login.ps1` if an AI job reports it is not.

Categories with no local specialist (image labelling, video annotation, transcription, "other") classify as NOT_FEASIBLE rather than being attempted badly. Six categories are always a real person's own experience (survey, usability testing, voice recording, video recording, physical capture, social engagement): they classify as HUMAN_REQUIRED and cannot be put on the permitted list.

## Isolation

Each job gets `data/executions/job-N/` with `input/ output/ logs/ tests/ qa/ package/` and `INSTRUCTIONS.md`.

- Task text is passed explicitly inside a fixed prompt whose non-negotiable rules come first; task text is data and cannot change the rules (TESTED: an injected "ignore all previous instructions" line stays below the rules).
- The AI tool is started with the job folder as its working directory and a restricted tool list (`workers.CLAUDE_TOOLS`).
- File previews are served only from inside the job workspace (path traversal refused).
- Input files you attach are staged under `data/executions/staging/assignment-N/` with sanitised names and a 10 MB limit.
- Limit to be aware of: confinement of an AI CLI to its folder relies on that tool's own permission model plus the working directory; it is not an OS-level sandbox (UNVERIFIED beyond the fake-runner tests).

## Duplicate prevention and recovery

- Atomic claim (`UPDATE … WHERE claimed_by IS NULL OR claim_until < now`) — a second worker cannot claim the same assignment (TESTED).
- An assignment with a job is never offered again; `start_execution` is idempotent (TESTED: one job after two dispatches).
- After a restart, a running job with no live worker becomes `interrupted` and its assignment INTERRUPTED; it is resumed only by you, never re-run silently (TESTED).
- Heartbeats every 20 s distinguish a live worker in another process from a dead one (existing, kept).
- Job time limit from settings (`job_timeout_seconds`), bounded concurrency (1–4), daily execution minutes (TESTED).

## Independent QA (`qa.py` + `qa_gate.py`)

The worker's own report of success is not an input. The verdict combines the checks `qa.py` actually executed (existence, empty files, NOTES.md, placeholder text, secret-like strings, named deliverables, and per work type: unit tests really run, CSV parse/columns/no records lost/required/allowed values/duplicates, annotation consistency, workflow JSON validity…) with integrity checks re-computed from disk:

| Check | Catches | Result |
|---|---|---|
| `hashes_recorded` | a deliverable changed, added or removed after QA | FAIL |
| `no_unexpected_files` | programs/launchers among deliverables (.exe, .bat, .ps1, .lnk …) | FAIL |
| `workspace_contained` | files written outside the expected folders | MANUAL_CHECK_REQUIRED |
| `package_matches_deliverables` | manifest/archive not identical to the deliverables | FAIL |

Verdict: **FAIL** if any check failed (blocks packaging and submission); **MANUAL_CHECK_REQUIRED** if any check was not executed or needs a person (never reported as verified, never auto-packaged); **PASS** only when every check was executed and passed. Each execution gets `qa/QUALITY_REPORT.json` and `.md` with every check and the SHA-256 of every deliverable; the package carries `MANIFEST.json`. Status: IMPLEMENTED, TESTED (tampering, unexpected program, stray file, manual check, clean pass).

"No fabricated data" is enforced where it is checkable: data work may not output more rows than it received (`no_records_lost`), placeholders fail, and AI writing must list sources and is routed to your review. Factual accuracy of AI-written prose cannot be proven mechanically — that is why writing normally ends at MANUAL_CHECK_REQUIRED.

## Controlled submission (`submission.py`)

Separate module; the orchestrator never calls a platform. Eleven checks, all evaluated and shown:

correct task · correct platform · correct account (API-verified connection) · eligibility · automation permission · AI-use permission · submission permission (platform evidence + your pre-authorization; never for human-only work) · deadline · required deliverables · QA result = PASS · submission destination is the platform's documented API host.

- Idempotency key = SHA-256(platform, task id, package hash). The attempt row is committed **before** sending.
- Timeout, network error, crash, or an answer without the platform's own reference → attempt is `unknown`, nothing is recorded as submitted, and it is **never re-sent** until reconciled (the platform's lookup, or your confirmation with the reference you see in your account).
- A confirmed submission moves the assignment to SUBMITTED only. Acceptance and payment are separate recorded events.
- Credential-like fields in a platform's answer are removed before the answer is stored (TESTED).
- `submission.REGISTRY` is empty: no real platform adapter exists because none is permitted. Status: IMPLEMENTED, TESTED with a simulated level-5 platform; real automated submission BLOCKED.

## What I could not verify

- A real AI worker run started by the orchestrator (needs your settings switched on and a real task; not run to avoid creating records in your database without your say-so).
- Any real platform task execution or submission (BLOCKED by policy, by design).
