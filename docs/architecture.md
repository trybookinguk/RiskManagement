# RiskManagement on the NAS: redesign (proposal)

**Status:** proposal for review. Nothing below is built yet.

**Goal:** a self-contained fraud and risk platform that runs only on the
Synology NAS. It has its own compact warehouse fed by three sources:

- **S3:** bookings and accounts
- **Stripe:** charges, Radar signals and disputes
- **Castle.io:** logins

It has no runtime dependency on s3reporting or the Pi.

## 1. Why change

| Today | Problem |
| --- | --- |
| Runs against s3reporting's ~3.5 GB `warehouse.db` | Ties the fraud tool to whichever host runs the reporting pipeline. Writing fraud tables into that file over a share would risk corruption. |
| Imports s3reporting's warehouse, email, SharePoint and S3 code | Two repos have to stay in step, and fraud changes can break nightly reporting. |
| Stripe disputes come from manual CSV exports | Stale between exports. Nothing about Stripe's own fraud view (Radar, card country) is used. |
| 6 of 10 risk tests show "No data" | S3 has no card country, risk score or login history (see s3reporting `docs/s3_inventory`). |

## 2. Target architecture

```
                 ┌──────────────── Synology NAS ───────────────────────────────┐
 S3 exports ───▶ │ ingest_s3.py      ┐                                         │
 (BookingData,   │                   │                                         │
  Accounts,      │ ingest_stripe.py  ├─▶ risk.db (SQLite, single writer) ─▶ fraud_risk_report.py
  RiskReport)    │                   │      bookings · accounts · exposure     │   ├─ xlsx → SharePoint
 Stripe API ───▶ │ ingest_castle.py  ┘      stripe_* · castle_logins           │   ├─ email (Graph)
 Castle API ───▶ │                          chargebacks · risk_fraud(_history) │   └─ risk_fraud tables
 PayPal CSV ───▶ │ --chargeback-file (PayPal only)                             │
 SharePoint ───▶ │ tier_snapshot.json (read-only, written by s3reporting)      │
                 └─────────────────────────────────────────────────────────────┘
```

- **Own container.** It is named `riskmanagement` and built from this repo's
  `deploy/synology/Dockerfile` (Python 3.12, pandas, openpyxl, boto3, stripe,
  msal, requests).
  - Code: `/volume1/docker/RiskManagement`, mounted at `/app`.
  - Data: `/volume1/docker/riskmanagement-data`, mounted at `/data`. It holds
    `risk.db`, the reports and the dispute drop folder.
  - Secrets: `/volume1/docker/RiskManagement/.env`, which is gitignored.
- **One writer.** Only this container writes `risk.db`, and it sits on the
  NAS's local volume, so there is no SQLite-over-network-share risk.
- **No s3reporting imports.** The small Graph helpers (send mail, upload to
  SharePoint, read the tier snapshot) get vendored into `risk/graph.py`.
  That's about 150 lines.
- **The Pi stays as it is.** s3reporting carries on with its nightly reporting.
  The only thing shared is S3, which both read, and the tier snapshot on
  SharePoint, which s3reporting writes and RiskManagement reads.

## 3. The compact warehouse (`risk.db`)

### 3.1 From S3: `ingest_s3.py` (nightly)

The warehouse keeps only what the tests and matching use, and only for a
rolling window: 400 days of bookings, enough for the 365-day account sample and
the dispute ratio.

| Table | Source | Columns kept |
| --- | --- | --- |
| `bookings` (PK BookingTransactionId) | monthly `BookingData` files for the window (no 2 GB BookingDataAll) | BookingTransactionId, BookingId, BookingUrlId (GUID, text), AccountId, EventId, EventName, TransactionDate, EventDate, PaymentReceived, TicketQuantity, Status, PaymentType, IPCountry, BookingCountryCode, GatewayName, GatewayId, GatewayReference, Wallet |
| `accounts` | `Accounts` | Id, AccountName, AccountStatus, DateTimeCreated, BankAccountIsDeleted, BankAccountStatusId, LastLogIn, Country |
| `risk_exposure` | `RiskReport` | as now |

- **Seeding:** the first run loads the last 14 monthly files. After that, each
  night it re-reads the current month and the previous month, then prunes rows
  outside the window.
- **Size:** about 18 of the ~40 columns over about 13 months. That should come
  to a few hundred MB rather than 3.5 GB; I'll measure it on the first seed.
- **Left out:** IPAddress, customer names and emails, since no test needs them.

### 3.2 From Stripe: `ingest_stripe.py` (nightly, incremental)

It uses a restricted, read-only key with read access to Charges, Disputes,
Radar early fraud warnings and Radar reviews. It pages by `created` from a
stored cursor, and re-reads disputes from the last 120 days because their
status changes.

| Table | Stripe object | Fields kept |
| --- | --- | --- |
| `stripe_charges` | Charge | id, created, amount, currency, status, paid, refunded, description (= booking GUID), payment_intent, `outcome.type / network_status / reason / risk_level / risk_score`, `payment_method_details.card.country / brand / funding / checks.cvc_check / address_postal_code_check`, wallet type, billing country |
| `stripe_disputes` | Dispute | id, charge, created, amount, reason, status, evidence due date |
| `stripe_efw` | Early fraud warning | id, charge, created, fraud_type, actionable |

- **Matching to bookings:** match `description` against BookingUrlId, the same
  GUID match that works today (124 of 124). `GatewayId` is the fallback.
- **Disputes:** these replace the Stripe "Disputes_Export" CSV. PayPal stays as
  a CSV for now; see the open questions.
