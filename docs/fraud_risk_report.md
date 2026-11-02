# Fraud Risk Report

**Script:** `fraud_risk_report.py` (engine: `fraud/risk.py`, chargebacks: `fraud/chargebacks.py`) in trybookinguk/RiskManagement. It uses the warehouse, email, SharePoint and S3 code from trybookinguk/s3reporting
**Output:** `<date>_FraudRiskReport.xlsx` in `REPORTS_DIR`, uploaded to SharePoint **Platform Data/Users and Accounts** and emailed to henry@trybooking.co.uk (Kathryn is auto-CC'd). The email carries the top-10 tables and a link to the workbook. The workbook is also attached when it's under 2.5 MB. A full run is bigger than that, and the email app's permissions (`Mail.Send` only) can't send larger attachments.
**Warehouse tables:** `chargebacks`, `chargeback_history`, `chargeback_files`, `risk_exposure`, `risk_fraud`, `risk_fraud_history`, and the optional `booking_signals` and `account_logins`

## What it is for

The report ranks every active **account** and every recent **event** by how likely it is to cause a fraud loss. It also shows how much money is at stake. Three kinds of evidence go into the ranking:

1. **Risk tests.** These replicate the TryBooking admin "Risk analysis" settings, run against each account's and each event's most recent bookings and logins.
2. **Chargebacks.** These are disputes raised by customers through PayPal and Stripe. When we **win** a case, it is usually a customer who didn't recognise the transaction. When we **lose**, either the event wasn't good enough or there was fraud. Events with many chargebacks, especially lost ones, are the fraud risk.
3. **Exposure.** This is the money we hold or owe for the account, taken from the S3 RiskReport (the Risk Exposure Report).

Each account and event gets a **Risk Score from 0 to 100** and a **Risk Band**. The workbook is sorted by score, highest first.

---

## 1. Chargebacks

### Sources

| File | Columns used |
|---|---|
| PayPal "All Dispute Cases" (`AllDisputeCases_*.CSV`) | Buyer Email Address, Final Case Outcome, Invoice ID, Case filing date, plus Case ID, Case reason, Disputed amount, Disputed transaction ID |
| Stripe "Disputes export" (`Disputes_Export_*.csv`) | Description (= Invoice ID), Customer Email, Status, Dispute Created (UTC), plus id, Reason, Dispute Amount, Charge ID, Payment Intent |

The Stripe **Description** is the booking ID without dashes, for example `a03afb3484d94395af46d658614c935c`. It is re-formatted to match the PayPal Invoice ID, for example `a03afb34-84d9-4395-af46-d658614c935c`.

### Loading (new or changed rows)

Pass each export with `--chargeback-file`. The script tells PayPal and Stripe files apart automatically. If the file exists it is loaded; if it is missing, you get a warning and the report uses what's already in the table.

- A dispute not seen before is added as a **new chargeback row**.
- A dispute already in the table is updated when its status, outcome, amount, email, date or invoice changes. Every changed field is written to `chargeback_history` (old value → new value, date, file).
- A dispute that hasn't changed is left alone, so re-loading the same file is safe.
- Each load is logged in `chargeback_files`.

Each dispute is keyed on its own ID: `PP:<Case ID>` for PayPal and `ST:<du_…>` for Stripe.

### Status mapping

| Dispute Status | PayPal (Final Case Outcome / Case status) | Stripe (Status) |
|---|---|---|
| **Won** | Win | won |
| **Lost** | Loss | lost |
| **Refunded** | Refund | charge_refunded |
| **Open** | blank outcome, still being reviewed | needs_response, under_review, warning_needs_response, warning_under_review |
| **Closed** | case closed with no outcome | warning_closed (inquiry closed, no chargeback) |

A dispute counts as a **Fraud Claim** if its reason mentions fraud or "unauthorised". Examples are PayPal "Unauthorized - ATO" and Stripe "fraudulent".

### Matching to the booking

Each chargeback is matched to its original booking in the warehouse. This gives the **Event Organiser Account Number** (`AccountId`), the **Event ID** and the Booking Transaction ID. The script tries these in order:

1. **Invoice ID → booking ID.** The GUID is compared with dashes removed and case ignored, against `BookingUrlId`, then `BookingId`, then `GatewayReference`, then `GatewayId`.
2. **Gateway reference fallback.** The PayPal transaction ID, Stripe charge ID or Stripe payment intent is compared against `GatewayId`.

If several booking rows match, the Successful one wins, then the latest. The **Matched On** column shows which rule matched. Unmatched chargebacks are retried on every run, because new bookings arrive nightly. `--rematch` re-matches all of them.

**Account Tier** is the account's *current* tier from the tier pipeline (`tier_snapshot` in `tier_state.db`). It is refreshed on every run.

> **Booking GUID.** The S3 inventory (Nov 2026) confirmed that `BookingUrlId` is the booking GUID (text). The warehouse used to coerce it to an integer, so it was stored blank. That left the first live run matching only by gateway reference (347 of 471; the PayPal cases stayed unmatched). The warehouse now keeps it as text. Rows loaded before the fix are refilled **once** with s3reporting's `python3 warehouse_backfill.py`, run outside the 02:00–03:30 jobs. Matching scans `bookings` for the GUID (about 15–30 s on the Pi) rather than adding an index, which would add about 190 MB to `warehouse.db` and its backups. Each run prints *Unmatched chargebacks by source*.

---

## 2. Risk tests

Settings: **No. of Logins = 10**, **No. of Bookings = 100**.

- **Per event:** the last 100 bookings of the event, by transaction date, any status.
- **Per account:** the last 100 bookings across the whole account within the last 365 days (`--booking-lookback-days`), plus the last 10 logins.
- **Threshold:** the % of the evaluated sample that is flagged. With a full 100-booking sample this is the same as a count. *Average Risk Score* is the exception: it is the mean Stripe risk score (0–100). A test **Fails** when the value is **≥ the threshold**.
- **Minimum evidence:** a test needs at least 5 evaluable bookings (or 3 logins) before it can pass or fail. Fewer than that shows **Insufficient**, and none at all shows **No data**. Neither scores any points.
- The **booking address country** is the billing or booking country from the signals file if present. Otherwise it is the booking's `BookingCountryCode`, which is mostly GBR but not always (5 distinct values in the Oct 2026 S3 sample). If both are blank, it is taken as UK.

| Test | Threshold | Active | Weight | Flagged when… | Data source |
|---|---|---|---|---|---|
| Account Logins Test | 10 | ✅ | 6 | login IP country is not UK | `account_logins` (optional file) |
| Average Risk Score | 20 | ✅ | 6 | mean Stripe risk score ≥ 20 | `booking_signals.RiskScore` (optional file) |
| Booking Browser Time Zone Test | 10 | ✅ | 4 | browser time zone doesn't fit the booking country (e.g. not Europe/London / GMT / BST for UK) | `booking_signals.BrowserTimeZone` (optional) |
| Booking Credit Card Location Test | 10 | ✅ | 5 | card issue country ≠ booking address country | `booking_signals.CardCountry` (optional) |
| Booking Credit Card Outside UK Test | 20 | ✅ | 5 | card issued outside UK | `booking_signals.CardCountry` (optional) |
| Booking Failed Email Delivery Test | 10 | ✅ | 3 | confirmation email failed | `booking_signals.EmailDeliveryFailed` (optional) |
| Booking Failed Transactions Test | 20 | ✅ | 5 | booking `Status = Failed` | **S3 bookings** ✔ |
| Booking IP Location Test | 20 | ❌ | 5 | IP country ≠ booking address country | **S3 bookings** `IPCountry` ✔ |
| Booking Outside UK Test | 20 | ✅ | 6 | IP country not UK | **S3 bookings** `IPCountry` ✔ |
| Quicksale Test | 50 | ❌ | 3 | quicksale flag, or `PaymentType` contains "quick" | S3 `PaymentType` / signals |

Inactive tests are still calculated and shown in the workbook, but they add **0 points**. To change a threshold, active flag or weight, edit `RISK_TESTS` in `fraud/risk.py`.

UK means GB, GBR, UK, UKG (TryBooking's own UK code, seen in the live data), United Kingdom, Great Britain, England, Scotland, Wales or Northern Ireland. Other countries are recognised as ISO 2- or 3-letter codes or by name (`fraud/countries.py`, the full ISO 3166 list). A blank or "unknown" country is left out of the sample rather than counted as foreign. Any value that can't be recognised is counted as foreign. Such values are listed on the Summary sheet and in the run log, so they can be added to the country table.

### Which S3 file and column each test uses

From the S3 inventory of 2026-11-01. The snapshot is s3reporting's `docs/s3_inventory/2026-11-01_S3Inventory_columns.csv`; re-run `python3 s3_inventory.py` there to refresh it. Every column below is already in the warehouse `bookings` table, loaded nightly by `prepare_data.py`. The only exception is `BookingUrlId`, which needs the one-off backfill described above.

| Test / need | S3 file | Column (inventory: type, % filled) | Warehouse |
|---|---|---|---|
| Booking Failed Transactions | `YYYYMM-BookingData-TBUK.csv`, `YYYYMMDD-BookingDataAll-TBUK.csv` | `Status` (text, 100%, 2 values) | `bookings.Status` |
| Booking Outside UK | BookingData / BookingDataAll | `IPCountry` (text, 100%, 11 values) | `bookings.IPCountry` |
| Booking IP Location | BookingData / BookingDataAll | `IPCountry` vs `BookingCountryCode` (text, 100%, 5 values) | `bookings.IPCountry`, `bookings.BookingCountryCode` |
| Quicksale | BookingData / BookingDataAll | `PaymentType` (text, 58–76%, 4–5 values) | `bookings.PaymentType` |
| Last-N ordering, scope | BookingData / BookingDataAll | `TransactionDate`, `EventDate`, `EventId`, `AccountId` | `bookings` |
| Chargeback → booking | BookingData / BookingDataAll | `BookingUrlId` (text GUID, 100%) and `GatewayId` (text, 58–75%) | `bookings.BookingUrlId` (backfilled), `bookings.GatewayId` |
| Exposure | `YYYYMM-RiskReport-TBUK.csv` | `FullBalance`, `Balance`, `SalesForUpcomingEvents`, `Exposure`, `FutureDays` | `risk_exposure` (written by the report run) |
| Account name, status, age | `YYYYMM-Accounts-TBUK.csv` | `AccountName`, `AccountStatus`, `DateTimeCreated` | `accounts` |

Run s3reporting's `s3_inventory.py` to see the actual values of the category columns (its *ValuesInSample* column). Check `PaymentType` has a quicksale value before switching the Quicksale Test on, and check the `IPCountry` / `BookingCountryCode` formats against the Summary sheet's unrecognised-country line.

### Data the S3 exports don't have

The inventory confirms that only **Failed Transactions**, **Outside UK**, **IP Location** and **Quicksale** can be calculated from S3. No S3 report has a card issuing country (TransactionReconciliation `CardType` is the brand only), a Stripe risk score, a browser time zone or email delivery status. The users file (`LastLoginDatetime`, `LastLoginMethod`) and AccountFeatureList (`LastLoginDate`) hold only the *last* login, with no IP address. The other tests therefore need extra inputs, which are optional and can be loaded on any run:

- `--signals-file <csv>`: one row per booking or payment. A **Stripe payments export** works as-is: `id` (charge), `PaymentIntent ID`, `Card Issue Country`, `Card Address Country`, and `Risk Score` if your export includes it. A TryBooking extract can also be used, with columns `BookingTransactionId`, `BookingUrlId`, `RiskScore`, `CardCountry`, `BookingCountry`, `BrowserTimeZone`, `EmailDeliveryFailed` or `IsQuicksale`. Column names are matched loosely. Rows join to bookings on BookingTransactionId, booking GUID or gateway ID.
- `--logins-file <csv>`: `AccountId, LoginDateTime, IPAddress, IPCountry`. The S3 Users export only has the *last* login date and no IP, so this test needs a login history extract from TryBooking.

The **Tests Evaluated** column (e.g. `2/8`) and the Summary sheet's *Test data coverage* show how much evidence each score is based on. A test with no data on any row of a sheet has its `%` and `Result` columns hidden from that sheet. The Summary sheet lists it under *Tests with no data* along with the file that would supply it: `--signals-file` for risk score, card country, browser time zone and email delivery, and `--logins-file` for account logins.

---

## 3. Exposure (money at stake)

From the S3 **RiskReport** (`YYYYMM-RiskReport-TBUK.csv`), stored as `risk_exposure`:

- **Account Amount At Risk** = the largest of FullBalance, SalesForUpcomingEvents, |Exposure|, and upcoming-event sales from the bookings table.
- **Event Amount At Risk** = the event's successful ticket sales, while its last session is in the future or less than 30 days ago (the main chargeback window). After that it is 0.

If the RiskReport can't be loaded, upcoming sales from bookings are used and the Summary sheet says so. `--risk-report-file` uses a local copy instead of S3.

---

## 4. Risk Score (0–100)

**Risk Score = Test Points (max 40) + Chargeback Points (max 40) + Exposure Points (max 20)**

### Test Points (max 40)
The weights of the active tests add up to 40. A test that **fails** scores:

> weight × min(value ÷ threshold, 2) ÷ 2

A test at exactly its threshold earns half its weight. A test at twice the threshold or more earns its full weight. A pass, Insufficient or No data earns 0. Points are *not* scaled up when data is missing, so a score never rests on one test.

### Chargeback Points (max 40)
| Evidence | Points |
|---|---|
| each **Lost** or **Refunded** chargeback | 12 |
| each of those that was a **Fraud Claim** | +4 |
| each **Open** chargeback | 6 |
| each **Won** chargeback | 1 |
| dispute ratio ≥ 1.0% | +10 |
| dispute ratio ≥ 0.5% | +5 |

**Dispute ratio** = chargebacks ÷ successful paid bookings. For an account this covers the last 365 days; for an event it covers the event's lifetime. It is only scored when there are at least 20 paid bookings. Card schemes start monitoring merchants at about 0.9%.

When two rows have the same score, the one with **more chargebacks** is listed first, then the one with more money at risk.

### Exposure Points (max 20)
| Amount At Risk | Points |
|---|---|
| ≥ £50,000 | 20 |
| ≥ £10,000 | 15 |
| ≥ £2,500 | 10 |
| ≥ £500 | 5 |
| > £0 | 2 |

### Risk Bands
| Band | Score |
|---|---|
| **Critical** | ≥ 60 |
| **High** | 40–59.9 |
| **Medium** | 20–39.9 |
| **Low** | < 20 |

Worked example: an event fails *Outside UK* at 40% (2× its threshold of 20, so 6 points) and *Failed Transactions* at 25% (6.25 ÷ 10 × 5 ≈ 3.1 points). It has 2 lost fraud chargebacks (2 × 16 = 32 points) and a dispute ratio under 0.5% and £3,000 at risk (10 points). Risk Score = 9.1 + 32 + 10 = **51.1, High**.

---

## 5. Scope

- **Events:** any event with a session on or after today minus 90 days (`--event-lookback-days`), plus any event with a chargeback.
- **Accounts:** the owners of those events, any account in the RiskReport with a non-zero balance or upcoming sales, and any account with a chargeback.
- Event rows carry their organiser's **Account Logins Test** result, because logins belong to the account.

## 6. The workbook

| Sheet | Contents |
|---|---|
| Summary | Band counts, chargeback load and match stats, RiskReport status, test data coverage, tests with no data and the file each needs |
| Account Risk | One row per account, sorted by Risk Score: tier, score breakdown, failed tests, chargeback counts, balances, each test's value and result |
| Event Risk | One row per event, sorted by Risk Score, with the same detail at event level |
| Chargebacks | Every dispute: Customer Email, Dispute Date, Invoice ID, Dispute Status, Event Organiser Account Number, Event ID, Account Tier, source, reason, amount, match method |
| Risk Tests | The test settings used for this run |

`risk_fraud` in the warehouse holds the latest run, with a `Level` column of Account or Event. `risk_fraud_history` keeps each run's score per account and event so trends can be tracked.

## 7. Running it

```bash
# Runs on the Synology NAS inside the s3reporting container: this repo is at
# /risk, s3reporting at /app, data at /data (see deploy/synology/README.md).
# Prefix each command below with:  sudo docker exec -w /risk s3reporting
# Dispute exports go in /volume1/docker/s3reporting-data/disputes (= /data/disputes).

# One-off, after deploying the BookingUrlId fix (outside 02:00-03:30), from s3reporting
sudo docker exec -w /app s3reporting python3 warehouse_backfill.py

# Load new dispute exports and send the report
python3 fraud_risk_report.py \
  --chargeback-file /data/disputes/AllDisputeCases_20231102000000_20261031235959_O_01.CSV \
  --chargeback-file /data/disputes/Disputes_Export_Query_2014-07-31_to_2026-11-01.csv

# Re-score using the chargebacks already loaded
python3 fraud_risk_report.py

# Only load or update chargebacks
python3 fraud_risk_report.py --load-only --chargeback-file new_disputes.csv

# Add the optional evidence
python3 fraud_risk_report.py --signals-file stripe_payments.csv --logins-file logins.csv

# Safe dry run: [TEST] subject, or no email at all
TEST_MODE=true python3 fraud_risk_report.py
python3 fraud_risk_report.py --no-email --output-dir /tmp

# A different SharePoint folder, or no upload (then a large workbook can't be emailed)
python3 fraud_risk_report.py --sharepoint-folder "Platform Data/Fraud"
python3 fraud_risk_report.py --no-upload
```

Needs `openpyxl` for the Excel output (`pip install --break-system-packages openpyxl`).
