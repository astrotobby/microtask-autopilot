# Milestone 1: Platform Access & Execution Engine (report)

Branch: `milestone-1/platform-access-engine` (not pushed). Backup taken first: `C:/Users/chanz/Downloads/microtask-autopilot-backup-20261116-111624`
(working-tree patch, untracked list, git status/HEAD, database snapshot, data JSON files, email templates). The earlier backup was not touched.
No destructive git command was run.

## 1. Features implemented

| Area | What now exists |
|---|---|
| Capability profiles | 11 capabilities per platform (`public_discovery`, `email_notification_monitoring`, `authenticated_monitoring`, `task_reading`, `task_classification`, `task_preparation`, `ai_execution`, `quality_assurance`, `proof_preparation`, `automatic_submission`, `earnings_monitoring`), each `allowed` / `prohibited` / `unverified` with its reason. Unverified fails closed. Computed from the existing permission engine, so there is one source of truth. |
| Permission evidence | `capability_evidence.json`: a grant counts only with an https `source_url`, a `source_type` (platform terms, API docs, assignment instructions, written permission), the exact clause, `verified_at` (not in the future) and `expires_at` (not past). A prohibition always wins. A submission grant is ignored unless execution is permitted. The shipped file grants nothing. |
| Four-part gate | `capabilities.check` answers separately: platform rule, the assignment's own rules, your pre-authorization, and your limits. All four must be yes. |
| Submission mode | All 25 platforms report `automatic_submission: prohibited` and `submission_mode: human_required`, unchanged from before. |
| Standard adapter | `PlatformAdapter` with the 11 methods (`get_platform_info` … `retrieve_earnings`), each returning `{supported, ok, reason, …}`. One instance per platform; Whop has a subclass. A new platform needs only an entry in `ADAPTERS`; the orchestrator is unchanged. |
| Email discovery | `.eml` folder import (`data/email_inbox`, on by default, local only) and a **read-only** Gmail connector (off by default; uses the app password already stored for outreach; `select(readonly=True)` and `BODY.PEEK[]`, so nothing is marked read, moved or deleted). It extracts platform (sender or link domain, so look-alike domains are refused), title, link (unsubscribe/settings links skipped), reward, currency, deadline, skills, availability and eligibility. Imports are idempotent by content hash and go through the normal import rules. |
| 8 categories | `task_intel.category8`: 1 fully automatable & permitted · 2 AI-assisted & permitted · 3 preparation permitted, submission restricted · 4 human required · 5 platform prohibited · 6 country/account ineligible · 7 policy unclear · 8 not economically worthwhile. Economics can only demote a task. |
| Ranking settings | New: `min_reward_usd`, `max_daily_tasks`, `preferred_platforms`, `preferred_categories`, `email_import_enabled`, `email_monitor_enabled`. Existing: `min_hourly_rate_usd`, `max_concurrency` (max workers), `max_daily_execution_minutes`, `permitted_categories`, `excluded_task_types`. Preferences add +3 each; unclear policy −5; not worthwhile −10. `authorize` enforces the minimum reward and the daily task cap. |
| Workers | Software, data (deterministic without AI when a spec is given), writing, AI evaluation, content, automation, and the new **research** specialist with research QA checks: Sources section, at least two independent sites, and a manual spot-check flag. Independent QA (`qa_gate`) is unchanged. |
| Outcomes A/B/C | `prepare_submission` returns A (every submission condition passed and you pre-authorized it), B (package ready, you submit) or C (the engine may not execute it), with the exact reasons. A task the engine may not execute is always C, never B. |
| States | `lifecycle.SPEC_STATES` (the 17 states) derived from the engine state, so there is no second state column. Idempotency is unchanged (claims, unique external ids, email content hashes, the submission ledger). |
| Ledger | Per-entry `actual_minutes`, `estimated_minutes`, `approved_at`, `rejected_at`, `rejection_reason`; `earnings_overview` (received all-time and today, estimated today labelled as an estimate, pending, approved, paid, USD per hour of measured execution). Only recorded payments count as earned. |
| Scheduler | New job `email_discovery`, every 30 minutes; each source has its own switch. |
| Dashboard | New tab **Platform Access Engine** with sections A–F (Platform Control Centre capability grid, Opportunity Intelligence, Autonomous Execution, Submission Centre with an A/B/C check per task, Earnings, System Health), and an "Import notification emails now" button. |
| API | `GET /api/engine/capabilities`, `/opportunities`, `/health`, `/earnings`, `/tasks/<id>/submission`; `POST /api/engine/email/import`; `/ledger` now includes `overview`. |

## 2. Files changed

