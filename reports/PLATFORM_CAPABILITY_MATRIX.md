# Platform Capability Matrix

Generated on 2026-11-15 from `microtask_monitor/capabilities.json` by the permission engine (`permissions.matrix()`), so it states exactly what the running code enforces.

**25 platforms are registered. 2 can be read automatically (plus one eligibility-policy monitor). 0 are verified for automated task execution or automated submission.**

## How to read it

| Level | Meaning |
|---|---|
| 0 | Disabled for automation (manual use) |
| 1 | Read-only discovery |
| 2 | Authorized account monitoring |
| 3 | Task preparation |
| 4 | Authorized task execution |
| 5 | Authorized automatic submission |

Action results: **PERMITTED** = recorded evidence (source URL, clause, review date no older than 120 days). *prohibited* = the terms that were read prohibit automated access. *unverified* = no clause on record, so the action is blocked. *you* = done by you in your own account.

Status of the evidence: the capability records were reviewed between 2026-11-08 and 2026-11-12 (dates per platform below). **They were not re-read from the platforms' websites in the 2026-11-15 implementation session**; that session derived levels from the existing reviewed records. A record marked *not on record* has never had its terms read and is therefore at level 0.

## Summary

| # | Platform | ID | Level | Discovery mode | Kenya | Payout (general) | Official API | Terms reviewed |
|---|---|---|---|---|---|---|---|---|
| 1 | Atlas Capture | `atlas_capture` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-09 |
| 2 | Clickworker | `clickworker` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-09 |
| 3 | GainWorkers | `gainworkers` | **0** Disabled for automation (manual use) | manual_import | task_specific | unverified | yes | 2026-11-08 |
| 4 | Hive Micro | `hivemicro` | **0** Disabled for automation (manual use) | manual_import | account_check | unverified | no | 2026-11-12 |
| 5 | Microworkers | `microworkers` | **0** Disabled for automation (manual use) | manual_import | task_specific | unverified | no | 2026-11-09 |
| 6 | RapidWorkers | `rapidworkers` | **0** Disabled for automation (manual use) | manual_import | task_specific | unverified | no | 2026-11-09 |
| 7 | SproutGigs | `sproutgigs` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-09 |
| 8 | TimeBucks | `timebucks` | **0** Disabled for automation (manual use) | manual_import | account_check | unverified | no | 2026-11-12 |
| 9 | AfterQuery Experts | `afterquery` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-09 |
| 10 | CrowdGen by Appen | `crowdgen` | **0** Disabled for automation (manual use) | manual_import | project_specific | unverified | no | 2026-11-12 |
| 11 | Handshake AI | `handshake_ai` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-09 |
| 12 | Mindrift | `mindrift` | **1** Read-only discovery | policy_monitor | unsupported | unverified | no | 2026-11-08 |
| 13 | Neevo | `neevo` | **0** Disabled for automation (manual use) | manual_import | project_specific | unverified | no | 2026-11-12 |
| 14 | OneForma | `oneforma` | **0** Disabled for automation (manual use) | manual_import | project_specific | unverified | no | 2026-11-12 |
| 15 | OpenTrain AI | `opentrain_ai` | **3** Task preparation | public_api | job_specific | documented | yes | 2026-11-08 |
| 16 | TELUS Digital AI Community | `telus_digital_ai` | **0** Disabled for automation (manual use) | manual_import | role_specific | unverified | no | 2026-11-12 |
| 17 | BLEND (Tasq.ai) | `blend` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-12 |
| 18 | Braintrust | `braintrust` | **0** Disabled for automation (manual use) | manual_import | job_specific | unverified | no | 2026-11-09 |
| 19 | REWORKIO | `reworkio` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-08 |
| 20 | PaidViewpoint | `paidviewpoint` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-12 |
| 21 | Prolific | `prolific` | **0** Disabled for automation (manual use) | manual_import | unsupported | unverified | no | 2026-11-12 |
| 22 | Swagbucks | `swagbucks` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-12 |
| 23 | User Interviews | `user_interviews` | **0** Disabled for automation (manual use) | manual_import | unsupported | unverified | no | 2026-11-12 |
| 24 | UserTesting | `usertesting` | **0** Disabled for automation (manual use) | manual_import | unverified | unverified | no | 2026-11-12 |
| 25 | Whop Workforce | `whop_workforce` | **3** Task preparation | authorized_api | task_specific | unverified | yes | 2026-11-10 |

## Per-platform detail

### A. Microtask platforms

#### Atlas Capture (`atlas_capture`)

