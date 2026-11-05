"""S3 exports -> warehouse tables, plus the tables derived from them.

Loaders take a source (rm.s3.S3Source / LocalSource) and a key, stream only
the columns they need, and upsert. Personal data (IP address, username) is
hashed on the way in; card numbers go through the last-4 guard.

Derived tables are rebuilt from scratch each run (cheap, and always
consistent with what's loaded):
    events            per-event dates incl. EventCreated (+ source)
    account_lifetime  per-account totals over every booking
    event_lifetime    per-event totals
"""
import logging
from collections import defaultdict

import pandas as pd

from .. import crypto, db
from ..s3 import canonical_guid, describe_head, read_chunks, text, to_bool, to_float, to_int, to_iso

log = logging.getLogger(__name__)

UK_VALUES = ("GB", "GBR", "UK", "UKG", "UNITED KINGDOM", "GREAT BRITAIN", "ENGLAND", "SCOTLAND",
             "WALES", "NORTHERN IRELAND")


class _IpHashCache:
    """IPs repeat a lot across bookings; cache their hashes (bounded)."""

    def __init__(self, hasher, limit=500_000):
        self.hasher, self.limit, self.cache = hasher, limit, {}

    def __call__(self, ip):
        if ip is None:
            return None
        h = self.cache.get(ip)
        if h is None:
            h = self.hasher.ip(ip)
            if len(self.cache) >= self.limit:
                self.cache.clear()
            self.cache[ip] = h
        return h


def _records(df: pd.DataFrame) -> list:
    df = df.astype(object).where(df.notna(), None)
    return df.to_dict("records")


def _col(chunk, name):
    return chunk[name] if name in chunk.columns else pd.Series([None] * len(chunk), index=chunk.index, dtype=object)


def _require(chunk, names, key, source=None):
    missing = [n for n in names if n not in chunk.columns]
    if missing:
        msg = f"{key}: missing required columns {missing}"
        if source is not None:
            try:
                msg += f". File starts: {describe_head(source.head_bytes(key, 4096))}"
            except Exception:  # the description is only a hint
                pass
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Bookings (BookingDataAll / BookingData)
# ---------------------------------------------------------------------------

BOOKING_COLUMNS = ["BookingTransactionId", "BookingId", "BookingUrlId", "AccountId", "EventId", "EventName",
                   "TransactionDate", "EventDate", "PaymentReceived", "TicketQuantity", "BookingFee", "Status",
                   "TransactionType", "PaymentType", "IPCountry", "IPAddress", "BookingCountryCode",
                   "EventPostcode", "GatewayName", "GatewayId", "GatewayReference", "Wallet", "CustomerId"]


def booking_rows(chunk: pd.DataFrame, ip_hash, key: str = "") -> list:
    _require(chunk, ["BookingTransactionId", "AccountId", "EventId"], key)
    out = pd.DataFrame({
        "BookingTransactionId": to_int(chunk["BookingTransactionId"]),
        "BookingId": to_int(_col(chunk, "BookingId")),
        "BookingUrlId": _col(chunk, "BookingUrlId").map(canonical_guid),
        "AccountId": to_int(chunk["AccountId"]),
        "EventId": to_int(chunk["EventId"]),
        "TransactionDate": to_iso(_col(chunk, "TransactionDate")),
        "EventDate": to_iso(_col(chunk, "EventDate")),
        "PaymentReceived": to_float(_col(chunk, "PaymentReceived")),
        "TicketQuantity": to_int(_col(chunk, "TicketQuantity")),
        "BookingFee": to_float(_col(chunk, "BookingFee")),
        "Status": text(_col(chunk, "Status")),
        "TransactionType": text(_col(chunk, "TransactionType")),
        "PaymentType": text(_col(chunk, "PaymentType")),
        "IPCountry": text(_col(chunk, "IPCountry")),
        "IPHash": text(_col(chunk, "IPAddress")).map(ip_hash),
        "BookingCountryCode": text(_col(chunk, "BookingCountryCode")),
        "EventPostcodeOutward": _col(chunk, "EventPostcode").map(crypto.postcode_outward),
        "GatewayName": text(_col(chunk, "GatewayName")),
        "GatewayId": text(_col(chunk, "GatewayId")),
        "GatewayReference": text(_col(chunk, "GatewayReference")),
        "Wallet": text(_col(chunk, "Wallet")),
        "CustomerId": to_int(_col(chunk, "CustomerId")),
    })
    out = out[out["BookingTransactionId"].notna()]
    return _records(out)


