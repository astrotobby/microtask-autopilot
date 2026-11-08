# Microtask Autopilot — public opportunity monitor

This repository component checks configured **public RSS/Atom feeds** hourly using GitHub Actions.

## Currently configured feeds

- We Work Remotely — Programming
- We Work Remotely — Design
- We Work Remotely — Sales and Marketing
- Himalayas — Remote Jobs

These are supplementary remote-work listings, not direct task feeds from Atlas Capture, Clickworker, Microworkers, RapidWorkers, SproutGigs, AfterQuery, Handshake AI, or Braintrust. The listed microtask platforms do not currently have verified public RSS feeds configured here. Do not assume that an item from a general remote-job board is a microtask or is open to Kenyan applicants; check the original listing.

## Setup and testing

1. Open the repository's **Actions** tab.
2. Select **Hourly public opportunity monitor**.
3. Use **Run workflow** and select the `main` branch to run a manual test.
4. Open the completed run and download the `hourly-opportunity-report` artifact.
5. The scheduled run is set for 17 minutes past each hour (UTC).

## Scope and safety

- This monitor reads public HTTPS RSS/Atom feeds only.
- It does not log in to worker accounts, access private dashboards, accept tasks, complete work, submit proofs, or handle payouts.
- It does not bypass CAPTCHA, MFA, anti-bot controls, platform rules, or identity checks.
- Only add feeds whose use is permitted by their publisher and platform.
- Preserve source attribution and original listing links.
- Never place passwords, cookies, session tokens, API keys, or identity documents in this repository.

GitHub scheduled workflows are periodic jobs, not a continuously running computer. Start times can be delayed and usage is subject to GitHub Actions limits.
