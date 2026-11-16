# Owner Lane Design: Unattended Income From Your Own Businesses

Status: DESIGN (2026-11-16). Nothing in this document is built yet unless it says IMPLEMENTED.

## 1. Why a separate lane

The worker lane (25 platforms) cannot run without you: those platforms pay *you* as a worker and their terms forbid bots doing the work. The owner lane automates businesses **you own**, where you set the rules and the only external terms are the official APIs and the law (payments, email, content). That is the route to income that arrives with no human in the loop.

Goal: a paid order is found, sold, fulfilled, delivered and booked without anyone touching it. You keep only: withdrawals, disputes/refunds, and one-time approvals (prices, email templates, legal details).

## 2. What already exists (checked 2026-11-16)

| Asset | State |
|---|---|
| Whop business **AI Video Pro** (`biz_DR9Z8WvJCxWicE`) | Kenya, ID verified, card payments approved. Lifetime volume USD 1. No return/refund policy set. |
| Product **Podcast Shorts That Actually Get Watched** (`prod_Cv8Oy0qTgpZwD`) | Visible. Plans: $29 trial (3 clips), $149 episode pack (10), $397/month (30). 0 members. |
| Product **AI Video Pro Course** (`prod_ej5Fj35WpsMn9`) | Visible, $47 one-time, chat + course experiences. 0 members. |
| Recent payments | $1 test (Nov), several $14 attempts in Sept that were blocked or failed. No real sale yet. |
| `Podcast Shorts Business/` folder | PLAYBOOK.md (fulfilment + outreach rules), lead finder (`leads/find_leads.py`, 12 tier-1 leads), `leads.json` (8 leads: 7 no contact, 1 sample clip ready), email templates (not yet approved), clip tools (`ana/words.py`, `ana/cut.py`), `orders.json` (0 orders). |
| `whop-clipper` | Separate pipeline for Whop clipping campaigns (worker side). Out of scope here; its campaign-submission step stays manual under Whop's rules. |
| Blog worker (Cloudflare, astrotobby.site) | Ads, Shopify product rotation, email capture, publish queue. |
| This repo | Permission gate, scheduler, QA gate, ledger, emergency stop, Whop OAuth (read-only bounty scope). |

**Side effect to fix:** creating the OAuth app on 2026-11-16 made Whop add a free, *visible* product "Microtask Autopilot (personal)" (`prod_JVP7P7fnRV97D`) to the AI Video Pro store. It should be hidden.

The honest starting point: the products exist, delivery tooling exists, but there are **no customers**. The design therefore puts fulfilment first (so any sale delivers itself) and demand generation second.

## 3. The loop

```
 demand ─► offer ─► checkout ─► order intake ─► fulfilment ─► independent QC ─► delivery ─► booking
 (leads,    (sample   (Whop      (poll Whop     (clip/cut     (qa_gate rules  (Whop chat   (ledger:
  content)   clip)     plan)      payments)      worker)       + clip checks)   + file)     sale)
     ▲                                                                                        │
     └────────────── learning: which leads/clips convert, refunds, reorders ◄────────────────┘
```

Everything runs on this PC under the existing single scheduler, global stop and audit trail.

## 4. Pipelines

### A. Podcast Shorts service (primary: highest price, tooling exists)