def event_name_rows(chunk: pd.DataFrame) -> list:
    df = pd.DataFrame({"EventId": to_int(chunk["EventId"]), "AccountId": to_int(chunk["AccountId"]),
                       "EventName": text(_col(chunk, "EventName"))})
    df = df[df["EventId"].notna()].drop_duplicates("EventId", keep="last")
    return _records(df)


def load_bookings(conn, hasher, source, key: str, chunksize: int = 100_000) -> dict:
    ip_hash = _IpHashCache(hasher)
    read = written = 0
    for chunk in read_chunks(source, key, BOOKING_COLUMNS, chunksize):
        rows = booking_rows(chunk, ip_hash, key)
        with conn:
            written += db.upsert(conn, "bookings", rows, ["BookingTransactionId"])
            db.upsert(conn, "event_names", event_name_rows(chunk), ["EventId"])
        read += len(chunk)
        log.info("  %s: %s rows read", key, f"{read:,}")
    return {"rows_read": read, "rows_written": written}


# ---------------------------------------------------------------------------
# Accounts (+ daily snapshot)
# ---------------------------------------------------------------------------

ACCOUNT_COLUMNS = ["Id", "AccountId", "AccountName", "AccountStatus", "DateTimeCreated", "FirstEventCreation",
                   "LastEventCreation", "IsVerified", "BankAccountIsDeleted", "BankAccountStatusId", "Country",
                   "Postcode", "Industry", "SubIndustry", "GatewayGroup", "TierSystem", "PreviousTier"]


def load_accounts(conn, source, key: str, snapshot_date: str) -> dict:
    read = written = 0
    now = db.utcnow()
    for chunk in read_chunks(source, key, ACCOUNT_COLUMNS):
        idcol = "Id" if "Id" in chunk.columns else "AccountId"
        _require(chunk, [idcol], key, source)
        df = pd.DataFrame({
            "AccountId": to_int(chunk[idcol]),
            "AccountName": text(_col(chunk, "AccountName")),
            "AccountStatus": text(_col(chunk, "AccountStatus")),
            "DateTimeCreated": to_iso(_col(chunk, "DateTimeCreated")),
            "FirstEventCreation": to_iso(_col(chunk, "FirstEventCreation")),
            "LastEventCreation": to_iso(_col(chunk, "LastEventCreation")),
            "IsVerified": to_bool(_col(chunk, "IsVerified")),
            "BankAccountIsDeleted": to_bool(_col(chunk, "BankAccountIsDeleted")),
            "BankAccountStatusId": to_int(_col(chunk, "BankAccountStatusId")),
            "Country": text(_col(chunk, "Country")),
            "PostcodeOutward": _col(chunk, "Postcode").map(crypto.postcode_outward),
            "Industry": text(_col(chunk, "Industry")),
            "SubIndustry": text(_col(chunk, "SubIndustry")),
            "GatewayGroup": text(_col(chunk, "GatewayGroup")),
            "TierSystem": text(_col(chunk, "TierSystem")),
            "PreviousTier": text(_col(chunk, "PreviousTier")),
        })
        df = df[df["AccountId"].notna()]
        df["UpdatedAt"] = now
        snap = df[["AccountId", "LastEventCreation", "AccountStatus", "BankAccountIsDeleted",
                   "BankAccountStatusId"]].copy()
        snap.insert(1, "SnapshotDate", snapshot_date)
        with conn:
            written += db.upsert(conn, "accounts", _records(df), ["AccountId"])
            db.upsert(conn, "account_snapshots", _records(snap), ["AccountId", "SnapshotDate"])
            tiers = df[df["TierSystem"].notna()][["AccountId", "TierSystem"]].rename(columns={"TierSystem": "Tier"})
            tiers["SnapshotDate"] = snapshot_date
            db.upsert(conn, "account_tiers", _records(tiers), ["AccountId"])
        read += len(chunk)
    return {"rows_read": read, "rows_written": written}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

USER_COLUMNS = ["UserId", "AccountId", "RoleName", "Username", "IsDeleted", "LastLoginDatetime"]


