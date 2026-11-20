# Moving Microtask Autopilot to its always-on cloud host

Updated 20 November 2026.

## What is already live

The public discovery workflow is on GitHub Actions and runs on a best-effort 15-minute schedule while the Windows PC is off. It scans the permitted public sources, validates the registry and publishes its report to the `report-data` branch.

## What needs a paid host

The full task queue, database, scheduler, deliverables, secrets and authenticated account connections need a persistent single-instance service. This repository includes a Render Blueprint:

- `render.yaml` — a paid Starter web service and 1 GB persistent disk.
- `Dockerfile` — packages the Python scheduler service.
- `microtask_monitor/cloud_service.py` — scheduler health and protected cloud administration.
- `microtask_monitor/secret_store.py` — encrypted cloud secret store.
- `microtask_monitor/cloud_export.py` — encrypted migration bundle tool.

The entry-level Render service is expected to cost about US$7/month plus about US$0.25/month for the 1 GB persistent disk; confirm current pricing in the Render dashboard before creating it. No service is created by committing this Blueprint.

## Deploy

1. Connect the Render integration to ChatGPT / open Render directly, then create a Blueprint from this GitHub repository. Review the service name, Starter plan, instance count and persistent disk before confirming.
2. In Render Environment, set `AUTOPILOT_SECRET_ENCRYPTION_KEY` to a fresh Fernet key. Generate one locally with:

   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Do not commit this key or paste it into chat. Render generates `AUTOPILOT_ADMIN_TOKEN`; retrieve it only inside the Render dashboard.
3. Deploy and verify the health endpoint returns `{"ok":true,"worker_alive":true}`.
4. Open `/admin` using HTTP Basic Auth: username `admin`, password is the generated `AUTOPILOT_ADMIN_TOKEN`.

## Move current local state

1. On the Windows PC, from the repository folder, install the requirements if needed:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Create the encrypted export. The command prompts for a new 12+ character passphrase twice; it does not print it:

   ```powershell
   python -m microtask_monitor.cloud_export
   ```

3. The encrypted file is written to `data/cloud-migration.enc`. Keep its passphrase separately and private. Do not email the passphrase together with the file.
4. Upload that file and enter the same passphrase in the protected Render `/admin` page's migration section. The service verifies the archive and database, backs up its current database, imports the operating state and restarts the scheduler.

The archive includes the live SQLite database, relevant work files, permitted snapshots/configuration, and your profile. It deliberately excludes browser profiles/cookies, Windows Credential Manager, cached temporary data, sample runs and local machine credentials. Platform sessions must be reconnected on the cloud host.

## Reconnect account services

- **Whop:** add the callback URL shown on `/admin` to your Whop developer application's allowed redirect URLs, enter the public client ID, then use “Authorize Whop (read-only)”. Keep the scope to `bounty:basic:read`.
- **Gmail:** create/use an app password for the Gmail account and enter it in the protected `/admin` form. The service encrypts it at rest. Never put it into a repository or chat.
- **Other platforms:** reconnect only where the platform allows the required account access. The migration intentionally marks old browser sessions as expired; it does not copy cookies or impersonate a signed-in browser.

## AI task execution

The service can run deterministic workers for eligible imported tasks. The Docker image does not silently copy Claude Code, Codex or local ChatGPT/browser authentication from the PC. AI-assisted work needs a cloud-compatible worker and credentials provisioned through the hosting provider, and the task itself must allow AI assistance. A separately billed API credential may be required; do not add one until its usage budget is understood. The queue's existing AI-use, task approval, policy, QA and submission gates remain in force.

## Safety and accounting

The cloud service does not automatically accept tasks, bypass assessments, claim work hours, submit unverified work, or withdraw funds. Prospecting/outreach and new freelance-lead campaigns remain disabled on the cloud host by default. An opportunity is not an assignment; a completed file is not a delivered task; an approved task is not a payment. Only platform evidence can move an earning to received.

## Recovery

The cloud service exposes `/health` for host restarts and `/admin/status` for authenticated scheduler and queue status. The service also creates a consistent SQLite online backup once per UTC day and retains the last 7 daily database snapshots under `/app/data/backups`. Render separately provides daily persistent-disk snapshots for paid disks. Keep an independent off-host export as an additional recovery option.
