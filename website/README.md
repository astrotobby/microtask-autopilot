# Public dashboard source

This directory contains the static, read-only public dashboard. It is a safe project-status view, not the local control dashboard and not a live job feed. The public registry snapshot is deliberately limited to platform names, categories, official URLs, high-level Kenya/discovery/tracking labels, and review dates.

## Run locally

From the repository root:

```sh
python3 -m http.server 8080 --directory website
```

Open `http://127.0.0.1:8080/`. The page has no server or database dependency.

## Refresh the public snapshot

From the repository root, run:

```sh
python3 website/export_registry.py
```

The exporter reads only the tracked `platforms.json`, `integrations.json`, and `capabilities.json` records. It does not read the local SQLite database, profile answers, applications, tasks, submitted evidence, notifications, or earnings records. Review the resulting `website/data/platforms.json` before committing it.

## Safety and deployment

The site must remain static and read-only. Do not add private local records, logged-in platform data, real application documents, task evidence, payment details, passwords, API keys, or one-time codes. Do not invent live jobs or payments. The single documented public API source is for read-only public listing discovery; account activity, applications, assessments, identity checks, task acceptance/completion and evidence submission remain human-performed.

The `website/` directory is the source copy pushed to the existing GitHub repository. The managed website project mirrors its contents into its static output directory for publication. Keep the copy, route manifest and platform snapshot synchronized for each release. `website/app.config.ts` carries the project's public logo URL; it is separate from the site favicon.
