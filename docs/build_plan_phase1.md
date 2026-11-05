# Phase 1 build plan: risk data warehouse and search API

**Scope:** a data warehouse on the NAS, fed nightly from S3, SharePoint and
Stripe, with sensitive fields hashed, plus a REST API and a web dashboard to
search it. Risk scoring, the models and the report come in phase 2, and they'll read
from this warehouse. See `architecture.md` for the overall design.

## 1. Repo layout

```
RiskManagement/
  rm/                          # the package
    config.py                  # env vars and paths, validated at start-up
    db.py                      # schema, migrations, connections (WAL), upsert helpers
    crypto.py                  # keyed hashing + backup encryption (section 4)
    s3.py                      # S3 client, report-file discovery, chunked/preamble-aware CSV reader
    graph.py                   # Graph token, SharePoint list/download/delete/move/upload, alert email
    stripe_api.py              # paging, retries, rate limiting, day windows
    ingest/
      s3_reports.py
      sharepoint_transactions.py
      stripe_objects.py
  bin/
    ingest_s3.py               # nightly
    ingest_sharepoint.py       # nightly
    ingest_stripe.py           # nightly: previous day
    stripe_backfill.py         # date-range import
    backup.py                  # encrypted risk.db backup to SharePoint
    rm_admin.py                # init-db, status, create/revoke API keys
  api/
    app.py                     # FastAPI: JSON API + dashboard (section 6)
    auth.py                    # Microsoft sign-in (Entra ID) + allow-list
    templates/, static/        # dashboard pages (server-rendered, htmx)
  deploy/synology/             # Dockerfile, docker-compose.yml, README
  tests/
  legacy/                      # today's fraud_risk_report.py + fraud/ (still s3reporting-dependent), replaced in phase 2
```

## 2. Code taken from s3reporting

Only the pieces needed to **extract and store** the data are copied, and each
one is cut down to what's used.

| s3reporting source | Goes to | Kept | Left behind |
| --- | --- | --- | --- |
| `modules/utils/sharepoint.py` | `rm/graph.py` | `authenticate_graph`, retry wrapper, `list_files`, `download_file`, `upload` | Folder defaults. **Adds** `delete_file` and `move_file` |
| `modules/utils/email_utils.py` | `rm/graph.py` | Graph `sendMail` (HTML + small attachments) | HTML table helpers, large-attachment path |
| `modules/utils/data_loader.py` | `rm/s3.py` | S3 client set-up, monthly file-key discovery, chunked CSV reading | Disk cache, combined pickle, pandas dtype maps, every report-specific loader |
| `s3_inventory.py` `parse_head` | `rm/s3.py` | Preamble and delimiter handling (TransactionReconciliation and AccountMovement have a preamble) | Inventory and email |
| `warehouse_backfill.py` | `rm/s3.py` | Streaming only the needed columns with `usecols` | — |
| `modules/utils/backup_crypto.py` | `rm/crypto.py` | scrypt key derivation, plus `encrypt_stream` / `decrypt_stream` for the encrypted backup **file** (no field encryption) | — |
| — | — | Tiers come from the S3 Accounts export (`TierSystem`, `PreviousTier`), not s3reporting's tier snapshot | Tier calculation, `tier_state.db` |

Not copied:
- `warehouse.py`, `prepare_data.py` and DuckDB
- the dashboard, Zoho and GA4 code
- `tier_state.db`

RiskManagement no longer imports s3reporting at runtime.

## 3. Warehouse: `/data/risk.db` (SQLite, WAL)

