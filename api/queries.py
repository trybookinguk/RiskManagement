"""Read-only queries behind the dashboard pages and the JSON API.

Every function takes an open (read-only) connection and returns plain dicts and
lists, so the HTML pages and /api/... return exactly the same data. Nothing here
returns personal data: the warehouse only holds hashes, and pages show
"has email / has phone" flags and card last 4 at most.
"""
import re

from rm.s3 import canonical_guid

PRIORITIES = ["P1", "P2", "P3", "P4", "-"]
_PORDER = "CASE Priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 WHEN 'P3' THEN 2 WHEN 'P4' THEN 3 ELSE 4 END"
_REFUND = "LOWER(COALESCE(TransactionType,'')) LIKE '%refund%'"
_PAID = ("COALESCE(PaymentReceived,0) > 0 AND LOWER(COALESCE(Status,'')) NOT LIKE '%refund%' "
         f"AND NOT {_REFUND}")


def _rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _row(conn, sql, *args):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def _one(conn, sql, *args):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r else None


def _has_table(conn, name):
    return _one(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", name) is not None


# --------------------------------------------------------------------- risk ---

def runs(conn, limit=2):
    return [r[0] for r in conn.execute("SELECT DISTINCT RunDate FROM risk_daily ORDER BY RunDate DESC LIMIT ?",
                                       (limit,))]


def priority_counts(conn, run, level):
    if not run:
        return {}
    return {r[0]: r[1] for r in conn.execute(
        "SELECT Priority, COUNT(*) FROM risk_daily WHERE RunDate=? AND Level=? GROUP BY Priority", (run, level))}


def risk_list(conn, level="account", run=None, priority=None, limit=50, offset=0):
    """Scored accounts or events for a report run (default: the latest), most urgent first."""
    run = run or (runs(conn, 1) or [None])[0]
    if not run:
        return {"run": None, "rows": [], "total": 0}
    where, args = "r.RunDate=? AND r.Level=?", [run, level]
    if priority:
        where += " AND r.Priority=?"
        args.append(priority)
    name = ("a.AccountName" if level == "account" else "e.EventName")
    join = ("LEFT JOIN accounts a ON a.AccountId = r.Id" if level == "account" else
            "LEFT JOIN events e ON e.EventId = r.Id LEFT JOIN accounts a ON a.AccountId = r.AccountId")
    extra = "" if level == "account" else ", a.AccountName, e.FirstSessionDate"
    total = _one(conn, f"SELECT COUNT(*) FROM risk_daily r WHERE {where}", *args)
    rows = _rows(conn, f"""
        SELECT r.Id, r.AccountId, {name} AS Name{extra}, r.Priority, r.FraudScore, r.ExposureScore,
               r.AmountAtRisk, r.Reasons
        FROM risk_daily r {join} WHERE {where}
        ORDER BY {_PORDER.replace('Priority', 'r.Priority')}, r.FraudScore DESC, r.AmountAtRisk DESC
        LIMIT ? OFFSET ?""", *args, limit, offset)
    return {"run": run, "rows": rows, "total": total}


def score_history(conn, level, id_, limit=90):
    return list(reversed(_rows(conn, """SELECT RunDate, Priority, FraudScore, ExposureScore, AmountAtRisk, Reasons
                                        FROM risk_daily WHERE Level=? AND Id=? ORDER BY RunDate DESC LIMIT ?""",
                               level, id_, limit)))


# ------------------------------------------------------------------ account ---

def account(conn, aid, today):
    a = _row(conn, "SELECT * FROM accounts WHERE AccountId=?", aid)
    if not a:
        return None
    a.pop("UpdatedAt", None)
    out = {"account": a,
           "lifetime": _row(conn, "SELECT Events, Bookings, PaidBookings, Tickets, Gross, RefundedBookings, "
                                  "ForeignIpBookings, FirstBookingDate, LastBookingDate FROM account_lifetime "
                                  "WHERE AccountId=?", aid)}
    out["scores"] = score_history(conn, "account", aid)
    out["latest"] = out["scores"][-1] if out["scores"] else None
    out["balances"] = list(reversed(_rows(conn, """SELECT SnapshotDate, FullBalance, Balance, SalesForUpcomingEvents,
                                                         Exposure FROM balances WHERE AccountId=?
                                                  ORDER BY SnapshotDate DESC LIMIT 400""", aid)))
    out["balance"] = out["balances"][-1] if out["balances"] else None
    t = {"count": 0, "total": 0, "first": None, "last": None, "recent": []}
    if _has_table(conn, "bank_transfers"):
        t.update(_row(conn, """SELECT COUNT(*) AS count, COALESCE(SUM(Amount),0) AS total, MIN(DateCreated) AS first,
                                      MAX(DateCreated) AS last FROM bank_transfers WHERE AccountId=?""", aid))
        t["recent"] = _rows(conn, "SELECT DateCreated, Amount FROM bank_transfers WHERE AccountId=? "
                                  "ORDER BY DateCreated DESC LIMIT 20", aid)
        t["data_since"] = _one(conn, "SELECT MIN(DateCreated) FROM bank_transfers")
    out["transfers"] = t
    out["past"] = _row(conn, """SELECT COUNT(*) AS events, COALESCE(SUM(l.Gross),0) AS gross FROM events e
                                LEFT JOIN event_lifetime l ON l.EventId = e.EventId
                                WHERE e.AccountId=? AND e.LastSessionDate < ?""", aid, today)
    run = (runs(conn, 1) or [None])[0]
    out["events"] = _rows(conn, """
        SELECT e.EventId, e.EventName, e.EventCreated, e.EventCreatedSource, e.FirstSessionDate, e.LastSessionDate,
               l.Bookings, l.Gross, r.Priority, r.FraudScore, r.ExposureScore, r.AmountAtRisk
        FROM events e LEFT JOIN event_lifetime l ON l.EventId = e.EventId
        LEFT JOIN risk_daily r ON r.Level='event' AND r.Id = e.EventId AND r.RunDate = ?
        WHERE e.AccountId=? ORDER BY e.LastSessionDate DESC LIMIT 200""", run, aid)
    out["users"] = _rows(conn, "SELECT RoleName, IsDeleted, LastLoginDatetime FROM users WHERE AccountId=? "
                               "ORDER BY LastLoginDatetime DESC", aid)
    out["timeline"] = _gaps(a.get("DateTimeCreated"), a.get("FirstEventCreation"), a.get("LastEventCreation"),
                            min((e["FirstSessionDate"] for e in out["events"]
                                 if e["FirstSessionDate"] and e["FirstSessionDate"] >= today), default=None))
    return out


def _days(a, b):
    from datetime import datetime
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(str(b)[:19]) - datetime.fromisoformat(str(a)[:19])).total_seconds() / 86400
    except ValueError:
        return None