def load_users(conn, hasher, source, key: str) -> dict:
    read = written = 0
    now = db.utcnow()
    for chunk in read_chunks(source, key, USER_COLUMNS):
        _require(chunk, ["UserId"], key, source)
        df = pd.DataFrame({
            "UserId": to_int(chunk["UserId"]),
            "AccountId": to_int(_col(chunk, "AccountId")),
            "RoleName": text(_col(chunk, "RoleName")),
            "UsernameHash": _col(chunk, "Username").map(hasher.email),
            "IsDeleted": to_bool(_col(chunk, "IsDeleted")),
            "LastLoginDatetime": to_iso(_col(chunk, "LastLoginDatetime")),
        })
        df = df[df["UserId"].notna()]
        df["UpdatedAt"] = now
        with conn:
            written += db.upsert(conn, "users", _records(df), ["UserId"])
        read += len(chunk)
    return {"rows_read": read, "rows_written": written}


# ---------------------------------------------------------------------------
# RiskReport -> balances (one row per account per day)
# ---------------------------------------------------------------------------

BALANCE_COLUMNS = ["AccountId", "AccountID", "FullBalance", "Balance", "SalesForUpcomingEvents", "Exposure",
                   "FutureDays", "LastEventDate"]


def load_balances(conn, source, key: str, snapshot_date: str) -> dict:
    read = written = 0
    for chunk in read_chunks(source, key, BALANCE_COLUMNS):
        idcol = "AccountId" if "AccountId" in chunk.columns else "AccountID"
        _require(chunk, [idcol], key, source)
        df = pd.DataFrame({
            "AccountId": to_int(chunk[idcol]),
            "FullBalance": to_float(_col(chunk, "FullBalance")),
            "Balance": to_float(_col(chunk, "Balance")),
            "SalesForUpcomingEvents": to_float(_col(chunk, "SalesForUpcomingEvents")),
            "Exposure": to_float(_col(chunk, "Exposure")),
            "FutureDays": to_int(_col(chunk, "FutureDays")),
            "LastEventDate": to_iso(_col(chunk, "LastEventDate")),
        })
        df = df[df["AccountId"].notna()]
        df.insert(1, "SnapshotDate", snapshot_date)
        with conn:
            written += db.upsert(conn, "balances", _records(df), ["AccountId", "SnapshotDate"])
        read += len(chunk)
    return {"rows_read": read, "rows_written": written}


# ---------------------------------------------------------------------------
# AccountMovementDaily -> balance_movements (one row per account per day)
# ---------------------------------------------------------------------------

MOVEMENT_COLUMNS = {   # export column -> table column
    "AccountId": "AccountId", "TicketValue": "TicketValue", "Donation": "Donation",
    "TotalBookingFee": "TotalBookingFee", "TicketRefundedAmount": "TicketRefundedAmount",
    "DonationRefundedAmount": "DonationRefundedAmount", "Pending": "Pending",
    "OpeningBalance": "OpeningBalance", "ClosingBalance": "ClosingBalance",
    "StripeTransfers": "StripeTransfers", "FundTransafer": "FundTransfer", "Recredit": "Recredit",
    "DebitListing": "DebitListing", "AdditionalDebitBilling": "AdditionalDebitBilling",
    "AdditionalCreditBilling": "AdditionalCreditBilling",
}


def load_movements(conn, source, key: str, movement_date: str) -> dict:
    """One daily file (the date comes from its name). Has a gateway preamble line."""
    read = written = 0
    for chunk in read_chunks(source, key, list(MOVEMENT_COLUMNS)):
        _require(chunk, ["AccountId"], key, source)
        df = pd.DataFrame({dst: (to_int(chunk[src]) if dst == "AccountId" else to_float(_col(chunk, src)))
                           for src, dst in MOVEMENT_COLUMNS.items()})
        df = df[df["AccountId"].notna()]
        df.insert(1, "MovementDate", movement_date)
        with conn:
            written += db.upsert(conn, "balance_movements", _records(df), ["AccountId", "MovementDate"])
        read += len(chunk)
    return {"rows_read": read, "rows_written": written}


def prune_movements(conn, cutoff: str) -> int:
    with conn:
        return conn.execute("DELETE FROM balance_movements WHERE MovementDate < ?", (cutoff,)).rowcount


