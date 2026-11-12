# Microtask Autopilot Dashboard — Permanent Website Plan

## Product scope

Publish a public, read-only snapshot dashboard for the existing Microtask Autopilot system. The site must make the current project status easy to inspect without implying that discovery equals application, completion, approval, or payment. It must show the 25-platform registry, integration boundaries, opportunity metrics, Kenya eligibility posture, risk posture, top opportunities, and the Earning Proof boundary.

## Architecture

- `server.js`: dependency-free Node HTTP server that serves the static dashboard and two safe read-only endpoints: `/api/health` and `/api/data`.
- `public/index.html`: semantic page structure and accessible navigation.
- `public/styles.css`: responsive operational-cockpit visual system.
- `public/app.js`: renders the generated snapshot, filtering and platform detail interactions.
- `public/data.json`: generated, non-secret snapshot from the project's current registry and SQLite data. It is explicitly timestamped and labeled as a snapshot.
- `public/manus-routes.json`: route declaration for the hosted website.
- `Dockerfile` and `package.json`: self-contained production runtime on port 3000.

The first hosted version is intentionally read-only. Private platform accounts, passwords, OTPs, cookies, identity checks, application submission, task completion, evidence submission, withdrawals, and payment verification remain outside the public site.

## Design

### Candidate styles

- **Operational cockpit — 0.08**: dense but calm control-room panels with strong status color and clear evidence boundaries.
- **Civic data ledger — 0.06**: restrained public-service typography and table-first transparency.
- **Editorial field guide — 0.04**: more narrative cards and generous whitespace around platform notes.

### Chosen direction: Operational cockpit

- **Design Movement:** modern mission-control dashboard, softened for a trustworthy public data product.
- **Core Principles:** evidence before claims; scan quickly, inspect deeply; status colors always have text labels; human boundaries stay visible.
- **Color Philosophy:** navy and ink establish seriousness; warm amber marks review rather than danger; green is reserved for confirmed operational states; coral is reserved for high-risk or restricted states.
- **Layout Paradigm:** a broad command header leads into a vertical stream of signal cards, then an evidence table and searchable platform registry instead of a generic centered marketing grid.
- **Signature Elements:** status rails on metric cards, pill labels with plain-language meanings, and a persistent “what this does not prove” boundary panel.
- **Interaction Philosophy:** filtering and expansion reveal more evidence without changing the source claims; external links always open the official source in a new tab.
- **Animation:** only short opacity/translate transitions for filtering and panel expansion; no looping or distracting motion.
- **Typography System:** system sans stack for legibility, bold compact headings, tabular numerals for metrics, and 13–14px supporting text.
- **Brand Essence:** a Kenya-focused evidence console for turning public opportunities into honest next actions; clear, grounded, protective.
- **Brand Voice:** “See what is actually connected.” and “A discovered listing is not a paid result.”
- **Wordmark & Logo:** compact compass-and-check mark paired with the Microtask Autopilot wordmark.
- **Signature Brand Color:** deep field navy `#14233d`, used as the visual anchor for trustworthy operations.

## Publication

Use the managed Webdev container deployment with `Dockerfile`, health endpoint `/api/health`, and port `3000`. Publish the read-only snapshot first; later updates can regenerate `public/data.json`, commit, push, checkpoint, and republish.
