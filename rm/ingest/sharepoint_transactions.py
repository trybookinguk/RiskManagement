"""Booking Transactions CSV (SharePoint "Platform Data/Booking Transactions").

One CSV row per payment attempt. The warehouse keeps:
    booking_transactions          the latest row per booking (the export's
                                  "Booking ID" is the booking GUID = S3 BookingUrlId)
    booking_transaction_history   every distinct version of a row, so a decline
                                  followed by a successful payment on the same
                                  Booking ID keeps both
Both are pruned to PAYMENT_RETENTION_DAYS (1,100).

Personal data (first/last name, email, phone) is hashed on the way in. The
card number goes through the last-4 guard: one unmasked card number anywhere
rejects the whole file, before anything is written.

``parse`` validates and transforms a file without touching the database (so a
bad file can be rejected cleanly); ``store`` writes the result in the caller's
transaction.
"""
import hashlib
import io
import re

import pandas as pd

from .. import crypto, db
from datetime import date, datetime

from ..s3 import canonical_guid, text, to_bool, to_float, to_int, to_iso

# Export column -> internal name. Matching ignores case, spaces and punctuation.
COLUMNS = {
    "Transaction Date": "TransactionDate",
    "Gateway ID": "GatewayId",
    "Booking ID": "BookingGuid",
    "Merchant Ref ID": "MerchantRefId",
    "Card Type": "CardType",
    "Card Number": "CardNumber",
    "Cvv Check": "CvvCheck",
    "Amount": "Amount",
    "Transaction": "TransactionType",
    "Event Organiser": "EventOrganiser",
    "Event Name": "EventName",
    "Gateway Message": "GatewayMessage",
    "Gateway Message Description": "GatewayMessageDescription",
    "Status": "Status",
    "Booking First Name": "FirstName",
    "Booking Last Name": "LastName",
    "Booking Email": "Email",
    "Booking Phone": "Phone",
    "Waited In Queue": "WaitedInQueue",
    "Wait Time": "WaitTime",
    "Starting Position": "StartingPosition",
}
REQUIRED = ["Booking ID", "Transaction Date", "Amount", "Status", "Gateway Message Description"]

# Decline detection from Gateway Message Description (and Status). Refine as
# real values are seen - the raw text is always kept.
_DECLINE = re.compile(r"declin|do not honou?r|insufficient|refus|denied|not authori[sz]ed|blocked|fraud|"
                      r"lost|stolen|expired|invalid|incorrect|failed|failure|error|rejected|cancelled by issuer|"
                      r"pickup|restricted|limit exceeded|3ds.*fail|authentication fail", re.I)
_APPROVED = re.compile(r"approv|success|authori[sz]ed|accepted|captured|settled|complete", re.I)


class FileRejected(ValueError):
    """The file can't be imported. The message is safe to show and store (no personal data)."""


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


_BY_KEY = {_key(k): v for k, v in COLUMNS.items()}


def read_csv(data: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, encoding=enc)
        except UnicodeDecodeError:
            continue
        except pd.errors.ParserError as e:
            raise FileRejected(f"not a readable CSV ({str(e).splitlines()[0][:150]})") from None
        except pd.errors.EmptyDataError:
            raise FileRejected("the file is empty") from None
    raise FileRejected("unrecognised text encoding (expected UTF-8 or Windows-1252)")


def is_decline(description, status):
    desc = description.strip() if isinstance(description, str) else ""
    st = status.strip() if isinstance(status, str) else ""
    if _DECLINE.search(desc) or re.search(r"declin|fail|refus|reject|error", st, re.I):
        return 1
    if _APPROVED.search(desc) or re.search(r"approv|success|complete|paid", st, re.I):
        return 0
    return None


def _row_hash(values: dict) -> str:
    payload = "\x1f".join("" if v is None else str(v) for _, v in sorted(values.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


_NUM_DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})")
_NAME_DATE = re.compile(r"(\d{1,2})[_\- ]([A-Za-z]{3})[A-Za-z]*[_\- ](\d{2,4})")


def shape(value) -> str:
    """'AB12-x' -> 'AA99-a': shows a value's format without its content."""
    out = re.sub(r"[A-Z]", "A", re.sub(r"[a-z]", "a", re.sub(r"[0-9]", "9", str(value))))
    return out[:40]


