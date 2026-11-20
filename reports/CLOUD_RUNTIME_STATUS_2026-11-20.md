# Cloud runtime status — 20 November 2026

## What is now running independently of the owner's PC

The public GitHub Actions workflow is configured for best-effort discovery every 15 minutes, with a daily full test run at 08:00 Africa/Nairobi. It runs on GitHub-hosted Linux runners and publishes a signed-by-hash report bundle to the public `report-data` branch. The local PC can be switched off without stopping these scheduled public scans.

The cloud report registry now contains 27 platform records and 37 discovery-source records. The report is a directory and status view, not proof that every platform has a live feed or that an account can receive work in Kenya.

## What the cloud scanner actually contacts

- Four configured public RSS/Atom feeds.
- OpenTrain's documented, anonymous public job-listing API (read-only).
- Mindrift's public eligibility-policy page (read-only policy monitor).

The other platform entries are represented with the least-permissive approved status. Manual-review adapters only read task snapshots a user has exported into the local `data/manual` directory; they do not sign in, scrape account pages, accept tasks, complete timed assessments, upload evidence or submit applications. Micro1 and Sovrano remain account/manual-review paths.

GitHub scheduled workflows are best-effort: runs may start late or be dropped during high load. The 15-minute schedule is not a real-time or strict SLA guarantee.

## What is not yet an offline-capable cloud worker

The full engine still runs on the Windows machine:
- SQLite task, application, assignment, queue and earnings state lives under the local `data` directory.
- OAuth/API credentials live in Windows Credential Manager.
- Browser sign-in sessions are kept in the local OS-encrypted browser profile.
- CLI execution workers (Claude Code, Codex or OpenCode) are installed and signed in locally.
- The scheduler and employer-reply monitor currently use that local database and those local secrets.

A GitHub Actions runner is ephemeral and does not have those durable records, account authorizations or worker runtimes. It therefore cannot safely execute the full queue merely by adding a cron trigger.

## Required next step for true 24/7 task execution

Provision a persistent worker host with:
1. a durable database/storage volume and automated, tested backups;
2. encrypted cloud secrets and a fresh, user-authorized Whop OAuth connection (never copy Windows browser cookies or credential stores);
3. a cloud-compatible execution worker with bounded CPU/RAM, workspace isolation, timeout and restart recovery;
4. authenticated control/monitoring endpoints, alerts, audit logs and a kill switch;
5. platform-by-platform, current evidence for any account access, task acceptance, AI assistance, execution or submission action.

No paid host or recurring subscription has been provisioned by this commit. A persistent VM, storage and any AI/API usage may incur charges. Until those are explicitly provisioned and credentials are safely connected, the reliable offline capability is cloud public discovery and report generation, not authenticated account work or full task execution.

## Income and safety accounting

An opportunity is not an accepted task; a draft is not an application; a generated deliverable is not a platform submission; and expected earnings are not money received. Record completion, acceptance and payment only when supported by real platform evidence. Withdrawals remain a user action.
