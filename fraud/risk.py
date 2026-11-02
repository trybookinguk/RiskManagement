#!/usr/bin/env python3
"""
Fraud risk engine: per-account and per-event risk tests, chargeback roll-up,
exposure, and a 0-100 risk score, written to the warehouse ``risk_fraud`` table.

The scoring logic is documented for non-technical readers in
``docs/fraud_risk_report.md`` — keep the two in step.

Data sources (all in the SQLite warehouse unless noted)
-------------------------------------------------------
- ``bookings``         : S3 BookingData/BookingDataAll (IPCountry, Status,
                         PaymentType, BookingCountryCode, GatewayId, BookingUrlId…).
                         Which S3 file/column feeds each test is mapped in
                         docs/fraud_risk_report.md (from the S3 inventory).
- ``accounts``         : S3 Accounts snapshot (name, status, created)
- ``chargebacks``      : PayPal + Stripe disputes (fraud/chargebacks.py)
- ``risk_exposure``    : S3 RiskReport snapshot (balances) — written by the
                         report script each run
- ``booking_signals``  : OPTIONAL per-booking signals that S3 does not carry
                         (Stripe risk score, card issue country, billing/booking
                         address country, browser time zone, email delivery
                         failure, quicksale flag). Loaded from a CSV.
- ``account_logins``   : OPTIONAL login history with IP country. Loaded from a CSV.

A test whose data is missing is reported as "No data" and scores 0 — it never
counts as a pass or a fail.
"""

import csv
import io
import logging
import math
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (mirrors the TryBooking admin "Risk analysis" settings)
# ---------------------------------------------------------------------------

SAMPLE_LOGINS = 10      # "No. of Logins"
SAMPLE_BOOKINGS = 100   # "No. of Bookings"
MIN_BOOKINGS_EVALUATED = 5   # fewer usable rows than this -> "Insufficient"
MIN_LOGINS_EVALUATED = 3

# key, name, description, threshold, active, weight (score points), scope
# Thresholds are % of the evaluated sample (with a 100-booking sample this is
# the same as a count), except Average Risk Score which is the mean score.
# Weights of the ACTIVE tests sum to 40 = the maximum test component.
RISK_TESTS = [
    dict(key="account_logins", name="Account Logins Test",
         description="A test that will check an account's last number of logins and cross "
                     "reference the IP address whether it is located in UK.",
         threshold=10, active=True, weight=6, scope="logins"),
    dict(key="avg_risk_score", name="Average Risk Score",
         description="The average Stripe Risk score of credit card payments",
         threshold=20, active=True, weight=6, scope="bookings"),
    dict(key="browser_timezone", name="Booking Browser Time Zone Test",
         description="A test that will match the booking address with the browser's "
                     "timezone setting.",
         threshold=10, active=True, weight=4, scope="bookings"),
    dict(key="card_location", name="Booking Credit Card Location Test",
         description="A test that will match the address of the booking against the "
                     "address of where the credit card was issued.",
         threshold=10, active=True, weight=5, scope="bookings"),
    dict(key="card_outside_uk", name="Booking Credit Card Outside UK Test",
         description="A test that will count the number of bookings done outside of UK",
         threshold=20, active=True, weight=5, scope="bookings"),
    dict(key="failed_email", name="Booking Failed Email Delivery Test",
         description="A test that will count the number of failed email deliveries for a "
                     "specific event.",
         threshold=10, active=True, weight=3, scope="bookings"),
    dict(key="failed_transactions", name="Booking Failed Transactions Test",
         description="A test that will count the number of failed booking gateway "
                     "transactions.",
         threshold=20, active=True, weight=5, scope="bookings"),
    dict(key="ip_location", name="Booking IP Location Test",
         description="A test that will check the last number of booking IP address "
                     "locations and match it with the booking address.",
         threshold=20, active=False, weight=5, scope="bookings"),
    dict(key="outside_uk", name="Booking Outside UK Test",
         description="A test that will check the last number of booking IP address "
                     "locations outside of UK and match it with the booking address.",
         threshold=20, active=True, weight=6, scope="bookings"),
    dict(key="quicksale", name="Quicksale Test",
         description="A test that will count the number of bookings which are quicksale",
         threshold=50, active=False, weight=3, scope="bookings"),
]
TEST_BY_KEY = {t["key"]: t for t in RISK_TESTS}

MAX_TEST_POINTS = 40
MAX_CHARGEBACK_POINTS = 40
MAX_EXPOSURE_POINTS = 20

# Chargeback points
CB_POINTS_LOST = 12          # per lost (or refunded) dispute
CB_POINTS_OPEN = 6           # per open dispute
CB_POINTS_WON = 1            # per won dispute (usually "didn't recognise it")
CB_POINTS_FRAUD_LOST = 4     # extra per lost/refunded dispute with a fraud reason
CB_RATIO_BANDS = [(1.0, 10), (0.5, 5)]   # dispute ratio % -> points
CB_RATIO_MIN_BOOKINGS = 20   # don't score the ratio below this many paid bookings

# Exposure points (amount at risk, GBP)
EXPOSURE_BANDS = [(50000, 20), (10000, 15), (2500, 10), (500, 5), (0.01, 2)]
EVENT_AT_RISK_GRACE_DAYS = 30   # event sales stay "at risk" this long after the event