def _gaps(*dates):
    labels = ["Account created", "First event created", "Last event created", "Next event date"]
    out, prev = [], None
    for label, d in zip(labels, dates):
        out.append({"label": label, "date": d, "gap": None if prev is None or not d else _days(prev, d)})
        prev = d or prev
    return out


# -------------------------------------------------------------------- event ---

def event(conn, eid):
    e = _row(conn, "SELECT * FROM events WHERE EventId=?", eid)
    if not e:
        return None
    e.pop("UpdatedAt", None)
    a = _row(conn, "SELECT AccountId, AccountName, DateTimeCreated, TierSystem, AccountStatus FROM accounts "
                   "WHERE AccountId=?", e["AccountId"]) or {}
    out = {"event": e, "account": a,
           "lifetime": _row(conn, "SELECT Bookings, PaidBookings, Tickets, Gross, RefundedBookings, ForeignIpBookings "
                                  "FROM event_lifetime WHERE EventId=?", eid)}
    out["scores"] = score_history(conn, "event", eid)
    out["latest"] = out["scores"][-1] if out["scores"] else None
    out["timeline"] = _gaps(a.get("DateTimeCreated"), e.get("EventCreated"), None, e.get("FirstSessionDate"))
    out["timeline"] = [g for g in out["timeline"] if g["label"] != "Last event created"]
    s = _row(conn, f"""
        WITH paid AS (SELECT TransactionDate, PaymentReceived, TicketQuantity FROM bookings
                      WHERE EventId=? AND {_PAID}),
             f AS (SELECT MIN(TransactionDate) AS first FROM paid)
        SELECT f.first AS FirstSale, COUNT(*) AS PaidBookings, SUM(p.PaymentReceived) AS Gross,
               SUM(COALESCE(p.TicketQuantity,0)) AS Tickets,
               SUM(CASE WHEN p.TransactionDate < datetime(f.first, '+24 hours') THEN p.PaymentReceived ELSE 0 END)
                   AS FirstDay
        FROM paid p, f""", eid) or {}
    s["AvgTicket"] = (s["Gross"] / s["Tickets"]) if s.get("Tickets") else None
    out["sales"] = s
    out["ip_countries"] = _rows(conn, """SELECT COALESCE(NULLIF(TRIM(IPCountry),''),'(blank)') AS Country, COUNT(*) AS n
                                         FROM bookings WHERE EventId=? GROUP BY 1 ORDER BY n DESC LIMIT 10""", eid)
    out["bookings"] = _rows(conn, """SELECT BookingTransactionId, BookingId, BookingUrlId, TransactionDate, EventDate,
                                            PaymentReceived, TicketQuantity, Status, TransactionType, PaymentType,
                                            IPCountry, GatewayName
                                     FROM bookings WHERE EventId=? ORDER BY TransactionDate DESC LIMIT 100""", eid)
    out["attempts"] = _rows(conn, f"""
        SELECT COALESCE(h.Status,'(blank)') AS Status, COALESCE(h.GatewayMessageDescription,'(blank)') AS Message,
               CASE h.IsDecline WHEN 1 THEN 'decline' WHEN 0 THEN 'approved' ELSE 'unclassified' END AS Class,
               COUNT(*) AS n,
               SUM(CASE WHEN h.PhoneHash IS NULL THEN 1 ELSE 0 END) AS NoPhone
        FROM booking_transaction_history h
        WHERE NOT ({_REFUND.replace('TransactionType', 'h.TransactionType')})
          AND h.BookingGuid IN (SELECT BookingUrlId FROM bookings WHERE EventId=? AND BookingUrlId IS NOT NULL)
        GROUP BY 1, 2, 3 ORDER BY n DESC""", eid)
    return out


