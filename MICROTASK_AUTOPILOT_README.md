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
5. The artifact contains a JSON report and a spreadsheet-friendly CSV.

Scheduled runs are configured for 17 minutes past each hour (UTC). GitHub may delay scheduled runs, and usage is subject to account/repository limits.

## Scope and safety

- Public feeds only; no worker-account logins or private dashboards.
- No accepting tasks, completing timed work, submitting proofs, or handling payouts.
- No bypassing CAPTCHA, MFA, anti-bot controls, identity checks, or platform rules.
- Only add feeds whose use is permitted by their publisher.
- Never store passwords, cookies, session tokens, API keys, or identity documents in this repository.
