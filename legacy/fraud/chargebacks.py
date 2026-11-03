#!/usr/bin/env python3
"""
Chargeback (dispute) ingest for the fraud risk report.

Loads the two dispute exports into the warehouse ``chargebacks`` table:

- **PayPal** "All Dispute Cases" report (``AllDisputeCases_*.CSV``) — 3-line
  banner, then a header containing ``Case ID`` / ``Invoice ID``.
  Used columns: Case ID, Buyer Email Address, Final Case Outcome (falling back
  to Case status), Invoice ID, Case filing date, Case reason, Disputed amount,
  Disputed transaction ID.
- **Stripe** "Disputes export" (``Disputes_Export_*.csv``).
  Used columns: id, Description (the booking GUID without dashes), Customer
  Email, Status, Dispute Created (UTC), Reason, Dispute Amount, Charge ID,
  Payment Intent.

Stripe's ``Description`` is re-formatted to the PayPal Invoice ID layout
(8-4-4-4-12 lower-case GUID) so both sources share one ``InvoiceId`` that is
compared to the booking ID in the warehouse.

Loading is an idempotent upsert keyed on ``DisputeId`` (``PP:<Case ID>`` or
``ST:<du_…>``): unseen disputes are inserted, disputes whose status/outcome (or
other tracked fields) changed are updated and the change is logged to
``chargeback_history``, and identical rows are left alone. Re-running the same
file is therefore safe.

Each run then (re)matches any chargeback that has no booking yet to the
warehouse ``bookings`` table to fill AccountId (Event Organiser Account
Number), EventId and BookingTransactionId, and refreshes AccountTier from the
tier pipeline's snapshot.
"""

import csv
import hashlib
import io
import logging
import os
import re
import sqlite3
from datetime import datetime

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CHARGEBACK_COLUMNS = [
    "DisputeId",            # PP:<Case ID> | ST:<du_…>  (primary key)
    "Source",               # PayPal | Stripe
    "CustomerEmail",
    "DisputeDate",          # ISO 'YYYY-MM-DD HH:MM:SS'
    "InvoiceId",            # booking GUID, 8-4-4-4-12 lower case
    "DisputeStatus",        # normalised: Won | Lost | Refunded | Open | Closed
    "DisputeStatusRaw",     # as exported
    "DisputeReason",
    "IsFraudClaim",         # 1 if unauthorised / fraudulent reason
    "DisputeAmount",
    "GatewayRef",           # PayPal transaction ID / Stripe charge ID
    "PaymentIntent",        # Stripe only
    "AccountId",            # Event Organiser Account Number (from booking)
    "EventId",              # from booking
    "BookingTransactionId", # from booking
    "AccountTier",          # from tier snapshot
    "MatchMethod",          # which booking column matched (blank = unmatched)
    "SourceFile",
    "FirstLoadedAt",
    "LastUpdatedAt",
]

# Fields whose change counts as a "chargeback change" (logged to history).
TRACKED_FIELDS = [
    "CustomerEmail", "DisputeDate", "InvoiceId", "DisputeStatus",
    "DisputeStatusRaw", "DisputeReason", "DisputeAmount",
]

STATUS_WON, STATUS_LOST, STATUS_REFUNDED, STATUS_OPEN, STATUS_CLOSED = (
    "Won", "Lost", "Refunded", "Open", "Closed")


def ensure_tables(conn: sqlite3.Connection) -> None:
    cols = ", ".join(
        f'"{c}" TEXT PRIMARY KEY' if c == "DisputeId" else f'"{c}"'
        for c in CHARGEBACK_COLUMNS
    )
    conn.execute(f"CREATE TABLE IF NOT EXISTS chargebacks ({cols})")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_cb_account ON chargebacks(AccountId)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_cb_event ON chargebacks(EventId)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_cb_invoice ON chargebacks(InvoiceId)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chargeback_history ("
        "DisputeId TEXT, ChangedAt TEXT, Field TEXT, OldValue TEXT, NewValue TEXT, SourceFile TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chargeback_files ("
        "FileName TEXT, Sha256 TEXT, Source TEXT, LoadedAt TEXT, "
        "RowsRead INTEGER, RowsNew INTEGER, RowsChanged INTEGER, RowsUnchanged INTEGER)"
    )


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def format_guid(value) -> str:
    """Return a GUID as lower-case 8-4-4-4-12, or the trimmed input if it
    isn't a 32-hex-digit GUID (with or without dashes/braces)."""
    if value is None:
        return ""
    text = str(value).strip()
    compact = re.sub(r"[^0-9a-fA-F]", "", text).lower()
    if _HEX32.match(compact) and len(re.sub(r"[-{}\s]", "", text)) == 32:
        return f"{compact[:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:]}"
    return text