RISK_BANDS = [(60, "Critical"), (40, "High"), (20, "Medium"), (0, "Low")]

# ---------------------------------------------------------------------------
# Country / time-zone helpers
# ---------------------------------------------------------------------------

UK = "GB"
_UK_ALIASES = {
    # "UKG" is TryBooking's own UK code (seen in the Nov 2026 live run, 447 rows).
    "GB", "GBR", "UK", "UKG", "UNITED KINGDOM", "GREAT BRITAIN", "BRITAIN", "ENGLAND",
    "SCOTLAND", "WALES", "NORTHERN IRELAND",
    "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND",
}
_UNKNOWN = {"", "-", "--", "UNKNOWN", "N/A", "NA", "NONE", "NULL", "XX", "ZZ", "NAN"}

# Raw values seen that could not be resolved to an ISO2 code (reported so the
# country mapping can be extended when S3 formats change).
UNRECOGNISED_COUNTRIES = {}


def norm_country(value):
    """Normalise a country (ISO2 / ISO3 / name) to ISO2 upper case, UK -> 'GB'.
    Returns None for blank/unknown; an unresolvable value is returned as-is
    (upper case) and counted in UNRECOGNISED_COUNTRIES."""
    from fraud.countries import ISO2_CODES, ISO3_TO_ISO2, NAME_TO_ISO2
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    s = re.sub(r"\s+", " ", str(value).strip().upper())
    if s in _UNKNOWN:
        return None
    if s in _UK_ALIASES:
        return UK
    if len(s) == 2 and s in ISO2_CODES:
        return s
    if len(s) == 3 and s in ISO3_TO_ISO2:
        return ISO3_TO_ISO2[s]
    if s in NAME_TO_ISO2:
        return NAME_TO_ISO2[s]
    if s.startswith("THE ") and s[4:] in NAME_TO_ISO2:
        return NAME_TO_ISO2[s[4:]]
    # ISO writes "KOREA, REPUBLIC OF"; feeds may say "REPUBLIC OF KOREA" (and vice versa).
    if ", " in s:
        a, b = s.split(", ", 1)
        if f"{b} {a}" in NAME_TO_ISO2:
            return NAME_TO_ISO2[f"{b} {a}"]
    for lead in ("REPUBLIC OF ", "DEMOCRATIC REPUBLIC OF ", "ISLAMIC REPUBLIC OF ", "UNITED REPUBLIC OF "):
        if s.startswith(lead) and f"{s[len(lead):]}, {lead.strip()}" in NAME_TO_ISO2:
            return NAME_TO_ISO2[f"{s[len(lead):]}, {lead.strip()}"]
    UNRECOGNISED_COUNTRIES[s] = UNRECOGNISED_COUNTRIES.get(s, 0) + 1
    return s


_GB_TZ = {"EUROPE/LONDON", "EUROPE/BELFAST", "GB", "GB-EIRE", "GMT", "BST", "UTC",
          "ETC/GMT", "ETC/UTC", "ETC/GMT+0", "ETC/GMT-0", "ETC/GMT0", "GMT0", "UCT",
          "ETC/UCT", "ZULU", "EUROPE/JERSEY", "EUROPE/GUERNSEY", "EUROPE/ISLE_OF_MAN"}
_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])?(\d{1,2})(?::?(\d{2}))?$")


def tz_matches_country(tz, country):
    """True/False whether a browser time zone is plausible for a country, or
    None if it can't be judged. Accepts IANA names (Europe/London) and UTC
    offsets ('+01:00', 'GMT+1', or JS getTimezoneOffset minutes like '-60')."""
    if tz is None or country is None:
        return None
    s = str(tz).strip()
    if not s:
        return None
    up = s.upper()
    if country == UK and up in _GB_TZ:
        return True
    # Pure integer = JS getTimezoneOffset() minutes (UTC - local).
    if re.fullmatch(r"[+-]?\d{1,4}", s) and abs(int(s)) > 14:
        minutes = -int(s)
    else:
        m = _OFFSET_RE.match(up.replace(" ", ""))
        if m:
            sign = -1 if m.group(1) == "-" else 1
            minutes = sign * (int(m.group(2)) * 60 + int(m.group(3) or 0))
        else:
            minutes = None
    if minutes is not None:
        if country == UK:
            return minutes in (0, 60)
        return None  # offsets can't be pinned to a non-UK country reliably
    try:
        import pytz
        zones = {z.upper() for z in pytz.country_timezones.get(country, [])}
    except Exception:
        zones = set()
    if not zones:
        return None
    return up in zones


_TRUTHY = {"1", "TRUE", "YES", "Y", "T", "FAILED", "FAIL", "BOUNCED", "BOUNCE"}
_FALSY = {"0", "FALSE", "NO", "N", "F", "DELIVERED", "SENT", "OK", "SUCCESS"}


def _flag(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).strip().upper()
    if s in _TRUTHY:
        return 1
    if s in _FALSY:
        return 0
    return None


def _num(v):
    try:
        f = float(str(v).replace(",", "").replace("£", "").strip())
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _compact(v):
    return re.sub(r"[^0-9a-z]", "", str(v or "").lower())


# ---------------------------------------------------------------------------
# Optional supplementary inputs
# ---------------------------------------------------------------------------