# ------------------------------------------------------------------ booking ---

def booking(conn, ref):
    """By booking GUID, BookingTransactionId or BookingId."""
    ref = str(ref).strip()
    g = canonical_guid(ref)
    if g and re.fullmatch(r"[0-9a-f-]{36}", g):
        rows = _rows(conn, "SELECT * FROM bookings WHERE BookingUrlId=? ORDER BY TransactionDate", g)
        guid = g
    elif ref.isdigit():
        rows = _rows(conn, "SELECT * FROM bookings WHERE BookingTransactionId=? OR BookingId=? ORDER BY TransactionDate",
                     int(ref), int(ref))
        guid = next((r["BookingUrlId"] for r in rows if r.get("BookingUrlId")), None)
    else:
        return None
    for r in rows:
        r.pop("IPHash", None)
    attempts = []
    if guid:
        attempts = _rows(conn, """SELECT TransactionDate, TransactionType, Amount, Status, GatewayMessageDescription,
                                         IsDecline, CardType, CardLast4, GatewayId, SourceFile, Versions,
                                         CASE WHEN EmailHash IS NOT NULL THEN 1 ELSE 0 END AS HasEmail,
                                         CASE WHEN PhoneHash IS NOT NULL THEN 1 ELSE 0 END AS HasPhone
                                  FROM booking_transaction_history WHERE BookingGuid=? ORDER BY TransactionDate""",
                         guid)
    ids = [r["BookingTransactionId"] for r in rows]
    recon = []
    if ids or guid:
        marks = ",".join("?" * len(ids)) or "NULL"
        recon = _rows(conn, f"""SELECT TransactionDate, TxnType, Amount, CardType, CardLast4, GatewayMessage, Provider,
                                       PayoutId, ArrivalDate FROM gateway_recon
                                WHERE BookingTransactionId IN ({marks}) OR BookingUrlId=? ORDER BY TransactionDate""",
                      *ids, guid)
    if not rows and not attempts:
        return None
    first = rows[0] if rows else {}
    ev = _row(conn, "SELECT EventId, EventName, AccountId FROM events WHERE EventId=?", first.get("EventId")) or {}
    return {"guid": guid, "bookings": rows, "attempts": attempts, "recon": recon, "event": ev}


# ------------------------------------------------------------------- search ---

