# First-Paid-Task Milestone

Date: 2026-11-16
Branch: `main`
Base commit: `b552664`
Repository: https://github.com/astrotobby/microtask-autopilot

This milestone was about moving the existing system one step closer to one genuine paid task, not adding features. The work below is a local audit, one ready-to-send application packet and one small execution-engine change. No application was submitted, no client was contacted, no contract was accepted and no money was recorded.

## 1. Verified live state

Checked read-only against `data/autopilot.sqlite3` on 2026-11-16 (schema version 12). The current database matches the previous snapshot and does not contradict it.

| Item | Verified value |
|---|---|
| Business leads (`biz_leads`) | 96 total: 69 rejected, 22 qualified, 5 moved to proposal-prepared |
| Proposal drafts (`biz_proposals`) | 5, all status `prepared`, all `sent_at` empty |
| Applications submitted | 0 (`submissions` 0; no application row has `submitted_at`) |
| Awarded projects (`biz_projects`) | 0 |
| Invoices / payments / earnings | 0 / 0 / 0 |
| Execution jobs | 0 |
| Whop submissions | 1, status `denied`, reason "Proof missing or unclear" (5 Nov 2026) — a real past attempt, not income |
| Open opportunities in the engine | 2,418 |
| Business sources | HN public API, We Work Remotely RSS, Himalayas RSS, engine listings — all last status `ok` |

Real income to date: **USD 0**. Nothing in this report changes that.

## 2. Top three real targets

Ranked by skill fit, Kenya eligibility evidence, pay/payment clarity, deadline, assessment burden, time to first payment and AI-use rules.

1. **OpenTrain AI — Associate Forward Deployed Engineer** (lead 87, `$40–60/hr`, part-time contract).
   Kenya is named in the job's own applicant-location list; the rate is stated; the requirements are foundational Python, APIs, CLI, Git, data validation and AI coding tools — all truthfully met today. Assessment: 3 screening questions plus a short AI interview. Listing valid through 2026-12-12. **Selected as primary.**
2. **OpenTrain AI — Backend Software Engineer / Backend Coding Task Evaluator** (leads 84 and 80, `$30–100/hr` per accepted task).
   Kenya listed, strong portfolio match (deterministic verifiers and an independent QA gate already exist in this repo), but "Mid-Senior level", 7 screening questions and a coding challenge. Drafts 4 and 5 already exist and are not usable as written.
3. **Collabify — HN "Seeking Freelancer"** (lead 5, `$3,000–4,000/month`, remote worldwide).
   Verified live through the public Hacker News API and the Google Form. The form's required first question asks whether you have personally built and maintained a multi-tenant SaaS product used by paying customers. The owner has not, so this is not actionable without a false statement. Draft 1 must not be sent.

## 3. Primary target packet

Written to `data/packets/packet-6-lead87-associate-fde.txt` (local, git-ignored because it contains personal application fields).

It contains the original links, the listing's real requirements quoted, the honest match/gap table, Kenya evidence, pay clarity, an estimated scope, the AI-use position (unknown until a client says otherwise), ready-to-paste profile/answer/resume text, and the exact manual steps only the owner can perform. It is explicitly labelled as not submitted.

The existing five drafts were read in full first. The primary target had no draft, so nothing is duplicated. The three weak drafts were diagnosed: irrelevant skills listed for backend roles, unearned rate claims, and a listing title presented as a requirement.

## 4. Execution-engine change (one proven blocker removed)

All AI work previously required Claude Code or Codex. Both are installed on this machine, but both consume the owner's own subscription. The owner's constraint is no paid usage, so the only verified zero-cost AI runner is the bundled OpenCode CLI with a free model. That was the blocker removed.

Verified before integrating (2026-11-16):

- `opencode-cli.exe --version` → `opencode v2.3.0`.
- `--help` and `run --help` were read; no flags were guessed.
- `opencode models` lists `opencode/deepseek-v4.1-flash-free` (the model currently selected in the app).
- A real non-interactive run in a scratch folder with `run --model opencode/deepseek-v4.1-flash-free --format json --archived` answered correctly and every `step_finish` reported `"cost":0`.
- A sandbox test with a workspace-local `.opencode/opencode.json` policy: file writes inside the workspace succeeded, `python -c` succeeded, and reading `C:/Windows/win.ini` was denied with `Permission denied: external_directory`. Exit code 0 on the clean run.
- `--standalone` was included so a job never joins the shared app server, and `--archived` keeps jobs out of session lists.
- Timeout and cancel now terminate the whole process tree (`taskkill /F /T` on Windows) instead of only the parent.

Change set:

- `microtask_monitor/workers.py`: optional `opencode` runner (default `opencode/deepseek-v4.1-flash-free`), workspace-local deny-by-default policy, file-based prompt, free-model guard, process-tree termination, `AUTOPILOT_AI_RUNNER` / `AUTOPILOT_OPENCODE_BIN` / `AUTOPILOT_OPENCODE_MODEL` overrides.
- `tests/test_execution.py`: 5 new tests (command shape, workspace policy, paid-model refusal, fallback order, process-tree termination on timeout and cancel); the existing "no runner installed" test now also patches OpenCode discovery.
- `MICROTASK_AUTOPILOT_README.md`, `reports/USER_ONBOARDING_GUIDE.md`: documentation.

Preserved unchanged: `DataWorker` (deterministic, no AI), Claude and Codex runners, `policy.py`, `permissions.py`, `control.py`, the orchestrator, independent QA, packaging, pause/cancel and recovery. Default runner order is still `claude` → `codex`; OpenCode is used only when explicitly selected or when neither of the other two is installed. Direct-client versus platform permission separation is untouched: an award still requires evidence, and AI still requires a recorded client permission with its source. Platform execution and submission remain blocked.

## 5. Verification

- Focused: `python -m unittest tests.test_execution` → 44 tests, OK (exit 0).
- Full suite: `python -m unittest discover -s tests` → **764 tests, OK (1 skipped)**, exit 0.
- `git diff --check` → clean.
- No live award, job, submission or payment was created. Tests use temporary directories and fake runners.

## 6. Owner action (the next genuine step)

Open the packet at `data/packets/packet-6-lead87-associate-fde.txt` and follow its numbered steps: read the listing, add a truthful resume to a free OpenTrain account, submit the application yourself, take the AI interview yourself if invited, and record the submission date. Do not record an award or payment until they are real.

If the client later awards the work and permits AI help in writing, record that permission with its source before starting any execution job.

## 7. Status

No task has been earned. There is no award, no accepted deliverable and no received payment. This report records preparation and verification only.