SIGNAL_FIELDS = ["RiskScore", "CardCountry", "BookingCountry", "BrowserTimeZone",
                 "EmailDeliveryFailed", "IsQuicksale"]
_SIGNAL_ALIASES = {
    "BookingTransactionId": ["bookingtransactionid", "booking transaction id"],
    "BookingRef": ["bookingurlid", "booking url id", "booking id", "bookingid", "invoice id",
                   "booking guid", "description"],
    "GatewayId": ["gatewayid", "gateway id", "charge id", "id", "payment id"],
    "PaymentIntent": ["paymentintent id", "payment intent", "paymentintent", "payment_intent"],
    "RiskScore": ["riskscore", "risk score", "risk_score", "outcome risk score",
                  "radar risk score", "stripe risk score"],
    "CardCountry": ["cardcountry", "card country", "card issue country",
                    "card issuing country", "card_country", "issuing country"],
    "BookingCountry": ["bookingcountry", "booking country", "booking address country",
                       "card address country", "billing country", "billing address country",
                       "address country"],
    "BrowserTimeZone": ["browsertimezone", "browser time zone", "browser timezone",
                        "timezone", "time zone"],
    "EmailDeliveryFailed": ["emaildeliveryfailed", "email delivery failed", "email failed",
                            "email bounced", "bounced", "email delivery status"],
    "IsQuicksale": ["isquicksale", "quicksale", "is quicksale", "quick sale"],
}
_LOGIN_ALIASES = {
    "AccountId": ["accountid", "account id", "account"],
    "LoginDateTime": ["logindatetime", "login date time", "login datetime", "login date",
                      "login time", "logindate", "datetime", "timestamp", "date"],
    "IPAddress": ["ipaddress", "ip address", "ip"],
    "IPCountry": ["ipcountry", "ip country", "country"],
}


def _read_csv_dicts(path):
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return reader.fieldnames or [], list(reader)


def _map_headers(fieldnames, aliases):
    low = {h.strip().lower(): h for h in fieldnames if h}
    out = {}
    for field, names in aliases.items():
        for n in names:
            if n in low:
                out[field] = low[n]
                break
    return out


def ensure_supplementary_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS booking_signals (Key TEXT PRIMARY KEY, KeyType TEXT, "
        + ", ".join(f'"{f}"' for f in SIGNAL_FIELDS) + ", SourceFile TEXT, LoadedAt TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS account_logins (AccountId INTEGER, LoginDateTime TEXT, "
        "IPAddress TEXT, IPCountry TEXT, SourceFile TEXT, "
        "PRIMARY KEY (AccountId, LoginDateTime, IPAddress))")


def load_signals_file(conn, path) -> dict:
    """Upsert a per-booking signals CSV (e.g. a Stripe payments export, or a
    TryBooking extract). Each row is keyed by every booking identifier present
    (BookingTransactionId, booking GUID, gateway charge / payment-intent ID) so
    it can be joined to whichever one the warehouse booking carries."""
    ensure_supplementary_tables(conn)
    fieldnames, rows = _read_csv_dicts(path)
    cols = _map_headers(fieldnames, _SIGNAL_ALIASES)
    if not any(f in cols for f in SIGNAL_FIELDS):
        raise ValueError(f"{os.path.basename(path)}: no recognised signal columns "
                         f"({', '.join(SIGNAL_FIELDS)}).")
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    name = os.path.basename(path)
    keyed = 0
    with conn:
        for r in rows:
            vals = {f: (r.get(cols[f]) or "").strip() if f in cols else None for f in SIGNAL_FIELDS}
            vals["RiskScore"] = _num(vals["RiskScore"]) if vals["RiskScore"] else None
            for f in ("EmailDeliveryFailed", "IsQuicksale"):
                vals[f] = _flag(vals[f]) if vals[f] not in (None, "") else None
            for f in ("CardCountry", "BookingCountry"):
                vals[f] = norm_country(vals[f]) if vals[f] else None
            vals["BrowserTimeZone"] = vals["BrowserTimeZone"] or None

            keys = []
            if "BookingTransactionId" in cols and r.get(cols["BookingTransactionId"]):
                bt = _num(r[cols["BookingTransactionId"]])
                if bt is not None:
                    keys.append(("btid", str(int(bt))))
            if "BookingRef" in cols and r.get(cols["BookingRef"]):
                ref = _compact(r[cols["BookingRef"]])
                if ref:
                    keys.append(("ref", ref))
            for f in ("GatewayId", "PaymentIntent"):
                if f in cols and r.get(cols[f]):
                    v = r[cols[f]].strip()
                    # A bare "id" column only counts when it looks like a gateway ref.
                    if cols[f].strip().lower() == "id" and not re.match(r"^(ch|py|pi|txn)_", v):
                        continue
                    keys.append(("gw", v))
            for ktype, k in keys:
                conn.execute(
                    "INSERT INTO booking_signals (Key, KeyType, " + ",".join(SIGNAL_FIELDS)
                    + ", SourceFile, LoadedAt) VALUES (?,?," + ",".join("?" * len(SIGNAL_FIELDS))
                    + ",?,?) ON CONFLICT(Key) DO UPDATE SET "
                    + ",".join(f'"{f}"=COALESCE(excluded."{f}", "{f}")' for f in SIGNAL_FIELDS)
                    + ", SourceFile=excluded.SourceFile, LoadedAt=excluded.LoadedAt",
                    [f"{ktype}:{k}", ktype] + [vals[f] for f in SIGNAL_FIELDS] + [name, now],
                )
                keyed += 1
    stats = {"file": name, "rows": len(rows), "keys": keyed,
             "fields": [f for f in SIGNAL_FIELDS if f in cols]}
    log.info("signals %(file)s: rows=%(rows)d keys=%(keys)d fields=%(fields)s", stats)
    return stats


