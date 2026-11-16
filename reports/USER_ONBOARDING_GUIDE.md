# User Onboarding Guide

For the owner of this PC. Plain steps, in order. Nothing here asks you to paste a password, cookie, one-time code, recovery code or identity document anywhere — if a screen in this program ever seems to ask for one, stop.

## 1. Start

1. Double-click `dashboard/open-dashboard.bat` (or run `py -3 microtask_monitor/local_server.py` from the repository folder).
2. Open http://127.0.0.1:8765/. The first start upgrades the database and keeps a backup next to it.
3. Top right: **■ STOP ALL AUTOMATION**. It works from any page, at any time. Nothing is deleted; **Release stop** continues.

## 2. Read the Control Centre

It shows real counts only. "Registered platforms: 25" is the list you track, not 25 automations. The line under the cards shows how many platforms sit at each automation level. Today: OpenTrain AI and Whop Workforce can be read automatically; everything else is manual.

## 3. Decide your standing instructions (Automation Settings)

Everything is off until you turn it on.

| Setting | What it means | Suggested start |
|---|---|---|
| Run permitted tasks without asking each time | The engine starts work that passes every check | On, once you have read this guide |
| Allow AI-based specialists | Starts Claude Code / Codex on this PC using your existing sign-in (no paid API) | Off until you have tried one job by hand in the Execution Centre |
| Package work whose every QA check passed | Zips deliverables with a QA report and manifest | On |
| Permitted task categories | Only these are ever started | Data processing, spreadsheet processing |
| Maximum task value / minimum hourly rate / daily minutes / concurrency | Limits | USD 50 / 0 / 240 / 1 |
| Permitted submission actions | Greyed out for every platform: none is verified for automatic submission | — |

Six categories can never be ticked because they need you personally: surveys, usability tests, voice recordings, video recordings, physical-world capture, social engagement.

## 4. Connect platforms (one at a time)

Open **Platform Manager** for the facts and **Connections** to sign in. For each platform the manager shows: official URL, Kenya status, payout status, automation level, whether an official API exists, and what needs you.

The sign-in itself:

1. Connections → the platform → **Open login window**. A normal Edge/Chrome window opens on a separate profile kept only for that platform, at the platform's own address. Check the address bar.
2. Sign in yourself. Complete any CAPTCHA, code, identity check or KYC yourself, in that window.
3. Press **I'm signed in**. The program records that you said so; it does not read the session.
4. About once a week it asks you to confirm you are still signed in.

Being signed in does not let the program act in the account. The connection will show `AUTOMATION_NOT_PERMITTED` or `POLICY_UNVERIFIED` for every platform whose terms have not been verified to allow account automation — that is correct.

### Per platform (state of the records on 2026-11-15)

| Platform | Kenya | What the program does | What you do |
|---|---|---|---|
| OpenTrain AI | job-specific | Reads the public jobs API hourly; ranks; drafts applications | Apply, interview, work, submit, withdraw |
| Whop Workforce | per bounty (country codes) | After you authorize the official API: reads bounties and your submission results | Create your own Whop OAuth app, authorize it (Whop tab); take bounties, submit proof, withdraw |
| Mindrift | **unsupported** | Watches the published eligibility policy only | Do not sign up; never use a VPN |
| Prolific, User Interviews | **unsupported** (Prolific: waitlist only) | Nothing | Do not sign up with a false country |
| Atlas Capture | unverified; needs iPhone 11+ | Nothing | Not workable with your Android phone |
| Handshake AI | unverified | Nothing — the exact service/URL is not confirmed | Confirm the official address first |
| Microworkers, SproutGigs, GainWorkers, REWORKIO | task-specific / unverified | Nothing — terms prohibit bots/scraping | Everything by hand; import tasks to track them |
| Clickworker, RapidWorkers, Hive Micro, TimeBucks, AfterQuery, CrowdGen, Neevo, OneForma, TELUS Digital AI, BLEND, Braintrust, PaidViewpoint, Swagbucks, UserTesting | unverified or project/role-specific — check in the sign-up flow or your account | Nothing automatic | Sign in yourself; import task details to have them classified, priced and tracked |

Full detail with sources: `PLATFORM_CAPABILITY_MATRIX.md`.

Payouts: the records hold only what each platform says in general. In **Earnings Ledger → Payout compatibility for Kenya**, press **Record** to note what *your* account actually offers (PayPal, Payoneer, bank transfer, M-Pesa, other) and where you saw it. Withdrawals are always done by you.

## 5. Add work

**Task Pipeline → Import a task.**

- *A task from a platform account*: choose the platform, paste the real title and instructions, the reward and deadline, and the task's AI rule if it states one (say where). If the task lists allowed countries, the pipeline uses them; otherwise press **I checked: Kenya accepted** and say where you saw it.
- *Your own client's work*: choose **Direct client work**. Attach input files. For data clean-up, attach a `spec.json` (see below) and the job runs with no AI at all.

Each task then shows its state, automation class with reasons, expected value and score.

Minimal `spec.json` for a CSV clean-up:

```json
{"input": "customers.csv", "output": "customers_clean.csv",
 "columns": ["name", "email", "country"], "required": ["email"],
 "dedupe_on": ["email"], "case": {"email": "lower", "name": "title"}}
```

## 6. Watch it work

- **Autopilot Queue**: what is ready, and for each item either "yes — fully authorized" or the exact reasons it will not start. **Run one orchestrator pass now** does a pass immediately; otherwise it runs every two minutes.
- QA results: PASS, FAIL or MANUAL_CHECK_REQUIRED. PASS is not the platform's acceptance.
- **Submissions**: packaged work, with the checks. Today every item says *manual submission required*: submit it yourself, then record the platform's reference in the Execution Centre.
- If a submission's outcome is ever unknown, it is listed and not sent again; tell the program what you see in your account.

## 7. Record outcomes and money

Execution Centre → the assignment: record *under review*, *accepted/rejected*, then *payment pending*, then *paid* with the payout reference and date. The **Earnings Ledger** keeps seven separate totals; only "Money actually received" is income.

## 8. If something looks wrong

1. Press **■ STOP ALL AUTOMATION**.
2. Control Centre → Scheduler shows each job's last result; a job stopped after repeated failures has a **Re-enable** button.
3. Platform Manager shows a red banner if a platform's automation was suspended because its record changed; read the platform's current terms, then press **I re-read the terms**.
4. Your data: `data/autopilot.sqlite3` plus dated backups beside it. A safety copy from 2026-11-15 is in `Downloads/microtask-autopilot-safety-backup-20261115`.

## Schedule

While this PC is on: report sync every 15 minutes, discovery hourly, orchestrator every 2 minutes, daily analysis from 08:00 Nairobi time (a missed run happens when the PC is next on). The cloud scan runs on GitHub's schedule, which is best-effort and can be late.