New: `microtask_monitor/capabilities.py`, `microtask_monitor/capability_evidence.json`, `microtask_monitor/platform_adapters.py`,
`microtask_monitor/email_discovery.py`, `samples/email/*.eml` (4 samples and a README), `tests/test_milestone1.py`, this report.

Modified: `permissions.py` (evidence file), `control.py` (new settings), `task_intel.py` (8 categories, preferences),
`orchestrator.py` (minimum reward, daily task cap), `execution.py` / `workers.py` / `qa.py` (research specialist), `lifecycle.py` (spec states),
`ledger.py` (outcome fields, earnings overview), `scheduler.py` (email job), `engine_api.py` (endpoints), `dashboard/index.html` (new tab).

Also on this branch, from before the milestone (uncommitted): `outreach.py` and `tests/test_outreach.py`, the no-clip email variant.

## 3. Database migrations

None. Everything new comes from existing tables (assignments, assignment_events, execution_jobs, task_intel, payments) or the `state`
key-value table (`email_imported`, `email_discovery_health`). The schema stays at version 11.

## 4. Tests

- Baseline before the milestone: 665 tests, OK (1 skipped).
- New: `tests/test_milestone1.py`, 44 tests covering capabilities and states; fail-closed behaviour; evidence validation, expiry and prohibition; the four-part gate; the adapter interface on all 25; outcomes A/B/C; `.eml` parsing on the samples, look-alike domains, oversized and HTML mail; idempotent import; the read-only IMAP contract (a fake server that fails on any write command); the 8 categories and economic demotion; preferences; settings validation; minimum reward and daily cap; research routing and QA; spec states; ledger fields; endpoints; the scheduler switch; the dashboard sections; source rules (no web client, browser automation, cookies or IMAP writes). Also one end-to-end test.
- End-to-end, on a temporary database through the adapter interface only, with simulated platform evidence: import → classify (deterministic, category 3) → prepare → execution refused until you record that you took the task → deterministic execution → independent QA PASS → outcome B → automatic submit refused → state `SUBMISSION_READY`, output verified.
- Full suite: 708 tests; see section 10.

## 5. Platforms with discovery

| Channel | Platforms |
|---|---|
| Public API | OpenTrain AI |
| Authorized API (read-only OAuth) | Whop Workforce |
| Policy monitor | Mindrift |
| Email notifications (`.eml` folder, or read-only inbox once you switch it on) | 22 platforms (the 3 listed as unavailable in Kenya are skipped) |
| Manual import | All 25 |

## 6. Authorized monitoring

Whop Workforce only (your OAuth app, bounty read scope). Earnings monitoring by API: Whop only. Every other platform: you record payments with a reference.

## 7. Autonomous execution

Zero platforms permit automated task completion on the evidence on record (`ai_execution`: 19 unverified, 6 prohibited). The engine can
run work autonomously only:
- for your direct-client lane, where you are the authority; or
- once a platform grant with source, clause and expiry is added to `capability_evidence.json`, and the specific task's rules also permit it.

## 8. Automatic submission verified

None, on any of the 25 platforms. Submission stays human-required everywhere. This is deliberate and matches each platform's record.

## 9. Manual platforms

22 platforms have no permitted automated discovery: Atlas Capture, Clickworker, GainWorkers, HiveMicro, Microworkers, RapidWorkers, SproutGigs,
TimeBucks, AfterQuery, CrowdGen, Handshake AI, Neevo, OneForma, TELUS Digital AI, Blend, Braintrust, Reworkio, PaidViewpoint, Prolific,
Swagbucks, User Interviews and UserTesting. Their tasks arrive through email notifications or your own import, then go through the same
classification and the 8 categories.

## 10. Blockers and the next milestone

- **The real blocker to platform income is permission, not code.** No platform's terms on record allow automated completion. The
  fastest permitted earning path is still the owner lane (your Shorts service on Whop, plus outreach), which is already running.
- To raise a platform's level, add a grant to `capability_evidence.json`, quoting its own terms or API documentation (https link, clause,
  dates). The engine then uses it automatically.
- Switch on the read-only Gmail monitor (`email_monitor_enabled`) if you want platform notification emails picked up without exporting them.
- The outreach templates' approval showed "not approved" after the no-clip sections were added. It needs a re-approval in the dashboard
  (the approval is a hash of the file, so any edit resets it).
- Country is fixed to Kenya throughout the engine; it is not a setting.
- Next milestone, suggested: a payment-reconciliation importer (PayPal, Payoneer and Whop statements) so the ledger fills itself, and
  evidence-review reminders before each grant expires.

Full suite after the milestone: 708 tests. One dashboard wording test failed: the new grid's "auto-submit" label broke the rule that
the dashboard never says "auto-submit". The label was renamed and that module and the milestone tests were re-run: OK. Every other test passed (1 skipped).
