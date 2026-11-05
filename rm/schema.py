"""Warehouse schema, as numbered migrations.

``PRAGMA user_version`` holds the applied version; ``db.migrate`` runs every
migration above it, each in its own transaction. Never edit a released
migration — add a new one.

Conventions
-----------
- Dates/times are ISO-8601 TEXT ('YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS', UTC for
  Stripe, as exported for S3).
- Money is REAL in pounds.
- ``*Hash`` columns are crypto.Hasher hashes (32 hex chars); personal data is
  never stored in clear.
- ``UpdatedAt`` is when the row was last written by an ingest job (UTC).
"""

MIGRATIONS = [
    (1, "initial warehouse", """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE import_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,          -- s3 | sharepoint | stripe | backup | ...
    ref         TEXT,                   -- S3 key, file name, Stripe day, ...
    checksum    TEXT,                   -- sha256 of a file's bytes, when there is one
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,          -- running | ok | failed | skipped
    rows_read   INTEGER DEFAULT 0,
    rows_written INTEGER DEFAULT 0,
    message     TEXT
);
CREATE INDEX ix_import_log_source ON import_log(source, started_at);
CREATE INDEX ix_import_log_checksum ON import_log(checksum);

-- ---------------------------------------------------------------- S3 ------
CREATE TABLE accounts (
    AccountId           INTEGER PRIMARY KEY,
    AccountName         TEXT,
    AccountStatus       TEXT,
    DateTimeCreated     TEXT,           -- Date Account Created
    FirstEventCreation  TEXT,
    LastEventCreation   TEXT,           -- Date Last Event Created
    IsVerified          INTEGER,
    BankAccountIsDeleted INTEGER,
    BankAccountStatusId INTEGER,
    Country             TEXT,
    PostcodeOutward     TEXT,
    Industry            TEXT,
    SubIndustry         TEXT,
    GatewayGroup        TEXT,
    UpdatedAt           TEXT
);

CREATE TABLE account_snapshots (
    AccountId           INTEGER NOT NULL,
    SnapshotDate        TEXT NOT NULL,
    LastEventCreation   TEXT,
    AccountStatus       TEXT,
    BankAccountIsDeleted INTEGER,
    BankAccountStatusId INTEGER,
    PRIMARY KEY (AccountId, SnapshotDate)
);

CREATE TABLE users (
    UserId            INTEGER PRIMARY KEY,
    AccountId         INTEGER,
    RoleName          TEXT,
    UsernameHash      TEXT,
    IsDeleted         INTEGER,
    LastLoginDatetime TEXT,
    UpdatedAt         TEXT
);
CREATE INDEX ix_users_account ON users(AccountId);
CREATE INDEX ix_users_username ON users(UsernameHash);

CREATE TABLE bookings (                 -- lifetime, slim
    BookingTransactionId INTEGER PRIMARY KEY,
    BookingId           INTEGER,
    BookingUrlId        TEXT,           -- booking GUID
    AccountId           INTEGER,
    EventId             INTEGER,
    EventName           TEXT,
    TransactionDate     TEXT,
    EventDate           TEXT,           -- session date (Date of Event)
    PaymentReceived     REAL,
    TicketQuantity      INTEGER,
    BookingFee          REAL,
    Status              TEXT,
    TransactionType     TEXT,
    PaymentType         TEXT,
    IPCountry           TEXT,
    IPHash              TEXT,
    BookingCountryCode  TEXT,
    EventPostcodeOutward TEXT,
    GatewayName         TEXT,
    GatewayId           TEXT,
    GatewayReference    TEXT,
    Wallet              TEXT,
    CustomerId          INTEGER,
    UpdatedAt           TEXT
);
CREATE INDEX ix_bookings_account ON bookings(AccountId, TransactionDate);
CREATE INDEX ix_bookings_event ON bookings(EventId);
CREATE INDEX ix_bookings_bookingid ON bookings(BookingId);
CREATE INDEX ix_bookings_urlid ON bookings(BookingUrlId);
CREATE INDEX ix_bookings_gateway ON bookings(GatewayId);
CREATE INDEX ix_bookings_ip ON bookings(IPHash);

CREATE TABLE events (                   -- derived from bookings + account_snapshots
    EventId             INTEGER PRIMARY KEY,
    AccountId           INTEGER,
    EventName           TEXT,
    FirstSessionDate    TEXT,           -- Date of Event (first session)
    LastSessionDate     TEXT,
    FirstBookingDate    TEXT,
    LastBookingDate     TEXT,
    EventCreated        TEXT,
    EventCreatedSource  TEXT,           -- export | snapshot | last_event | first_event | first_booking
    EventPostcodeOutward TEXT,
    UpdatedAt           TEXT
);
CREATE INDEX ix_events_account ON events(AccountId);

CREATE TABLE account_lifetime (
    AccountId         INTEGER PRIMARY KEY,
    Events            INTEGER,
    Bookings          INTEGER,
    PaidBookings      INTEGER,
    Tickets           INTEGER,
    Gross             REAL,
    RefundedBookings  INTEGER,
    ForeignIpBookings INTEGER,
    FirstBookingDate  TEXT,
    LastBookingDate   TEXT,
    UpdatedAt         TEXT
);

CREATE TABLE event_lifetime (
    EventId           INTEGER PRIMARY KEY,
    AccountId         INTEGER,
    Bookings          INTEGER,
    PaidBookings      INTEGER,
    Tickets           INTEGER,
    Gross             REAL,
    RefundedBookings  INTEGER,
    ForeignIpBookings INTEGER,
    UpdatedAt         TEXT
);

CREATE TABLE balances (                 -- RiskReport, one row per account per day
    AccountId              INTEGER NOT NULL,
    SnapshotDate           TEXT NOT NULL,
    FullBalance            REAL,
    Balance                REAL,
    SalesForUpcomingEvents REAL,
    Exposure               REAL,
    FutureDays             INTEGER,
    LastEventDate          TEXT,
    PRIMARY KEY (AccountId, SnapshotDate)
);

CREATE TABLE gateway_recon (            -- TransactionReconciliation, 1,100 days
    BookingTransactionId INTEGER NOT NULL,
    GatewayId          TEXT NOT NULL,
    TxnType            TEXT NOT NULL,      -- export column "Transaction" (reserved word in SQL)
    TransactionDate    TEXT,
    BookingUrlId       TEXT,
    CardType           TEXT,
    CardLast4          TEXT,
    Amount             REAL,
    GatewayMessage     TEXT,
    PayoutId           TEXT,
    ArrivalDate        TEXT,
    Provider           TEXT,
    UpdatedAt          TEXT,
    PRIMARY KEY (BookingTransactionId, GatewayId, TxnType)
);
CREATE INDEX ix_recon_card ON gateway_recon(CardType, CardLast4);
CREATE INDEX ix_recon_date ON gateway_recon(TransactionDate);

CREATE TABLE account_tiers (
    AccountId    INTEGER PRIMARY KEY,
    Tier         TEXT,
    SnapshotDate TEXT
);

-- ------------------------------------------------ SharePoint export ------
CREATE TABLE booking_transactions (     -- latest row per Booking ID, 1,100 days
    BookingId          INTEGER PRIMARY KEY,
    TransactionDate    TEXT,
    GatewayId          TEXT,
    MerchantRefId      TEXT,
    CardType           TEXT,
    CardLast4          TEXT,
    CvvCheck           TEXT,
    Amount             REAL,
    TransactionType    TEXT,
    EventOrganiser     TEXT,
    EventName          TEXT,
    GatewayMessage     TEXT,
    GatewayMessageDescription TEXT,
    IsDecline          INTEGER,
    Status             TEXT,
    FirstNameHash      TEXT,
    LastNameHash       TEXT,
    EmailHash          TEXT,
    PhoneHash          TEXT,
    WaitedInQueue      INTEGER,
    WaitTime           TEXT,
    StartingPosition   INTEGER,
    RowHash            TEXT,
    SourceFile         TEXT,
    FirstSeenAt        TEXT,
    UpdatedAt          TEXT
);
CREATE INDEX ix_bt_date ON booking_transactions(TransactionDate);
CREATE INDEX ix_bt_gateway ON booking_transactions(GatewayId);
CREATE INDEX ix_bt_card ON booking_transactions(CardType, CardLast4);
CREATE INDEX ix_bt_email ON booking_transactions(EmailHash);
CREATE INDEX ix_bt_phone ON booking_transactions(PhoneHash);

CREATE TABLE booking_transaction_history (  -- every distinct version, 1,100 days
    BookingId          INTEGER NOT NULL,
    RowHash            TEXT NOT NULL,
    TransactionDate    TEXT,
    GatewayId          TEXT,
    MerchantRefId      TEXT,
    CardType           TEXT,
    CardLast4          TEXT,
    CvvCheck           TEXT,
    Amount             REAL,
    TransactionType    TEXT,
    GatewayMessage     TEXT,
    GatewayMessageDescription TEXT,
    IsDecline          INTEGER,
    Status             TEXT,
    EmailHash          TEXT,
    PhoneHash          TEXT,
    SourceFile         TEXT,
    ImportedAt         TEXT,
    PRIMARY KEY (BookingId, RowHash)
);
CREATE INDEX ix_bth_date ON booking_transaction_history(TransactionDate);

-- ----------------------------------------------------------- Stripe ------
CREATE TABLE stripe_charges (           -- 1,100 days
    ChargeId           TEXT PRIMARY KEY,
    Created            TEXT,
    Amount             REAL,
    Currency           TEXT,
    Status             TEXT,
    Paid               INTEGER,
    Refunded           INTEGER,
    AmountRefunded     REAL,
    Description        TEXT,            -- booking GUID
    PaymentIntent      TEXT,
    OutcomeType        TEXT,
    NetworkStatus      TEXT,
    OutcomeReason      TEXT,
    RiskLevel          TEXT,
    FailureCode        TEXT,
    CardCountry        TEXT,
    CardBrand          TEXT,
    CardFunding        TEXT,
    CardLast4          TEXT,
    CardFingerprint    TEXT,
    CvcCheck           TEXT,
    PostcodeCheck      TEXT,
    Wallet             TEXT,
    BillingCountry     TEXT,
    BillingPostcodeOutward TEXT,
    BillingPostcodeHash TEXT,
    BillingEmailHash   TEXT,
    UpdatedAt          TEXT
);
CREATE INDEX ix_sc_created ON stripe_charges(Created);
CREATE INDEX ix_sc_pi ON stripe_charges(PaymentIntent);
CREATE INDEX ix_sc_desc ON stripe_charges(Description);
CREATE INDEX ix_sc_fp ON stripe_charges(CardFingerprint);
CREATE INDEX ix_sc_card ON stripe_charges(CardBrand, CardLast4);
CREATE INDEX ix_sc_email ON stripe_charges(BillingEmailHash);

CREATE TABLE stripe_disputes (
    DisputeId      TEXT PRIMARY KEY,
    ChargeId       TEXT,
    PaymentIntent  TEXT,
    Created        TEXT,
    Amount         REAL,
    Currency       TEXT,
    Reason         TEXT,
    Status         TEXT,
    EvidenceDueBy  TEXT,
    UpdatedAt      TEXT
);
CREATE INDEX ix_sd_charge ON stripe_disputes(ChargeId);

CREATE TABLE stripe_efw (
    EfwId      TEXT PRIMARY KEY,
    ChargeId   TEXT,
    Created    TEXT,
    FraudType  TEXT,
    Actionable INTEGER,
    UpdatedAt  TEXT
);
CREATE INDEX ix_efw_charge ON stripe_efw(ChargeId);

CREATE TABLE stripe_reviews (
    ReviewId      TEXT PRIMARY KEY,
    ChargeId      TEXT,
    PaymentIntent TEXT,
    Created       TEXT,
    Reason        TEXT,
    OpenedReason  TEXT,
    ClosedReason  TEXT,
    Open          INTEGER,
    UpdatedAt     TEXT
);

CREATE TABLE stripe_sync (
    Object      TEXT NOT NULL,          -- charges | disputes | efw | reviews
    Day         TEXT NOT NULL,          -- Europe/London calendar day
    Status      TEXT NOT NULL,          -- ok | failed
    Rows        INTEGER DEFAULT 0,
    CompletedAt TEXT,
    PRIMARY KEY (Object, Day)
);

-- ------------------------------------------------------ dashboard -------
CREATE TABLE dashboard_users (
    Email    TEXT PRIMARY KEY,          -- work email (lower case), Microsoft sign-in
    Role     TEXT NOT NULL CHECK (Role IN ('viewer', 'admin')),
    Active   INTEGER NOT NULL DEFAULT 1,
    AddedAt  TEXT,
    AddedBy  TEXT
);

CREATE TABLE api_tokens (
    TokenId    TEXT PRIMARY KEY,        -- public id shown in lists
    Name       TEXT NOT NULL,
    TokenHash  TEXT NOT NULL,           -- sha256 of the secret
    CreatedAt  TEXT,
    LastUsedAt TEXT,
    Revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE api_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    At        TEXT NOT NULL,
    Who       TEXT,
    Path      TEXT,
    QueryHash TEXT,
    Rows      INTEGER,
    Status    INTEGER
);
CREATE INDEX ix_audit_at ON api_audit(At);
"""),
]

