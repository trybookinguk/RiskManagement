# RiskManagement on the NAS: design (proposal v2)

**Status:** design for review. No code yet. v2 adds the following to v1:

- two separate scores
- configurable risk models
- the new conditions
- per-account history profiles
- account-age weighting

## 1. Goals

1. **Exposure Risk Score (account level).** How much money TryBooking stands to
   lose if an account goes bad. It's based on the account's fund balances and
   its exposure on each upcoming event.
2. **Fraud Risk Score (account, event and user level).** How likely the
   activity is to be fraudulent. It's based on two things:
   - the login risk profile of the account's users, from **Castle.io**
   - the transactions on each event, from **Stripe**, judged against the
     account's own history
3. **Configurable models.** Thresholds, weights, active flags and bands are
   config, not code. New conditions can be added over time without changing the
   engine, and every score records which model version produced it.
4. **Runs only on the Synology NAS,** with its own compact warehouse. It has no
   runtime dependency on s3reporting or the Pi.

The two scores stay separate: high exposure is not evidence of fraud, and
low-value fraud still matters. They're combined only into a **review
priority** (section 6.4), which decides who looks at what first.

## 2. Architecture

```
                 ┌──────────────────────────── Synology NAS (riskmanagement container) ───────────────────────┐
 S3 exports ───▶ │ ingest_s3      ┐                                                                           │
 Stripe API ───▶ │ ingest_stripe  ├─▶ risk.db ─▶ build_profiles ─▶ score (models.yaml) ─▶ report             │
 Castle API ───▶ │ ingest_castle  │   raw facts    account/user/      exposure model      xlsx → SharePoint  │
 PayPal CSV ───▶ │ load_paypal    ┘                card profiles      fraud model         email (Graph)      │
 SharePoint ───▶ │ tier_snapshot.json (read-only)                     review priority     risk_* tables      │
                 └────────────────────────────────────────────────────────────────────────────────────────────┘
```

The pipeline has four layers, each in its own tables:

| Layer | What it holds | Rebuilt |
| --- | --- | --- |
| **Raw** | Facts as ingested: bookings, accounts, balances, Stripe charges, disputes and early fraud warnings, Castle events, PayPal disputes | Incrementally, nightly |
| **Profiles** | History per account, user and card, built from past events (section 5) | Nightly, from raw |
| **Features** | Per account, event and user values that conditions test, e.g. "foreign card share, last 100 Stripe charges" | Every scoring run |
| **Scores** | Condition results, points, scores, bands and model version, with history kept | Every scoring run |

- **One writer, local disk.** Only this container writes `risk.db`, on the NAS
  volume `/volume1/docker/riskmanagement-data`, mounted at `/data`.
- **No s3reporting imports.** The Graph mail and SharePoint helpers are
  vendored.

## 3. Data sources

### 3.1 S3: bookings, accounts, balances (`ingest_s3`)

The warehouse keeps only the columns the models use, over a rolling 400-day
window. It's loaded from the monthly `BookingData` files, so it never reads the
2 GB BookingDataAll.

| Table | Source | Columns |
| --- | --- | --- |
| `bookings` | BookingData | BookingTransactionId, BookingId, BookingUrlId (GUID), AccountId, EventId, EventName, EventPostcode, EventDate, TransactionDate, PaymentReceived, TicketQuantity, Status, PaymentType, IPCountry, BookingCountryCode, GatewayName, GatewayId, GatewayReference, Wallet |
| `accounts` | Accounts | Id, AccountName, AccountStatus, **DateTimeCreated** (account age), BankAccountIsDeleted, BankAccountStatusId, IsVerified, Country |
| `users` | users | UserId, AccountId, RoleName (maps Castle users to accounts) |
| `balances` | RiskReport (+ AccountMovement) | FullBalance, Balance, SalesForUpcomingEvents, Exposure, FutureDays, Pending, ClosingBalance |

### 3.2 Stripe: transactions (`ingest_stripe`)

It uses a restricted, read-only key and pulls incrementally by `created`.

| Table | Fields used |
| --- | --- |
| `stripe_charges` | `id`, `created`, `amount`, `status`, `outcome` (type, network_status, reason, `risk_level`, `risk_score`), `description` (= booking GUID), and card details (below) |
| `stripe_disputes` | `id`, `charge`, `created`, `amount`, `reason`, `status`. Replaces the Disputes CSV. |
| `stripe_efw` | Early fraud warnings: `charge`, `fraud_type` |

