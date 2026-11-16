# End-to-End Test Report

Date: 2026-11-15. Branch `feature/autonomous-earning-engine` (uncommitted working tree). Python 3.13.14 on Windows 10.
Status labels are defined in `AUTONOMY_ARCHITECTURE.md`. Nothing in this report was produced with a real platform account.

## 1. Automated suite (actual output)

Command: `py -3 -m unittest discover -s tests`

```
Ran 627 tests in 119.956s
OK (skipped=1)
```

No failures or errors in the final run.


History in this session (all real runs):

| Run | Tree | Result |
|---|---|---|
| Baseline | your uncommitted work as found (13-platform registry) | 553 tests, 4 failures, 1 skipped |
| After merging 31 GitHub commits | merged, before any new code | 566 tests, 5 failures, 1 error, 1 skipped |
| After the engine was built | first full run | 627 tests, 1 failure, 1 skipped |
| Second full run | after updating that test | 627 tests, 1 error (my own test edit referenced a missing fixture), 1 skipped |
| Final | this report | 627 tests, OK (skipped=1) |

The pre-existing failures were: three tests that still expected 13 platforms or an OpenTrain-only API list after the registry grew to 25 and Whop's API read was added; a dashboard sentence lost in the merge; and `test_deadline_reminders_stop_when_the_application_is_submitted`, which used a fixed date that had passed (it failed on the GitHub version too) and is now anchored to the current date. The one failure after the build asserted the old background-thread name and now asserts the single scheduler.

The one skipped test is pre-existing and opt-in: a real Windows Credential Manager round trip that only runs when `AUTOPILOT_TEST_CREDMAN=1` is set.

## 2. New engine tests: `tests/test_autonomy_engine.py` (61 tests)

Temporary SQLite databases, fake adapters, a fake AI runner and a simulated "permitted" platform (`permissive_caps()`); no network (one test patches `urlopen` to fail if anything tries).

| Required area | Test class | What is proven |
|---|---|---|
| 25-platform registry integrity | RegistryTests | exactly 25 unique expected ids in platforms.json, capabilities.json, integrations.json, discovery_sources.json, the dashboard list, the adapters and the permission matrix; registered ≠ automated |
| Platform permissions | PermissionTests | levels derived from evidence; unknown platform/action and incomplete evidence fail closed; stale review removes automation; changed record suspends until user review; settings cannot enable what a platform does not permit |
| Hard restrictions | PermissionTests | CAPTCHA, VPN, fake reviews/engagement, recordings, withdrawals, referrals are refused even with full simulated permission |
| AI permission | PermissionTests, IntelligenceTests | unknown / prohibited / human-only AI rules block; deterministic work needs no AI rule |
| Account connection states | ConnectionStateTests | all nine states; CONNECTED never implies automation; API-verified state; no secret stored |
| Opportunity discovery | DiscoveryTests | ingest without duplicates; rate limit; circuit breaker and cool-down; blocked by suspension and global stop; manual platforms have no discovery method; no network for unauthorized/manual platforms; stale expiry |
| Country eligibility | IntelligenceTests | positive evidence required; ineligible cannot be confirmed; user confirmation needs a reference |
| Payment verification | IntelligenceTests, LedgerTests | missing amount and upfront cost flagged; platform payout status separate from account-verified route |
| Task classification | IntelligenceTests | categories; human-required kinds; class needs platform permission and task rule, never simplicity |
| Task prioritization | IntelligenceTests | blocked high-pay task ranks below permitted low-pay task; expected value discounted; no invented exchange rate; history moves acceptance probability |
| Worker dispatch | OrchestratorTests | nothing runs without standing authorization; direct task runs to a packaged deliverable; platform task needs real permission and must be taken by the user; AI work needs pre-authorization |
| Execution isolation | OrchestratorTests | workspace folders; runner confined to the job folder; task text placed after fixed rules; atomic claims; one job per assignment |
| QA validation | QaGateTests | tampered file → FAIL; unexpected program → FAIL; stray file → MANUAL_CHECK_REQUIRED; standing approval refuses anything but a clean pass; hashes in the report |
| Submission authorization | SubmissionTests | empty registry; direct work is manual; all eleven checks, each blocking |
| Idempotency | SubmissionTests | same package → same key; confirmed attempt never re-sent; timeout never blindly repeated; missing reference records nothing; crash mid-send → unknown; reconcile by lookup or user |
| Assignment state transitions | StateMachineTests | no skipping; DISCOVERED→PAID impossible; every event has evidence and a timestamp; pause/resume/expire |
| Earnings records | LedgerTests | seven totals never mix; approval is not payment; paid needs pending + reference + confirmation |
| Crash recovery | RecoveryAndStopTests, SchedulerTests | interrupted job never re-run silently; dead leases and claims released |
| Global kill switch | RecoveryAndStopTests | blocks orchestrator, dispatch, permission gate and non-read-only scheduler jobs; running jobs asked to pause; only the user releases it |
| Secret redaction | StateMachineTests, SubmissionTests, EngineApiTests | tokens redacted in the audit trail; credential fields stripped from stored platform answers; API refuses credential fields |
| Dashboard APIs | EngineApiTests | every `/api/engine` view; import, filters, settings validation, stop/resume; 400/403/404 paths |
| Notification deduplication | SchedulerTests | one notification for repeated identical conditions |
| Scheduling | SchedulerTests | intervals, leases, single owner, back-off, auto-stop and re-enable, 08:00 Nairobi (05:00 UTC) target, missed run recovered, cleanup confined to the executions folder |
| Structure | SourceRuleTests | no network/browser-automation/cookie access in engine modules; emergency stop and all views present; loopback binding |

