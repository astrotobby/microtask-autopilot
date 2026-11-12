# Delivery outcomes

- [x] Publish a permanent read-only website for the Microtask Autopilot project.
  - The website serves a public dashboard from a production container on port 3000.
  - The website exposes `/api/health` for unauthenticated readiness checks.
  - The website serves `/manus-routes.json` with the complete page route set.

- [x] Show the current 25-platform registry and integration boundaries.
  - The dashboard lists all 25 registered platforms.
  - Each platform shows its category, official URL, Kenya eligibility posture, discovery mode, automation boundary, health, and payout-verification posture where recorded.
  - The dashboard distinguishes automated public discovery from policy monitoring and manual or unverified records.

- [x] Show current opportunity and earning-proof metrics.
  - The dashboard shows snapshot timestamp, total and open opportunities, Kenya-confirmed and worldwide-confirmed counts, review count, risk counts, and received-payment record totals.
  - The dashboard labels opportunity and payment values as a read-only snapshot rather than live private-account verification.

- [x] Show top opportunity evidence without overclaiming.
  - Top records include title, platform, eligibility class, score, payment text, risk label, risk score, source link, and verification timestamp.
  - External links point to the original source or application URL.
  - No public action submits an application, accepts work, completes a task, uploads evidence, or requests payment.

- [x] Preserve the Earning Proof boundary.
  - The website explains that received-payment records are entered by the worker.
  - Expected or pending amounts are not counted as received.
  - The site does not claim independent bank, wallet, or platform verification.

- [ ] Replace snapshot refresh with a scheduled authenticated backend sync.
  - This remains intentionally open because the current hosted version must not receive private credentials or scrape private dashboards.