def date_from_name(filename):
    """3_Nov_26_BookingTransactionsReport.csv -> date(2026, 11, 3), else None."""
    m = _NAME_DATE.search(filename or "")
    if not m:
        return None
    d, mon, y = m.groups()
    y = int(y) + (2000 if len(y) == 2 else 0)
    try:
        return datetime.strptime(f"{int(d)} {mon[:3].title()} {y}", "%d %b %Y").date()
    except ValueError:
        return None


def date_order(values, filename=None) -> str:
    """'dmy' or 'mdy' for n/n/YYYY dates in this file.

    A first number over 12 means day-first; a second number over 12 means
    month-first. If every date is ambiguous, the date in the file name decides
    (the reading closest to it, not after it); otherwise month-first, which is
    how the Booking Transactions export writes dates (3 Nov 2026 = 11/03/2026).
    """
    pairs = [(int(m.group(1)), int(m.group(2)), int(m.group(3)))
             for m in (_NUM_DATE.match(str(v)) for v in values if v) if m]
    if not pairs:
        return "mdy"
    if any(a > 12 for a, b, _ in pairs):
        return "dmy"
    if any(b > 12 for a, b, _ in pairs):
        return "mdy"
    hint = date_from_name(filename)
    if hint:
        def gap(order):
            best = None
            for a, b, y in pairs:
                try:
                    d = date(y, b, a) if order == "dmy" else date(y, a, b)
                except ValueError:
                    return 10 ** 6
                g = (hint - d).days
                g = 10 ** 5 + abs(g) if g < -1 else abs(g)       # dates after the file date are unlikely
                best = g if best is None else min(best, g)
            return best
        return "dmy" if gap("dmy") < gap("mdy") else "mdy"
    return "mdy"


def parse(data: bytes, hasher, filename: str = None) -> pd.DataFrame:
    """Validate and transform one export. Raises FileRejected with a safe reason."""
    raw = read_csv(data)
    raw.columns = [str(c).replace("﻿", "").strip() for c in raw.columns]
    mapping = {c: _BY_KEY[_key(c)] for c in raw.columns if _key(c) in _BY_KEY}
    missing = [c for c in REQUIRED if COLUMNS[c] not in mapping.values()]
    if missing:
        raise FileRejected(f"missing required column(s): {', '.join(missing)}. "
                           f"Expected the Booking Transactions export with: {', '.join(COLUMNS)}")
    df = raw[list(mapping)].rename(columns=mapping)
    if df.empty:
        raise FileRejected("the file has a header but no rows")

    def col(name):
        return df[name] if name in df.columns else pd.Series([None] * len(df), index=df.index, dtype=object)

    # Card guard first: nothing is stored if any row holds a card number.
    last4 = []
    for i, v in enumerate(col("CardNumber").tolist(), start=2):
        try:
            last4.append(crypto.card_last4(v))
        except crypto.SensitiveDataError:
            raise FileRejected(f"row {i}: 'Card Number' holds more than the last 4 digits. "
                               "The whole file was rejected and nothing was stored. "
                               "Re-export with masked card numbers.") from None

    order = date_order(text(col("TransactionDate")).dropna().tolist(), filename)
    out = pd.DataFrame({
        "BookingGuid": col("BookingGuid").map(canonical_guid),
        "TransactionDate": to_iso(col("TransactionDate"), dayfirst=(order == "dmy")),
        "GatewayId": text(col("GatewayId")),
        "MerchantRefId": text(col("MerchantRefId")),
        "CardType": text(col("CardType")),
        "CardLast4": pd.Series(last4, index=df.index, dtype=object),
        "CvvCheck": text(col("CvvCheck")),
        "Amount": to_float(col("Amount")),
        "TransactionType": text(col("TransactionType")),
        "EventOrganiser": text(col("EventOrganiser")),
        "EventName": text(col("EventName")),
        "GatewayMessage": text(col("GatewayMessage")),
        "GatewayMessageDescription": text(col("GatewayMessageDescription")),
        "Status": text(col("Status")),
        "FirstNameHash": col("FirstName").map(hasher.name),
        "LastNameHash": col("LastName").map(hasher.name),
        "EmailHash": col("Email").map(hasher.email),
        "PhoneHash": col("Phone").map(hasher.phone),
        "WaitedInQueue": to_bool(col("WaitedInQueue")),
        "WaitTime": text(col("WaitTime")),
        "StartingPosition": to_int(col("StartingPosition")),
    })
    bad_dates = out["TransactionDate"].isna() & text(col("TransactionDate")).notna()
    if bad_dates.any():
        i = list(out.index[bad_dates])[:5]
        raise FileRejected(f"'Transaction Date' not recognised on row(s) {', '.join(str(x + 2) for x in i)} "
                           f"(format {shape(df.loc[i[0], 'TransactionDate'])})")
    out = out[out["BookingGuid"].notna()].copy()
    if out.empty:
        raise FileRejected("no rows have a Booking ID")
    out.attrs["date_order"] = order
    out["IsDecline"] = [is_decline(d, s) for d, s in zip(out["GatewayMessageDescription"], out["Status"])]
    out = out.astype(object).where(out.notna(), None)
    out["RowHash"] = [_row_hash(r) for r in out.to_dict("records")]
    out["_order"] = range(len(out))
    return out


