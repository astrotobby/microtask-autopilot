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

The eight requested services are recorded in `microtask_monitor/platforms.json`: Atlas Capture, Clickworker, Microworkers, RapidWorkers, SproutGigs, AfterQuery, Handshake AI and Braintrust.

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

## Eligibility and safety policy

- Treat Kenya eligibility as unconfirmed until the official signup flow or role explicitly accepts Kenya.
- Clickworker says country registration can be paused and payment methods vary; check the account's Payment details before doing work.
- Atlas Capture's standard consumer capture requires iPhone 11 or newer; verify a supported device and country before investing in equipment.
- Handshake AI is the program on joinhandshake.com; do not confuse it with unrelated similarly named sites. Some opportunities have specific work-authorization requirements.
- Microworkers, SproutGigs and RapidWorkers remain manual-only until their current official terms and task rules are verified.
- Never automate CAPTCHA, MFA, KYC, private account access, task completion or proof submission without explicit platform permission. No fake work, fabricated evidence or location circumvention.
- Public discovery pages are navigation aids, not live feeds. The hourly workflow currently refreshes configured RSS sources; it does not promise hourly refresh of the eight account portals.