def guid_compact(value) -> str:
    """Comparison key: lower-case hex with dashes removed."""
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


def _iso(value, formats) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return text


def _money(value):
    text = str(value or "").replace(",", "").replace("£", "").strip()
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def normalise_paypal_status(final_outcome: str, case_status: str) -> str:
    o = (final_outcome or "").strip().lower()
    if o in ("win", "won"):
        return STATUS_WON
    if o in ("loss", "lost"):
        return STATUS_LOST
    if o == "refund":
        return STATUS_REFUNDED
    s = (case_status or "").strip().lower()
    if "closed" in s:
        return STATUS_CLOSED
    return STATUS_OPEN


def normalise_stripe_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "won":
        return STATUS_WON
    if s == "lost":
        return STATUS_LOST
    if s == "charge_refunded":
        return STATUS_REFUNDED
    if s == "warning_closed":
        return STATUS_CLOSED  # early-fraud inquiry closed without a chargeback
    return STATUS_OPEN        # needs_response, under_review, warning_*


def _is_fraud_reason(reason: str) -> int:
    r = (reason or "").lower()
    return int("fraud" in r or "unauthori" in r)


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def detect_source(text: str) -> str:
    """Return 'paypal' or 'stripe' from a file's header lines."""
    head = text[:5000].lower()
    if "case id" in head and "invoice id" in head:
        return "paypal"
    if "dispute created (utc)" in head and "description" in head:
        return "stripe"
    raise ValueError("Unrecognised dispute file: expected a PayPal 'All Dispute Cases' "
                     "report or a Stripe disputes export.")


def _dict_rows(text: str, header_marker: str):
    """Yield dict rows from CSV text, starting at the first line whose cells
    include ``header_marker`` (skips report banners)."""
    rows = list(csv.reader(io.StringIO(text)))
    for i, row in enumerate(rows):
        cells = [c.strip().lstrip("﻿") for c in row]
        if header_marker in cells:
            header = cells
            for r in rows[i + 1:]:
                if not r or all(not c.strip() for c in r):
                    continue
                yield {h: (r[j].strip() if j < len(r) else "") for j, h in enumerate(header)}
            return
    raise ValueError(f"Header row containing {header_marker!r} not found.")


def parse_paypal(text: str, source_file: str) -> list:
    out = []
    for r in _dict_rows(text, "Case ID"):
        case_id = r.get("Case ID", "")
        if not case_id:
            continue
        out.append({
            "DisputeId": f"PP:{case_id}",
            "Source": "PayPal",
            "CustomerEmail": r.get("Buyer Email Address", "").lower(),
            "DisputeDate": _iso(r.get("Case filing date"), ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"]),
            "InvoiceId": format_guid(r.get("Invoice ID")),
            "DisputeStatus": normalise_paypal_status(r.get("Final Case Outcome") or r.get("Outcome"),
                                                     r.get("Case status")),
            "DisputeStatusRaw": r.get("Final Case Outcome") or r.get("Case status", ""),
            "DisputeReason": r.get("Case reason", ""),
            "IsFraudClaim": _is_fraud_reason(r.get("Case reason", "") + " " + r.get("Case type", "")),
            "DisputeAmount": _money(r.get("Disputed amount")),
            "GatewayRef": r.get("Disputed transaction ID") or r.get("Transaction ID", ""),
            "PaymentIntent": "",
            "SourceFile": source_file,
        })
    return out


def parse_stripe(text: str, source_file: str) -> list:
    out = []
    for r in _dict_rows(text, "Dispute Created (UTC)"):
        did = r.get("id", "")
        if not did:
            continue
        out.append({
            "DisputeId": f"ST:{did}",
            "Source": "Stripe",
            "CustomerEmail": r.get("Customer Email", "").lower(),
            "DisputeDate": _iso(r.get("Dispute Created (UTC)"), ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]),
            "InvoiceId": format_guid(r.get("Description")),
            "DisputeStatus": normalise_stripe_status(r.get("Status")),
            "DisputeStatusRaw": r.get("Status", ""),
            "DisputeReason": r.get("Reason", ""),
            "IsFraudClaim": _is_fraud_reason(r.get("Reason", "")),
            "DisputeAmount": _money(r.get("Dispute Amount")),
            "GatewayRef": r.get("Charge ID", ""),
            "PaymentIntent": r.get("Payment Intent", ""),
            "SourceFile": source_file,
        })
    return out


