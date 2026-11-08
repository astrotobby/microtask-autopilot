# Microtask Autopilot — public opportunity monitor

This repository component checks configured **public RSS/Atom feeds** hourly using GitHub Actions.

## Setup
1. Add verified public RSS/Atom feed URLs to `microtask_monitor/feeds.json`.
2. Open the repository's **Actions** tab and enable workflows if GitHub asks.
3. Run **Hourly public opportunity monitor** manually once to test it.
4. Download the `hourly-opportunity-report` artifact from a completed run.

## Scope and safety
- This monitor reads public HTTPS feeds only.
- It does not log in to worker accounts, access private dashboards, accept tasks, complete work, submit proofs, or handle payouts.
- It does not bypass CAPTCHA, MFA, anti-bot controls, platform rules, or identity checks.
- Only add feeds whose use is permitted by their publisher and platform.
- Never place passwords, cookies, session tokens, API keys, or identity documents in this repository.

GitHub scheduled workflows are periodic jobs, not a continuously running computer. Start times can be delayed and usage is subject to GitHub Actions limits.
