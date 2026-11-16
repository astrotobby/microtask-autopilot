# Autonomy Architecture

Microtask Autopilot — autonomous engine added on 2026-11-15 (branch `feature/autonomous-earning-engine`, schema version 9).

## Status legend used in every report

| Label | Meaning |
|---|---|
| IMPLEMENTED | Code exists in the repository |
| TESTED | Covered by automated tests with temporary databases, fake adapters and simulated responses |
| LIVE-VERIFIED | Exercised against the real local dashboard/database or a real permitted public interface in this session |
| UNVERIFIED | Not proven in this session; stated as such |
| BLOCKED | Deliberately not done because permission, evidence or a human step is missing |

## The honest headline

The engine is a complete pipeline — discover, check eligibility, classify, rank, execute, quality-check, package, submit, track payment — in which **every automated step is gated by recorded platform permission**. Today the permission evidence on file supports:

- automated **discovery** on 2 of 25 platforms (OpenTrain AI public jobs API; Whop Workforce API once you authorize it);
- automated **execution, QA and packaging** for the *direct-client lane* (your own client's work that you add yourself);
- automated **execution on a registered platform**: none (BLOCKED — no platform's terms have been verified to permit it);
- automated **submission**: none (BLOCKED — same reason; the adapter registry is empty by design).

So the system does every routine thing it may legitimately do without asking, and shows the exact reason for everything it leaves to you. It will not produce platform income by itself until a platform's record gains explicit, dated evidence for execution/submission, or you do the platform steps yourself.

## Components

```
                         control.py  (settings = your standing authorization, global stop, pause, resource limits)
                              │
 discovery.py ──► intelligence.ingest / assignments ──► lifecycle.py ──► task_intel.py ──► orchestrator.py ──► execution.py + workers.py
 (adapters)        (dedupe by platform+external id)     (state machine)   (class, value,     (rank, authorize,     (isolated workspace)
      │                                                                     score)            claim, dispatch)            │
      └──────────────── permissions.py  (levels 0-5, evidence, the single gate) ──────────────────┘                    qa.py + qa_gate.py
                                                                                                                     (independent verdict)
 scheduler.py (one loop, DB leases, backoff, auto-stop)        submission.py (11 checks, idempotency, never blind retry) ──► ledger.py (7 totals)
 engine_api.py (/api/engine/*) ──► dashboard/index.html (Control Centre, Platform Manager, Task Pipeline, Autopilot Queue, Submissions, Ledger, Automation Settings)
```

| Module | Responsibility | Status |
|---|---|---|
| `permissions.py` | Capability levels 0–5 per platform, per-action results with evidence URL/clause/date, staleness (120 days), suspension on a changed record, hard "never automate" list, `decide()` gate | IMPLEMENTED, TESTED |
| `control.py` | Automation settings (strictly validated, stored in SQLite), global stop, pause, free disk/memory limits | IMPLEMENTED, TESTED, LIVE-VERIFIED (stop/resume, settings view) |
| `connections.py` (extended) | Nine connection states; `session` and `automation` reported separately so CONNECTED never implies permission | IMPLEMENTED, TESTED |
| `discovery.py` | Adapter engine: OpenTrain public API, Whop authorized API, policy monitor, manual import; identity check, policy check, rate limit, circuit breaker, health | IMPLEMENTED, TESTED; OpenTrain read LIVE-VERIFIED; Whop UNVERIFIED live (not authorized) |
| `task_intel.py` | Extraction, 25 categories, 9 automation classes, economics, 0–100 score | IMPLEMENTED, TESTED |
| `lifecycle.py` | Engine state machine over the evidence-checked stages; eligibility; payment conditions; holds | IMPLEMENTED, TESTED |
| `orchestrator.py` | Intake, ranking, authorization, atomic claims, dispatch, recovery | IMPLEMENTED, TESTED |
| `qa_gate.py` | PASS / FAIL / MANUAL_CHECK_REQUIRED from executed checks + hash/package integrity; quality report per execution | IMPLEMENTED, TESTED |
| `submission.py` | Separate submission engine: 11 pre-checks, idempotency keys, unknown-outcome handling, reconcile | IMPLEMENTED, TESTED with a simulated adapter; no real adapter (BLOCKED) |
| `ledger.py` | Seven separated totals, per-assignment ledger, payout routes for Kenya | IMPLEMENTED, TESTED, LIVE-VERIFIED (view) |
| `scheduler.py` | Single scheduler, leases, backoff, auto-stop, restart recovery, cleanup | IMPLEMENTED, TESTED, LIVE-VERIFIED (started with the server) |
| `engine_api.py` + dashboard | `/api/engine/*` and seven views + emergency stop | IMPLEMENTED, TESTED, LIVE-VERIFIED |

Existing modules kept and reused: `execution.py`, `workers.py`, `qa.py`, `assignments.py`, `whop.py`, `opentrain.py`, `intelligence.py`, `risk.py`, `daily.py`, `workflow.py`, `policy.py`, `autonomy.py`, `secret_store.py`.

## Capability levels

| Level | Name | What the program may do | Platforms today |
|---|---|---|---|
| 0 | Disabled for automation (manual use) | Nothing automated; you import task details | 22 |
| 1 | Read-only discovery | Permitted public information / documented public API | Mindrift (its published eligibility-policy page only; Kenya is unsupported, so nothing else) |
| 2 | Authorized account monitoring | Documented API with your authorization, read-only | — |
| 3 | Task preparation | Classify and prepare from data read through 1/2 | OpenTrain AI, Whop Workforce |
| 4 | Authorized task execution | Terms explicitly permit automated/AI-assisted completion | none |
| 5 | Authorized automatic submission | Terms explicitly permit automated submission + your pre-authorization | none |

A level is computed, never typed in: `permissions.derive()` reads the capability record and downgrades any "permitted" result that lacks a source URL, a clause or a review date younger than 120 days.

## The decision chain for an automated start

`orchestrator.authorize()` returns every failing reason, not just the first:

1. global stop not engaged; automation not paused;
2. free disk and memory above your limits;
3. task class is executable (`DETERMINISTIC_LOCAL_PROCESSING`, `AI_ASSISTED_WITH_PERMISSION`, `FULLY_AUTOMATABLE_WITH_PERMISSION`);
4. category on your permitted list and not on your excluded list;
5. reward within your maximum task value (a non-USD reward is never auto-compared: it waits for you);
6. expected hourly rate at or above your minimum; daily execution minutes and concurrency within limits;
7. AI-based runs pre-authorized by you (if the task needs AI);
8. `permissions.decide(platform, "task_execution", task)` — platform evidence, suspension, hard restrictions, the task's AI rule;
9. on a registered platform, the task must already have been taken by you (the engine never takes a task).

## State machine

```
DISCOVERED → ELIGIBILITY_CHECKED → QUALIFIED → AUTOMATION_CLASSIFIED → READY_FOR_EXECUTION → EXECUTING → QUALITY_ASSURANCE
 → READY_FOR_SUBMISSION → SUBMITTED → AWAITING_REVIEW → ACCEPTED | REJECTED → PAYMENT_PENDING → PAID
Side states: HUMAN_REQUIRED, POLICY_BLOCKED, EXPIRED, CANCELLED, FAILED, PAUSED, INTERRUPTED
```

The stored stage moves one step at a time and each step is checked against the database (a started job, executed QA rows, a package, an earnings row, a recorded payment). Every transition writes an `assignment_events` row with evidence kind, reference and timestamp. DISCOVERED → PAID is impossible by construction. Status: IMPLEMENTED, TESTED.

## Data model added (migration 9, additive)

`permission_evidence`, `task_intel`, `submission_attempts`, `scheduler_jobs`, `payout_routes`, and nullable columns on `assignments` (`eligibility_checked_at`, `hold`, `hold_reason`, `claimed_by`, `claim_until`, `paused_from`). Migration 8 converges the two different "migration 7"s that existed locally and on GitHub. Your database was upgraded 7 → 9 with an automatic backup (`data/autopilot.sqlite3.bak-v7-…`). LIVE-VERIFIED.

## Known limits

- No exchange rates: rewards in other currencies are not converted or ranked by hourly rate.
- Time and acceptance figures start as planning estimates and are replaced by measured history as outcomes are recorded.
- CPU use is bounded by concurrency and job time limits; CPU percentage is not measured (no third-party package is used).
- Policy re-verification is a staleness/consistency check on the records. It does not fetch platform terms pages (most platforms' terms prohibit or do not clearly permit automated access); re-reading terms is a human step.