def parse_file(path: str) -> tuple:
    """Return (source, rows) for a PayPal or Stripe dispute export."""
    text = _read_text(path)
    source = detect_source(text)
    name = os.path.basename(path)
    rows = parse_paypal(text, name) if source == "paypal" else parse_stripe(text, name)
    return source, rows


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def _same(a, b) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return str(a) == str(b)


def load_file(conn: sqlite3.Connection, path: str) -> dict:
    """Upsert one dispute export. Returns {source, read, new, changed, unchanged}."""
    ensure_tables(conn)
    source, rows = parse_file(path)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()

    new = changed = unchanged = 0
    with conn:
        for row in rows:
            existing = conn.execute(
                f"SELECT {','.join(TRACKED_FIELDS)} FROM chargebacks WHERE DisputeId=?",
                (row["DisputeId"],),
            ).fetchone()
            if existing is None:
                rec = dict(row, FirstLoadedAt=now, LastUpdatedAt=now)
                cols = [c for c in CHARGEBACK_COLUMNS if c in rec]
                conn.execute(
                    f"INSERT INTO chargebacks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [rec[c] for c in cols],
                )
                new += 1
                continue

            diffs = [(f, old, row[f]) for f, old in zip(TRACKED_FIELDS, existing)
                     if not _same(old, row[f])]
            if not diffs:
                unchanged += 1
                continue
            changed += 1
            for field, old, new_val in diffs:
                conn.execute(
                    "INSERT INTO chargeback_history VALUES (?,?,?,?,?,?)",
                    (row["DisputeId"], now, field, None if old is None else str(old),
                     None if new_val is None else str(new_val), row["SourceFile"]),
                )
            upd = {f: row[f] for f in TRACKED_FIELDS}
            upd.update(IsFraudClaim=row["IsFraudClaim"], GatewayRef=row["GatewayRef"],
                       PaymentIntent=row["PaymentIntent"], SourceFile=row["SourceFile"],
                       LastUpdatedAt=now)
            # An InvoiceId change invalidates the earlier booking match.
            if any(f == "InvoiceId" for f, _, _ in diffs):
                upd.update(AccountId=None, EventId=None, BookingTransactionId=None, MatchMethod=None)
            sets = ",".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE chargebacks SET {sets} WHERE DisputeId=?",
                         list(upd.values()) + [row["DisputeId"]])

        conn.execute(
            "INSERT INTO chargeback_files VALUES (?,?,?,?,?,?,?,?)",
            (os.path.basename(path), sha, source, now, len(rows), new, changed, unchanged),
        )

    stats = {"file": os.path.basename(path), "source": source, "read": len(rows),
             "new": new, "changed": changed, "unchanged": unchanged}
    log.info("chargebacks %(file)s (%(source)s): read=%(read)d new=%(new)d "
             "changed=%(changed)d unchanged=%(unchanged)d", stats)
    return stats


# ---------------------------------------------------------------------------
# Booking match + tier
# ---------------------------------------------------------------------------

# Booking columns tried, in order, for each key. The GUID is compared with
# dashes stripped and lower-cased on both sides, so either storage format hits.
_GUID_COLUMNS = ("BookingUrlId", "BookingId", "GatewayReference", "GatewayId")
_GATEWAY_COLUMNS = ("GatewayId", "GatewayReference")


def _bookings_columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(bookings)")}


