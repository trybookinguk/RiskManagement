# Phase 1 build plan: risk data warehouse and search API

**Scope:** a data warehouse on the NAS, fed nightly from S3, SharePoint and
Stripe, with sensitive fields hashed or encrypted, plus a REST API to search
it. Risk scoring, the models and the report come in phase 2, and they'll read
from this warehouse. See `architecture.md` for the overall design.

## 1. Repo layout

```
RiskManagement/
  rm/                          # the package
    config.py                  # env vars and paths, validated at start-up
    db.py                      # schema, migrations, connections (WAL), upsert helpers
    crypto.py                  # keyed hashing + field encryption (section 4)
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
    app.py                     # FastAPI app (section 6)
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
| `modules/utils/backup_crypto.py` | `rm/crypto.py` | Fernet + scrypt, `encrypt_stream` / `decrypt_stream` for the backup | — |
| `modules/tier_snapshot.py` (format only) | `rm/ingest/s3_reports.py` | Reading `tier_snapshot.json` from SharePoint | Tier calculation |

Not copied:
- `warehouse.py`, `prepare_data.py` and DuckDB
- the dashboard, Zoho and GA4 code
- `tier_state.db`

RiskManagement no longer imports s3reporting at runtime.

## 3. Warehouse: `/data/risk.db` (SQLite, WAL)

| Table | Source | Key | Contents |
| --- | --- | --- | --- |
| `bookings` | S3 `BookingData` (monthly files, 400-day window) | BookingTransactionId | Booking, event, account, amounts, status, payment type, IP country, booking country, event postcode, gateway ids, wallet. IP address is stored **HMAC only** (section 4). |
| `accounts` | S3 `Accounts` | AccountId | Name, status, created date (account age), bank account status, verified, country, postcode (**outward code** only) |
| `users` | S3 `users` | UserId | AccountId, role, last login. Username/email stored **HMAC + encrypted**. |
| `balances` | S3 `RiskReport` | (AccountId, snapshot date) | FullBalance, Balance, SalesForUpcomingEvents, Exposure, FutureDays. Kept **daily**, so exposure history is available. |
| `gateway_recon` | S3 `TransactionReconciliation` | (BookingTransactionId, GatewayId, Transaction) | Card type, **last 4**, amount, gateway message, provider, payout id |
| `booking_transactions` | SharePoint "Booking Transactions" | see open question 1 | All 21 export columns. Card = brand + last 4. Email and phone are **HMAC + encrypted**, names are **encrypted**. Plus CVV check, gateway message, status and queue fields. |
| `stripe_charges` | Stripe | charge id | created, amount, status, outcome (type, network_status, reason, **risk_level**), description (booking GUID), payment_intent, card (country, brand, funding, last4, **fingerprint**, cvc check, postcode check), billing postcode (outward plain, full encrypted), billing email (HMAC + encrypted), refunded |
| `stripe_disputes` | Stripe | dispute id | charge, created, amount, reason, status, evidence due |
| `stripe_efw` | Stripe | warning id | charge, created, fraud_type, actionable |
| `stripe_reviews` | Stripe | review id | charge, reason, opened/closed, closed reason |
| `account_tiers` | SharePoint `tier_snapshot.json` | AccountId | tier, snapshot date |
| `import_log` | all jobs | run id | source, file or day, rows read/inserted/updated, status, error, started/finished |
| `stripe_sync` | Stripe jobs | (object, day) | status, rows, completed at. Used for resume and catch-up. |
| `api_keys`, `api_audit` | API | — | Hashed API keys and scopes, and every request (section 6) |

- **Indexes:** AccountId, EventId, GatewayId, BookingUrlId, TransactionDate,
  (card brand, last4), card fingerprint, email HMAC, phone HMAC.
- **Schema versioning:** the version is stored in `meta`.
  `rm_admin.py init-db` applies migrations.

## 4. Sensitive data: hashing and encryption

**One secret in `.env`:** `RISK_ENCRYPTION_PASSPHRASE`, the "encrypt word".
From it, scrypt (with a random salt generated once and stored in `meta`)
derives two separate keys through HKDF:

- **Hash key.** Used for HMAC-SHA256 of normalised values (lower-cased email,
  E.164 phone, IP, address line). Equal values give equal hashes, so the
  warehouse can match and count "same email on many cards" without holding the
  value. A *keyed* hash is used because a plain SHA-256 of an email can be
  reversed by guessing.
- **Encryption key.** Fernet (AES-128-CBC + HMAC) for fields that a person may
  need to read during an investigation: email, phone, name and full postcode.
  They can only be decrypted through the API with the `pii` scope (section 6).

| Data | Stored as |
| --- | --- |
| Customer email, phone | HMAC (search and matching) + encrypted (reading) |
| Customer first and last name, full postcode | encrypted. The postcode outward code is also kept in plain text. |
| IP address, street address | HMAC only |
| Card | brand + **last 4** + Stripe fingerprint only. **Ingest rejects any card field with more than 4 digits**, so a full card number can never be stored by mistake. |
| Organiser and event names, amounts, countries | plain |

More notes on the secret and the data:
- **Keep the passphrase in the password manager.** If it's lost, the
  encrypted fields can't be recovered, and new rows can't be matched against
  old hashes.
- Changing the passphrase is a re-key job, `rm_admin.py rekey`, planned for
  phase 2.
- **No plaintext copies:** SharePoint files are processed in memory or in a
  temp file that is deleted straight away.
- **Backup:** `risk.db` is backed up nightly to SharePoint, encrypted with the
  same passphrase (the stream format from `backup_crypto`).
- **At rest:** also put `riskmanagement-data` in a DSM **encrypted shared
  folder**.

## 5. Ingest jobs

### 5.1 `ingest_s3.py` (nightly 04:10)
- Reads `BookingData` for the current and previous month (upsert), then
  prunes bookings older than 400 days.
- Reads the newest `Accounts`, `users`, `RiskReport` and
  `TransactionReconciliation`.
- Uses only the columns listed in section 3 (`usecols`), streamed in chunks.
- First run: `--seed` loads the 14 monthly BookingData files. It doesn't need
  BookingDataAll.

### 5.2 `ingest_sharepoint.py` (nightly 04:20): "Platform Data > Booking Transactions"
For each file in the folder (CSV, plus XLSX if needed):
1. Download, and compute a SHA-256 of the content. If that checksum is already
   in `import_log` as imported, **delete** the file and move on.
2. Validate the header against the 21 expected columns. Names are matched
   tolerantly; missing required columns mean the file fails.
3. **Reject the file** if any Card Number value has more than 4 digits.
4. Normalise, then hash and encrypt the PII. **Upsert** all rows in **one
   transaction**.
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
- `--from` can go back at most 400 days, the warehouse window, unless
  `--force` is given.
- `--from` can't be after `--to`.

**How it runs:**
- One day at a time, recording each day in `stripe_sync`, so an interrupted
  run **resumes** and skips completed days unless `--redo` is given.
- At most 20 requests a second, below Stripe's live read limit, retrying 429
  and 5xx responses with backoff.
- `--dry-run` prints the days and the number of requests it would make.

## 6. REST API (search)

FastAPI + uvicorn, in its **own container**. It opens `risk.db`
**read-only**, so it can never change the warehouse.

**Endpoints (all GET, JSON, paged with `limit` up to 500 and `offset`):**

| Endpoint | Finds |
| --- | --- |
| `/health` | Liveness, schema version, last successful import per source |
| `/accounts?q=` · `/accounts/{id}` | Accounts by name or id, with tier, age, balances and latest exposure |
| `/accounts/{id}/events` · `/events/{id}` | Events with booking, sales and decline counts |
| `/events/{id}/bookings` · `/bookings/{ref}` | `{ref}` = BookingId, BookingTransactionId or the booking GUID |
| `/transactions?gateway_id=&booking_id=&from=&to=&status=` | SharePoint export rows and gateway recon |
| `/stripe/charges/{id}` · `/stripe/charges?booking=` | Charge with card country, risk level and checks |
| `/cards?brand=&last4=&fingerprint=&from=&to=` | Every booking on a card. Reuse across events and accounts. |
| `/purchasers?email=` or `?phone=` | **Hashed lookup:** the API hashes the query with the same key, so it never stores or logs the value |
| `/disputes?status=&account_id=&from=` | Stripe and PayPal disputes |
| `/imports` | Import history and failures |

**Security:**
- **Auth:** `Authorization: Bearer <key>`. Keys are created with
  `rm_admin.py create-key --name henry --scope search|pii` and stored hashed.
- **Scopes:** `search` returns PII fields **masked**, like `h***@g***.com` and
  `*** *** 1234`. `pii` decrypts them, and every such response is recorded in
  `api_audit`.
- **Audit:** `api_audit` records every call: key name, endpoint, a hash of the
  parameters, the row count and the time.
- **Network:** listens only on the NAS LAN (port 8085). Access is through the
  DSM reverse proxy over HTTPS, with the DSM firewall limited to the office or
  VPN subnet. **Never port-forwarded to the internet.**
- `/docs` serves the OpenAPI UI and needs a key.

## 7. Deployment on the NAS

- **Image:** one image from `deploy/synology/Dockerfile`, with Python 3.12,
  pandas, boto3, stripe, msal, requests, cryptography, openpyxl, fastapi,
  uvicorn and pytest.
- **Containers (docker-compose):**
  - `riskmanagement`, a worker that runs `sleep infinity`; jobs are started
    with `docker exec`.
  - `riskmanagement-api`, running uvicorn, with `restart: unless-stopped`.
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
  RISK_ENCRYPTION_PASSPHRASE=…                            # the "encrypt word"
  RISK_DB=/data/risk.db
  ALERT_EMAIL=henry@trybooking.co.uk
  SHAREPOINT_TRANSACTIONS_FOLDER=Platform Data/Booking Transactions
  BOOKINGS_WINDOW_DAYS=400
  API_PORT=8085
  ```

## 8. Testing

- **Unit tests:**
  - crypto: round-trip, the same value gives the same HMAC, a wrong passphrase
    fails
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
- **API:** FastAPI TestClient, covering:
  - auth and scopes
  - masking versus `pii`
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
5. **REST API.**
6. **Backup, alert emails, and the NAS runbook** (`deploy/synology/README.md`).

## 10. To confirm before coding

1. **SharePoint export:**
   - Is it CSV or XLSX, and is there one file per export?
   - Is there a filename pattern?
   - What uniquely identifies a row? Is **Gateway ID** unique per row, or do a
     payment and its refund share it, making (Gateway ID, Transaction) the key?
   - Does it include **declined** attempts?
2. **Failed files:** move them to `Booking Transactions/Failed/` (recommended)
   rather than leaving them in place?
3. **One passphrase** for both hashing and encryption (two keys derived from
   it): OK?
4. **API users:** who or what will call it? People with curl or a browser,
   Excel or Power BI, or the dashboard? Is LAN plus VPN only right?
5. **Retention:** how long should SharePoint transaction rows and Stripe
   charges be kept? Suggested: the same 400 days as bookings, with disputes
   kept indefinitely.
