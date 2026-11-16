# Security and Compliance Report

Status labels are defined in `AUTONOMY_ARCHITECTURE.md`. No secret, account identifier or private record appears in this file.

## Compliance position

No platform restriction was circumvented, and no code path exists that could circumvent one:

| Never automated | How it is enforced | Status |
|---|---|---|
| CAPTCHA solving/bypass, MFA bypass, KYC/identity verification | `policy.ALWAYS_BLOCKED`; `permissions.HARD_RESTRICTIONS` matched on each task's own words; connections report a human step and stop | IMPLEMENTED, TESTED |
| Geo-restriction circumvention (VPN/proxy) | same; platforms that list Kenya as unsupported are disabled (Mindrift, Prolific, User Interviews) and cannot be connected or imported into | IMPLEMENTED, TESTED |
| Fake accounts, fake engagement, fake reviews, referral manipulation | hard-restriction patterns → task classified NOT_FEASIBLE and held POLICY_BLOCKED | IMPLEMENTED, TESTED |
| Fabricated recordings/evidence; pretending to be the user in surveys, usability tests, recordings | six human-only categories can never be executed or put on the permitted list; classified HUMAN_REQUIRED | IMPLEMENTED, TESTED |
| Physical-world actions | HUMAN_REQUIRED | IMPLEMENTED, TESTED |
| Withdrawals, transfers, purchases | no code performs them; tasks asking for them are NOT_FEASIBLE; payments are only *recorded*, by you, with a reference | IMPLEMENTED, TESTED |
| Tasks whose instructions prohibit AI/automation | `execution.detect_ai_rules` + AI_PROHIBITED class; cannot be overridden | IMPLEMENTED, TESTED |
| Upfront fees, deposits, paid memberships | cost flags → NOT_FEASIBLE (the $0 rule) | IMPLEMENTED, TESTED |
| Logging in, taking a task, applying on your behalf | not implemented anywhere; the orchestrator refuses a platform task you have not taken yourself | IMPLEMENTED, TESTED |
| Automated submission | `submission.REGISTRY` is empty; 11 checks; level 5 required | BLOCKED on all 25 platforms |

Permission evidence recorded per platform and action (`permission_evidence` table): platform id, action, result, evidence URL, clause, scope, verification date, last review date, record fingerprint, suspension reason. Missing evidence blocks; evidence older than 120 days blocks; a capability record that changes without a newer review date suspends that platform until you confirm you re-read the terms (TESTED).

Important limitation: the evidence on file was reviewed 2026-11-08 to 2026-11-12. It was **not re-read from the platforms' sites on 2026-11-15**. Re-reading terms is a human step; the engine only detects staleness and changes in its own records.

## Account connections

- You sign in yourself in a normal browser window on a dedicated profile folder (`data/browser-profiles/<platform>`), with no remote-debugging or automation flags. The program never reads that profile's cookies or passwords (existing design, kept).
- API credentials (Whop OAuth tokens) live only in Windows Credential Manager, least-privilege scope `bounty:basic:read`.
- Nine connection states; `CONNECTED` describes the session only. The combined state shows `AUTOMATION_NOT_PERMITTED` or `POLICY_UNVERIFIED` for a signed-in platform whose terms do not permit account automation (TESTED).
- The API rejects any request body containing a credential-like field name (password, cookie, otp, token, api key, secret, session id…), including on every new `/api/engine` endpoint (TESTED).

## Local server

| Control | Status |
|---|---|
| Binds to 127.0.0.1 only; Host header allow-list; same-origin + JSON content-type for state changes (CSRF / DNS-rebinding protection) | existing; TESTED; LIVE-VERIFIED (log line "Local-only binding") |
| Exclusive port (no second copy) | existing; TESTED |
| Request body limit 14 MB; strict JSON object bodies | existing; TESTED |
| No external JavaScript; no `<script src>`; content escaped with `esc()`; links limited to https with `rel="noopener noreferrer"` | TESTED (the new views are inline and use the same helpers) |
| Engine inputs validated: unknown settings rejected, numeric ranges enforced, task imports limited to 100 records, https-only links, 3-letter currency, 10 MB/10 files for inputs with sanitised names | IMPLEMENTED, TESTED |
| Emergency global stop: header button on every page, `/api/engine/stop`; release needs your confirmation | IMPLEMENTED, TESTED, LIVE-VERIFIED |

## Data protection

- SQLite with versioned, additive migrations and an automatic file backup before every upgrade (LIVE-VERIFIED: `autopilot.sqlite3.bak-v7-…` was created when your database went 7 → 9; 2,018 stored opportunities were preserved).
- `data/` is git-ignored (database, browser profiles, executions, cache). Reports in `reports/` contain no account data.
- Audit trail: `audit_log` for every control action; `assignment_events` for every state change; both pass text through `secret_store.redact` (TESTED with bearer tokens and API-key-like strings).
- Platform responses stored as submission evidence have credential-like fields removed first (TESTED — this caught and fixed a real leak path during development).
- Deliverables are scanned for secret-like strings before packaging (existing QA check).
- SHA-256 recorded for every input, output and package file; quality report and manifest per execution.
- Atomic writes for reports and caches (`reliability.atomic_write_*`); resumable downloads and HTTPS validation in the existing sync (unchanged).

## Untrusted content

Listings, task instructions and uploaded files are data. They are escaped in HTML, never executed, and placed after fixed rules in worker prompts. Task content cannot change settings, permissions or the stop state: those change only through validated API calls that require your confirmation.

## Automatic stops

| Trigger | Effect | Status |
|---|---|---|
| You press STOP | nothing new starts; running jobs are asked to pause; claims released; permission gate denies every action | TESTED, LIVE-VERIFIED |
| Capability record changed without re-review | that platform's automation suspended | TESTED |
| Discovery adapter fails 4 times in a row | adapter stopped for 6 hours (circuit breaker), audit entry | TESTED |
| Scheduler job fails 5 times in a row | job stopped until you re-enable it; one notification | TESTED |
| QA fails twice | assignment held FAILED | IMPLEMENTED |
| Low disk or memory | no new job starts | TESTED |

## $0 operating requirement

No new dependency was added. Everything is Python standard library, SQLite, the existing GitHub Actions scan and the Claude Code / Codex CLI you already have. No paid API, proxy, CAPTCHA service, hosting or database. The engine's execution cost model is therefore 0, and any task that requires spending is excluded.

Not reviewed in this session: the `webapp/` and `website/` folders that arrived from GitHub in the merge (a hosted public dashboard). They are not used by the local engine. If that site is deployed anywhere, confirm separately that it is free and exposes no private data (UNVERIFIED).

## Residual risks

1. Hard-restriction and category detection is pattern-based. It errs towards blocking (false positives send a task to you), but a cleverly worded task could avoid a pattern; the platform-permission gate still applies independently.
2. An AI CLI worker is confined by its working directory and tool allow-list, not by an OS sandbox.
3. Evidence quality depends on honest, current capability records; the engine cannot read terms for you.
4. Notifications by email/Telegram (existing, optional) send task titles off this PC if you configure them.
