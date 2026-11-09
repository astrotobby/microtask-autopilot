# Microtask Autopilot — public opportunity monitor

This component scans configured public RSS/Atom feeds hourly through GitHub Actions, then applies transparent keyword-based relevance triage.

## Configured public feeds

- We Work Remotely — Programming
- We Work Remotely — Design
- We Work Remotely — Sales and Marketing
- Himalayas — Remote Jobs

These are supplementary remote-job sources, not direct task feeds from Atlas Capture, Clickworker, Microworkers, RapidWorkers, SproutGigs, AfterQuery, Handshake AI, or Braintrust.

## What the scanner does

- Reads public HTTPS RSS/Atom feeds.
- Parses RSS and namespaced Atom entries.
- Deduplicates listings by URL.
- Scores keyword matches for microtask/data work, AI-related work, remote work, and Kenya-related terms.
- Flags explicit country restrictions when common restriction phrases appear.
- Produces JSON and CSV reports with source links, matched terms, score, priority, and an eligibility review label.

## How to interpret results

- `high_review` and `medium_review` mean worth human review, not confirmed jobs.
- `kenya_mentioned_verify_requirements` means a Kenya-related term appeared; it is not proof of eligibility.
- `remote_scope_unclear_kenya_not_confirmed` means the listing mentions remote work but does not establish that Kenyan residents can apply.
- `likely_restricted_check_listing` means a restriction phrase was detected; inspect the original listing.
- Every listing is marked as requiring human verification. Keyword scores do not prove legitimacy, pay, availability, or eligibility.

## Run and download reports

1. Open **Actions**.
2. Select **Hourly public opportunity monitor**.
3. Click **Run workflow**, choose `main`, and confirm.
4. Open the completed run and download `hourly-opportunity-report`.
5. The artifact contains a JSON report, a spreadsheet-friendly CSV, and a standalone HTML dashboard. Open the HTML file in a browser for a readable overview of the eight-platform directory, reviewed role snapshots, and public-feed results.

Scheduled runs are configured for 17 minutes past each hour (UTC). GitHub may delay scheduled runs, and usage is subject to account/repository limits.

## Local control dashboard

A no-dependency dashboard is available at `dashboard/index.html`.

### Automatic local agent on Windows

1. Download or clone this repository and extract it.
2. Open the `dashboard` folder.
3. Double-click `open-dashboard.bat`.
4. The launcher starts the local Python server at `http://127.0.0.1:8765` and opens the dashboard.
5. The local agent checks GitHub Actions for the latest successful run every 15 minutes, downloads the public report artifact automatically, validates it, and stores opportunities in SQLite at `data/autopilot.sqlite3`.
6. The dashboard fetches the latest synchronized report automatically. Manual artifact downloads and JSON imports are no longer required for normal operation.
7. For automatic startup after Windows sign-in, double-click `install-autostart.bat` once. This creates a current-user scheduled task; it does not require a paid host.
8. Use **Export backup** regularly for browser-side application and earnings records.

The report sync uses the public GitHub REST API and needs no GitHub token because this repository is public. If the repository becomes private, the sync design will need an approved read-only authentication method. The server binds only to `127.0.0.1`, not your local network. It syncs public opportunity reports; it does not sign in to platform accounts, complete tasks or submit applications. The SQLite database stores deduplicated opportunities and report history. The dashboard's application and earnings records remain in this browser's local storage; those records are not yet synchronized into SQLite or between devices.

The local agent must be running for automatic dashboard updates. If it is not running, double-click `open-dashboard.bat` again. Do not store passwords, cookies, tokens, OTPs, identity documents or payment credentials in the dashboard. Currency totals are kept separate; no exchange rate is assumed.

## Scope and safety

- Public feeds only; no worker-account logins or private dashboards.
- No accepting tasks, completing timed work, submitting proofs, or handling payouts.
- No bypassing CAPTCHA, MFA, anti-bot controls, identity checks, or platform rules.
- Only add feeds whose use is permitted by their publisher.
- Never store passwords, cookies, session tokens, API keys, or identity documents in this repository.

## Target platform registry

The original eight services plus OpenTrain AI, GainWorkers, Mindrift and REWORKIO (twelve in total, see below) are recorded in `microtask_monitor/platforms.json`: Atlas Capture, Clickworker, Microworkers, RapidWorkers, SproutGigs, AfterQuery, Handshake AI and Braintrust.

