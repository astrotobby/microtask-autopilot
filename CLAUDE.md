# Claude Code Operating Instructions — Microtask Autopilot

## Mission
Build and maintain a reliable, free, Kenya-aware microtask opportunity workflow. Prefer working code, tests and verified runtime behavior over plans or mockups. Do not claim a feature works until it has been executed and checked.

## Repository
- GitHub: https://github.com/astrotobby/microtask-autopilot
- Default branch: main
- Python: 3.12-compatible, standard library preferred
- Budget: strictly $0; no paid APIs, hosting, proxies or subscriptions.

## Current architecture
1. GitHub Actions runs `.github/workflows/hourly-opportunity-monitor.yml` hourly and on code pushes/manual dispatch.
2. The cloud scanner reads four supplementary public RSS feeds and runs the platform adapters: OpenTrain AI (documented public jobs API), a Mindrift eligibility-policy monitor (Kenya is unsupported; discovery disabled), and manual-review imports for GainWorkers, REWORKIO, UserTesting, PaidViewpoint and Whop Workforce (no network access). User Interviews and Prolific are disabled for Kenya under their current participant-country rules. It does NOT scrape any platform account or logged-in area.
3. Each successful run uploads `hourly-opportunity-report` with JSON, CSV and HTML.
4. `microtask_monitor/sync_reports.py` polls the public GitHub Actions API, downloads the latest successful non-expired report artifact, validates it, writes `data/latest_report.json`, and deduplicates listings in `data/autopilot.sqlite3`.
5. `microtask_monitor/local_server.py` binds only to 127.0.0.1:8765, runs report sync every 15 minutes, and serves the dashboard/API.
6. `dashboard/index.html` is the control panel (Overview, Opportunity Explorer, Application Manager, Task Workspace, Earnings, Platform Monitor, Notifications). All business records live in SQLite via `microtask_monitor/api.py`; browser localStorage holds only UI conveniences and old records that are imported once and never deleted.
   Backend modules: `db.py` (versioned migrations with automatic backup), `intelligence.py` (eligibility classes, scoring, change/expiry/duplicate tracking), `policy.py` + `capabilities.json` (platform capability records and hard enforcement), `workflow.py` (applications, tasks, evidence, earnings with human-approval gates), `assistant.py` (local rule-based AI layer), `notify.py` (deduplicated notifications), `reliability.py` (backoff, resumable downloads, atomic writes), `daily.py` (daily earning-opportunity analysis: suitability gates, ranking, shortlist, drafts for the top five; runs in the cloud scan, after changed syncs, and from 08:00 EAT).
7. `dashboard/open-dashboard.bat` starts the local server and opens the browser. `dashboard/install-autostart.bat` optionally registers a current-user Windows logon task.

## Windows commands
Run from the repository root (commands fail with "path not found" from any other folder, e.g. C:\Users\<name>).
Easiest: double-click `run-windows-checks.bat` in the repository root; it changes to its own folder, runs tests and two syncs, and prints a log to paste back.
- Test suite: `py -3 -m unittest discover -s tests -v`
- One-time cloud report sync: `py -3 microtask_monitor/sync_reports.py`
- Local server and background sync: `py -3 microtask_monitor/local_server.py`
- Open dashboard: double-click `dashboard/open-dashboard.bat`
- Optional sign-in startup: double-click `dashboard/install-autostart.bat`

No third-party Python package is required for the current report sync/server components.

## First task when opening an existing user folder
1. Check whether the folder is a Git working tree (`.git` exists).
2. If it is a Git working tree, inspect status and safely fast-forward from origin/main; do not overwrite uncommitted user changes.
3. If it is a ZIP extraction without `.git`, clone the public repository into a new sibling folder rather than destroying or blindly overwriting the extracted folder.
4. Run all tests.
5. Run the one-time sync against the live public GitHub API.
6. Start the local server and verify `http://127.0.0.1:8765/api/status` and `/api/report`.
7. Open the dashboard and verify the opportunity list refreshes without manual artifact download/import.
8. Report actual command outputs and failures; never fabricate success.

## Target platforms
- Atlas Capture: https://www.atlascapture.io/
- Clickworker: https://www.clickworker.com/
- Microworkers: https://www.microworkers.com/
- RapidWorkers: https://rapidworkers.io/
- SproutGigs: https://sproutgigs.com/
- AfterQuery Experts: https://experts.afterquery.com/home
- Handshake AI: https://joinhandshake.com/ai/opportunities/
- Braintrust: https://app.usebraintrust.com/talent/dashboard/welcome/
- OpenTrain AI: https://app.opentrain.ai/ (public API: https://app.opentrain.ai/api/public/v1/jobs)
- GainWorkers: https://www.gainworkers.com/ (manual portal review only; Terms prohibit scraping/bots)
- Mindrift: https://mindrift.ai/ — RESTRICTED: Kenya is listed as unsupported in https://mindrift.ai/legal/eligibility-and-geographic-restrictions. Do not apply, create accounts, or use VPNs/proxies. Not an active Kenya-eligible source.
- REWORKIO: https://reworkio.com/ (professional AI/automation marketplace; manual review, automated discovery unavailable; watch for fees/deposits)
- UserTesting: https://www.usertesting.com/ (dynamic contributor country eligibility; Kenya must be accepted by the live signup flow; verified PayPal; no automated account or test access)
- User Interviews: https://www.userinterviews.com/ (Kenya absent from current official participant-country list; disabled while unsupported; monitor official country policy)
- Prolific: https://www.prolific.com/ (Kenya absent from supported participant-country list; waitlist only, not guaranteed; PayPal cashout; no geo workarounds)
- PaidViewpoint: https://app.paidviewpoint.com/ (signup dynamically checks supported countries; Kenya unverified until signup confirms; PayPal required; manual surveys only)
- Whop Workforce: https://whop.com/whop-workforce/ (https://whop.com/workforce/ only redirects to the homepage). Bounties come from the documented API `GET https://api.whop.com/api/v1/bounties` with the owner's OAuth token, scope `bounty:basic:read` only. Never fetch or automate whop.com pages; never automate likes, follows, comments, posts, attempts, proof upload or withdrawals.