def load_logins_file(conn, path) -> dict:
    """Upsert an account login history CSV (AccountId, LoginDateTime,
    IPAddress, IPCountry)."""
    ensure_supplementary_tables(conn)
    fieldnames, rows = _read_csv_dicts(path)
    cols = _map_headers(fieldnames, _LOGIN_ALIASES)
    missing = {"AccountId", "LoginDateTime"} - set(cols)
    if missing:
        raise ValueError(f"{os.path.basename(path)}: missing login columns {sorted(missing)}.")
    name = os.path.basename(path)
    n = 0
    with conn:
        for r in rows:
            aid = _num(r.get(cols["AccountId"]))
            when = (r.get(cols["LoginDateTime"]) or "").strip()
            if aid is None or not when:
                continue
            iso = re.match(r"^\d{4}-\d{2}-\d{2}", when) is not None
            try:
                when = pd.to_datetime(when, dayfirst=not iso).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                pass
            ip = (r.get(cols["IPAddress"]) or "").strip() if "IPAddress" in cols else ""
            ctry = norm_country(r.get(cols["IPCountry"])) if "IPCountry" in cols else None
            conn.execute("INSERT OR REPLACE INTO account_logins VALUES (?,?,?,?,?)",
                         (int(aid), when, ip, ctry, name))
            n += 1
    log.info("logins %s: rows=%d", name, n)
    return {"file": name, "rows": n}


def _load_signal_index(conn):
    try:
        df = pd.read_sql_query("SELECT * FROM booking_signals", conn)
    except Exception:
        return {}
    return {row["Key"]: row for row in df.to_dict("records")}


# ---------------------------------------------------------------------------
# Scope, aggregates and samples
# ---------------------------------------------------------------------------

def _cols(conn, table):
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _temp_ids(conn, name, ids):
    conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
    conn.execute(f"CREATE TEMP TABLE {name} (id INTEGER PRIMARY KEY)")
    conn.executemany(f"INSERT OR IGNORE INTO temp.{name} VALUES (?)",
                     [(int(i),) for i in ids if i is not None and not pd.isna(i)])


_SAMPLE_COLS = ["BookingTransactionId", "AccountId", "EventId", "TransactionDate", "Status",
                "PaymentType", "IPCountry", "IPAddress", "BookingCountryCode", "GatewayId",
                "BookingUrlId", "PaymentReceived"]


def _select_list(have):
    return ", ".join(f'"{c}"' if c in have else f'NULL AS "{c}"' for c in _SAMPLE_COLS)


def scoped_events(conn, as_of: date, lookback_days: int, extra_event_ids=()):
    """Events with any session on/after (as_of - lookback_days), plus extras."""
    cutoff = (as_of - timedelta(days=lookback_days)).isoformat()
    ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT EventId FROM bookings WHERE EventId IS NOT NULL AND EventDate >= ?",
        (cutoff,))}
    ids |= {int(e) for e in extra_event_ids if e is not None and not pd.isna(e)}
    return sorted(ids)


def event_aggregates(conn, event_ids, as_of: date) -> pd.DataFrame:
    _temp_ids(conn, "_scope_events", event_ids)
    have = _cols(conn, "bookings")
    name_expr = "MAX(EventName)" if "EventName" in have else "NULL"
    df = pd.read_sql_query(
        f"SELECT EventId, MAX(AccountId) AS AccountId, {name_expr} AS EventName, "
        "MIN(EventDate) AS FirstEventDate, MAX(EventDate) AS LastEventDate, "
        "COUNT(*) AS Bookings, "
        "SUM(CASE WHEN Status='Successful' THEN 1 ELSE 0 END) AS SuccessfulBookings, "
        "SUM(CASE WHEN Status='Successful' AND PaymentReceived>0 THEN 1 ELSE 0 END) AS PaidBookings, "
        "COALESCE(SUM(CASE WHEN Status='Successful' THEN TicketQuantity END),0) AS Tickets, "
        "ROUND(COALESCE(SUM(CASE WHEN Status='Successful' THEN PaymentReceived END),0),2) AS GrossSales "
        "FROM bookings WHERE EventId IN (SELECT id FROM temp._scope_events) GROUP BY EventId",
        conn)
    grace = (as_of - timedelta(days=EVENT_AT_RISK_GRACE_DAYS)).isoformat()
    df["AmountAtRisk"] = df.apply(
        lambda r: r["GrossSales"] if str(r["LastEventDate"] or "") >= grace else 0.0, axis=1)
    return df