Existing suites (execution, QA, assignments, Whop, connections, policy, intelligence, daily pipeline, sync reliability, first-earning workflow, API, DB migrations, notifications) continue to pass and still cover the manual paths.

## 3. Live verification (real, this session)

| Check | Result |
|---|---|
| Database migration 7 → 9 on your real database | LIVE-VERIFIED. Backup `data/autopilot.sqlite3.bak-v7-20261115T203021Z` created automatically; `/api/health` reports version 9; stored opportunities preserved (2,018 before, 2,253 after the next report sync) |
| Local server | LIVE-VERIFIED. Started with `py -3 microtask_monitor/local_server.py`; log: "Local-only binding; not exposed to your network" |
| Report sync from GitHub | LIVE-VERIFIED. Artifact download needed authentication (HTTP 401), so the existing fallback to the public `report-data` branch was used and succeeded |
| OpenTrain public API read | LIVE-VERIFIED. A full real scan completed during a scheduler pass on a temporary database; on your database the adapter reported `ok` |
| Single scheduler | LIVE-VERIFIED. All eleven jobs reported `ok` except `daily_analysis`, which was not due; heartbeat alive |
| Dashboard views | LIVE-VERIFIED in the browser, no console errors: Control Centre (25 registered, 0 connected, 818 Kenya-eligible listings, 0 tasks), Platform Manager (25 rows), Task Pipeline (empty, import form with 26 choices), Autopilot Queue, Submissions, Earnings Ledger (25 payout rows, all totals 0), Automation Settings (6 human-only categories disabled; 0 platforms selectable for automatic submission) |
| Emergency stop | LIVE-VERIFIED. Stop → banner shown, orchestrator pass answered "The global stop is engaged"; Release → cleared |
| Claude Code present | LIVE-VERIFIED: `claude --version` → 2.1.319. This session itself ran on Claude Fable 5.2 |

## 4. Not verified (stated plainly)

| Item | Why |
|---|---|
| A real end-to-end task on your real database | Not run: it would have written test records into your real data. The identical path is covered on temporary databases |
| A real AI worker run started by the orchestrator | Needs your settings switched on; whether the `claude` CLI is signed in for background runs was not checked |
| Whop live sync | You have not authorized Whop; status is `needs_you`. Covered only by fake-API tests |
| Automated execution or submission on any real platform | BLOCKED by policy on all 25 |
| Mindrift policy monitor after the last fix | The monitor was unblocked in code and tests after the live discovery pass; the live health entry still showed the earlier "blocked" until the next hourly pass |
| Re-reading any platform's current terms | Not done in this session; records dated 2026-11-08 to 2026-11-12 were used |
| `webapp/` and `website/` (hosted dashboard from GitHub) | Not exercised or reviewed |
| GitHub Actions workflow after this branch | Not pushed, so not run |
| Real payments | None exist; the ledger shows zero received, which is correct |

## 5. Definition of done — honest status

| # | Criterion | Status |
|---|---|---|
| 1 | All 25 platforms represented correctly | Done, TESTED |
| 2 | Evidence-backed capability profile per platform | Done from existing reviewed records; not re-verified against live terms |
| 3 | Authorized opportunity discovery works | OpenTrain LIVE-VERIFIED; Whop tested, not live |
| 4 | Country eligibility checked correctly | Done, TESTED |
| 5 | Payment conditions assessed | Done, TESTED |
| 6 | Opportunities ranked intelligently | Done, TESTED |
| 7 | Automation feasibility determined | Done, TESTED |
| 8 | Permitted tasks assigned to specialist workers | Done, TESTED (deterministic data worker end to end; AI workers with a fake runner) |
| 9 | Execution in isolated workspaces | Done, TESTED |
| 10 | Outputs independently checked | Done, TESTED |
| 11 | Authorized submission workflows work | TESTED with a simulated permitted platform; no real platform qualifies |
| 12 | Duplicate submissions prevented | Done, TESTED |
| 13 | Assignment status tracked accurately | Done, TESTED |
| 14 | Payments distinguished from advertised compensation | Done, TESTED, LIVE-VERIFIED (view) |
| 15 | Dashboard displays genuine system activity | Done, LIVE-VERIFIED |
| 16 | Scheduled operation and recovery tested | Done, TESTED; scheduler LIVE-VERIFIED |
| 17 | Working kill switch | Done, TESTED, LIVE-VERIFIED |
| 18 | User data and credentials protected | Done, TESTED |
| 19 | Complete test suite executed | Done — results above |
| 20 | No platform restriction circumvented | True |

The system is therefore complete as an engine, and **not yet earning by itself**: the remaining gap is not code but permission and your own steps (authorize Whop, import tasks, take and submit work on the platforms, or supply direct client work).