MIGRATIONS.append((2, "slimmer bookings: event names table, 32-char hashes, no per-row UpdatedAt", """
CREATE TABLE event_names (
    EventId   INTEGER PRIMARY KEY,
    AccountId INTEGER,
    EventName TEXT
);
INSERT INTO event_names (EventId, AccountId, EventName)
    SELECT EventId, MAX(AccountId), MAX(EventName) FROM bookings WHERE EventId IS NOT NULL GROUP BY EventId;

CREATE TABLE bookings_v2 (               -- lifetime, slim
    BookingTransactionId INTEGER PRIMARY KEY,
    BookingId           INTEGER,
    BookingUrlId        TEXT,           -- booking GUID
    AccountId           INTEGER,
    EventId             INTEGER,
    TransactionDate     TEXT,
    EventDate           TEXT,           -- session date (Date of Event)
    PaymentReceived     REAL,
    TicketQuantity      INTEGER,
    BookingFee          REAL,
    Status              TEXT,
    TransactionType     TEXT,
    PaymentType         TEXT,
    IPCountry           TEXT,
    IPHash              TEXT,
    BookingCountryCode  TEXT,
    EventPostcodeOutward TEXT,
    GatewayName         TEXT,
    GatewayId           TEXT,
    GatewayReference    TEXT,
    Wallet              TEXT,
    CustomerId          INTEGER
);
INSERT INTO bookings_v2
    SELECT BookingTransactionId, BookingId, BookingUrlId, AccountId, EventId, TransactionDate, EventDate,
           PaymentReceived, TicketQuantity, BookingFee, Status, TransactionType, PaymentType, IPCountry,
           SUBSTR(IPHash, 1, 32), BookingCountryCode, EventPostcodeOutward, GatewayName, GatewayId,
           GatewayReference, Wallet, CustomerId
    FROM bookings;
DROP TABLE bookings;
ALTER TABLE bookings_v2 RENAME TO bookings;
CREATE INDEX ix_bookings_account ON bookings(AccountId, TransactionDate);
CREATE INDEX ix_bookings_event ON bookings(EventId);
CREATE INDEX ix_bookings_bookingid ON bookings(BookingId);
CREATE INDEX ix_bookings_urlid ON bookings(BookingUrlId);
CREATE INDEX ix_bookings_gateway ON bookings(GatewayId);
CREATE INDEX ix_bookings_ip ON bookings(IPHash);

UPDATE users SET UsernameHash = SUBSTR(UsernameHash, 1, 32);
UPDATE booking_transactions SET FirstNameHash = SUBSTR(FirstNameHash, 1, 32),
    LastNameHash = SUBSTR(LastNameHash, 1, 32), EmailHash = SUBSTR(EmailHash, 1, 32),
    PhoneHash = SUBSTR(PhoneHash, 1, 32);
UPDATE booking_transaction_history SET EmailHash = SUBSTR(EmailHash, 1, 32), PhoneHash = SUBSTR(PhoneHash, 1, 32);
UPDATE stripe_charges SET BillingPostcodeHash = SUBSTR(BillingPostcodeHash, 1, 32),
    BillingEmailHash = SUBSTR(BillingEmailHash, 1, 32);
"""))