Card details on each charge:
- `payment_method_details.card`: `country`, `brand`, `funding`, `last4`, **`fingerprint`**, and `checks` (cvc, postcode)
- `billing_details.address`: postcode and country
- `billing_details.email`, stored **hashed**

On the card fields:
- **Identifying a card:** `fingerprint` is Stripe's identifier for one card
  number, so it detects reuse reliably. Last 4 digits alone match unrelated
  cards. Conditions key on the fingerprint, and the report shows the last 4 so
  people can read it.
- **No raw PII stored:** no full card numbers, and no raw email or street
  address. Emails and street lines are SHA-256 hashed so the model can count
  "different purchasers" and "address changed". Postcode and country are kept
  in plain text.
- **Matching to bookings:** `description` is matched to `BookingUrlId`, the
  same GUID match that works today (124 of 124). `GatewayId` is the fallback.

**Browser information:** Stripe's Charge and PaymentIntent API objects don't
return browser, device or user-agent data. Radar collects it through Stripe.js
for its own scoring and only shows it in the Dashboard. Browser and device data
can come from **Castle** instead (section 3.3). Open question 1 asks you to
confirm what your Stripe account exposes.

### 3.3 Castle.io: user login profile (`ingest_castle`)

Castle's Events API, `POST /v1/events/query`, is Enterprise-plan only.

| Table | Fields used |
| --- | --- |
| `castle_events` | `id`, `timestamp`, `type` (login, and **checkout** if TryBooking sends it), `status` (succeeded / failed), `user.id` → UserId → AccountId, IP, IP country, device / browser / OS, Castle risk score, signals (new device, new country, …), policy action (allow / challenge / deny) |

If TryBooking's checkout also sends events to Castle, the same feed gives
**browser and location at the moment of purchase**. That would enable the
"different browser vs event location vs card origin" condition for bookings,
not just for logins.

### 3.4 PayPal and tiers

- **PayPal disputes:** CSV via `--chargeback-file`, as now, or the PayPal
  Disputes API later.
- **Account tier:** read from `tier_snapshot.json` on SharePoint, which
  s3reporting writes.

## 4. Configurable models (`config/models.yaml`)

### 4.1 How it works

- **Metrics are code, conditions are config.**
  - Each **metric** is a small named function in `risk/metrics/`. It turns
    features into one number per account, event or user, e.g.
    `card_reuse_max_bookings` or `foreign_card_share`.
  - Each **condition** in `models.yaml` says which metric it uses, at which
    level, the threshold, the points, the minimum evidence and whether it is
    active.
- **Adding a condition:** if the metric already exists, it's a YAML edit with
  no code change. If it needs a new metric, that's one function plus a YAML
  entry.
- **Versioning:** every scoring run records the YAML's `version` and a hash of
  the file next to each score, so old scores can be explained after the rules
  change.
- **Shadow mode:** `active: false` still computes the condition and shows it in
  the workbook, but adds 0 points. New conditions start in shadow mode, so
  their thresholds can be tuned on real data before they count.
- **Validation:** the engine refuses to start if a condition names an unknown
  metric or level, if weights are missing, or if band thresholds aren't
  ascending.

### 4.2 Shape (illustrative)

```yaml
version: 2026-11-02.1

models:
  exposure:                         # Score A, account level, 0-100
    level: account
    combine: sum_capped
    bands: {critical: 70, high: 45, medium: 20}
    conditions:
      - id: full_balance
        metric: full_balance_gbp
        points: {bands: [[50000, 30], [10000, 20], [2500, 10], [500, 5]]}
      - id: upcoming_event_sales
        metric: sales_upcoming_events_gbp
        points: {bands: [[50000, 25], [10000, 15], [2500, 8]]}
      - id: largest_event_exposure
        metric: max_event_exposure_gbp     # per-event sales not yet paid out
        points: {bands: [[20000, 20], [5000, 10], [1000, 5]]}
      - id: near_term_exposure
        metric: exposure_next_14d_gbp      # events happening soon = payout soon
        points: {bands: [[10000, 15], [2500, 8]]}
      - id: bank_account_changed
        metric: bank_account_changed_90d
        points: {if_true: 10}

  fraud:                            # Score B, 0-100
    levels: [account, event, user]
    combine: sum_capped
    bands: {critical: 60, high: 40, medium: 20}
    modifiers:
      - id: account_age
        metric: account_age_days
        multiplier: {bands: [[0, 1.5], [30, 1.3], [90, 1.15], [365, 1.0]]}
    conditions:
      # TryBooking's current tests (thresholds/active as provided)
      - {id: account_logins, metric: logins_outside_uk_pct, level: user, sample: last_10_logins,
         threshold: 10, weight: 6, active: true, min_evidence: 3}
      - {id: avg_risk_score, metric: stripe_avg_risk_score, level: event, sample: last_100_charges,
         threshold: 20, weight: 6, active: true, min_evidence: 5}
      # ... (all 10, section 6.2)
      # New conditions
      - {id: card_reuse, metric: card_reuse_max_bookings, level: event, window: 30d,
         threshold: 3, weight: 6, active: false}
      - {id: card_reuse_address_change, metric: card_reuse_distinct_addresses, level: event,
         window: 30d, threshold: 2, weight: 8, active: false}
      # ...
```