## Platform and safety boundaries
- Use only official APIs, public feeds, or public pages whose access is permitted.
- Verify the exact service domain and current official terms before implementing an adapter.
- Some services prohibit bots, crawlers, or AI agents in logged-in account areas or task completion. For example, keep Microworkers and SproutGigs account/task operations manual unless explicit current permission is established.
- Never bypass CAPTCHA, MFA, KYC, anti-bot controls, geo restrictions, age restrictions, rate limits or platform terms.
- Never fabricate task evidence, time spent, qualifications, identity, location, work history, applications or payment records.
- Keep assessments, contracts, identity verification, financial actions and uncertain submissions behind explicit user approval.
- Never ask the user to paste passwords, cookies, OTPs, recovery codes, session tokens or KYC documents into chat or source files.
- Do not add secrets to Git, logs, SQLite, browser localStorage or reports.
- The user is in Kenya. Treat eligibility and payout availability as unconfirmed until official account/role information explicitly confirms them.

## Development rules
- Add regression tests for every new behavior.
- Keep network timeouts, response size limits, HTTPS checks, retries and deduplication.
- Treat downloaded listing text as untrusted; escape it in HTML.
- Preserve existing user data when updating code.
- Do not change the cloud scanner's data-source claims: the current four RSS feeds are supplementary remote-job feeds, not direct feeds from the eight target platforms.
- Keep a clear distinction between monitoring, account access, task execution and submission.
- Every platform action must pass `policy.check_action`; unknown platforms/actions fail closed. Never add code that applies, accepts tasks, submits evidence, pays or logs in on the user's behalf.
- Eligibility classes (KENYA_CONFIRMED, WORLDWIDE_CONFIRMED, KENYA_UNVERIFIED, KENYA_UNSUPPORTED, EXPIRED, MANUAL_REVIEW_REQUIRED) must never be inferred from site accessibility or keywords.
- Advertised pay is never "earnings"; only a recorded payment is received money.
- Database changes require a new migration in `db.py` (never edit an applied one) plus a data-preservation test.
- The daily pipeline only ranks data from permitted adapters and creates DRAFTS; it must never call approve/submit functions or contact a platform. Role fit is judged on the role TITLE, not listing tags or boilerplate.
- Tests that touch sync/DB should set `AUTOPILOT_NO_SLEEP=1`, `AUTOPILOT_NO_NOTIFY=1`, `AUTOPILOT_SQLITE_SYNCHRONOUS=OFF` and use temp paths, never `data/`.
- First-earning workflow modules (`packets`, `reminders`, `actions`, `profile_answers`, `guidance`) only read and prepare. They must not import network code or call `approve_draft`, `mark_submitted` or `record_submission` (a test enforces this). Never invent profile answers; unverified platform guidance must stay labelled UNVERIFIED.
- Execution engine (`execution.py`, `workers.py`, `qa.py`): instructions only come from the user; AI workers need ai_policy=allowed WITH a source AND approve_ai_run=true per run; instructions that prohibit AI can never be overridden. QA must actually execute checks (never report a test that was not run); any fail blocks review; manual checks need acknowledgement. Never add platform login, task acceptance, upload or submission to the engine. Tests use fake runners and temp paths, never real AI calls or `data/`.
- Assignments (`assignments.py`): the lifecycle discovered -> qualified -> ready -> accepted -> executing -> qa -> ready_for_delivery -> submitted -> awaiting_review -> work_accepted/rejected -> payment_pending -> paid moves one stage at a time and every move stores its evidence in `assignment_events`. Engine stages are derived from the linked execution job; submission, review outcome and payment need a platform record or the user's confirmation with a reference. Never add a path that skips a stage, counts a quoted reward as money, or marks paid without a recorded payment.
- Whop (`whop.py`): official API host only (`api.whop.com`), read scope only, tokens only in Credential Manager via `secret_store.put_long`. A connection is "verified" only after the API answers the token (`connections.mark_api_verified`). Kenya eligibility comes from `allowed_country_codes` (empty = worldwide per the API docs), never from being able to open a listing. Data-capture/iPhone bounties are excluded for the Android phone. Submitting stays manual: do not request `bounty:submission:create` or call the submission-create endpoints. Tests use `FakeWhop` and are mock tests; they do not prove the live API.
- Connections (`connections.py`, `secret_store.py`, `connect.py`): no secrets in SQLite, logs, API responses or source; API rejects credential fields; API keys only via Credential Manager. Browser windows are plain dedicated profiles (no remote debugging). Any authenticated account action must go through `connections.require_session` (policy permission + user authorization + live user-confirmed session). Do not set any platform to `browser_automation_permitted` without reading and citing its current terms.