MIGRATIONS.append((3, "account tier from the S3 Accounts export", """
ALTER TABLE accounts ADD COLUMN TierSystem TEXT;      -- Accounts.TierSystem (current tier, as exported)
ALTER TABLE accounts ADD COLUMN PreviousTier TEXT;
"""))

MIGRATIONS.append((4, "daily balance movements; daily interim risk scores", """
CREATE TABLE balance_movements (        -- AccountMovementDaily, one row per account per day
    AccountId              INTEGER NOT NULL,
    MovementDate           TEXT NOT NULL,   -- the file's date (YYYYMMDD in its name)
    TicketValue            REAL,
    Donation               REAL,
    TotalBookingFee        REAL,
    TicketRefundedAmount   REAL,
    DonationRefundedAmount REAL,
    Pending                REAL,
    OpeningBalance         REAL,
    ClosingBalance         REAL,
    StripeTransfers        REAL,
    FundTransfer           REAL,            -- export column "FundTransafer"
    Recredit               REAL,
    DebitListing           REAL,
    AdditionalDebitBilling REAL,
    AdditionalCreditBilling REAL,
    PRIMARY KEY (AccountId, MovementDate)
);
CREATE INDEX ix_movements_date ON balance_movements(MovementDate);

CREATE TABLE risk_daily (               -- one row per scored account/event per report run
    RunDate       TEXT NOT NULL,
    Level         TEXT NOT NULL,            -- account | event
    Id            INTEGER NOT NULL,         -- AccountId or EventId
    AccountId     INTEGER,
    ExposureScore REAL,
    FraudScore    REAL,
    Priority      TEXT,                     -- P1..P4 or '-'
    AmountAtRisk  REAL,
    Reasons       TEXT,
    ModelVersion  TEXT,
    PRIMARY KEY (RunDate, Level, Id)
);
CREATE INDEX ix_risk_daily_id ON risk_daily(Level, Id);
"""))