| Step | Automated how | Official interface | Human today → after |
|---|---|---|---|
| 1. Find buyers | `find_leads.py`: shows with a large Shorts-vs-episode view gap and a **publicly listed** business email | YouTube Data API (public data) | none |
| 2. Personal sample | captions only, then download just the chosen section, cut a 9:16 sample, host as an unlisted link | yt-dlp sections + local clipper | you pick moments → automatic, QC-gated |
| 3. Outreach | personalised first email, at most one follow-up after 4+ days, ≤15 new emails/day | Gmail API (your account) | you approve each email → **you approve the templates once** |
| 4. Checkout | link to the Whop plan | Whop | none |
| 5. Order intake | poll `payments` every 5 min for `paid` on the product (a laptop can't receive webhooks without a public URL) | Whop API, read scope | you notice the sale → automatic |
| 6. Request footage | message the buyer in Whop chat asking for the episode link | Whop Chat API | you message → automatic |
| 7. Fulfil | parse the link, choose moments from captions, download only those sections at 1080p, render with `ana/cut.py` | local tools | you cut → automatic |
| 8. QC | playbook rules as executed checks: starts on a hook, ends on a finished sentence, captions match audio (re-transcribe and compare), 20–60 s, first/last frame inspected, file plays | `qa_gate` style verdict | you watch → automatic; any FAIL is re-cut, never shipped |
| 9. Deliver | upload each clip as a Whop file, post links + caption + hashtags in the chat, log the order | Whop Files + Chat API | you deliver → automatic |
| 10. Book | the Whop payment record is the evidence: revenue booked as *sale received on Whop* | Whop API | none |

Escalations (sent to Telegram/dashboard, never acted on automatically): refund or dispute, an angry or confused buyer message, footage that is not clearly English, a clip that fails QC twice, any request outside the plan.

### B. AI Video Pro Course ($47, already sells itself once traffic arrives)

Fulfilment is instant (Whop grants course access). The automatable work is **traffic**:
- blog posts from the existing publish queue, each linking to the course and the clip service;
- Shorts on your own channel, cut from your own material, linking to the store.

Constraint found: YouTube forces uploads from **unaudited API projects** to private. Either pass YouTube's API audit, or post through an already-verified service (the existing Composio connection that whop-clipper uses; confirm its free-tier limits before relying on it).

### C. Blog monetisation (already running)

Keep it as a traffic and email-capture front end for A and B. No new automation is proposed until A is producing sales.

## 5. Rules that stay fixed

- **Honest offers only.** No invented results, testimonials, reviews or view counts; samples use only the prospect's own public footage and are sent privately.
- **Email law.** CAN-SPAM applies to business email too: a valid postal address in every message, a working opt-out, opt-outs honoured within 10 business days, truthful subject lines; violations are penalised per email. UK/EU prospects may need stricter treatment (consent rules for sole traders), to be checked before including them. Daily caps, one follow-up, stop on any "no".
- **Platform terms.** Whop seller terms, YouTube API terms (audit), Gmail sending limits. Official APIs only; no scraping behind logins, no CAPTCHA solving.
- **Money.** The agent never withdraws, refunds or moves money. It may *read* payments and book them.
- **Data.** Buyer footage and emails stay on this PC and in Whop; tokens in Windows Credential Manager.
- **Stops.** The repo's global stop halts every owner-lane job too; a refund, dispute or two QC failures pause that pipeline automatically.

## 6. Engine changes

| Change | Detail |
|---|---|
| Lane `owner_business` in `permissions.py` | Like `direct_client`: you are the authority for your own business, levels derived from your one-time approvals (templates, prices, caps) instead of a third party's terms. |
| `owner/whop_orders.py` | Poll paid orders, open a fulfilment assignment per order (idempotent on the payment id). |
| `owner/fulfil_clips.py` | Wrap the existing `ana/` tools as a worker; per-clip QC checks registered in `qa.py`. |
| `owner/deliver.py` | Whop Files upload + chat message; idempotency key per order so nothing is delivered twice. |
| `owner/outreach.py` | Lead selection, sample generation, Gmail send with caps, follow-up timer, opt-out list, reply detection that stops the sequence. |
| Whop app scopes | Add read/write scopes only for the owner pipeline (payments read, chat send, files write) on **your business**; the bounty app stays read-only. |
| Ledger | New "sale received on Whop" source: a paid Whop payment from the API counts as received *into your Whop balance*; withdrawn-to-bank stays a separate, user-recorded event. |
| Dashboard | "Owner businesses" view: leads, emails sent/replied/opted-out, orders, deliveries, QC results, sales. |

## 7. Build order and acceptance tests

1. **Fulfilment autopilot (first).** A $1 test order placed by you on a hidden test plan is detected, the buyer is messaged, a test episode link is clipped, QC runs, clips are delivered in chat and the sale is booked, with no clicks. Repeat the same order id: nothing duplicates.
2. **Outreach autopilot.** With approved templates, the day's leads get personalised samples and at most 15 emails; a reply or opt-out stops the sequence; every email carries the postal address and opt-out.
3. **Traffic loop.** Weekly blog posts and Shorts linking to the store, posted through an interface that publishes publicly (audit or verified service).
4. **Learning.** Conversion by lead source, clip style and price feeds lead scoring.

## 8. What only you can provide

1. Approval to hide the stray "Microtask Autopilot (personal)" product.
2. A postal address or registered P.O. box for outreach emails (legal requirement).
3. One-time approval of the outreach email templates (`leads/email_templates.md`).
4. A refund policy for the store (Whop shows none today).
5. A Gmail connection for sending (OAuth, you sign in).
6. Whether to target UK/EU prospects or US/CA/AU only at first.

## 9. Risks

- **Demand is unproven.** There has been no real sale; the first goal is one paid order, not scale.
- **Deliverability.** Cold email from a new sender can land in spam; caps and personalisation reduce, not remove, this.
- **Quality.** A bad clip loses the customer; that is why QC failures are never shipped and two failures pause the order for you.
- **Platform changes.** API versions change (Whop already required a pinned version); failures stop the pipeline and alert you instead of guessing.

Sources for the external rules above: YouTube Data API videos.insert reference (unaudited projects restricted to private) <https://developers.google.com/youtube/v3/docs/videos/insert>; FTC CAN-SPAM compliance guide <https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business>; Whop memberships/webhooks and chat guides <https://docs.whop.com/developer/guides/memberships>, <https://docs.whop.com/developer/guides/chat>.