def account_aggregates(conn, account_ids, as_of: date) -> pd.DataFrame:
    _temp_ids(conn, "_scope_accounts", account_ids)
    c365 = (as_of - timedelta(days=365)).isoformat()
    today = as_of.isoformat()
    df = pd.read_sql_query(
        "SELECT AccountId, "
        "SUM(CASE WHEN TransactionDate>=? AND Status='Successful' AND PaymentReceived>0 THEN 1 ELSE 0 END) AS PaidBookings365, "
        "ROUND(COALESCE(SUM(CASE WHEN TransactionDate>=? AND Status='Successful' THEN PaymentReceived END),0),2) AS GrossSales365, "
        "ROUND(COALESCE(SUM(CASE WHEN EventDate>=? AND Status='Successful' THEN PaymentReceived END),0),2) AS UpcomingSales, "
        "COUNT(DISTINCT CASE WHEN EventDate>=? THEN EventId END) AS UpcomingEvents "
        # NOT INDEXED: the scoped accounts cover most bookings, so one sequential
        # scan beats millions of random AccountId-index lookups (10 min -> ~1-2 min
        # on the Pi's disk). Same result either way.
        "FROM bookings NOT INDEXED "
        "WHERE AccountId IN (SELECT id FROM temp._scope_accounts) GROUP BY AccountId",
        conn, params=(c365, c365, today, today))
    acc_cols = _cols(conn, "accounts")
    if {"Id", "AccountName"} <= acc_cols:
        sel = ["Id AS AccountId", "AccountName"]
        sel.append("AccountStatus" if "AccountStatus" in acc_cols else "NULL AS AccountStatus")
        sel.append("DateTimeCreated AS AccountCreated" if "DateTimeCreated" in acc_cols
                   else "NULL AS AccountCreated")
        accts = pd.read_sql_query(
            f"SELECT {', '.join(sel)} FROM accounts WHERE Id IN (SELECT id FROM temp._scope_accounts)",
            conn)
    else:
        accts = pd.DataFrame(columns=["AccountId", "AccountName", "AccountStatus", "AccountCreated"])
    base = pd.DataFrame({"AccountId": sorted({int(a) for a in account_ids})})
    for frame in (accts, df):
        frame["AccountId"] = pd.to_numeric(frame["AccountId"], errors="coerce").astype("Int64")
    return base.astype({"AccountId": "Int64"}).merge(accts, on="AccountId", how="left") \
               .merge(df, on="AccountId", how="left")


def booking_samples(conn, level: str, ids, as_of: date, lookback_days: int,
                    n: int = SAMPLE_BOOKINGS) -> pd.DataFrame:
    """Last ``n`` bookings per event (level='event') or per account
    (level='account', restricted to the lookback window)."""
    have = _cols(conn, "bookings")
    part = "EventId" if level == "event" else "AccountId"
    tmp = "_sample_ids"
    _temp_ids(conn, tmp, ids)
    where = f"{part} IN (SELECT id FROM temp.{tmp})"
    params = ()
    if level == "account":
        where += " AND TransactionDate >= ?"
        params = ((as_of - timedelta(days=lookback_days)).isoformat(),)
    sql = (
        f"SELECT * FROM (SELECT {_select_list(have)}, ROW_NUMBER() OVER ("
        f"PARTITION BY {part} ORDER BY TransactionDate DESC, BookingTransactionId DESC) AS rn "
        f"FROM bookings WHERE {where}) WHERE rn <= ?"
    )
    return pd.read_sql_query(sql, conn, params=params + (n,))


def login_samples(conn, account_ids, n: int = SAMPLE_LOGINS) -> pd.DataFrame:
    if "AccountId" not in _cols(conn, "account_logins"):
        return pd.DataFrame(columns=["AccountId", "LoginDateTime", "IPCountry"])
    _temp_ids(conn, "_login_ids", account_ids)
    return pd.read_sql_query(
        "SELECT * FROM (SELECT AccountId, LoginDateTime, IPCountry, ROW_NUMBER() OVER ("
        "PARTITION BY AccountId ORDER BY LoginDateTime DESC) AS rn FROM account_logins "
        "WHERE AccountId IN (SELECT id FROM temp._login_ids)) WHERE rn <= ?",
        conn, params=(n,))


# ---------------------------------------------------------------------------
# Test evaluation
# ---------------------------------------------------------------------------

def _signal_for(row, sig_index):
    if not sig_index:
        return {}
    keys = []
    if row.get("BookingTransactionId") is not None and not pd.isna(row["BookingTransactionId"]):
        keys.append(f"btid:{int(row['BookingTransactionId'])}")
    if row.get("GatewayId"):
        keys.append(f"gw:{str(row['GatewayId']).strip()}")
    if row.get("BookingUrlId") is not None and not pd.isna(row["BookingUrlId"]):
        keys.append(f"ref:{_compact(row['BookingUrlId'])}")
    merged = {}
    for k in keys:
        s = sig_index.get(k)
        if s:
            for f in SIGNAL_FIELDS:
                if merged.get(f) is None and s.get(f) is not None and not pd.isna(s.get(f)):
                    merged[f] = s[f]
    return merged