MIGRATIONS.append((5, "Booking Transactions keyed on the booking GUID; GUIDs in one form", """
UPDATE bookings SET BookingUrlId = CASE WHEN LENGTH(TRIM(BookingUrlId)) = 32 THEN LOWER(SUBSTR(TRIM(BookingUrlId),1,8)||'-'||SUBSTR(TRIM(BookingUrlId),9,4)||'-'||SUBSTR(TRIM(BookingUrlId),13,4)||'-'||SUBSTR(TRIM(BookingUrlId),17,4)||'-'||SUBSTR(TRIM(BookingUrlId),21,12)) WHEN LENGTH(TRIM(BookingUrlId)) = 36 AND TRIM(BookingUrlId) LIKE '%-%-%-%-%' THEN LOWER(TRIM(BookingUrlId)) ELSE TRIM(BookingUrlId) END WHERE BookingUrlId IS NOT NULL;
UPDATE gateway_recon SET BookingUrlId = CASE WHEN LENGTH(TRIM(BookingUrlId)) = 32 THEN LOWER(SUBSTR(TRIM(BookingUrlId),1,8)||'-'||SUBSTR(TRIM(BookingUrlId),9,4)||'-'||SUBSTR(TRIM(BookingUrlId),13,4)||'-'||SUBSTR(TRIM(BookingUrlId),17,4)||'-'||SUBSTR(TRIM(BookingUrlId),21,12)) WHEN LENGTH(TRIM(BookingUrlId)) = 36 AND TRIM(BookingUrlId) LIKE '%-%-%-%-%' THEN LOWER(TRIM(BookingUrlId)) ELSE TRIM(BookingUrlId) END WHERE BookingUrlId IS NOT NULL;

CREATE TABLE booking_transactions_v5 (  -- latest row per booking GUID ("Booking ID" in the export), 1,100 days
    BookingGuid        TEXT PRIMARY KEY,
    TransactionDate    TEXT,
    GatewayId          TEXT,
    MerchantRefId      TEXT,
    CardType           TEXT,
    CardLast4          TEXT,
    CvvCheck           TEXT,
    Amount             REAL,
    TransactionType    TEXT,
    EventOrganiser     TEXT,
    EventName          TEXT,
    GatewayMessage     TEXT,
    GatewayMessageDescription TEXT,
    IsDecline          INTEGER,
    Status             TEXT,
    FirstNameHash      TEXT,
    LastNameHash       TEXT,
    EmailHash          TEXT,
    PhoneHash          TEXT,
    WaitedInQueue      INTEGER,
    WaitTime           TEXT,
    StartingPosition   INTEGER,
    RowHash            TEXT,
    SourceFile         TEXT,
    FirstSeenAt        TEXT,
    UpdatedAt          TEXT
);
INSERT INTO booking_transactions_v5 SELECT CAST(BookingId AS TEXT), TransactionDate, GatewayId, MerchantRefId,
    CardType, CardLast4, CvvCheck, Amount, TransactionType, EventOrganiser, EventName, GatewayMessage,
    GatewayMessageDescription, IsDecline, Status, FirstNameHash, LastNameHash, EmailHash, PhoneHash, WaitedInQueue,
    WaitTime, StartingPosition, RowHash, SourceFile, FirstSeenAt, UpdatedAt FROM booking_transactions;
DROP TABLE booking_transactions;
ALTER TABLE booking_transactions_v5 RENAME TO booking_transactions;
CREATE INDEX ix_bt_date ON booking_transactions(TransactionDate);
CREATE INDEX ix_bt_gateway ON booking_transactions(GatewayId);
CREATE INDEX ix_bt_card ON booking_transactions(CardType, CardLast4);
CREATE INDEX ix_bt_email ON booking_transactions(EmailHash);
CREATE INDEX ix_bt_phone ON booking_transactions(PhoneHash);

CREATE TABLE booking_transaction_history_v5 (  -- every distinct version, 1,100 days
    BookingGuid        TEXT NOT NULL,
    RowHash            TEXT NOT NULL,
    TransactionDate    TEXT,
    GatewayId          TEXT,
    MerchantRefId      TEXT,
    CardType           TEXT,
    CardLast4          TEXT,
    CvvCheck           TEXT,
    Amount             REAL,
    TransactionType    TEXT,
    GatewayMessage     TEXT,
    GatewayMessageDescription TEXT,
    IsDecline          INTEGER,
    Status             TEXT,
    EmailHash          TEXT,
    PhoneHash          TEXT,
    SourceFile         TEXT,
    ImportedAt         TEXT,
    PRIMARY KEY (BookingGuid, RowHash)
);
INSERT INTO booking_transaction_history_v5 SELECT CAST(BookingId AS TEXT), RowHash, TransactionDate, GatewayId,
    MerchantRefId, CardType, CardLast4, CvvCheck, Amount, TransactionType, GatewayMessage, GatewayMessageDescription,
    IsDecline, Status, EmailHash, PhoneHash, SourceFile, ImportedAt FROM booking_transaction_history;
DROP TABLE booking_transaction_history;
ALTER TABLE booking_transaction_history_v5 RENAME TO booking_transaction_history;
CREATE INDEX ix_bth_date ON booking_transaction_history(TransactionDate);
"""))

