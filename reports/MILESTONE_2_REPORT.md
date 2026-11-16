# Milestone 2: Freelance Business Engine

Date: 2026-11-16
Branch: `main`
Repository: https://github.com/astrotobby/microtask-autopilot

## What was completed

This milestone adds a direct-client freelance business lane alongside (not in place of) the existing 25-platform microtask system.

- Four permitted discovery inputs: Hacker News public API, We Work Remotely RSS, Himalayas RSS, and eligible listings already in the existing engine.
- Manual and read-only email enquiry intake. Gmail monitoring remains opt-in; exported `.eml` files can be imported locally.
- Lead extraction, deduplication, skills/service matching, country and policy checks, ranking, and rejection reasons.
- Scope/effort estimation, pricing guardrails, and proposal preparation with explicit honesty checks.
- Proposal states and human-only submission/award evidence.
- Awarded-project tracking, milestones, execution through the existing isolated task runner, independent QA, handover and client acceptance.
- Invoices, costs, payment reconciliation into the existing payments ledger, and retainer suggestions.
- A business dashboard API and a configurable static portfolio/enquiry-site generator. The generated site is local; it has not been published or deployed.

## Safety and autonomy boundaries

- Fiverr and Upwork are excluded.
- Unknown marketplace permissions fail closed. No logged-in marketplace scraping, browser automation, credential collection, or automated bidding was added.
- Proposal preparation is enabled; proposal sending is disabled.
- Routine client messaging and email delivery are disabled by default.
- The engine does not accept work, sign terms, change scope, enter payment details, or mark money received.
- Project execution can start only after a project is explicitly awarded and the required evidence and client AI-use permission are recorded.
- Human review and independent QA remain required before handover.
- The business and microtask lanes share the existing payments table. Only recorded payments count as received income.

## Live local verification

Checked on the user's Windows computer on 2026-11-16:

- SQLite integrity: `ok`; schema version: `12`.
- The local server was restarted after the initial API health check became unresponsive. It is now listening on loopback `127.0.0.1:8765`.
- `GET /api/health`: `ok=true`, database version 12.
- `GET /api/business/health`: `ok=true`; latest business tick `2026-11-16T11:16:45Z`.
- `GET /api/business`: successful in approximately 839 ms; returns the dashboard's 60-lead view, 5 proposal records and 0 projects.
- Latest business cycle: discovery, inbound, proposal preparation and recurring-finance steps completed without a recorded source error.
- Business lead table: 96 total (22 qualified, 69 rejected, 5 moved to proposal-prepared).
- Proposals: 5 prepared, 0 sent.
- Projects awarded: 0; invoices: 0; payments recorded: 0.
- The existing microtask report contained 1,800 listings; the local database contained 2,374 opportunities.

These are software and database observations, not earnings. No application was submitted, no client contract was won, and no income was received during this verification.

## Verification

- Full test suite: 759 tests passed, 1 skipped.
- Python compilation of the changed business and integration modules: passed.
- `git diff --check`: passed.
- No destructive Git operation was used.
- Local database and user configuration are ignored by Git and were not included in the change set.

## Remaining user actions

1. Review each prepared proposal and remove any claim or requirement match that cannot be personally substantiated.
2. Submit proposals manually through the destination supplied by each client/platform.
3. Record a project award only after agreeing to the scope, price, deadline, payment terms and AI-use rules.
4. Configure contact details and verify every portfolio entry before publishing the generated website.
5. Confirm payment only after the funds actually arrive.

## Status

Milestone 2 implementation and local verification are complete. This report does not claim that the business is already profitable or that the $500/month target has been achieved.