# ---------------------------------------------------------------------------
# TransactionReconciliation -> gateway_recon (card last 4 guarded)
# ---------------------------------------------------------------------------

RECON_COLUMNS = ["BookingTransactionId", "TransactionDate", "GatewayId", "BookingUrlId", "CardType", "CardNo",
                 "Amount", "Transaction", "GatewayMessage", "PayoutId", "ArrivalDate", "Provider"]


def load_recon(conn, source, key: str, cutoff: str) -> dict:
    """Load rows on/after ``cutoff`` (YYYY-MM-DD). A full card number anywhere in
    the file aborts the whole file before anything is written."""
    frames = []
    read = 0
    for chunk in read_chunks(source, key, RECON_COLUMNS):
        _require(chunk, ["BookingTransactionId"], key, source)
        df = pd.DataFrame({
            "BookingTransactionId": to_int(chunk["BookingTransactionId"]),
            "GatewayId": text(_col(chunk, "GatewayId")).fillna(""),
            "TxnType": text(_col(chunk, "Transaction")).fillna(""),
            "TransactionDate": to_iso(_col(chunk, "TransactionDate")),
            "BookingUrlId": _col(chunk, "BookingUrlId").map(canonical_guid),
            "CardType": text(_col(chunk, "CardType")),
            "CardLast4": _col(chunk, "CardNo").map(crypto.card_last4),   # raises SensitiveDataError
            "Amount": to_float(_col(chunk, "Amount")),
            "GatewayMessage": text(_col(chunk, "GatewayMessage")),
            "PayoutId": text(_col(chunk, "PayoutId")),
            "ArrivalDate": to_iso(_col(chunk, "ArrivalDate")),
            "Provider": text(_col(chunk, "Provider")),
        })
        read += len(chunk)
        df = df[df["BookingTransactionId"].notna()]
        df = df[df["TransactionDate"].isna() | (df["TransactionDate"].fillna("") >= cutoff)]
        frames.append(df)
    written = 0
    now = db.utcnow()
    with conn:
        for df in frames:
            df["UpdatedAt"] = now
            written += db.upsert(conn, "gateway_recon", _records(df), ["BookingTransactionId", "GatewayId", "TxnType"])
    return {"rows_read": read, "rows_written": written}


# ---------------------------------------------------------------------------
# Derived tables
# ---------------------------------------------------------------------------

def rebuild_events(conn) -> int:
    """Rebuild ``events`` from bookings, with EventCreated from the best source:

    snapshot      LastEventCreation changed between two daily account snapshots ->
                  that timestamp is the created date of the account's next event
                  to start selling (exact, for events created after go-live)
    last_event    account's LastEventCreation -> its newest event by first booking
    first_event   account's FirstEventCreation -> its oldest event by first booking
    first_booking otherwise the event's first booking date (latest it could be)

    A created date is only assigned when it isn't after the event's first
    booking (an event can't sell before it exists).
    """
    events = conn.execute("""
        SELECT b.EventId, MAX(b.AccountId) AS AccountId, MAX(n.EventName) AS EventName,
               MIN(b.EventDate) AS FirstSessionDate, MAX(b.EventDate) AS LastSessionDate,
               MIN(b.TransactionDate) AS FirstBookingDate, MAX(b.TransactionDate) AS LastBookingDate,
               MAX(b.EventPostcodeOutward) AS EventPostcodeOutward
        FROM bookings b NOT INDEXED LEFT JOIN event_names n ON n.EventId = b.EventId
        WHERE b.EventId IS NOT NULL GROUP BY b.EventId""").fetchall()
    accounts = {r["AccountId"]: (r["FirstEventCreation"], r["LastEventCreation"]) for r in conn.execute(
        "SELECT AccountId, FirstEventCreation, LastEventCreation FROM accounts")}

    # Creation timestamps seen changing between consecutive snapshots.
    changes = defaultdict(list)
    prev = {}
    for r in conn.execute("SELECT AccountId, SnapshotDate, LastEventCreation FROM account_snapshots "
                          "ORDER BY AccountId, SnapshotDate"):
        a, v = r["AccountId"], r["LastEventCreation"]
        if a in prev and v and v != prev[a]:
            changes[a].append(v)
        prev[a] = v

    by_account = defaultdict(list)
    for e in events:
        by_account[e["AccountId"]].append(dict(e))

    out = []
    for acct, evs in by_account.items():
        evs.sort(key=lambda e: (e["FirstBookingDate"] or "9999", e["EventId"]))
        created = {}

        def assign(ev, when, src):
            if ev["EventId"] in created or not when:
                return False
            if ev["FirstBookingDate"] and when > ev["FirstBookingDate"]:
                return False
            created[ev["EventId"]] = (when, src)
            return True

        for when in sorted(changes.get(acct, [])):
            for ev in evs:  # first event to start selling after it was created
                if ev["FirstBookingDate"] and ev["FirstBookingDate"] >= when and ev["EventId"] not in created:
                    assign(ev, when, "snapshot")
                    break
        first_c, last_c = accounts.get(acct, (None, None))
        if evs and last_c:
            assign(evs[-1], last_c, "last_event")
        if evs and first_c:
            assign(evs[0], first_c, "first_event")
        for ev in evs:
            when, src = created.get(ev["EventId"], (ev["FirstBookingDate"], "first_booking"))
            out.append({**ev, "EventCreated": when, "EventCreatedSource": src if when else None})

    now = db.utcnow()
    for r in out:
        r["UpdatedAt"] = now
    with conn:
        conn.execute("DELETE FROM events")
        db.upsert(conn, "events", out, ["EventId"])
    return len(out)