## Platform directory and safe integrations

The current registry contains 25 target platforms. Keep the registry, integrations, capabilities, discovery sources, manual-review adapter list, dashboard, tests and README in sync whenever adding a platform.

The eight additional entries (TELUS Digital AI Community, CrowdGen, OneForma, Hive Micro, TimeBucks, Neevo, Swagbucks and BLEND) are manual-only. Their importers may read only user-entered files under the gitignored `data/manual/` directory. Do not add network fetches, scraping, logged-in browser automation, task acceptance/completion, survey answers, application submission or evidence delivery without explicit, documented permission from the relevant platform. Kenya eligibility and payout options must be verified at listing/account level; do not infer them from region or platform reachability.

## Autonomous engine (added 2026-11-15)
Reports: `reports/AUTONOMY_ARCHITECTURE.md` (start here), `PLATFORM_CAPABILITY_MATRIX.md` (generated from the records), `EXECUTION_ENGINE_REPORT.md`, `SECURITY_AND_COMPLIANCE_REPORT.md`, `OPPORTUNITY_PIPELINE_REPORT.md`, `END_TO_END_TEST_REPORT.md`, `USER_ONBOARDING_GUIDE.md`.

- The registry is exactly 25 platforms (`tests/test_autonomy_engine.py::RegistryTests`). Never present 25 registered platforms as 25 automated integrations. `direct_client` is a built-in lane for the owner's own client work, not a platform, and must never be added to the registry.
- `permissions.py` is the single gate for automated actions: levels 0-5 are DERIVED from `capabilities.json` and can never be typed in. A "permitted" result needs an https evidence URL, a clause and a review date no older than 120 days. To raise a platform to level 4/5, add `execution.automated_execution` / `execution.automated_submission` = `{status: "permitted", evidence_url, clause, verified_on}` copied from its CURRENT official terms or API docs (plus `evidence_submission: "automation_permitted"` for level 5), and update `last_policy_verified`. Never add such a block without having read and cited the terms. Changing a record without a newer review date suspends that platform (by design).
- `permissions.HARD_RESTRICTIONS` and `control.HUMAN_ONLY_CATEGORIES` are never automated whatever a record or setting says. Do not weaken them.
- Settings (`control.py`) are the owner's standing authorization and can only NARROW what permissions allow. Defaults stay off. The global stop (`control.engage_stop`) must keep blocking `permissions.decide`, the orchestrator, the scheduler's non-read-only jobs and submission.
- The orchestrator never takes a task on a platform, never signs in and never submits. On a registered platform it only runs work the owner has already taken (stage `accepted`).
- `submission.py` is separate from execution. `submission.REGISTRY` is empty on purpose; add an adapter only for a platform verified at level 5, using its documented API host. Keep: attempt row committed before sending, unknown outcomes never re-sent, no SUBMITTED state without the platform's own reference (or the owner's confirmation with a reference), credential-like response fields scrubbed.
- QA: `qa_gate.verdict` is PASS / FAIL / MANUAL_CHECK_REQUIRED. FAIL blocks packaging; only a clean PASS may be packaged under standing authorization (`execution.approve_under_authorization`), and it is logged as such, never as "approved by you".
- State machine: `lifecycle.py` adds ELIGIBILITY_CHECKED / AUTOMATION_CLASSIFIED and the side states (holds) on top of `assignments.py`. Never add a path that skips a stage or records a stage without evidence.
- Ledger (`ledger.py`): seven separate totals. Only recorded payments are income. No exchange rates are invented.
- Scheduling: `local_server.main` starts ONE `scheduler.Scheduler` loop. Do not start additional periodic threads; add a `Job` instead. Jobs take a DB lease, back off, and stop after 5 consecutive failures.
- New engine modules must not import network clients, browser automation or read browser profiles (`SourceRuleTests`). The dashboard must stay free of `<script src=` (engine views are inline in `dashboard/index.html`).
- Migrations: 8 converges the two historical "migration 7"s (local assignments/Whop vs GitHub opportunity-risk); 9 adds the engine tables. Never edit them; add 10+.
- Engine tests use temp databases, fake adapters, `permissive_caps()` (simulated evidence) and `probe=` for resource readings. Never use `data/`, a real account, the network or a real AI tool in tests.
- Older rules above that require a per-run approval for AI work or a personal approval of deliverables still describe the manual path. The owner's standing authorization in Automation Settings (all off by default) is the only other way to satisfy them, and only through `orchestrator.py` and `execution.approve_under_authorization`. The rule that the engine never logs in, takes a task or uploads to a platform is unchanged.