MIGRATIONS.append((6, "Booking Transactions: one row per payment attempt, so overlapping files never duplicate", """
-- An attempt is (booking GUID, transaction date to the second, transaction type, n-th such row in the file).
-- The same attempt in a later file updates its row (e.g. Unknown -> Successful) instead of adding one.
CREATE TABLE booking_transaction_history_v6 (  -- one row per payment attempt, 1,100 days
    AttemptKey         TEXT PRIMARY KEY,
    BookingGuid        TEXT NOT NULL,
    RowHash            TEXT NOT NULL,
    TransactionDate    TEXT,
    GatewayId          TEXT,
    MerchantRefId      TEXT,
    CardType           TEXT,
    CardLast4          TEXT,
    CvvCheck           TEXT,
    Amount             REAL,
    TransactionType    TEXT,
    GatewayMessage     TEXT,
    GatewayMessageDescription TEXT,
    IsDecline          INTEGER,
    Status             TEXT,
    EmailHash          TEXT,
    PhoneHash          TEXT,
    SourceFile         TEXT,
    FileDate           TEXT,
    Versions           INTEGER NOT NULL DEFAULT 1,
    ImportedAt         TEXT,
    UpdatedAt          TEXT
);
INSERT INTO booking_transaction_history_v6
SELECT BookingGuid || '|' || COALESCE(TransactionDate, '') || '|' || LOWER(COALESCE(TransactionType, '')) || '|' ||
           (ROW_NUMBER() OVER (PARTITION BY BookingGuid, COALESCE(TransactionDate, ''),
                                            LOWER(COALESCE(TransactionType, '')) ORDER BY rowid) - 1),
       BookingGuid, RowHash, TransactionDate, GatewayId, MerchantRefId, CardType, CardLast4, CvvCheck, Amount,
       TransactionType, GatewayMessage, GatewayMessageDescription, IsDecline, Status, EmailHash, PhoneHash,
       SourceFile, SUBSTR(ImportedAt, 1, 10), 1, ImportedAt, ImportedAt
FROM booking_transaction_history;
DROP TABLE booking_transaction_history;
ALTER TABLE booking_transaction_history_v6 RENAME TO booking_transaction_history;
CREATE INDEX ix_bth_date ON booking_transaction_history(TransactionDate);
CREATE INDEX ix_bth_guid ON booking_transaction_history(BookingGuid);

ALTER TABLE booking_transactions ADD COLUMN FileDate TEXT;
UPDATE booking_transactions SET FileDate = SUBSTR(UpdatedAt, 1, 10);
"""))

LATEST_VERSION = MIGRATIONS[-1][0]