_GUID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
_CARD = re.compile(r"^(visa|mastercard|master card|mc|amex|american express|discover|maestro|paypal|card)\s*(\d{4})$",
                   re.I)
_PHONE = re.compile(r"^\+?[\d ()-]{10,}$")
_GATEWAY = re.compile(r"^(ch|pi|py|pm|re|txn|tr|po)_[A-Za-z0-9]+$|^[A-Z0-9]{12,20}$")


def classify(q: str) -> str:
    q = q.strip()
    if _GUID.match(q):
        return "guid"
    if "@" in q:
        return "email"
    if _CARD.match(q):
        return "card"
    digits = re.sub(r"\D", "", q)
    if _PHONE.match(q) and (q.startswith(("+", "0")) or " " in q) and len(digits) >= 10:
        return "phone"
    if q.isdigit():
        return "number"
    if _GATEWAY.match(q):
        return "gateway"
    return "text"


def _booking_hits(conn, where, *args, limit=50):
    rows = _rows(conn, f"""SELECT t.BookingGuid, t.TransactionDate, t.Amount, t.Status, t.EventName, t.EventOrganiser,
                                  t.CardType, t.CardLast4
                           FROM booking_transactions t WHERE {where} ORDER BY t.TransactionDate DESC LIMIT ?""",
                   *args, limit)
    return [{"type": "booking", "id": r["BookingGuid"], "url": f"/bookings/{r['BookingGuid']}",
             "label": f"{r['EventName'] or '?'} ({r['EventOrganiser'] or '?'})",
             "detail": f"{(r['TransactionDate'] or '')[:16]}  £{r['Amount'] or 0:,.2f}  {r['Status'] or ''}  "
                       f"{r['CardType'] or ''} {r['CardLast4'] or ''}".strip()} for r in rows]