_LATEST_COLS = ["BookingGuid", "TransactionDate", "GatewayId", "MerchantRefId", "CardType", "CardLast4", "CvvCheck",
                "Amount", "TransactionType", "EventOrganiser", "EventName", "GatewayMessage",
                "GatewayMessageDescription", "IsDecline", "Status", "FirstNameHash", "LastNameHash", "EmailHash",
                "PhoneHash", "WaitedInQueue", "WaitTime", "StartingPosition", "RowHash", "SourceFile",
                "FirstSeenAt", "UpdatedAt"]
_HISTORY_COLS = ["BookingGuid", "RowHash", "TransactionDate", "GatewayId", "MerchantRefId", "CardType", "CardLast4",
                 "CvvCheck", "Amount", "TransactionType", "GatewayMessage", "GatewayMessageDescription", "IsDecline",
                 "Status", "EmailHash", "PhoneHash", "SourceFile", "ImportedAt"]


def store(conn, df: pd.DataFrame, source_file: str) -> dict:
    """Write a parsed file. Call inside ``with conn:`` so it commits or rolls back as one.

    Latest row per Booking ID = newest Transaction Date (file order breaks ties).
    An older row never replaces a newer one already in the warehouse.
    """
    now = db.utcnow()
    df = df.copy()
    df["SourceFile"] = source_file
    df["FirstSeenAt"] = now
    df["UpdatedAt"] = now
    df["ImportedAt"] = now

    hist = [tuple(r[c] for c in _HISTORY_COLS) for r in df.to_dict("records")]
    before = conn.total_changes
    conn.executemany(f"INSERT INTO booking_transaction_history ({', '.join(_HISTORY_COLS)}) "
                     f"VALUES ({', '.join('?' * len(_HISTORY_COLS))}) ON CONFLICT (BookingGuid, RowHash) DO NOTHING",
                     hist)
    new_versions = conn.total_changes - before

    latest = (df.assign(_d=df["TransactionDate"].fillna(""))
                .sort_values(["BookingGuid", "_d", "_order"])
                .drop_duplicates("BookingGuid", keep="last"))
    upd = [c for c in _LATEST_COLS if c not in ("BookingGuid", "FirstSeenAt")]
    sql = (f"INSERT INTO booking_transactions ({', '.join(_LATEST_COLS)}) VALUES ({', '.join('?' * len(_LATEST_COLS))}) "
           f"ON CONFLICT (BookingGuid) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in upd)} "
           f"WHERE COALESCE(excluded.TransactionDate, '') >= COALESCE(booking_transactions.TransactionDate, '')")
    before = conn.total_changes
    conn.executemany(sql, [tuple(r[c] for c in _LATEST_COLS) for r in latest.to_dict("records")])
    changed = conn.total_changes - before
    return {"rows_read": len(df), "rows_written": changed, "new_versions": new_versions,
            "bookings": int(latest.shape[0]), "declines": int(sum(1 for v in df["IsDecline"] if v == 1))}


def prune(conn, cutoff: str) -> int:
    """Delete rows older than the retention cutoff (YYYY-MM-DD). Call inside ``with conn:``."""
    n = conn.execute("DELETE FROM booking_transactions WHERE TransactionDate < ?", (cutoff,)).rowcount
    n += conn.execute("DELETE FROM booking_transaction_history WHERE TransactionDate < ?", (cutoff,)).rowcount
    return n