def _lookup(conn, column: str, keys: list, compact: bool) -> dict:
    """Return {key: (BookingTransactionId, AccountId, EventId)} for bookings
    whose ``column`` equals one of ``keys``. Prefers a Successful row, then the
    latest TransactionDate, when several rows share a key."""
    if not keys:
        return {}
    if compact and column == "BookingUrlId":
        from modules.warehouse import BOOKING_URLID_NORM_EXPR
        expr = BOOKING_URLID_NORM_EXPR
    elif compact:
        expr = f"LOWER(REPLACE(CAST(\"{column}\" AS TEXT),'-',''))"
    else:
        expr = f"CAST(\"{column}\" AS TEXT)"
    conn.execute("DROP TABLE IF EXISTS temp._cb_keys")
    conn.execute("CREATE TEMP TABLE _cb_keys (k TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO temp._cb_keys VALUES (?)", [(k,) for k in keys])
    sql = (
        f"SELECT {expr} AS k, BookingTransactionId, AccountId, EventId, Status, TransactionDate "
        f"FROM bookings WHERE \"{column}\" IS NOT NULL AND {expr} IN (SELECT k FROM temp._cb_keys)"
    )
    best = {}
    for k, btid, aid, eid, status, tdate in conn.execute(sql):
        rank = (status == "Successful", tdate or "")
        if k not in best or rank > best[k][0]:
            best[k] = (rank, (btid, aid, eid))
    conn.execute("DROP TABLE IF EXISTS temp._cb_keys")
    return {k: v[1] for k, v in best.items()}


def match_to_bookings(conn: sqlite3.Connection, rematch: bool = False) -> dict:
    """Fill AccountId/EventId/BookingTransactionId for unmatched chargebacks."""
    ensure_tables(conn)
    where = "" if rematch else "WHERE AccountId IS NULL OR AccountId = ''"
    pending = conn.execute(
        f"SELECT DisputeId, InvoiceId, GatewayRef, PaymentIntent FROM chargebacks {where}"
    ).fetchall()
    if not pending:
        return {"pending": 0, "matched": 0}
    try:
        cols = _bookings_columns(conn)
    except sqlite3.OperationalError:
        cols = set()
    if not cols:
        log.warning("No bookings table in the warehouse — chargebacks left unmatched.")
        return {"pending": len(pending), "matched": 0}

    remaining = {d: (guid_compact(inv), gw or "", pi or "") for d, inv, gw, pi in pending}
    found = {}
    # 1) Invoice GUID -> booking ID columns.
    for col in _GUID_COLUMNS:
        if col not in cols or not remaining:
            continue
        keys = [g for g, _, _ in remaining.values() if len(g) == 32]
        hits = _lookup(conn, col, keys, compact=True)
        for d in list(remaining):
            g = remaining[d][0]
            if g in hits:
                found[d] = hits[g] + (f"InvoiceId={col}",)
                del remaining[d]
    # 2) Gateway reference fallback (PayPal txn ID / Stripe charge / intent).
    for col in _GATEWAY_COLUMNS:
        if col not in cols or not remaining:
            continue
        keys = [x for _, gw, pi in remaining.values() for x in (gw, pi) if x]
        hits = _lookup(conn, col, keys, compact=False)
        for d in list(remaining):
            _, gw, pi = remaining[d]
            hit = hits.get(gw) or hits.get(pi)
            if hit:
                found[d] = hit + (f"GatewayRef={col}",)
                del remaining[d]

    with conn:
        for d, (btid, aid, eid, method) in found.items():
            conn.execute(
                "UPDATE chargebacks SET BookingTransactionId=?, AccountId=?, EventId=?, MatchMethod=? "
                "WHERE DisputeId=?",
                (_int(btid), _int(aid), _int(eid), method, d),
            )
    log.info("chargeback match: pending=%d matched=%d unmatched=%d",
             len(pending), len(found), len(remaining))
    return {"pending": len(pending), "matched": len(found)}


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def load_tier_lookup() -> dict:
    """{AccountId(int): tier name} from the tier pipeline's latest snapshot."""
    try:
        from modules.tier_state import read_blob
        data = read_blob("tier_snapshot") or {}
    except Exception as e:  # tier_state.db absent off the Pi
        log.warning("Tier snapshot unavailable (%s) — AccountTier left blank.", e)
        return {}
    out = {}
    for aid, entry in (data.get("tiers") or {}).items():
        a = _int(aid)
        if a is not None and isinstance(entry, dict) and entry.get("tier"):
            out[a] = entry["tier"]
    return out


def refresh_tiers(conn: sqlite3.Connection, tiers: dict) -> int:
    """Set AccountTier on every matched chargeback (current tier)."""
    ensure_tables(conn)
    if not tiers:
        return 0
    n = 0
    with conn:
        for (aid,) in conn.execute(
                "SELECT DISTINCT AccountId FROM chargebacks WHERE AccountId IS NOT NULL").fetchall():
            tier = tiers.get(_int(aid))
            if tier:
                n += conn.execute("UPDATE chargebacks SET AccountTier=? WHERE AccountId=?",
                                  (tier, aid)).rowcount
    return n