def search(conn, q, hasher=None, limit=50):
    """{'kind', 'results': [{type, id, label, detail, url}]}. Email and phone are hashed
    here and never returned or logged."""
    q = (q or "").strip()
    kind = classify(q) if q else "empty"
    res = []
    if kind == "empty":
        return {"kind": kind, "results": []}
    if kind == "guid":
        g = canonical_guid(q)
        b = booking(conn, g)
        if b:
            label = (b["event"] or {}).get("EventName") or "booking"
            res.append({"type": "booking", "id": g, "url": f"/bookings/{g}", "label": label,
                        "detail": f"{len(b['bookings'])} S3 row(s), {len(b['attempts'])} payment attempt(s)"})
    elif kind in ("email", "phone"):
        if hasher is None:
            return {"kind": kind, "results": [], "error": "hashing is not available (passphrase not set)"}
        h = hasher.email(q) if kind == "email" else hasher.phone(q)
        col = "EmailHash" if kind == "email" else "PhoneHash"
        res += _booking_hits(conn, f"t.{col}=?", h, limit=limit)
        if kind == "email":
            for r in _rows(conn, """SELECT u.AccountId, a.AccountName, u.RoleName FROM users u
                                    LEFT JOIN accounts a ON a.AccountId = u.AccountId WHERE u.UsernameHash=?""", h):
                res.insert(0, {"type": "account", "id": r["AccountId"], "url": f"/accounts/{r['AccountId']}",
                               "label": r["AccountName"] or str(r["AccountId"]),
                               "detail": f"user of this account ({r['RoleName'] or 'role ?'})"})
    elif kind == "card":
        m = _CARD.match(q)
        brand, last4 = m.group(1).lower(), m.group(2)
        brand_sql = "" if brand == "card" else " AND LOWER(REPLACE(t.CardType,' ','')) LIKE ?"
        args = [last4] + ([] if brand == "card" else [{"mc": "master", "americanexpress": "amex"}.get(
            brand.replace(" ", ""), brand.replace(" ", "")) + "%"])
        res += _booking_hits(conn, "t.CardLast4=?" + brand_sql, *args, limit=limit)
    elif kind == "number":
        n = int(q)
        a = _row(conn, "SELECT AccountId, AccountName, AccountStatus FROM accounts WHERE AccountId=?", n)
        if a:
            res.append({"type": "account", "id": n, "url": f"/accounts/{n}", "label": a["AccountName"] or str(n),
                        "detail": f"account {n} - {a['AccountStatus'] or ''}"})
        e = _row(conn, "SELECT e.EventId, e.EventName, e.FirstSessionDate, a.AccountName FROM events e "
                       "LEFT JOIN accounts a ON a.AccountId = e.AccountId WHERE e.EventId=?", n)
        if e:
            res.append({"type": "event", "id": n, "url": f"/events/{n}", "label": e["EventName"] or str(n),
                        "detail": f"event {n} - {e['AccountName'] or ''}, first session {(e['FirstSessionDate'] or '')[:10]}"})
        for b in _rows(conn, "SELECT BookingTransactionId, BookingId, BookingUrlId, TransactionDate, PaymentReceived "
                             "FROM bookings WHERE BookingTransactionId=? OR BookingId=? LIMIT 5", n, n):
            ref = b["BookingUrlId"] or b["BookingTransactionId"]
            res.append({"type": "booking", "id": ref, "url": f"/bookings/{ref}",
                        "label": f"booking {b['BookingId']} / transaction {b['BookingTransactionId']}",
                        "detail": f"{(b['TransactionDate'] or '')[:16]}  £{b['PaymentReceived'] or 0:,.2f}"})
    elif kind == "gateway":
        for b in _rows(conn, "SELECT BookingUrlId, BookingTransactionId, TransactionDate, PaymentReceived FROM bookings "
                             "WHERE GatewayId=? OR GatewayReference=? LIMIT 20", q, q):
            ref = b["BookingUrlId"] or b["BookingTransactionId"]
            res.append({"type": "booking", "id": ref, "url": f"/bookings/{ref}",
                        "label": f"transaction {b['BookingTransactionId']}",
                        "detail": f"{(b['TransactionDate'] or '')[:16]}  £{b['PaymentReceived'] or 0:,.2f}"})
        if not res:
            res += _booking_hits(conn, "t.GatewayId=?", q, limit=limit)
    if kind in ("text", "gateway") and not res:
        if len(q) < 3:
            return {"kind": kind, "results": [], "error": "type at least 3 characters"}
        like = f"%{q}%"
        for a in _rows(conn, "SELECT AccountId, AccountName, AccountStatus, DateTimeCreated FROM accounts "
                             "WHERE AccountName LIKE ? ORDER BY DateTimeCreated DESC LIMIT ?", like, limit):
            res.append({"type": "account", "id": a["AccountId"], "url": f"/accounts/{a['AccountId']}",
                        "label": a["AccountName"], "detail": f"account {a['AccountId']} - created "
                                                              f"{(a['DateTimeCreated'] or '')[:10]}"})
        for e in _rows(conn, "SELECT e.EventId, e.EventName, e.FirstSessionDate, a.AccountName FROM events e "
                             "LEFT JOIN accounts a ON a.AccountId = e.AccountId WHERE e.EventName LIKE ? "
                             "ORDER BY e.FirstSessionDate DESC LIMIT ?", like, limit):
            res.append({"type": "event", "id": e["EventId"], "url": f"/events/{e['EventId']}",
                        "label": e["EventName"], "detail": f"event {e['EventId']} - {e['AccountName'] or ''}, "
                                                           f"first session {(e['FirstSessionDate'] or '')[:10]}"})
    return {"kind": kind, "results": res[:limit * 2]}


# ------------------------------------------------------------------ imports ---

def imports(conn, limit=60):
    last = _rows(conn, """SELECT l.source, l.ref, l.status, l.started_at, l.finished_at, l.rows_read, l.rows_written,
                                 l.message
                          FROM import_log l JOIN (SELECT source, MAX(id) AS id FROM import_log GROUP BY source) x
                            ON x.id = l.id ORDER BY l.source""")
    recent = _rows(conn, """SELECT source, ref, status, started_at, finished_at, rows_read, rows_written, message
                            FROM import_log ORDER BY id DESC LIMIT ?""", limit)
    failed = _rows(conn, """SELECT source, ref, started_at, message FROM import_log
                            WHERE status='failed' ORDER BY id DESC LIMIT 20""")
    return {"last": last, "recent": recent, "failed": failed}


def health(conn):
    return {"schema": _one(conn, "PRAGMA user_version"), "latest_run": (runs(conn, 1) or [None])[0],
            "last_import": _one(conn, "SELECT MAX(started_at) FROM import_log WHERE status='ok'")}