def _row_flags(row, sig):
    """Return {test_key: hit(0/1) or None if not evaluable} plus risk score."""
    ip = norm_country(row.get("IPCountry"))
    booking_ctry = (norm_country(sig.get("BookingCountry"))
                    or norm_country(row.get("BookingCountryCode")) or UK)
    card = norm_country(sig.get("CardCountry"))
    status = str(row.get("Status") or "").strip().lower()
    ptype = str(row.get("PaymentType") or "").upper()
    tzm = tz_matches_country(sig.get("BrowserTimeZone"), booking_ctry)
    quick = sig.get("IsQuicksale")
    return {
        "outside_uk": None if ip is None else int(ip != UK),
        "ip_location": None if ip is None else int(ip != booking_ctry),
        "failed_transactions": int(status == "failed"),
        "card_outside_uk": None if card is None else int(card != UK),
        "card_location": None if card is None else int(card != booking_ctry),
        "browser_timezone": None if tzm is None else int(not tzm),
        "failed_email": None if sig.get("EmailDeliveryFailed") is None
                        else int(sig["EmailDeliveryFailed"]),
        "quicksale": int(bool(quick) or "QUICK" in ptype.replace(" ", "")),
    }


def _result(value, n, threshold, min_n):
    if n == 0:
        return "No data"
    if n < min_n:
        return "Insufficient"
    return "Fail" if value >= threshold else "Pass"


def evaluate_booking_tests(sample: pd.DataFrame, group_col: str, sig_index) -> dict:
    """{group_id: {test_key: (value, n, result)}} for booking-scoped tests."""
    out = {}
    if sample.empty:
        return out
    for gid, grp in sample.groupby(group_col):
        hits = {t["key"]: [0, 0] for t in RISK_TESTS if t["scope"] == "bookings"}
        scores = []
        for row in grp.to_dict("records"):
            sig = _signal_for(row, sig_index)
            for k, v in _row_flags(row, sig).items():
                if v is not None:
                    hits[k][0] += v
                    hits[k][1] += 1
            rs = _num(sig.get("RiskScore"))
            if rs is not None:
                scores.append(rs)
        res = {}
        for k, (h, n) in hits.items():
            if k == "avg_risk_score":
                continue
            val = round(100.0 * h / n, 1) if n else None
            res[k] = (val, n, _result(val, n, TEST_BY_KEY[k]["threshold"], MIN_BOOKINGS_EVALUATED))
        n = len(scores)
        val = round(sum(scores) / n, 1) if n else None
        res["avg_risk_score"] = (val, n, _result(val, n, TEST_BY_KEY["avg_risk_score"]["threshold"],
                                                 MIN_BOOKINGS_EVALUATED))
        out[int(gid)] = res
    return out


def evaluate_login_tests(logins: pd.DataFrame) -> dict:
    out = {}
    if logins.empty:
        return out
    thr = TEST_BY_KEY["account_logins"]["threshold"]
    for aid, grp in logins.groupby("AccountId"):
        ctry = [norm_country(c) for c in grp["IPCountry"]]
        known = [c for c in ctry if c is not None]
        n = len(known)
        val = round(100.0 * sum(c != UK for c in known) / n, 1) if n else None
        out[int(aid)] = {"account_logins": (val, n, _result(val, n, thr, MIN_LOGINS_EVALUATED))}
    return out


# ---------------------------------------------------------------------------
# Chargebacks
# ---------------------------------------------------------------------------

