# Opportunity Pipeline Report

How work is found, checked, classified, priced and ranked. Status labels are defined in `AUTONOMY_ARCHITECTURE.md`.

## Sources actually in use

| Source | Method | Frequency | Status |
|---|---|---|---|
| OpenTrain AI jobs | Documented public API (`/api/public/v1/jobs`), read-only, ≤100 requests/min, 45-minute cache | Hourly cloud scan + hourly local adapter (the local read is skipped when the cloud report is younger than 90 minutes) | LIVE-VERIFIED: a real scan completed in this session; the running server reported `opentrain_ai: ok` |
| Four supplementary remote-job RSS feeds | Existing cloud scanner | Hourly (GitHub Actions, best-effort) | existing; LIVE-VERIFIED via report sync (public report-data branch) |
| Whop Workforce bounties | Documented API with your OAuth authorization, scope `bounty:basic:read` | Hourly (with the discovery job) once authorized | IMPLEMENTED, TESTED with a fake API; live UNVERIFIED — status is `needs_you` until you authorize it |
| Mindrift eligibility policy | Policy monitor run by the cloud scan (Kenya is unsupported; nothing else is read) | With the cloud scan | existing |
| The other 22 platforms | **No automated access.** You copy task details from your own account into Task Pipeline → Import a task | When you do it | IMPLEMENTED, TESTED |
| Direct client work | You add it | When you do it | IMPLEMENTED, TESTED |

In your database at the time of the live check: 2,018 stored opportunities, 734 open listings with explicit Kenya or worldwide eligibility, 0 tasks in the task pipeline (nothing had been imported and Whop is not connected).

## Adapter contract (`discovery.py`)

Every adapter has `identify_platform`, `verify_platform_identity`, `check_policy`, `parse_opportunity`, `report_adapter_health`. Discovery methods exist only where permitted:

- `OpenTrainAdapter.discover_public_opportunities`
- `WhopAdapter.discover_authorized_account_opportunities`
- `ManualImportAdapter.import_records` (no discovery method at all — a test asserts their absence)

Before each run the engine checks: identity verified from the reviewed registry → `permissions.decide` → circuit breaker → rate limit (minimum interval per adapter). One adapter failing never stops the others. Handshake AI has no confirmed official URL, so its identity check fails and nothing is ever opened for it.

## Normalisation and duplicates

- Listings go through `intelligence.ingest`: canonical URL, one row per (platform, external id), change tracking, duplicate marking, expiry of listings no longer returned by a complete scan (existing, kept).
- Tasks become assignments with a `UNIQUE(platform_id, external_id)` constraint; importing the same record twice returns the existing one (TESTED).
- Listings not re-verified for 14 days stop counting as open; assignments past their deadline before submission become EXPIRED (TESTED).

## Eligibility (Kenya)

Decided only from recorded facts, in this order: platform lists Kenya as unsupported → ineligible; the task's own allowed-country list → eligible / ineligible; a listing class already verified as KENYA_CONFIRMED or WORLDWIDE_CONFIRMED → eligible; otherwise **unverified**. Being able to open a listing proves nothing. An unverified task waits at ELIGIBILITY_CHECKED until you confirm where the platform shows Kenya is accepted; a task the facts mark ineligible cannot be confirmed (TESTED).

## Payment conditions

`lifecycle.payment_conditions` reports the stated amount and currency, the platform's general payout status, and problems: no amount stated, or a possible upfront cost. Upfront costs make a task NOT_FEASIBLE. Platform-level payout status and what *your account* offers are tracked separately (Earnings Ledger → Payout compatibility); only you can mark an account route as verified, with where you saw it.

## Task intelligence (`task_intel.py`)

Extracted per task: title, description, platform, compensation, currency, payment method note, country restrictions, required skills, required equipment, estimated duration, deadline, acceptance conditions, evidence requirements, AI policy (declared + detected in the text), automation policy, submission policy, risks.

25 categories: software development, coding/debugging, data processing, spreadsheet processing, data annotation, image labelling, video annotation, text classification, writing, technical writing, AI evaluation, LLM evaluation, prompt engineering, content creation, translation, transcription, research, survey, usability testing, voice recording, video recording, physical-world capture, social engagement, administrative, other.

Automation class — blocking facts are checked before permissive ones:

| Class | When |
|---|---|
| NOT_FEASIBLE | Kenya excluded; upfront cost; a never-automated action; no local specialist for the category |
| HUMAN_REQUIRED | needs your own opinion, voice, face, presence, identity or attestation |
| ELIGIBILITY_UNVERIFIED | nothing on record proves Kenya is accepted |
| PLATFORM_AUTOMATION_PROHIBITED | the platform's terms prohibit automated access/completion |
| AI_PROHIBITED | the task's instructions prohibit AI or automation |
| MANUAL_REVIEW_REQUIRED | platform permission unverified, AI rule not established, or instructions not on file |
| DETERMINISTIC_LOCAL_PROCESSING | permitted + a deterministic spec was supplied (no AI) |
| AI_ASSISTED_WITH_PERMISSION | permitted + AI allowed with its source; submission stays with you |
| FULLY_AUTOMATABLE_WITH_PERMISSION | as above and automated submission is also verified |

A simple task on an unverified platform is MANUAL_REVIEW_REQUIRED, not automatable (TESTED).

## Economics and ranking

| Figure | How |
|---|---|
| Advertised compensation | as stated (net if given) |
| Acceptance probability | measured accepted/rejected on that platform, blended with a prior (weight 4) |
| Payment reliability | measured paid/accepted when history exists; otherwise 0.9 for a documented payout route, 0.6 for unverified |
| Expected compensation | advertised × acceptance |
| Risk-adjusted value / net value | × payment reliability, minus execution cost (0) |
| Estimated time | stated in the task, else a planning estimate per worker type (labelled as such) |
| Effective hourly rate | net ÷ time; compared with your minimum only when the currency is USD |
| Repeat availability | identical titles seen on the platform |

Score (0–100): automation feasibility up to 30, proven eligibility 20, value up to 20, acceptance up to 10, payout evidence up to 8, skill match up to 6, repeatability up to 3, deadline ±, −4 per risk flag, −40 for blocked classes. A blocked task advertising USD 500 ranks below a permitted USD 4 task (TESTED). These are ranking estimates, never earnings.

## What would increase real opportunities

1. Authorize Whop (your own OAuth app, read scope) so bounties are read and classified automatically.
2. Import tasks from the platforms where you already have accounts; each is classified, priced and tracked.
3. For any platform whose current terms genuinely permit automated or AI-assisted completion, record the clause, URL and date in its capability record — that is the only route to level 4.
4. Confirm the Handshake AI service and URL so it can be onboarded.