The hourly report includes this registry so the monitor can distinguish target platforms from supplementary public job-board listings. Unknown official URLs and uncertain service identities remain explicitly unverified. Kenya eligibility is not assumed; check the individual task, role and account restrictions.

The registry is intentionally conservative: it tracks platform status and allowed/blocked actions but does not sign in, scrape private dashboards, accept tasks, complete work or submit proofs. Platform automation must remain disabled unless the exact service identity, current official terms and task-specific instructions explicitly permit the proposed action.

The workflow runs unit tests against the registry before each scan.

## AfterQuery discovery

- Official portal: https://experts.afterquery.com/home
- Independent discovery directory: https://www.sidequest.expert/roles?platform=afterquery
- Reviewed role snapshots and fit notes: `microtask_monitor/discovery_sources.json`

The official Experts portal is the only application destination. The third-party directory is independent, is not affiliated with AfterQuery, may use referral links, and may contain stale or incomplete listings. Verify live status, pay, requirements and Kenya eligibility in the official portal before applying.

The current setup does not scrape the logged-in Experts portal or the third-party directory. No verified public RSS/API endpoint or permission for automated fetching has been established. The report includes the discovery links and dated role snapshots for human review; these snapshots are not refreshed hourly. You can use the directory's free daily role-alert signup to receive updates, then review alerts and applications yourself.

Initial role triage:
- Software Engineering Expert: conditional review; listed as worldwide at $75/hour, but the listing asks for 1–3 years of professional software engineering experience and independent debugging/code-review evidence. The rate is not guaranteed.
- Content Writer & AI Evaluation Specialist (UK/US): low priority until Kenya eligibility is explicitly confirmed; older listing, 40 hours/week, and targets UK/US candidates.
- Algorithms and Theory Researcher: not recommended without a relevant PhD, published algorithms research and released research code.

These are third-party listing summaries, not an offer, verified opening guarantee, or confirmation that a Kenyan applicant will be accepted.

## Braintrust talent dashboard

- Official talent dashboard supplied by the user: https://app.usebraintrust.com/talent/dashboard/welcome/
- Public talent overview: https://www.usebraintrust.com/for-talent
- Terms: https://www.usebraintrust.com/terms
- Help centre: https://support.usebraintrust.com/hc/en-us

Braintrust is a vetted professional talent marketplace, not a general microtask board. Its current talent overview describes profile creation, an AI skills interview, certification and matching to contract/full-time roles and AI gig work. The platform's Terms list restricted territories; Kenya is not named in the current listed territories, but eligibility for each role and the payout route must still be confirmed. Never use a VPN to evade location restrictions.

The dashboard is account-facing. This project does not log in, read private account pages, complete assessments, submit applications, accept contracts or handle identity verification. The user must review and complete those steps personally and truthfully. Verify payout availability and method for Kenya inside official account settings before relying on Braintrust for income.

## Eight-platform discovery coverage

The report now carries discovery entry points for all eight target services:

| Platform | Discovery entry point | Automation boundary |
|---|---|---|
| Atlas Capture | https://www.atlascapture.io/opportunities | Public information only; recording and submissions are human-performed. Standard consumer capture needs iPhone 11+; confirm country/device support. |
| Clickworker | https://www.clickworker.com/clickworker/ | Public information only; registration and payment availability vary by country. Check account Payment details before doing work. |
| Microworkers | https://www.microworkers.com/ | Manual account activity pending current terms review. |
| RapidWorkers | https://rapidworkers.io/ | User-confirmed domain; verify operator, terms, payment and task availability. |
| SproutGigs | https://sproutgigs.com/ | Manual account activity pending current terms review. |
| AfterQuery Experts | https://experts.afterquery.com/home | Official portal; manual review and application. |
| Handshake AI | https://joinhandshake.com/ai/opportunities/ | Official Handshake AI program page; check role requirements and work authorization. |
| Braintrust | https://www.usebraintrust.com/for-talent | Professional talent marketplace; manual profile, vetting and applications. |

The source directory is `microtask_monitor/discovery_sources.json`. The hourly report packages all eight platform records and discovery links alongside public-feed listings. It does not scrape account dashboards, infer that a remote listing accepts Kenyan applicants, or claim a job is open just because a platform landing page exists.

## Twelve-platform registry and integration modes

The registry (`microtask_monitor/platforms.json`) now holds 12 platforms in three groups, with eligibility and integration health tracked independently (`microtask_monitor/integrations.json`):