def chargeback_frame(conn) -> pd.DataFrame:
    if "DisputeId" not in _cols(conn, "chargebacks"):
        return pd.DataFrame(columns=["DisputeId", "AccountId", "EventId", "DisputeStatus",
                                     "IsFraudClaim", "DisputeAmount", "DisputeDate"])
    df = pd.read_sql_query("SELECT * FROM chargebacks", conn)
    for c in ("AccountId", "EventId", "BookingTransactionId"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    df["DisputeAmount"] = pd.to_numeric(df["DisputeAmount"], errors="coerce").fillna(0.0)
    df["IsFraudClaim"] = pd.to_numeric(df["IsFraudClaim"], errors="coerce").fillna(0).astype(int)
    return df


def chargeback_rollup(cb: pd.DataFrame, key: str, as_of: date) -> pd.DataFrame:
    cols = ["Chargebacks", "CB_Won", "CB_Lost", "CB_Refunded", "CB_Open", "CB_Closed",
            "CB_FraudClaims", "CB_FraudLost", "CB_Amount", "CB_Last365", "LastChargebackDate"]
    d = cb[cb[key].notna()].copy()
    if d.empty:
        return pd.DataFrame(columns=[key] + cols)
    c365 = (as_of - timedelta(days=365)).isoformat()
    lostish = d["DisputeStatus"].isin(["Lost", "Refunded"])
    d["_won"] = (d["DisputeStatus"] == "Won").astype(int)
    d["_lost"] = (d["DisputeStatus"] == "Lost").astype(int)
    d["_ref"] = (d["DisputeStatus"] == "Refunded").astype(int)
    d["_open"] = (d["DisputeStatus"] == "Open").astype(int)
    d["_closed"] = (d["DisputeStatus"] == "Closed").astype(int)
    d["_fraudlost"] = (lostish & (d["IsFraudClaim"] == 1)).astype(int)
    d["_365"] = (d["DisputeDate"].fillna("") >= c365).astype(int)
    g = d.groupby(key).agg(
        Chargebacks=("DisputeId", "count"), CB_Won=("_won", "sum"), CB_Lost=("_lost", "sum"),
        CB_Refunded=("_ref", "sum"), CB_Open=("_open", "sum"), CB_Closed=("_closed", "sum"),
        CB_FraudClaims=("IsFraudClaim", "sum"), CB_FraudLost=("_fraudlost", "sum"),
        CB_Amount=("DisputeAmount", "sum"), CB_Last365=("_365", "sum"),
        LastChargebackDate=("DisputeDate", "max"),
    ).reset_index()
    g["CB_Amount"] = g["CB_Amount"].round(2)
    return g


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_points(results: dict) -> tuple:
    """(points, failed_test_names, evaluated_count, active_count)."""
    pts = 0.0
    failed, evaluated = [], 0
    active = [t for t in RISK_TESTS if t["active"]]
    for t in active:
        val, n, res = results.get(t["key"], (None, 0, "No data"))
        if res in ("Pass", "Fail"):
            evaluated += 1
        if res == "Fail":
            thr = t["threshold"] or 1
            severity = min(val / thr, 2.0) / 2.0
            pts += t["weight"] * severity
            failed.append(t["name"])
    return round(min(pts, MAX_TEST_POINTS), 1), failed, evaluated, len(active)


def chargeback_points(row, paid_bookings, disputes_for_ratio) -> tuple:
    lostish = (row.get("CB_Lost") or 0) + (row.get("CB_Refunded") or 0)
    pts = (CB_POINTS_LOST * lostish + CB_POINTS_OPEN * (row.get("CB_Open") or 0)
           + CB_POINTS_WON * (row.get("CB_Won") or 0)
           + CB_POINTS_FRAUD_LOST * (row.get("CB_FraudLost") or 0))
    ratio = None
    if paid_bookings and paid_bookings >= CB_RATIO_MIN_BOOKINGS:
        ratio = round(100.0 * (disputes_for_ratio or 0) / paid_bookings, 3)
        for limit, p in CB_RATIO_BANDS:
            if ratio >= limit:
                pts += p
                break
    return round(min(pts, MAX_CHARGEBACK_POINTS), 1), ratio


def exposure_points(amount) -> int:
    a = amount or 0
    for limit, p in EXPOSURE_BANDS:
        if a >= limit:
            return p
    return 0


def risk_band(score) -> str:
    for limit, band in RISK_BANDS:
        if score >= limit:
            return band
    return "Low"


def _apply_tests(frame, id_col, booking_res, login_res, login_key_col="AccountId"):
    for t in RISK_TESTS:
        k = t["key"]
        vals, ns, rs = [], [], []
        for _, r in frame.iterrows():
            src = login_res.get(int(r[login_key_col]), {}) if t["scope"] == "logins" and pd.notna(r[login_key_col]) \
                else booking_res.get(int(r[id_col]), {}) if pd.notna(r[id_col]) else {}
            v, n, res = src.get(k, (None, 0, "No data"))
            vals.append(v); ns.append(n); rs.append(res)
        frame[f"{k}__value"] = vals
        frame[f"{k}__n"] = ns
        frame[f"{k}__result"] = rs
    return frame


def _score_frame(frame, paid_col, ratio_disputes_col, risk_amount_col):
    tp, cp, ep, ratio, failed, cov = [], [], [], [], [], []
    for _, r in frame.iterrows():
        res = {t["key"]: (r[f"{t['key']}__value"], r[f"{t['key']}__n"], r[f"{t['key']}__result"])
               for t in RISK_TESTS}
        p, f, ev, act = test_points(res)
        tp.append(p); failed.append(", ".join(f)); cov.append(f"{ev}/{act}")
        c, rt = chargeback_points(r, _num(r.get(paid_col)), _num(r.get(ratio_disputes_col)))
        cp.append(c); ratio.append(rt)
        ep.append(exposure_points(_num(r.get(risk_amount_col))))
    frame["TestPoints"] = tp
    frame["ChargebackPoints"] = cp
    frame["ExposurePoints"] = ep
    frame["CB_RatioPct"] = ratio
    frame["FailedTests"] = failed
    frame["TestsEvaluated"] = cov
    frame["RiskScore"] = (frame["TestPoints"] + frame["ChargebackPoints"] + frame["ExposurePoints"]).round(1)
    frame["RiskBand"] = frame["RiskScore"].apply(risk_band)
    return frame


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_risk_fraud(conn, as_of: date, risk_report: pd.DataFrame = None,
                     tiers: dict = None, event_lookback_days: int = 90,
                     booking_lookback_days: int = 365) -> tuple:
    """Compute account- and event-level risk. Returns (accounts_df, events_df)."""
    tiers = tiers or {}
    cb = chargeback_frame(conn)

    event_ids = scoped_events(conn, as_of, event_lookback_days, cb["EventId"].dropna().tolist())
    events = event_aggregates(conn, event_ids, as_of)
    log.info("Scoped %d events (session on/after %s, or with chargebacks)",
             len(events), as_of - timedelta(days=event_lookback_days))

    acct_ids = set(pd.to_numeric(events["AccountId"], errors="coerce").dropna().astype(int))
    acct_ids |= set(cb["AccountId"].dropna().astype(int))
    rr = None
    if risk_report is not None and not risk_report.empty:
        rr = risk_report.copy()
        rr["AccountId"] = pd.to_numeric(rr["AccountId"], errors="coerce").astype("Int64")
        rr = rr[rr["AccountId"].notna()].drop_duplicates("AccountId", keep="last")
        def _col(name):
            return (pd.to_numeric(rr[name], errors="coerce").fillna(0) if name in rr
                    else pd.Series(0, index=rr.index))
        active = rr[(_col("FullBalance") != 0) | (_col("SalesForUpcomingEvents") != 0)]
        acct_ids |= set(active["AccountId"].astype(int))
    accounts = account_aggregates(conn, acct_ids, as_of)
    log.info("Scoped %d accounts", len(accounts))

    sig_index = _load_signal_index(conn)
    ev_sample = booking_samples(conn, "event", event_ids, as_of, booking_lookback_days)
    ac_sample = booking_samples(conn, "account", list(acct_ids), as_of, booking_lookback_days)
    ev_res = evaluate_booking_tests(ev_sample, "EventId", sig_index)
    ac_res = evaluate_booking_tests(ac_sample, "AccountId", sig_index)
    login_res = evaluate_login_tests(login_samples(conn, list(acct_ids)))

    # ---- accounts
    if rr is not None:
        keep = [c for c in ["AccountId", "AccountName", "FullBalance", "Balance",
                            "SalesForUpcomingEvents", "Exposure", "FutureDays", "LastEventDate"]
                if c in rr.columns]
        rr = rr[keep].rename(columns={"AccountName": "RR_AccountName",
                                      "LastEventDate": "RR_LastEventDate"})
        accounts = accounts.merge(rr, on="AccountId", how="left")
        if "RR_AccountName" in accounts:
            accounts["AccountName"] = accounts["AccountName"].fillna(accounts["RR_AccountName"])
            accounts = accounts.drop(columns=["RR_AccountName"])
    for c in ["FullBalance", "Balance", "SalesForUpcomingEvents", "Exposure"]:
        if c not in accounts:
            accounts[c] = None
        accounts[c] = pd.to_numeric(accounts[c], errors="coerce")
    accounts["AmountAtRisk"] = pd.concat([
        accounts["FullBalance"], accounts["SalesForUpcomingEvents"],
        accounts["Exposure"].abs(), pd.to_numeric(accounts["UpcomingSales"], errors="coerce"),
    ], axis=1).max(axis=1).fillna(0).round(2)
    accounts = accounts.merge(chargeback_rollup(cb, "AccountId", as_of), on="AccountId", how="left")
    accounts["AccountTier"] = accounts["AccountId"].map(lambda a: tiers.get(int(a), "") if pd.notna(a) else "")
    accounts = _apply_tests(accounts, "AccountId", ac_res, login_res)
    accounts = _score_frame(accounts, "PaidBookings365", "CB_Last365", "AmountAtRisk")
    accounts.insert(0, "Level", "Account")

    # ---- events
    events["AccountId"] = pd.to_numeric(events["AccountId"], errors="coerce").astype("Int64")
    events["EventId"] = pd.to_numeric(events["EventId"], errors="coerce").astype("Int64")
    events = events.merge(chargeback_rollup(cb, "EventId", as_of), on="EventId", how="left")
    events = events.merge(accounts[["AccountId", "AccountName", "AccountTier"]],
                          on="AccountId", how="left")
    events = _apply_tests(events, "EventId", ev_res, login_res)
    events = _score_frame(events, "PaidBookings", "Chargebacks", "AmountAtRisk")
    events.insert(0, "Level", "Event")

    cb_cols = [c for c in accounts.columns if c.startswith("CB_") or c == "Chargebacks"]
    for f in (accounts, events):
        for c in cb_cols:
            if c in f and c not in ("CB_RatioPct", "LastChargebackDate"):
                f[c] = pd.to_numeric(f[c], errors="coerce").fillna(0)
    accounts = accounts.sort_values(["RiskScore", "Chargebacks", "AmountAtRisk"], ascending=False)
    events = events.sort_values(["RiskScore", "Chargebacks", "AmountAtRisk"], ascending=False)
    return accounts.reset_index(drop=True), events.reset_index(drop=True)


def write_risk_fraud(conn, accounts: pd.DataFrame, events: pd.DataFrame, as_of: date) -> int:
    """Replace ``risk_fraud`` with this run and append scores to ``risk_fraud_history``."""
    run = as_of.isoformat()
    both = pd.concat([accounts, events], ignore_index=True, sort=False)
    both.insert(0, "RunDate", run)
    with conn:
        both.to_sql("risk_fraud", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_rf_account ON risk_fraud(AccountId)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_rf_event ON risk_fraud(EventId)")
        conn.execute("CREATE TABLE IF NOT EXISTS risk_fraud_history (RunDate TEXT, Level TEXT, "
                     "AccountId INTEGER, EventId INTEGER, RiskScore REAL, RiskBand TEXT, "
                     "Chargebacks INTEGER, FailedTests TEXT)")
        conn.execute("DELETE FROM risk_fraud_history WHERE RunDate=?", (run,))
        hist = both[["RunDate", "Level", "AccountId", "EventId", "RiskScore", "RiskBand",
                     "Chargebacks", "FailedTests"]]
        hist.to_sql("risk_fraud_history", conn, if_exists="append", index=False)
    return len(both)