Severity scoring stays as now: the test points equal the weight multiplied by
`min(value / threshold, 2) / 2`. A test at its threshold scores half its
weight, and one at twice the threshold scores the full weight. Conditions below
`min_evidence` show "Insufficient" and score 0.

## 5. History profiles

Each night the pipeline builds a profile from **completed** events (event date
in the past) that had **no lost disputes**. The profile is the account's
"normal", and the fraud conditions measure the current event against it.

| Profile | Built from | Fields |
| --- | --- | --- |
| `account_profile` | the account's completed events in the last 365 days | events count, median tickets per event, median order value, foreign IP share, foreign card share, card countries seen, decline rate, refund rate, card reuse rate, typical booking lead time, typical sales velocity, PayPal/Stripe mix |
| `user_profile` | Castle logins in the last 180 days | usual countries, usual devices and browsers, typical login hours, failed-login rate, average Castle risk |
| `card_profile` | Stripe charges by card fingerprint across **all** accounts, last 180 days | bookings, accounts, events, distinct billing postcodes, distinct purchaser emails (hashed), first and last seen |

How a profile feeds the conditions:
- **Deviation:** a condition can compare an event's value with the account's
  profile, for example "foreign card share is 3× the account's normal", rather
  than only with a fixed threshold. This is how **international payment
  history** works: an account that has always sold to overseas buyers isn't
  flagged for it, but a UK-only account that suddenly isn't is.
- **Cold start:** an account with fewer than 3 completed events has no
  reliable profile. Its conditions compare against the **peer baseline** for
  its industry and tier, and the **account-age modifier** raises its score.

## 6. Conditions

### 6.1 Exposure Risk Score (account)

These conditions are listed in the YAML above:
- full balance
- sales for upcoming events
- largest single-event exposure
- exposure on events in the next 14 days
- bank account changed recently

Section 4.1 covers how to add more. The output also shows the **£ amount at
risk** next to the score, and a per-event table.

### 6.2 Fraud Risk Score: current TryBooking tests

| Condition | Level | Source | Threshold / weight / active |
| --- | --- | --- | --- |
| Account Logins (outside UK, last 10) | user → account | Castle | 10% / 6 / on |
| Average Risk Score (last 100) | event, account | Stripe Radar `risk_score` | 20 / 6 / on |
| Card Location (card country ≠ booking country) | event | Stripe | 10% / 5 / on |
| Card Outside UK | event | Stripe | 20% / 5 / on |
| Failed Transactions | event | Stripe declines + S3 Status | 20% / 5 / on |
| Outside UK (IP) | event | S3 IPCountry | 20% / 6 / on |
| IP Location (IP ≠ booking country) | event | S3 | 20% / 5 / off |
| Quicksale | event | S3 PaymentType | 50% / 3 / off |
| Browser Time Zone | event | Castle checkout events, if sent | 10% / 4 / on |
| Failed Email Delivery | event | **no source** (email provider bounces) | 10% / 3 / on |

### 6.3 Fraud Risk Score: new conditions

All of these start in shadow mode (`active: false`). Thresholds and weights
are set after reviewing the shadow results.