| Group | Platforms |
|---|---|
| Microtask platforms | Atlas Capture, Clickworker, Microworkers, RapidWorkers, SproutGigs, GainWorkers |
| AI training and evaluation | AfterQuery Experts, Handshake AI, OpenTrain AI, Mindrift |
| Professional freelance and expert marketplaces | Braintrust, REWORKIO |

Every platform is described by three separate boundaries:

- **PUBLIC DISCOVERY** – unauthenticated, documented, permitted automated reads.
- **AUTHENTICATED MANUAL REVIEW** – you sign in and review yourself; nothing here logs in for you.
- **AUTOMATION PERMITTED** – only the read-only monitoring listed below. No platform permits automated applications, task acceptance, submissions, payments or contract acceptance.

| Platform | Public discovery | Automation permitted | Status (verified 2026-11-08) |
|---|---|---|---|
| OpenTrain AI | Documented anonymous `GET /api/public/v1/jobs` (cursor paging, no key) | Read-only job listing | **Implemented and tested against the live API.** |
| Mindrift | None – restricted for Kenya | Read-only watch of the public eligibility policy | **Restricted for user location.** Kenya is listed as unsupported in the official policy (page "Last updated: May 27, 2026"). Not counted as an active Kenya-eligible source. |
| GainWorkers | None – task lists need login, `robots.txt` disallows them, the Terms prohibit scraping/bots; the `/api/v2` API is an employer/SMM-reseller API, not a worker-task API | None | **Manual portal review.** Optional local records in `data/manual/gainworkers_tasks.json` are scored, deduplicated and checked for expired/filled tasks. |
| REWORKIO | None – `/projects/` and `/opportunities/` are client-rendered shells over an undocumented backend; the dashboard needs sign-in | None | **Manual review links only; automated discovery unavailable.** Optional local records in `data/manual/reworkio_briefs.json`. |

### OpenTrain AI
- Fetches `country=KE` first (small, complete), then the unfiltered list, with at most ~100 requests/minute (documented limit: 120/min/IP), `Retry-After` handling on HTTP 429, a repeated-cursor guard and a 45-minute cache.
- Eligibility is always derived locally from each job's own country list: **Kenya listed** → `kenya_explicitly_eligible`; **other countries only** → `restricted_other_countries`; **empty list and the posting says it is open worldwide** → `worldwide_eligible`; **empty list with no statement** → `kenya_eligibility_unspecified` (never assumed eligible). A job's eligible countries are separate from OpenTrain's payout-country list.
- Partner jobs (`EXTERNAL_AGGREGATED`) are flagged; jobs whose `applyMode` is `external` are flagged as external applications.
- Payment is copied from the listing. Missing or invalid payment is shown as "Not disclosed"; unusually high amounts (over USD 150/hour) are flagged `suspicious_verify_manually` and earn no scoring bonus. No earnings are estimated.

### Mindrift eligibility monitor
`microtask_monitor/mindrift.py` compares the live public policy with the reviewed baseline in `microtask_monitor/eligibility_baseline.json`. If Kenya disappears from the list or the policy changes, the report carries an alert and `needs_manual_review`; **discovery stays disabled and no applications are recommended** until you review the policy yourself and update the baseline. A layout change that cannot be parsed is reported as an error, never as "Kenya removed". Do not create accounts, and do not use VPNs, proxies or alternate identities.

### GainWorkers and REWORKIO manual records
Optional JSON files under `data/manual/` (gitignored) let you record what you saw while signed in: `title`, `url`, `payment` or `budget`, `region`/`location`, `remaining_slots`, `status`, `description`, `skills`, `posted`/`discovered_at`, `last_verified`. They are scored on your skills, checked for expired/filled/closed items, deduplicated, and flagged for possible upfront costs (paid membership, deposits, escrow funding). Nothing is purchased, accepted or submitted.

### Unified scoring
Priority order: Kenya-explicit > worldwide > relevance to your skills (AI training/evaluation, annotation, prompting, Python, JavaScript, backend/full-stack, AI-assisted coding, AI automation, writing/research) > legitimate disclosed pay > recent posting > availability. Scores are triage aids, not guarantees; every item requires human verification and approval.

## Phase 2: persistent work management (applications, tasks, evidence, earnings)

The dashboard is now a control panel backed by the local SQLite database (`data/autopilot.sqlite3`), not browser storage.