- Official URL: https://www.atlascapture.io/
- Identity: verified from the reviewed registry (`user_confirmed_official_url`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unverified - Country availability is not stated on the public pages. Moot while the iPhone requirement is unmet.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `not_read`; terms reviewed: 2026-11-09
- Sources read: https://www.atlascapture.io/, https://www.atlascapture.io/opportunities

#### Clickworker (`clickworker`)

- Official URL: https://www.clickworker.com/
- Identity: verified from the reviewed registry (`official_homepage_known`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unverified - Not confirmed; check the official signup flow and each task.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `not_read`; terms reviewed: 2026-11-09
- Sources read: https://www.clickworker.com/

#### GainWorkers (`gainworkers`)

- Official URL: https://www.gainworkers.com/
- Identity: verified from the reviewed registry (`official_homepage_and_terms_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = prohibited; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = prohibited; auto submission = you
- Kenya: task_specific - Geo rules appear per task after login; Terms forbid VPN/proxy use to bypass them.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `explicit_prohibition_scraping_and_bots`; terms reviewed: 2026-11-08
- Sources read: https://www.gainworkers.com/terms, https://www.gainworkers.com/robots.txt, https://www.gainworkers.com/api-docs

#### Hive Micro (`hivemicro`)

- Official URL: https://hivemicro.com/
- Identity: verified from the reviewed registry (`official_homepage_and_payment_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: account_check - Official site claims broad country coverage but Kenya must be confirmed in signup and per-job qualification. PayPal country support must also be checked.
- Payout: unverified - Official help lists PayPal (USD 10 minimum) and Bitcoin (USD 20 minimum), with payment batches after review. Confirm live eligibility and account thresholds.
- AI use: unverified
- Automation clause on record: `not_verified_manual_only_no_supported_worker_api`; terms reviewed: 2026-11-12
- Sources read: https://hivemicro.com/, https://help.hivemicro.com/

#### Microworkers (`microworkers`)

- Official URL: https://www.microworkers.com/
- Identity: verified from the reviewed registry (`official_homepage_known`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = prohibited; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = prohibited; auto submission = you
- Kenya: task_specific - FAQ says anyone from any country can be a member; individual tasks and withdrawal methods can be country-limited.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `explicit_prohibition_robots_scrapers_proxies`; terms reviewed: 2026-11-09
- Sources read: https://www.microworkers.com/, https://www.microworkers.com/terms.php, https://www.microworkers.com/faq.php

#### RapidWorkers (`rapidworkers`)

- Official URL: https://rapidworkers.io/
- Identity: verified from the reviewed registry (`user_confirmed_domain`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: task_specific - Employers target countries per campaign; Kenya availability is task-specific and payout route unverified.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `none_found_in_terms_read`; terms reviewed: 2026-11-09
- Sources read: https://rapidworkers.io/, https://www.rapidworkers.io/terms-and-conditions/, https://www.rapidworkers.io/faqs/

#### SproutGigs (`sproutgigs`)

- Official URL: https://sproutgigs.com/
- Identity: verified from the reviewed registry (`official_homepage_known`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = prohibited; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = prohibited; auto submission = you
- Kenya: unverified - Not confirmed; check the official signup flow and each task.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `restricted_per_robots_txt_statement_terms_not_read`; terms reviewed: 2026-11-09
- Sources read: https://sproutgigs.com/, https://sproutgigs.com/robots.txt

#### TimeBucks (`timebucks`)

- Official URL: https://timebucks.com/
- Identity: verified from the reviewed registry (`official_homepage_payment_and_terms_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: account_check - Available offers and payout methods are generated by country and account verification state. Kenya and the actual cashout options remain account-level checks.
- Payout: unverified - Official payout guidance says payout methods are selected/generated for the contributor's country and account verification state, with a USD 3 minimum and country-dependent fees; PayPal may cost 4% (minimum USD 0.25).
- AI use: unverified
- Automation clause on record: `not_verified_manual_only_no_supported_worker_api`; terms reviewed: 2026-11-12
- Sources read: https://timebucks.com/, https://timebucks.com/terms, https://timebucks.com/payouts

### B. AI training and data contributor platforms

#### AfterQuery Experts (`afterquery`)

- Official URL: https://experts.afterquery.com/home
- Identity: verified from the reviewed registry (`user_confirmed_official_url`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unverified - Role-specific; some roles list worldwide. Verify each opening.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `none_found_in_company_terms`; terms reviewed: 2026-11-09
- Sources read: https://experts.afterquery.com/home, https://www.afterquery.com/terms

#### CrowdGen by Appen (`crowdgen`)

- Official URL: https://crowdgen.com/
- Identity: verified from the reviewed registry (`official_homepage_and_payment_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: project_specific - Every project has its own target countries and qualification criteria; an account's access must be verified.
- Payout: unverified - CrowdGen Dots payment methods vary by country; official help lists options including PayPal, local bank transfer, Airtm, SWIFT and Payoneer. Current availability must be checked in the account. Official guidance describes payment by the 14th for the prior month's approved work.
- AI use: unverified
- Automation clause on record: `not_verified_manual_only_no_supported_worker_api`; terms reviewed: 2026-11-12
- Sources read: https://crowdgen.com/, https://crowdgen.com/payments/

#### Handshake AI (`handshake_ai`)

- Official URL: **not confirmed** (the exact service and URL must be verified before anything is opened)
- Identity: NOT verified: The exact service and official URL have not been confirmed (`needs_exact_service_confirmation`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = prohibited; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = prohibited; auto submission = you
- Kenya: unverified - Not confirmed; check the official signup flow and each task.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `explicit_prohibition_scraping_and_bot_accounts`; terms reviewed: 2026-11-09
- Sources read: https://joinhandshake.com/ai/opportunities/, https://ai.joinhandshake.com/terms-of-service, https://joinhandshake.com/legal/tos/

#### Mindrift (`mindrift`)

- Official URL: https://mindrift.ai/
- Identity: verified from the reviewed registry (`official_website_dashboard_and_policy_urls_confirmed`)
- Automation level: **1 - Read-only discovery** - disabled: Kenya is listed as unsupported by this platform
- Discovery: `policy_monitor`
- Actions: public discovery = prohibited; policy monitor = PERMITTED; account monitoring = prohibited; task preparation = you; task execution = prohibited; auto submission = you
- Kenya: unsupported - Kenya is explicitly listed as not currently supported (policy 'Last updated: May 27, 2026').
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `policy_monitor_only`; terms reviewed: 2026-11-08
  - Evidence for policy monitor: https://mindrift.ai/legal/eligibility-and-geographic-restrictions (verified 2026-11-08)
- Sources read: https://mindrift.ai/legal/eligibility-and-geographic-restrictions, https://mindrift.toloka.ai/

#### Neevo (`neevo`)

- Official URL: https://www.neevo.ai/
- Identity: verified from the reviewed registry (`official_homepage_and_payment_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: project_specific - Task availability depends on profile language, country and specific job requirements; Kenya access is unverified until a matching project appears in the account.
- Payout: unverified - Official support requires a verified PayPal account. Payment follows job completion, the deadline and quality validation; rate and processing timing are job-dependent.
- AI use: unverified
- Automation clause on record: `not_verified_manual_only_no_supported_worker_api`; terms reviewed: 2026-11-12
- Sources read: https://www.neevo.ai/, https://help.neevo.ai/

#### OneForma (`oneforma`)

- Official URL: https://www.oneforma.com/
- Identity: verified from the reviewed registry (`official_homepage_and_payment_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: project_specific - Project location, language and assessment rules vary. Confirm Kenya is listed for each project and that the account is accepted.
- Payout: unverified - Official help describes Payoneer, PayPal or Tipalti depending on country/account, a typical USD 10 minimum and project-dependent weekly or bi-weekly cycles. Verify the current Payment Settings panel.
- AI use: unverified
- Automation clause on record: `not_verified_manual_only_no_supported_worker_api`; terms reviewed: 2026-11-12
- Sources read: https://www.oneforma.com/, https://www.oneforma.com/help-center/

#### OpenTrain AI (`opentrain_ai`)

- Official URL: https://app.opentrain.ai/
- Identity: verified from the reviewed registry (`official_url_and_documentation_verified`)
- Automation level: **3 - Task preparation**
- Discovery: `public_api`
- Actions: public discovery = PERMITTED; policy monitor = unverified; account monitoring = unverified; task preparation = PERMITTED; task execution = unverified; auto submission = you
- Kenya: job_specific - Each job lists eligible countries; an empty list is NOT assumed open to Kenya.
- Payout: documented - Kenya appears on OpenTrain's payout-country list (checked 2026-11-08).
- AI use: unverified
- Automation clause on record: `documented_api_read_only_permitted`; terms reviewed: 2026-11-08
  - Evidence for public discovery: https://app.opentrain.ai/api/public/v1/openapi.json (verified 2026-11-08)
  - Evidence for task preparation: https://app.opentrain.ai/api/public/v1/openapi.json (verified 2026-11-08)
- Sources read: https://www.opentrain.ai/docs/, https://app.opentrain.ai/api/public/v1/openapi.json, https://www.opentrain.ai/docs/payments/payout-country-availability/

#### TELUS Digital AI Community (`telus_digital_ai`)

- Official URL: https://app.telusinternational.ai/contributor/jobs
- Identity: verified from the reviewed registry (`official_contributor_portal_user_supplied`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: role_specific - A Kenya-specific Personalized Internet Assessor result has appeared on the official careers board, but current availability must be checked live. Other roles may exclude Kenya.
- Payout: unverified - Role/contract-specific. Confirm payment terms, minimums and Kenya payout route in the contributor account before accepting work.
- AI use: unverified
- Automation clause on record: `not_verified_manual_only_no_supported_worker_api`; terms reviewed: 2026-11-12
- Sources read: https://app.telusinternational.ai/contributor/jobs, https://jobs.telusdigital.com/search/jobs/in/kenya?cfm5=AI+Community&ns_category=ai-community, https://www.telusdigital.com/careers/ai-community

### C. Professional marketplaces

#### BLEND (Tasq.ai) (`blend`)

- Official URL: https://www.getblend.com/
- Identity: verified from the reviewed registry (`official_careers_and_payment_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unverified - Translator/voice projects depend on language pair, location and client requirements. Kenya participation and payout availability must be checked in the project dashboard.
- Payout: unverified - Official help describes Payoneer, PayPal and Payoneer-managed bank transfers subject to country availability; payment terms vary by project. Help pages report a USD 20 (2,000 credit) withdrawal minimum; verify current dashboard rules.
- AI use: unverified
- Automation clause on record: `not_verified_manual_only_no_supported_worker_api`; terms reviewed: 2026-11-12
- Sources read: https://www.getblend.com/, https://help.getblend.com/

#### Braintrust (`braintrust`)

- Official URL: https://app.usebraintrust.com/talent/dashboard/welcome/
- Identity: verified from the reviewed registry (`user_confirmed_official_talent_dashboard`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: job_specific - Kenya is not on Braintrust's restricted-territory list (terms updated Feb 2025); role-specific limits and your payout method still need confirming.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `none_found_in_terms_read`; terms reviewed: 2026-11-09
- Sources read: https://app.usebraintrust.com/talent/dashboard/welcome/, https://www.usebraintrust.com/terms, https://www.usebraintrust.com/talent-terms

#### REWORKIO (`reworkio`)

- Official URL: https://reworkio.com/
- Identity: verified from the reviewed registry (`official_website_faq_and_dashboard_urls_confirmed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = prohibited; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = prohibited; auto submission = you
- Kenya: unverified - Claims 160+ countries; Kenya eligibility and payout route are unverified.
- Payout: unverified - Verify a Kenya-compatible payout route in your own account before relying on income.
- AI use: unverified
- Automation clause on record: `undocumented_private_endpoints_not_to_be_used`; terms reviewed: 2026-11-08
- Sources read: https://reworkio.com/faq/, https://reworkio.com/robots.txt, https://reworkio.com/dashboard/

### D. Research, survey and usability platforms

#### PaidViewpoint (`paidviewpoint`)

- Official URL: https://paidviewpoint.com/
- Identity: verified from the reviewed registry (`official_support_and_terms_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unverified - dynamic_signup_country_gate_unverified_kenya
- Payout: unverified - PayPal required; first cashout USD 15, later USD 10 then USD 5 if eligible and in good standing; stated processing within 72 hours. Kenya signup is unverified.
- AI use: unverified
- Automation clause on record: `No permission for automated account activity or task completion established; keep manual.`; terms reviewed: 2026-11-12
- Sources read: https://paidviewpoint.com/, https://paidviewpoint.com/support, https://paidviewpoint.com/legal/terms-of-service

#### Prolific (`prolific`)

- Official URL: https://www.prolific.com/
- Identity: verified from the reviewed registry (`official_participant_country_and_cashout_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)** - disabled: Kenya is listed as unsupported by this platform
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unsupported - unsupported_country_waitlist_kenya_not_listed
- Payout: unverified - PayPal only; minimum cashout GBP 6 or USD 6; account and country matching rules apply. Kenya is currently unsupported for ordinary participant registration.
- AI use: unverified
- Automation clause on record: `No permission for automated account activity or task completion established; keep manual.`; terms reviewed: 2026-11-12
- Sources read: https://www.prolific.com/, https://participant-help.prolific.com/en/articles/445007-who-can-participate-in-studies-on-prolific, https://participant-help.prolific.com/en/articles/445062-how-do-i-cash-out-on-prolific

#### Swagbucks (`swagbucks`)

- Official URL: https://www.swagbucks.com/
- Identity: verified from the reviewed registry (`official_homepage_and_country_reward_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = unverified; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unverified - Official help says the service is available in some countries in Africa but does not establish Kenya specifically. Verify live registration and account eligibility.
- Payout: unverified - Reward-store choices depend on country/account; PayPal or electronic gift cards may be offered. Electronic rewards may take up to five business days after redemption; points are not cash received until paid.
- AI use: unverified
- Automation clause on record: `not_verified_manual_only_no_supported_worker_api`; terms reviewed: 2026-11-12
- Sources read: https://www.swagbucks.com/, https://help.swagbucks.com/

#### User Interviews (`user_interviews`)

- Official URL: https://www.userinterviews.com/
- Identity: verified from the reviewed registry (`official_participant_country_and_payment_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)** - disabled: Kenya is listed as unsupported by this platform
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unsupported - currently_unsupported_country_list_kenya_not_listed
- Payout: unverified - Participant incentive options vary by study. Platform-distributed rewards use Tremendous; researcher-paid incentives vary. Kenya participation is currently not supported under the published participant-country list.
- AI use: unverified
- Automation clause on record: `No permission for automated account activity or task completion established; keep manual.`; terms reviewed: 2026-11-12
- Sources read: https://www.userinterviews.com/, https://support.userinterviews.com/hc/en-us/articles/51535822654099-Countries-where-you-can-participate-in-studies, https://support.userinterviews.com/hc/en-us/articles/51535828055699-Learn-how-and-when-you-ll-receive-your-incentive-payment

#### UserTesting (`usertesting`)

- Official URL: https://www.usertesting.com/
- Identity: verified from the reviewed registry (`official_participant_support_and_signup_docs_reviewed`)
- Automation level: **0 - Disabled for automation (manual use)**
- Discovery: `manual_import` - no network access; you import task details
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = prohibited; task preparation = you; task execution = unverified; auto submission = you
- Kenya: unverified - signup_gate_dynamic_country_availability_unverified
- Payout: unverified - Verified PayPal; USD; usually 14 days after completed test; no minimum threshold (official support article).
- AI use: unverified
- Automation clause on record: `No permission for automated account activity or task completion established; keep manual.`; terms reviewed: 2026-11-12
- Sources read: https://www.usertesting.com/, https://support.usertesting.com/hc/en-us/articles/4405063917075-Applicant-FAQ, https://support.usertesting.com/hc/en-us/articles/4405059155859-Where-do-I-apply, https://participant-support.usertesting.com/hc/en-us/articles/38814220821523-FAQ-Payments, https://participant-support.usertesting.com/hc/en-us/articles/38410176419603-Can-I-join-the-UserTesting-Network-from-my-country

### E. Bounty marketplace

#### Whop Workforce (`whop_workforce`)

- Official URL: https://whop.com/whop-workforce/
- Identity: verified from the reviewed registry (`official_url_and_documentation_verified`)
- Automation level: **3 - Task preparation**
- Discovery: `authorized_api`
- Actions: public discovery = unverified; policy monitor = unverified; account monitoring = PERMITTED; task preparation = PERMITTED; task execution = unverified; auto submission = you
- Kenya: task_specific - Every bounty carries allowed_country_codes. KE must be listed, or the list must be empty (documented as worldwide). Opening a listing proves nothing.
- Payout: unverified - Payout terms name ACH, push-to-card, wire and cheque 'subject to jurisdiction' and list no countries. Confirm the payout methods your own Whop account offers in Kenya, and finish any identity check, before relying on a reward.
- AI use: task_specific
- Automation clause on record: `documented_api_with_user_oauth_permitted__website_automation_prohibited`; terms reviewed: 2026-11-10
  - Evidence for account monitoring: https://docs.whop.com/api-reference/bounties/list-bounties (verified 2026-11-10)
  - Evidence for task preparation: https://docs.whop.com/api-reference/bounties/list-bounties (verified 2026-11-10)
- Sources read: https://whop.com/whop-workforce/, https://docs.whop.com/api-reference/bounties/bounty, https://docs.whop.com/api-reference/bounties/list-bounties, https://whop.com/earnings-terms/, https://whop.com/payout-terms/

## What would raise a level

A platform moves to level 4 or 5 only when its capability record gains an explicit block `execution.automated_execution` / `execution.automated_submission` = `{status: permitted, evidence_url, clause, verified_on}` copied from its current official terms or API documentation (and, for level 5, `evidence_submission: automation_permitted` plus a registered submission adapter and your pre-authorization). Changing a record without a newer review date suspends that platform's automation until you confirm you re-read the terms.