- **Declines:** Stripe records card declines that never became a booking.
  That's a stronger "failed transactions" signal than S3 `Status`.

### 3.3 From Castle: `ingest_castle.py` (nightly, incremental)

Castle's **Events API** has `POST /v1/events/query`, filtered by time and
`user.id`, plus a schema endpoint. Castle documents it as **Enterprise plan
only**.

| Table | Fields kept |
| --- | --- |
| `castle_logins` | event id, timestamp, event type (login succeeded or failed), status, user id, mapped AccountId, IP country, Castle risk score, policy action (allow / challenge / deny), device id |

`user.id` is whatever TryBooking's app sends to Castle. If that is a UserId,
it maps to AccountId through the S3 `users` export (UserId → AccountId), which
is ingested as a small `users` table with just those two columns.

If the plan isn't Enterprise there are three options, best first:
1. Ask Castle for a scheduled export to S3 or SFTP.
2. A Castle webhook receiver. This needs an inbound HTTPS endpoint on the NAS,
   which I'd avoid.
3. A periodic CSV export from the dashboard, loaded with `--logins-file`.

## 4. What each test uses after the redesign

| Test (TryBooking setting) | Source after redesign | Status |
| --- | --- | --- |
| Account Logins (10% / last 10 logins) | Castle: share of the last 10 logins from outside the UK | **New data** |
| Average Risk Score (20) | Stripe `outcome.risk_score`, averaged over the last 100 Stripe bookings | **New data** (needs Radar for Fraud Teams; otherwise `risk_level` mapped to 0/50/85) |
| Booking Credit Card Location (10%) | Stripe card country ≠ booking country | **New data** (Stripe bookings only) |
| Booking Credit Card Outside UK (20%) | Stripe card country ≠ GB | **New data** (Stripe bookings only) |
| Booking Failed Transactions (20%) | Stripe failed or blocked charges per event, plus S3 `Status` | **Better data** |
| Booking Outside UK (20%) | S3 `IPCountry` | unchanged |
| Booking IP Location (20%, inactive) | S3 `IPCountry` vs `BookingCountryCode` | unchanged |
| Quicksale (50%, inactive) | S3 `PaymentType` | unchanged |
| Booking Browser Time Zone (10%) | Not in S3, Stripe or Castle (Castle sees logins, not checkout) | **Still a gap**: needs TryBooking's checkout data |
| Booking Failed Email Delivery (10%) | Not in any of the three | **Still a gap**: needs the email provider's bounce data |

These are additions to propose, not TryBooking settings:
- Early fraud warnings per account
- Radar blocked or elevated share
- CVC and postcode check failures
- Castle deny or challenge rate

They'd start inactive (0 points) until the thresholds are agreed.

PayPal bookings have no card country or Radar score. Their card tests are
judged on the Stripe share of the sample only, and the minimum-evidence rule
still applies.

## 5. Scoring and output

- **Scoring:** the formula and bands stay as they are (tests 40, chargebacks 40,
  exposure 20). The difference is that 8 of 10 tests will now have data.
- **Output:** the same xlsx sheets, the SharePoint upload and the email.
  `risk_fraud` and `risk_fraud_history` move into `risk.db`.

## 6. Schedule (DSM Task Scheduler, Europe/London)

| Time | Job |
| --- | --- |
| 04:10 | `ingest_s3.py`. S3 files land by about 02:00, and this keeps clear of the Pi's 02:00–03:30 jobs. |
| 04:25 | `ingest_stripe.py` |
| 04:40 | `ingest_castle.py` |
| 05:00 | `fraud_risk_report.py` (daily, or weekdays only) |

## 7. Security

- Make **this repo private**. It will describe fraud controls and thresholds.
- Secrets go only in the NAS `.env`:
  - `AWS_*`: S3 read-only
  - `STRIPE_RESTRICTED_KEY`: read-only, scoped to the four resources above
  - `CASTLE_API_SECRET`
  - `AZURE_*`: Graph Mail.Send and Sites.ReadWrite
- No card numbers are stored, and no IP addresses or customer names. Customer
  email is kept only on disputes, as today.
- `risk.db` is backed up to SharePoint, encrypted, using the s3reporting backup
  approach (vendored).
- Dispute CSVs go into `/data/disputes` and never into a repo.

## 8. Migration plan

1. **Phase 1, standalone.** New container, `ingest_s3.py`, vendored Graph
   helpers, and the current tests running on `risk.db`. Check the scores match
   today's report for the same day.
2. **Phase 2, Stripe.** `ingest_stripe.py`, disputes from the API, the card and
   risk-score tests switched on.
3. **Phase 3, Castle.** `ingest_castle.py` and the Account Logins test.
4. **Phase 4, retire the s3reporting copy.** Merge a trimmed s3reporting#46:
   remove the fraud code, but **drop** the `/risk` mount and the openpyxl
   change, since they're no longer needed. Optionally drop the fraud tables
   from s3reporting's `warehouse.db`.

The current tool in s3reporting keeps working on the Pi until phase 1 is
signed off.

## 9. Open questions

1. **Castle:** is TryBooking on Castle's Enterprise plan, which the Events API
   needs? What does the app send as `user.id`: UserId, AccountId or email?
2. **Stripe:** is Radar for Fraud Teams enabled, which `risk_score` needs? Who
   can create the restricted key?
3. **PayPal:** keep the CSV export, or use the PayPal Disputes API (needs a
   REST app with dispute read scope)?
4. **Gaps:** can TryBooking expose browser time zone at checkout and email
   bounces? If not, should those two tests be marked "not measured" in the
   settings?
5. **Report cadence:** daily, or weekdays only?