| Area | What it does | What stays manual |
|---|---|---|
| **Opportunity Explorer** | Stores every discovered opportunity with a transparent score, an eligibility class and its reason, requirements found in the text, change history and expiry. Filter by platform, eligibility, skill, pay, category; sort by score, date or pay. | Opening the original link and applying. |
| **Application Manager** | Prepares a tailored draft from your *established* profile only, lists what is missing, lets you edit and approve it, and records when you submitted and what the platform answered. | Submitting. Approval never sends anything. |
| **Task Workspace** | Paste task instructions: get a simplified summary, rules, materials, evidence needs, common mistakes and a checklist; track work sessions, evidence (stored locally with a SHA-256) and your submission record. | Doing and submitting the work. |
| **Earnings** | Separates *expected*, *pending* and *received*. Only a payment you record (with a received date, not in the future) counts as received. Advertised pay is never earnings. | Receiving and verifying payment. |
| **Platform Monitor** | All 12 platforms: Kenya status, capability levels, integration health, policy verification date, restrictions, required manual action. | Everything the platform requires. |
| **Notifications** | Dashboard (always), Windows desktop balloon, optional email and Telegram. Deduplicated; a first run gives the top few plus one digest. | – |

### Eligibility classes (never guessed)
`KENYA_CONFIRMED` (the listing's own country list includes Kenya) · `WORLDWIDE_CONFIRMED` (it says open worldwide) · `KENYA_UNVERIFIED` · `KENYA_UNSUPPORTED` (country list excludes Kenya, or the platform policy lists Kenya as unsupported, e.g. Mindrift) · `EXPIRED` · `MANUAL_REVIEW_REQUIRED`. A site being reachable from Kenya, or a keyword like "Africa", never confirms eligibility.

### Platform capability records and enforcement
`microtask_monitor/capabilities.json` records, for each platform, discovery, API, login, permitted and prohibited automation, submission rules, country eligibility, payment verification, the last policy verification date and the policy URLs, using the levels `PUBLIC_DISCOVERY_ALLOWED`, `DOCUMENTED_API_ALLOWED`, `MANUAL_ONLY`, `HUMAN_APPROVAL_REQUIRED`, `AUTOMATION_PROHIBITED`, `UNVERIFIED`. Only platforms whose policy was actually read are marked verified; the others say `UNVERIFIED` honestly. `microtask_monitor/policy.py` enforces this in code: unknown platforms/actions fail closed, automated submission, task acceptance, CAPTCHA/MFA/KYC bypass, VPN/proxy use, scraping logged-in areas and probing private endpoints are blocked for every platform, and no platform currently permits automated application submission. Atlas Capture records that your Samsung A05 cannot run iPhone-only workflows.

### Reliability
Report downloads use retries with exponential backoff, resumable HTTP Range downloads of a gzip + manifest published by the workflow, SHA-256 verification of both files, an atomic all-or-nothing database import, and an atomic replace of `data/latest_report.json` only after the database commit. A failed or invalid download is never recorded as success and never replaces the last good report. `GET /api/health` shows the last success, failure streak and a plain-language explanation. GitHub scheduled workflows can be delayed; hourly execution is not guaranteed.

### AI assistance ($0)
`microtask_monitor/assistant.py` is rule-based and fully local by default (no model, no network, no paid API). Optionally set `AUTOPILOT_AI_PROVIDER=ollama` (and `AUTOPILOT_OLLAMA_MODEL`) to let a *local* Ollama model polish wording; its output is discarded if it adds numbers, links or length not in your draft. A 1-3B model needs roughly 2-4 GB of free RAM and runs slowly on a CPU-only laptop; it is optional.

### Notifications setup (all optional, free)
Desktop notifications work on Windows by default (disable with `{"desktop_notifications": false}` in `data/settings.json`). Email: set `AUTOPILOT_SMTP_HOST`, `AUTOPILOT_SMTP_PORT`, `AUTOPILOT_SMTP_USER`, `AUTOPILOT_SMTP_PASSWORD` (an app password), `AUTOPILOT_SMTP_TO`. Telegram: create your own bot with @BotFather and set `AUTOPILOT_TELEGRAM_BOT_TOKEN` and `AUTOPILOT_TELEGRAM_CHAT_ID`. Credentials come only from environment variables and are never stored or logged.

### Security
The server binds to 127.0.0.1 only, requires a loopback `Host` header, rejects cross-origin and non-JSON writes (CSRF/DNS-rebinding protection), has no endpoint that contacts a platform, and offers no delete endpoint. Never put passwords, cookies, OTPs or identity documents anywhere in this project.

### Profile
`microtask_monitor/profile.json` lists the skills established for matching and drafting, and `not_established` items (degrees, years of experience, certifications, portfolio links) that are never claimed without your confirmation.

## Daily earning-opportunity pipeline

Every day the project prepares a ranked, evidence-backed shortlist without you asking. It never applies for anything: it ranks what the permitted discovery adapters already collected and prepares drafts for you to review.

### When it runs
| Trigger | Where | Notes |
|---|---|---|
| Every hourly scan | GitHub Actions | The scan embeds a `daily_shortlist` in the report, so a shortlist exists even while your PC is off. A second schedule at 05:00 UTC (08:00 East Africa Time) labels the daily run. GitHub may delay scheduled runs: exact times are not guaranteed. |
| After each sync that brought changes | Your PC | Only when the new report added, changed or expired something. Identical data never triggers a repeat analysis, and dashboard refreshes never trigger one. |
| From 08:00 East Africa Time | Your PC | A scheduler thread checks every 10 minutes and runs at most once per Nairobi day. A laptop that was off at 08:00 runs the missed analysis when it is back on. |

Each analysis is recorded in SQLite (`analysis_runs`) with its date and time, is idempotent per source report, recovers after an interrupted run, and never deletes earlier valid shortlists or reports.

### What counts as suitable
Open, **Kenya-confirmed or worldwide-confirmed** (from the listing's own data), no fees/deposits/memberships, and a role whose **title** matches your established skills (Python, JavaScript/TypeScript, Node, backend/full-stack, AI coding tools, prompt/LLM work, n8n/Make automation, writing, data entry, AI video). Roles are excluded when they need domain expertise you have not established (e.g. engineering trades, scientific experts), specific tools or languages you have not established (Premiere Pro, C++, Rust, game engines, CAD), Apple hardware, or more than one unestablished qualification.

### Ranking (no reward for pay alone)
Kenya-confirmed > worldwide; role-level skill match; clearly stated and plausible compensation (a small capped bonus; unusually high pay gets nothing and is flagged); a verified payment route usable in Kenya; realistic entry requirements; freshness; and a small platform-priority bonus following your research order: OpenTrain AI, AfterQuery, Braintrust, Clickworker, Atlas Capture, Handshake AI, Microworkers, RapidWorkers, SproutGigs, GainWorkers, REWORKIO, Mindrift. The priority order guides where to look first and is **not** permission to automate: only OpenTrain AI has permitted automated discovery; the others appear in the coverage table as "check this yourself today", and Mindrift is marked restricted for Kenya.

### Outputs
Top five, a shortlist of up to 20, Kenya-confirmed and worldwide-confirmed lists, and newly discovered listings (separate from earlier ones). Each entry shows the original link, compensation, payment method (only if verified), eligibility evidence, matched skills, requirements, assessments/interviews mentioned, deadline and last-verified time. If nothing qualifies, the dashboard and notification say so plainly. Application drafts are prepared (once each, status `draft`) for the top five and saved in the Application Manager.

### Dashboard and notifications
The **DAILY EARNING OPPORTUNITIES** section shows today's top five, the full shortlist, newly discovered items, application status, the draft application, direct links and the last analysis time (local and cloud). You can mark each opportunity reviewed, shortlisted, applied, rejected or accepted; this is stored in SQLite (`opportunity_review`), not in the browser. Notifications (deduplicated): daily shortlist ready (once per day after 08:00 EAT), a few genuinely new high-quality Kenya-confirmed opportunities, deadlines within three days, and material changes (eligibility, availability, pay, deadline) to opportunities you track.

## First-earning workflow (applications you finish yourself)

Everything here prepares and tracks; **you** open the platform, apply, sit assessments and record what happened. Nothing is submitted, accepted or paid automatically.

- **Application packet** (Applications tab, "Open application packet"): opportunity details, original and apply links, compensation, payment route, assessments, the tailored draft with your own profile answers filled in, reusable answers with copy buttons, platform-specific steps, a follow-up plan and a "ready / not ready" list of blockers. Each save adds a versioned snapshot in SQLite. Blank personal answers are never invented.
- **Reusable profile answers** (Applications tab): name, contact, availability, experience, portfolio and similar. Passwords, one-time codes, ID/card numbers are rejected.
- **Reminders**: deadline milestones (7, 3, 1 days), assessment preparation, stale drafts, follow-ups, interviews, accepted work, task due dates, profile gaps. Stored once (never repeat after you tick them off) and delivered through the existing free notification channels.
- **Follow-up tracking**: record submitted, response, follow-up sent, interview scheduled/done, decision. A follow-up date is set 7 days after submission.
- **Platform guidance and quality checklists**: verified entries (OpenTrain AI, Microworkers, Handshake AI, Braintrust, RapidWorkers, Atlas Capture) cite the platform's own pages and dates; every other platform shows a generic checklist labelled UNVERIFIED. New task workspaces include the platform checklist.
- **TODAY'S ACTIONS** (Overview): at most twelve prioritised items (profile gaps, deadlines, approving drafts, applying, follow-ups, tasks, payment checks, two rotating manual platform checks).

### Platform capability review
Each platform record carries a `review` (what was read, when, and what it does and does not permit). Only Microworkers, Handshake AI, GainWorkers, Mindrift, REWORKIO and SproutGigs are AUTOMATION_PROHIBITED, each for a specific cited reason. Platforms whose terms could not be fully read stay UNVERIFIED.

## Execution engine (doing the actual work)

The **Execution Centre** tab turns real task instructions into checked deliverables:

1. You paste the instructions from your own account (and upload input files). Nothing is fetched from a logged-in area or invented.
2. The work is classified (software, writing, data, evaluation, content, automation). Unclear text stays `unknown` until you choose.
3. **AI-use gate.** Instructions that forbid AI block AI work, and that cannot be overridden. If the rules say nothing, the status is `unknown`, which blocks AI work until you record where the rules permit it. Every AI run also needs your explicit approval.
4. A specialist worker runs in an isolated folder `data/executions/job-N/` (`input/`, `output/`, `logs/`, `package/`):
   - The data worker is deterministic and uses no AI. It applies a `spec.json` you supply (trim, case, required columns, dedupe, categorise, allowed values).
   - Agent workers (software, writing, evaluation, content, automation) start your local **Claude Code** (`claude -p`) or **Codex** (`codex exec`) using your own sign-in. The prompt goes over stdin. The tools are restricted to the workspace, and the prompt forbids fabrication and platform contact.
5. **Independent QA** re-inspects the files and re-runs the tests itself. Any `fail` keeps the job out of review. `manual` items (e.g. factual claims, live n8n runs, media) are never counted as passes, and you must acknowledge them when you approve.
6. You approve the work. It is then packaged as a zip with deliverables, `QA_REPORT.txt`, `SUBMISSION_GUIDE.txt` and `MANIFEST.json` with SHA-256 hashes. You submit it yourself and record the reference.
7. Accepted/rejected is recorded separately. Payments count only when you record money actually received.

Jobs, events, files and QA are stored in SQLite (migration 5). Pause and cancel are supported. After a restart a running job becomes `interrupted` and resumes from its saved workspace. A job owned by another live process is left alone while its heartbeat continues.

**Platform execution rules** (`capabilities.json` → `execution`) for OpenTrain AI, AfterQuery, Braintrust and Clickworker:
- Task instructions are copied by hand on all four.
- None allows automated execution on the platform or automated submission.
- AI-assistance status is **unverified** on all four. The task or client rules decide, and you record them per job.

## Eligibility and safety policy

- Treat Kenya eligibility as unconfirmed until the official signup flow or role explicitly accepts Kenya.
- Clickworker says country registration can be paused and payment methods vary; check the account's Payment details before doing work.
- Atlas Capture's standard consumer capture requires iPhone 11 or newer; verify a supported device and country before investing in equipment.
- Handshake AI is the program on joinhandshake.com; do not confuse it with unrelated similarly named sites. Some opportunities have specific work-authorization requirements.
- Microworkers, SproutGigs and RapidWorkers remain manual-only until their current official terms and task rules are verified.
- Never automate CAPTCHA, MFA, KYC, private account access, task completion or proof submission without explicit platform permission. No fake work, fabricated evidence or location circumvention.
- Public discovery pages are navigation aids, not live feeds. The hourly workflow currently refreshes configured RSS sources; it does not promise hourly refresh of the eight account portals.