| Table | Source | Key | Contents |
| --- | --- | --- | --- |
| `bookings` | S3 `BookingDataAll` (seed / full refresh) + `BookingData` (nightly) | BookingTransactionId | **Lifetime**, slim (about 20 columns): booking, event, account, amounts, status, payment type, IP country, booking country, event postcode, gateway ids, wallet. IP address **hashed** (section 4). |
| `account_lifetime`, `event_lifetime` | derived from `bookings` nightly | AccountId / EventId | Lifetime totals for scoring: events, bookings, tickets, gross, refunds, foreign-IP share, first and last dates, sales per event |
| `accounts` | S3 `Accounts` | AccountId | Name, status, **DateTimeCreated** (account created), **FirstEventCreation**, **LastEventCreation**, bank account status, verified, country, postcode (**outward code** only) |
| `account_snapshots` | S3 `Accounts`, daily | (AccountId, snapshot date) | LastEventCreation, status, bank account status. When LastEventCreation changes, a new event was created on that date, which gives each new event its own created date from now on (§3.1). |
| `events` | derived from `bookings` + `account_snapshots` | EventId | AccountId, event name, **first session date** (`MIN(EventDate)`), last session date, first and last booking date, **EventCreated** + `EventCreatedSource` (§3.1) |
| `users` | S3 `users` | UserId | AccountId, role, last login. Username/email **hashed**. |
| `balances` | S3 `RiskReport` | (AccountId, snapshot date) | FullBalance, Balance, SalesForUpcomingEvents, Exposure, FutureDays. Kept **daily**, so exposure history is available. |
| `gateway_recon` | S3 `TransactionReconciliation` | (BookingTransactionId, GatewayId, Transaction) | Card type, **last 4**, amount, gateway message, provider, payout id. **1,100 days.** |
| `booking_transactions` | SharePoint "Booking Transactions" CSV | **Booking ID** | Latest row per booking: transaction date, gateway id (Stripe or PayPal), merchant ref, card type + **last 4**, CVV check, amount, transaction type, organiser, event, gateway message, **gateway message description (declines)**, status, queue fields (waited, wait time, starting position). First name, last name, email and phone **hashed**. **1,100-day retention.** |
| `booking_transaction_history` | same | (Booking ID, row hash) | Every distinct version of a row. A decline followed by a successful payment on the same Booking ID keeps both. **1,100 days.** |
| `stripe_charges` | Stripe | charge id | created, amount, status, outcome (type, network_status, reason, **risk_level**), description (booking GUID), payment_intent, card (country, brand, funding, last4, **fingerprint**, cvc check, postcode check), billing postcode (outward code plain, full **hashed**), billing email **hashed**, refunded. **1,100 days.** |
| `stripe_disputes` | Stripe | dispute id | charge, created, amount, reason, status, evidence due |
| `stripe_efw` | Stripe | warning id | charge, created, fraud_type, actionable |
| `stripe_reviews` | Stripe | review id | charge, reason, opened/closed, closed reason |
| `account_tiers` | S3 `Accounts` (`TierSystem`, `PreviousTier`) | AccountId | tier, snapshot date. s3reporting no longer writes `tier_snapshot.json` to SharePoint (it moved to the Pi's local `tier_state.db`), so the tier comes from the Accounts export instead. That has no dependency on the Pi. |
| `import_log` | all jobs | run id | source, file or day, rows read/inserted/updated, status, error, started/finished |
| `stripe_sync` | Stripe jobs | (object, day) | status, rows, completed at. Used for resume and catch-up. |
| `dashboard_users`, `api_audit` | API | — | Allowed sign-ins and roles, and every request (section 6) |

### 3.1 Event created date

S3 doesn't export a created date per event. It has only the account's
`FirstEventCreation` and `LastEventCreation`. Each event's `EventCreated` is
taken from the best source available, and the source is recorded so the
dashboard and the score can show how reliable it is:

| `EventCreatedSource` | Rule | Reliability |
| --- | --- | --- |
| `export` | An EventCreated column in the export, **if TryBooking adds one** (requested) | exact |
| `snapshot` | The event's first booking falls after a day on which `LastEventCreation` changed. Use that `LastEventCreation`. | exact to the day, for events created after go-live |
| `last_event` | The account's newest event by first booking date gets `LastEventCreation` (and the oldest gets `FirstEventCreation`) | exact for those two events |
| `first_booking` | Otherwise, the event's first booking date. An event must exist before its first sale, so this is the **latest** it can have been created. | upper bound |

The `--full-refresh` seed fills historic events from `last_event` and
`first_booking`. From go-live, daily snapshots give `snapshot`.

- **Indexes:** AccountId, EventId, GatewayId, BookingUrlId, TransactionDate,
  (card brand, last4), card fingerprint, email HMAC, phone HMAC.
- **Schema versioning:** the version is stored in `meta`.
  `rm_admin.py init-db` applies migrations.

## 4. Sensitive data: hashing (one passphrase)

**One secret in `.env`:** `RISK_ENCRYPTION_PASSPHRASE`, the "encrypt word".
scrypt turns it into the hashing key, using a random salt that is generated
once and stored in `meta`. The same passphrase also encrypts the nightly
backup file.

- **Hash only, no decryption.** Customer personal data is stored only as a
  **keyed hash** (HMAC-SHA256 of the normalised value: lower-case email,
  E.164 phone, trimmed upper-case name, IP, full postcode).
- **What hashing allows:** equal values give equal hashes, so the warehouse can
  answer "this email or card appears on 9 bookings across 3 organisers"
  without ever holding the email.
- **Why keyed:** a plain SHA-256 of an email can be reversed by guessing.
- **The trade-off:** nobody can read a customer's email or name back from the
  warehouse. To identify a person you search *with* the value (section 6), or
  look the booking up in TryBooking's admin.

| Data | Stored as |
| --- | --- |
| Customer email, phone, first and last name, IP address, street address, full postcode | **hash only** |
| Postcode outward code (e.g. `SW1A`) | plain, for location conditions |
| Card | brand + **last 4** + Stripe fingerprint only. **Ingest rejects any card field with more than 4 digits**, so a full card number can never be stored by mistake. |
| Organiser and event names, amounts, countries, gateway messages | plain |

Other safeguards:
- **Keep the passphrase in the password manager.** If it's lost or changed,
  new rows hash differently from old ones, so matching across the change
  breaks. The original values aren't kept, so there's no way to re-hash.
  Choose it once.
- **No plaintext copies:** SharePoint CSVs are processed in memory or in a
  temp file that is deleted straight away.
- **Backup:** `risk.db` is backed up nightly to SharePoint, encrypted with the
  passphrase (the stream format from `backup_crypto`).
- **At rest:** also put `riskmanagement-data` in a DSM **encrypted shared
  folder**.

## 5. Ingest jobs

### 5.1 `ingest_s3.py` (nightly 04:10)
- **Nightly:**
  - Upserts `BookingData` for the current and previous month.
  - Reads the newest `Accounts`, `users`, `RiskReport` and
    `TransactionReconciliation`.
  - Saves the daily `account_snapshots` row and rebuilds `events` (with
    EventCreated, §3.1), `account_lifetime` and `event_lifetime`.
  - Prunes `gateway_recon` to 1,100 days.
- **Columns:** only those listed in section 3 (`usecols`), streamed in chunks.
- **`--full-refresh`:** streams the newest non-empty **BookingDataAll** (~2 GB)
  once, keeping only the slim columns, then applies the monthly files from its
  month onward. Use it on the first run, and later whenever lifetime history
  needs rebuilding, e.g. after TryBooking corrects old data.
  - Memory use is one chunk at a time.
  - **Measured (step 2 test, 500,000 synthetic bookings):** 41 s, and
    216 MB of warehouse including indexes, so about 430 bytes per booking.
    BookingDataAll is about 2 GB, roughly 6–8 million bookings, so expect
    **2.5–3.5 GB** and **25–35 minutes** on the NAS for the first load. That
    replaces the earlier "under 1 GB" guess.
  - Event names are kept once per event (`event_names`), not on every
    booking.

### 5.2 `ingest_sharepoint.py` (nightly 04:20): "Platform Data > Booking Transactions"
For each **CSV** in the folder:
1. Download, and compute a SHA-256 of the content. If that checksum is already
   in `import_log` as imported, **delete** the file and move on.
2. Validate the header against the 21 expected columns. Names are matched
   tolerantly; missing required columns mean the file fails.
3. **Reject the file** if any Card Number value has more than 4 digits.
4. Normalise, then hash the personal fields. In **one transaction**:
   - **Upsert** on **Booking ID** into `booking_transactions`.
   - Add any new row version to `booking_transaction_history`.
   - Flag declines from **Gateway Message Description**.
   - Prune rows older than 1,100 days.
5. Write `import_log`, and only **after the commit succeeds**, delete the file
   from SharePoint.
6. If something fails: roll back, **move** the file to
   `Booking Transactions/Failed/` (so it isn't retried every night) with a
   `<name>.error.txt` saying why, and send an alert email. Nothing is deleted
   on failure.

With no files present, it logs "nothing to import" and exits 0.

### 5.3 `ingest_stripe.py` (nightly 04:30): the previous day
- The "previous day" is 00:00–24:00 **Europe/London**, converted to UTC
  timestamps, so the window handles the BST change.
- It lists charges (`created` in the window), disputes, early fraud warnings
  and reviews, 100 per page with auto-paging, and upserts them, so re-runs are
  safe.
- **Dispute status changes:** it also re-fetches every dispute that isn't
  closed (`needs_response`, `under_review`, `warning_*`). That's at most a few
  hundred calls.
- **Catch-up:** if earlier days in the last 7 have no completed `stripe_sync`
  row (the NAS was off, say), it does them first.

### 5.4 `stripe_backfill.py`: a date range
```bash
python3 bin/stripe_backfill.py --from 2025-10-01 --to 2026-11-02 [--objects charges,disputes,efw,reviews] [--redo] [--dry-run]
```
**Bounds:**
- `--to` can be yesterday at the latest; today is incomplete.
- `--from` can go back at most **1,100 days**, the payment retention.
- `--from` can't be after `--to`.
- Older dates are refused, because they'd be pruned straight away.

**How it runs:**
- One day at a time, recording each day in `stripe_sync`, so an interrupted
  run **resumes** and skips completed days unless `--redo` is given.
- At most 20 requests a second, below Stripe's live read limit, retrying 429
  and 5xx responses with backoff.
- `--dry-run` prints the days and the number of requests it would make.

## 6. Search: REST API + web dashboard

One FastAPI app in its **own container**. It opens `risk.db` **read-only**,
so it can never change the warehouse. It serves two things:

- **A web dashboard for people.** Pages are rendered on the server with htmx
  for in-page search. There's no JavaScript build step and nothing extra to
  install on the NAS.
- **A JSON API (`/api/...`)** behind the same pages, for future tools such as
  phase 2 scoring, Excel or Power BI.

**Dashboard pages:**

| Page | Shows |
| --- | --- |
| **Search** (home) | One box that recognises what you typed: account id or name, event id, Booking ID, BookingTransactionId, booking GUID, gateway id (`ch_`, `pi_`, PayPal), card `VISA 1234`, email, or phone. Email and phone are hashed on the server straight away and never logged. |
| **Account** | Tier, age, status, bank account; **timeline**: account created → first event created → last event created → next event date, with the gaps in days; current balances and the **balance history** chart; lifetime totals; events; disputes; declines |
| **Event** | Created date (and its source), first session date, days from account created → event created → event date; lifetime sales, bookings, tickets, refunds, declines, foreign-IP share; bookings and transactions |
| **Booking** | The S3 booking, SharePoint transaction rows (with declines), Stripe charge (card country, risk level, checks), dispute |
| **Card** (brand + last 4, or fingerprint) | Every booking on that card, across events and organisers, with how many distinct purchasers (by hash) used it |
| **Purchaser** (email or phone) | Every booking and card linked to that hash |
| **Disputes** | Filter by status, source and account |
| **Imports** | Last run per source, rows, failures, and files moved to `Failed/` |

**JSON endpoints** (GET, paged with `limit` up to 500 and `offset`):
- `/api/accounts`, `/api/accounts/{id}`
- `/api/events/{id}`, `/api/bookings/{ref}`, `/api/transactions`
- `/api/stripe/charges`, `/api/cards`, `/api/purchasers`
- `/api/disputes`, `/api/imports`, `/api/health`

**Sign-in and security:**
- **Sign-in with Microsoft (Entra ID)**, using the existing Azure tenant.
  Only email addresses in `dashboard_users` (managed with `rm_admin.py
  add-user`) get in. Roles: `viewer`, and `admin` (Imports page, user
  management).
- **App registration:** a separate one for the dashboard is better than
  reusing the Mail/SharePoint one. It needs a redirect URI such as
  `https://risk.<nas-hostname>/auth/callback`.
- **JSON API from scripts:** uses a bearer token from `rm_admin.py
  create-token`, stored hashed.
- **Audit:** every search and page view goes into `api_audit` (user, page, a
  hash of the query, row count, time).
- **No customer personal data is ever shown.** The warehouse only holds
  hashes. Pages show the last 4 card digits and hash-based counts.
- **Network:** NAS LAN only, through the DSM reverse proxy over HTTPS, with
  the DSM firewall limited to the office or VPN subnet. **Never
  port-forwarded to the internet.**

## 7. Deployment on the NAS

- **Image:** one image from `deploy/synology/Dockerfile`, with Python 3.12,
  pandas, boto3, stripe, msal, requests, cryptography, openpyxl, fastapi,
  uvicorn and pytest.
- **Containers (docker-compose):**
  - `riskmanagement`, a worker that runs `sleep infinity`; jobs are started
    with `docker exec`.
  - `riskmanagement-web`, running uvicorn (API + dashboard), with
    `restart: unless-stopped`.
  - Both mount `/volume1/docker/RiskManagement` at `/app` read-only, and
    `/volume1/docker/riskmanagement-data` at `/data`.
- **DSM Task Scheduler (root, daily):**

  | Time | Job |
  | --- | --- |
  | 04:10 | S3 |
  | 04:20 | SharePoint |
  | 04:30 | Stripe |
  | 04:50 | backup |
  | 05:00 | phase 2 scoring, later |

  Any non-zero exit sends an alert email to henry@.
- **NAS `.env`** (chmod 600, never committed):

  ```
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / S3_BUCKET   # read-only
  AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_SENDER_MAILBOX / SHAREPOINT_DRIVE_ID
  STRIPE_RESTRICTED_KEY=rk_live_…
  RISK_ENCRYPTION_PASSPHRASE=…                            # the "encrypt word": hashing + backup
  RISK_DB=/data/risk.db
  ALERT_EMAIL=henry@trybooking.co.uk
  SHAREPOINT_TRANSACTIONS_FOLDER=Platform Data/Booking Transactions
  PAYMENT_RETENTION_DAYS=1100
  WEB_PORT=8085
  WEB_ENTRA_CLIENT_ID / WEB_ENTRA_CLIENT_SECRET / WEB_BASE_URL   # dashboard sign-in
  WEB_SESSION_SECRET=…
  ```

## 8. Testing

- **Unit tests:**
  - hashing: normalisation (case, spaces, phone formats) gives the same
    hash, and a different passphrase gives a different hash
  - backup encrypt/decrypt round trip
  - the card-number guard
  - the Europe/London day windows across a BST change
  - header matching
- **S3:** moto, with fixtures copying the real headers, including the
  TransactionReconciliation preamble.
- **SharePoint:** the Graph calls are stubbed. The tests cover these cases:
  - a file is imported, then deleted
  - the same file again is a duplicate, deleted with no new rows
  - a bad file goes to `Failed/` and is **not** deleted
  - a file containing a full card number is rejected
- **Stripe:**
  - the HTTP calls are stubbed with recorded response shapes
  - paging
  - 429 retry
  - resume after interruption
  - backfill bounds
- **API and dashboard:** FastAPI TestClient, covering:
  - sign-in required
  - allow-list and roles
  - every page renders with fixture data
  - the search box routes each kind of input correctly
  - audit rows written
  - read-only (any attempted write fails)
- **First run on the NAS:**
  - `rm_admin.py init-db`
  - each job with `--dry-run`
  - a real run
  - `curl /health`
  - a one-week `stripe_backfill.py` before the full backfill

## 9. Build order (one PR each, merged before the next)

1. **Skeleton:** config, schema, crypto, `rm_admin.py`, Dockerfile, compose,
   and the tests above. `fraud/` and `fraud_risk_report.py` move to `legacy/`.
2. **S3 ingest.**
3. **SharePoint transactions ingest.**
4. **Stripe daily + backfill.**
5. **Search API + dashboard.**
6. **Backup, alert emails, and the NAS runbook** (`deploy/synology/README.md`).

## 10. Decisions (2026-11-03)

| # | Question | Answer | Where it's reflected |
| --- | --- | --- | --- |
| 1 | SharePoint export | **CSV.** **Booking ID** is the unique key. Gateway ID is unique too but has a different format for PayPal. Declines are shown in **Gateway Message Description**. | §3 `booking_transactions` + history, §5.2 |
| 2 | Failed files | **Move to `Failed/`.** | §5.2 |
| 3 | Sensitive data | **Hash only, one passphrase.** | §4 |
| 4 | API users | **A dashboard web interface.** | §6 |
| 5 | Retention | **1,100 days** for payment transactions. **Lifetime** for accounts and events, via a BookingDataAll full refresh. Stripe nightly plus date range on request. | §3, §5.1, §5.4 |

| 6 | Timeline dates | Capture **Date Account Created**, **Date Last Event Created** and **Date of Event**, and add a fraud-score element based on the gaps between them (one week). | §3 `accounts`, `account_snapshots`, `events`; §3.1; architecture.md N12 |

**Still needed (not blocking build steps 1–4):**
- **TryBooking dev (nice to have):** add an `EventCreated` column to the
  BookingData export, or an Events export. That gives an exact created date for
  every event, including historic ones (§3.1).
- A DNS name for the dashboard on the NAS, e.g. `risk.<nas-hostname>`, and
  whether the office uses a VPN.
- An Entra app registration for the dashboard sign-in. I'll give the exact
  settings in step 5.

## 11. Step 3b (added 2026-11-05)

- **Balances by file date.** `balances.SnapshotDate` is now the S3 file's
  last-written date in UK time, not the run date, so a RiskReport TryBooking
  didn't refresh isn't stored as today's.
- **Month-end history.** `--full-refresh` loads every month's RiskReport in
  the window. Each month's final file gives a month-end exposure history.
- **Daily movements.** `AccountMovementDaily` goes into `balance_movements`:
  opening and closing balance, pending, ticket value, refunds, fund and
  Stripe transfers.
- **Nightly risk email** (`rmctl.sh report`, 05:00). It contains:
  - what changed: balances against the previous and month-ago snapshots,
    the biggest rises, and priority counts against the previous night
  - new top-10 entries, new accounts and events created
  - the top 10 risk accounts and top 10 upcoming risk events
  - import health and the Booking Transactions check

  Scores are kept per day in `risk_daily`.
- **Interim model** (`rm/risk_interim.py`, `CONFIG`):
  - **Exposure:** the account model from architecture §4.2; for events,
    money held plus days to the session.
  - **Fraud:** N12 compressed timeline, overseas IP share and declines, times
    the M1 age multiplier.
  - **Priority:** from the §6.4 matrix.

  Phase 2 replaces it with `config/models.yaml`.
- **Transactions to events.** SharePoint transactions map to events by
  Booking ID, otherwise by a unique (event name, organiser) pair. The import
  check reports the split.

## 12. Booking Transactions export: what the real file showed (2026-11-05)

- **Booking ID is the booking GUID**, e.g. `caded601-55c4-…`, the same value
  as S3 `BookingUrlId`. Schema v5 keys `booking_transactions` and its
  history on `BookingGuid`. GUIDs are stored in one form everywhere (lower
  case with dashes) and transactions link to bookings and events through
  `bookings.BookingUrlId`. A unique (event name, organiser) pair is the
  fallback.
- **Transaction Date is month-first**: `11/03/2026 11:04:26` is 3 Nov 2026
  in `3_Nov_26_BookingTransactionsReport.csv`. The order is decided per
  file:
  - a first number over 12 means day-first
  - a second number over 12 means month-first
  - otherwise the date in the file name decides, then month-first

  The import prints the order it used.
- **Status `Unknown`** rows have no Gateway ID; they look like abandoned
  attempts. They're left unclassified rather than counted as declines.