| # | Condition | Level | Metric (from) | Notes |
| --- | --- | --- | --- | --- |
| N1 | **Card reuse** | event, account | Most bookings from one card fingerprint in a window (Stripe) | Many bookings on one card can be card testing or ticket resale. |
| N2 | **Card reuse with address change** | event, account | Distinct billing postcodes or address hashes per fingerprint | Weighted **higher than N1**, since the same card with changing addresses is a strong fraud signal. |
| N3 | **Card across accounts** | account | One fingerprint used at several organisers in a short window (card_profile) | Card-testing rings move between organisers. |
| N4 | **International payments vs history** | event, account | Foreign card share and foreign IP share, compared with `account_profile` | Deviation, not absolute. See section 5. |
| N5 | **Location mismatch** | event | Disagreement between browser (Castle), IP country (S3), card country (Stripe) and event location (EventPostcode → UK region) | Points scale with how many of the four disagree. Browser data needs Castle checkout events. |
| N6 | **New device or browser for a user** | user → account | Castle device not in `user_profile`, especially from a new country | Account-takeover signal on the organiser side. |
| N7 | **Profile deviation: volume and value** | event | Tickets, order value and sales velocity vs `account_profile` | Sudden spikes relative to the account's normal. |
| N8 | **Early fraud warnings** | event, account | Stripe EFWs per 100 charges | The card network reports fraud before any dispute. |
| N9 | **Radar blocked or elevated share** | event | `outcome.type = blocked` or `risk_level = elevated / highest` | |
| N10 | **CVC or postcode check failures** | event | `checks.cvc_check` and `address_postal_code_check` = fail | |
| N11 | **Castle deny or challenge rate** | user → account | Castle policy actions | |
| M1 | **Account age** | account | Days since `DateTimeCreated` | A **multiplier** on the fraud score, not points: ×1.5 under 30 days, ×1.3 under 90, ×1.15 under 365. |
| — | Chargebacks (lost, open, fraud, ratio) | event, account | Stripe disputes + PayPal CSV | Carried over from today. Outcome evidence, kept as fraud conditions. |

### 6.4 Review priority

| | Exposure Low | Exposure Medium | Exposure High | Exposure Critical |
| --- | --- | --- | --- | --- |
| **Fraud Critical** | P2 | P1 | P1 | P1 |
| **Fraud High** | P3 | P2 | P1 | P1 |
| **Fraud Medium** | P4 | P3 | P2 | P2 |
| **Fraud Low** | – | P4 | P3 | P3 |

The matrix is part of `models.yaml`, so it's configurable too.

## 7. Output

The xlsx goes to SharePoint and the email, as now.

| Sheet | Contents |
| --- | --- |
| Summary | Counts by priority, data coverage per source, model version |
| Review Queue | Accounts sorted by priority, then fraud score: both scores, £ at risk, the top 3 reasons |
| Account Exposure | Exposure score, balance breakdown, per-event exposure |
| Account Fraud | Fraud score, the age multiplier, every condition's value, result and points |
| Event Fraud | The same, per event, plus deviation from the account profile |
| Users | Castle profile per user: countries, devices, risk |
| Cards | Card fingerprints with reuse or address changes. Shows last 4, brand and country, never the full number. |
| Chargebacks | As now |
| Model | The `models.yaml` that was used, with each condition, threshold, weight, active flag and description |

Scores and condition results are kept in `risk_scores` and
`risk_condition_results`, with the run date and model version, so trends and
the effect of rule changes can be tracked.

## 8. Schedule, security, migration

- **Schedule (DSM Task Scheduler):**
  - 04:10 ingest S3
  - 04:25 ingest Stripe
  - 04:40 ingest Castle
  - 04:50 build profiles
  - 05:00 score and report
- **Security:**
  - Make this repo **private**.
  - Secrets go only in the NAS `.env`: S3 read, Stripe restricted read-only
    key, Castle API secret, and Graph.
  - No card numbers. Emails and street addresses are hashed. Dispute files go
    in `/data/disputes` only.
  - `risk.db` is backed up to SharePoint, encrypted.
- **Migration:**
  1. Engine, YAML and S3 ingest, with the current tests reproducing today's
     report.
  2. Stripe and the exposure model.
  3. Castle.
  4. Profiles and the new conditions in shadow mode.
  5. Tune the thresholds and switch conditions on.
  6. Retire the fraud code in s3reporting. That means a trimmed s3reporting#46
     **without** the `/risk` mount and openpyxl change.

## 9. What's needed (open questions)

See the PR conversation for the current list.