def rebuild_lifetime(conn) -> dict:
    uk = ",".join(f"'{v}'" for v in UK_VALUES)
    foreign = (f"CASE WHEN IPCountry IS NOT NULL AND TRIM(IPCountry) <> '' "
               f"AND UPPER(TRIM(IPCountry)) NOT IN ({uk}, 'UNKNOWN') THEN 1 ELSE 0 END")
    refunded = ("CASE WHEN LOWER(COALESCE(Status,'')) LIKE '%refund%' "
                "OR LOWER(COALESCE(TransactionType,'')) LIKE '%refund%' THEN 1 ELSE 0 END")
    now = db.utcnow()
    with conn:
        conn.execute("DELETE FROM event_lifetime")
        conn.execute(f"""
            INSERT INTO event_lifetime (EventId, AccountId, Bookings, PaidBookings, Tickets, Gross,
                                        RefundedBookings, ForeignIpBookings, UpdatedAt)
            SELECT EventId, MAX(AccountId), COUNT(*),
                   SUM(CASE WHEN COALESCE(PaymentReceived,0) > 0 THEN 1 ELSE 0 END),
                   SUM(COALESCE(TicketQuantity,0)), ROUND(SUM(COALESCE(PaymentReceived,0)), 2),
                   SUM({refunded}), SUM({foreign}), ?
            FROM bookings NOT INDEXED WHERE EventId IS NOT NULL GROUP BY EventId""", (now,))
        conn.execute("DELETE FROM account_lifetime")
        conn.execute(f"""
            INSERT INTO account_lifetime (AccountId, Events, Bookings, PaidBookings, Tickets, Gross,
                                          RefundedBookings, ForeignIpBookings, FirstBookingDate,
                                          LastBookingDate, UpdatedAt)
            SELECT AccountId, COUNT(DISTINCT EventId), COUNT(*),
                   SUM(CASE WHEN COALESCE(PaymentReceived,0) > 0 THEN 1 ELSE 0 END),
                   SUM(COALESCE(TicketQuantity,0)), ROUND(SUM(COALESCE(PaymentReceived,0)), 2),
                   SUM({refunded}), SUM({foreign}), MIN(TransactionDate), MAX(TransactionDate), ?
            FROM bookings NOT INDEXED WHERE AccountId IS NOT NULL GROUP BY AccountId""", (now,))
    return {
        "events": conn.execute("SELECT COUNT(*) FROM event_lifetime").fetchone()[0],
        "accounts": conn.execute("SELECT COUNT(*) FROM account_lifetime").fetchone()[0],
    }


def prune_payments(conn, cutoff: str) -> int:
    """Drop payment rows older than the retention cutoff (YYYY-MM-DD)."""
    with conn:
        return conn.execute("DELETE FROM gateway_recon WHERE TransactionDate < ?", (cutoff,)).rowcount
