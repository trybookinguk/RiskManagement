"""Interim risk scoring for the nightly report (until phase 2's models.yaml).

Two scores, as in docs/architecture.md:
  Exposure score (0-100)  how much money is at stake
  Fraud score    (0-100)  how suspicious the activity looks, using only the
                          conditions that already have data:
                            N12 compressed timeline (account created -> event
                                created -> first session, each gap < 7 days)
                            Booking Outside UK (S3 IPCountry, last 100 bookings)
                            Declines (SharePoint Booking Transactions, last 30 days)
                          x M1 account-age multiplier
Review priority (P1-P4) comes from the matrix in architecture.md section 6.4.

Every number lives in CONFIG so it can be tuned; each run records
CONFIG["version"] next to the scores (risk_daily.ModelVersion).
"""
from datetime import date, datetime, timedelta

CONFIG = {
    "version": "interim-2026-11-05",
    "uk_values": ["GB", "GBR", "UK", "UKG", "UNITED KINGDOM", "GREAT BRITAIN", "ENGLAND", "SCOTLAND", "WALES",
                  "NORTHERN IRELAND"],
    "exposure_account": {      # points by band: [[at least £, points], ...] highest first
        "full_balance": [[50000, 30], [10000, 20], [2500, 10], [500, 5]],
        "upcoming_sales": [[50000, 25], [10000, 15], [2500, 8]],
        "largest_event": [[20000, 20], [5000, 10], [1000, 5]],
        "next_14_days": [[10000, 15], [2500, 8]],
        "bank_changed_90d": 10,
    },
    "exposure_event": {
        "held": [[20000, 60], [5000, 40], [1000, 20], [1, 5]],
        "days_to_session": [[3, 30], [7, 20], [14, 10]],     # [[within N days, points], ...]
    },
    "fraud": {
        "timeline": {"weight": 8, "min_gap_days": 7, "per_gap": [[1, 2.5], [3, 2.0], [7, 1.5]],
                     "upper_bound_factor": 0.5},
        "outside_uk_ip": {"weight": 6, "threshold_pct": 20, "sample": 100, "min_evidence": 5},
        "declines": {"weight": 5, "threshold_pct": 20, "days": 30, "min_evidence": 5},
        "age_multiplier": [[30, 1.5], [90, 1.3], [365, 1.15]],   # [[under N days, x], ...]
    },
    "bands": {"fraud": [[60, "Critical"], [40, "High"], [20, "Medium"]],
              "exposure": [[70, "Critical"], [45, "High"], [20, "Medium"]]},
    "priority": {   # fraud band -> [exposure Low, Medium, High, Critical]
        "Critical": ["P2", "P1", "P1", "P1"],
        "High": ["P3", "P2", "P1", "P1"],
        "Medium": ["P4", "P3", "P2", "P2"],
        "Low": ["-", "P4", "P3", "P3"],
    },
}

_EXPOSURE_ORDER = ["Low", "Medium", "High", "Critical"]


def _band_points(value, bands):
    v = value or 0
    for limit, pts in bands:
        if v >= limit:
            return pts
    return 0


def _band(score, bands):
    for limit, name in bands:
        if score >= limit:
            return name
    return "Low"


def _severity(value_pct, threshold, weight):
    if value_pct is None or value_pct < threshold:
        return 0.0
    return weight * min(value_pct / threshold, 2.0) / 2.0


def _days(a, b):
    """Whole days from date-string a to date-string b (b - a), or None."""
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b[:19]) - datetime.fromisoformat(a[:19])).total_seconds() / 86400.0
    except ValueError:
        return None


def age_multiplier(account_created, today: date, cfg=CONFIG):
    d = _days(account_created, today.isoformat())
    if d is None:
        return 1.0, None
    for limit, mult in cfg["fraud"]["age_multiplier"]:
        if d < limit:
            return mult, int(d)
    return 1.0, int(d)


def timeline_points(account_created, event_created, first_session, source, cfg=CONFIG):
    """N12: (points, [gap A, B, C in days])."""
    t = cfg["fraud"]["timeline"]
    gaps = [_days(account_created, event_created), _days(event_created, first_session),
            _days(account_created, first_session)]
    known = [g for g in gaps if g is not None and g >= 0]
    if len(known) < 3:
        return 0.0, gaps
    short = [g for g in known if g < t["min_gap_days"]]
    if len(short) == 3:
        pts = t["weight"]
    else:
        pts = sum(_band_points_less(g, t["per_gap"]) for g in short)
    if source == "first_booking":
        pts *= t["upper_bound_factor"]
    return min(pts, t["weight"]), gaps


def _band_points_less(value, bands):
    for limit, pts in bands:
        if value < limit:
            return pts
    return 0


def priority(fraud_score, exposure_score, cfg=CONFIG):
    fb = _band(fraud_score, cfg["bands"]["fraud"])
    eb = _band(exposure_score, cfg["bands"]["exposure"])
    return cfg["priority"][fb][_EXPOSURE_ORDER.index(eb)], fb, eb


def _money(v):
    return f"£{v:,.0f}"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _upcoming_events(conn, today: str):
    rows = conn.execute("""
        SELECT b.EventId, MAX(b.AccountId) AS AccountId,
               MIN(b.EventDate) AS NextSession,
               SUM(CASE WHEN COALESCE(b.PaymentReceived,0) > 0
                         AND LOWER(COALESCE(b.Status,'')) NOT LIKE '%refund%'
                         AND LOWER(COALESCE(b.TransactionType,'')) NOT LIKE '%refund%'
                        THEN b.PaymentReceived ELSE 0 END) AS Held,
               COUNT(*) AS FutureBookings
        FROM bookings b WHERE b.EventDate >= ? AND b.EventId IS NOT NULL
        GROUP BY b.EventId""", (today,)).fetchall()
    return {r["EventId"]: dict(r) for r in rows}


def _ip_share(conn, event_ids, uk, sample):
    if not event_ids:
        return {}
    conn.execute("DROP TABLE IF EXISTS temp._ev")
    conn.execute("CREATE TEMP TABLE _ev (EventId INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO temp._ev VALUES (?)", [(e,) for e in event_ids])
    marks = ",".join("?" * len(uk))
    rows = conn.execute(f"""
        SELECT EventId,
               SUM(CASE WHEN c <> '' AND c <> 'UNKNOWN' THEN 1 ELSE 0 END) AS known,
               SUM(CASE WHEN c <> '' AND c <> 'UNKNOWN' AND c NOT IN ({marks}) THEN 1 ELSE 0 END) AS foreign_n
        FROM (SELECT EventId, UPPER(TRIM(COALESCE(IPCountry,''))) AS c,
                     ROW_NUMBER() OVER (PARTITION BY EventId ORDER BY TransactionDate DESC) AS rn
              FROM bookings WHERE EventId IN (SELECT EventId FROM temp._ev))
        WHERE rn <= ? GROUP BY EventId""", (*uk, sample)).fetchall()
    return {r["EventId"]: (r["known"], r["foreign_n"]) for r in rows}


def transaction_event_map(conn):
    """Map SharePoint transaction rows to events: by Booking ID, else by unique
    (event name, organiser name). Returns SQL for a temp table _txn_event(BookingId, EventId, How)."""
    conn.execute("DROP TABLE IF EXISTS temp._txn_event")
    conn.execute("""
        CREATE TEMP TABLE _txn_event AS
        WITH ids AS (SELECT DISTINCT BookingId FROM booking_transaction_history),
             by_booking AS (SELECT BookingId, MAX(EventId) AS EventId FROM bookings
                            WHERE BookingId IN (SELECT BookingId FROM ids) GROUP BY BookingId),
             names AS (SELECT LOWER(TRIM(n.EventName)) AS ev, LOWER(TRIM(a.AccountName)) AS org,
                              MIN(n.EventId) AS EventId, COUNT(*) AS n
                       FROM event_names n JOIN accounts a ON a.AccountId = n.AccountId GROUP BY 1, 2),
             latest AS (SELECT BookingId, LOWER(TRIM(EventName)) AS ev, LOWER(TRIM(EventOrganiser)) AS org
                        FROM booking_transactions)
        SELECT i.BookingId,
               COALESCE(bb.EventId, CASE WHEN nm.n = 1 THEN nm.EventId END) AS EventId,
               CASE WHEN bb.EventId IS NOT NULL THEN 'booking_id'
                    WHEN nm.n = 1 THEN 'event_name' ELSE 'unmatched' END AS How
        FROM ids i
        LEFT JOIN by_booking bb ON bb.BookingId = i.BookingId
        LEFT JOIN latest l ON l.BookingId = i.BookingId
        LEFT JOIN names nm ON nm.ev = l.ev AND nm.org = l.org""")


def _declines(conn, since: str):
    transaction_event_map(conn)
    rows = conn.execute("""
        SELECT m.EventId, COUNT(*) AS attempts, SUM(CASE WHEN h.IsDecline = 1 THEN 1 ELSE 0 END) AS declines
        FROM booking_transaction_history h JOIN temp._txn_event m ON m.BookingId = h.BookingId
        WHERE m.EventId IS NOT NULL AND h.TransactionDate >= ?
        GROUP BY m.EventId""", (since,)).fetchall()
    return {r["EventId"]: (r["attempts"], r["declines"]) for r in rows}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score(conn, today: date, cfg=CONFIG) -> dict:
    """Score upcoming events and accounts. Returns {'events': [...], 'accounts': [...], 'snapshot': date}."""
    t = today.isoformat()
    f = cfg["fraud"]
    events = _upcoming_events(conn, t)
    meta = {r["EventId"]: dict(r) for r in conn.execute(
        "SELECT EventId, EventName, EventCreated, EventCreatedSource, FirstSessionDate FROM events")} if events else {}
    accounts = {r["AccountId"]: dict(r) for r in conn.execute(
        "SELECT a.AccountId, a.AccountName, a.DateTimeCreated, a.AccountStatus, a.TierSystem "
        "FROM accounts a")}
    ip = _ip_share(conn, list(events), cfg["uk_values"], f["outside_uk_ip"]["sample"])
    dec = _declines(conn, (today - timedelta(days=f["declines"]["days"])).isoformat())

    ev_out = []
    for eid, e in events.items():
        m = meta.get(eid, {})
        a = accounts.get(e["AccountId"], {})
        reasons, pts = [], 0.0
        tl, gaps = timeline_points(a.get("DateTimeCreated"), m.get("EventCreated"), m.get("FirstSessionDate"),
                                   m.get("EventCreatedSource"), cfg)
        if tl:
            pts += tl
            d = ["?" if x is None else f"{x:.0f}d" for x in gaps]
            reasons.append(f"short timeline: account→event {d[0]}, event→session {d[1]}, account→session {d[2]}")
        known, foreign = ip.get(eid, (0, 0))
        ip_pct = 100.0 * foreign / known if known >= f["outside_uk_ip"]["min_evidence"] else None
        s = _severity(ip_pct, f["outside_uk_ip"]["threshold_pct"], f["outside_uk_ip"]["weight"])
        if s:
            pts += s
            reasons.append(f"{ip_pct:.0f}% overseas IPs")
        att, dn = dec.get(eid, (0, 0))
        dec_pct = 100.0 * dn / att if att >= f["declines"]["min_evidence"] else None
        s = _severity(dec_pct, f["declines"]["threshold_pct"], f["declines"]["weight"])
        if s:
            pts += s
            reasons.append(f"{dec_pct:.0f}% declined ({dn}/{att}, {f['declines']['days']}d)")
        mult, age = age_multiplier(a.get("DateTimeCreated"), today, cfg)
        if mult > 1:
            reasons.append(f"new account ({age}d)")
        max_pts = f["timeline"]["weight"] + f["outside_uk_ip"]["weight"] + f["declines"]["weight"]
        fraud = round(min(100.0, 100.0 * pts / max_pts * mult), 1)
        days_to = _days(t, e["NextSession"])
        days_pts = 0
        for limit, p in cfg["exposure_event"]["days_to_session"]:
            if days_to is not None and days_to <= limit:
                days_pts = p
                break
        exposure = min(100, _band_points(e["Held"], cfg["exposure_event"]["held"]) + days_pts)
        pr, fb, eb = priority(fraud, exposure, cfg)
        ev_out.append({
            "EventId": eid, "EventName": m.get("EventName"), "AccountId": e["AccountId"],
            "AccountName": a.get("AccountName"), "Tier": a.get("TierSystem"), "NextSession": e["NextSession"],
            "DaysToSession": None if days_to is None else int(days_to), "Held": round(e["Held"] or 0, 2),
            "FutureBookings": e["FutureBookings"], "EventCreated": m.get("EventCreated"),
            "EventCreatedSource": m.get("EventCreatedSource"), "Gaps": gaps, "IpPct": ip_pct, "DeclinePct": dec_pct,
            "AgeDays": age, "ExposureScore": exposure, "FraudScore": fraud, "Priority": pr,
            "FraudBand": fb, "ExposureBand": eb, "Reasons": "; ".join(reasons) or "-",
        })

    # Accounts: latest balances snapshot per account (on/before today)
    snap = conn.execute("SELECT MAX(SnapshotDate) FROM balances WHERE SnapshotDate <= ?", (t,)).fetchone()[0]
    bal = {r["AccountId"]: dict(r) for r in conn.execute("""
        SELECT b.* FROM balances b JOIN (SELECT AccountId, MAX(SnapshotDate) d FROM balances
                                         WHERE SnapshotDate <= ? GROUP BY AccountId) x
          ON x.AccountId = b.AccountId AND x.d = b.SnapshotDate""", (t,))}
    bank_changed = {r[0] for r in conn.execute("""
        SELECT s.AccountId FROM account_snapshots s JOIN account_snapshots p
          ON p.AccountId = s.AccountId AND p.SnapshotDate < s.SnapshotDate
        WHERE s.SnapshotDate >= ? AND (COALESCE(s.BankAccountStatusId,-1) <> COALESCE(p.BankAccountStatusId,-1)
                                       OR COALESCE(s.BankAccountIsDeleted,-1) <> COALESCE(p.BankAccountIsDeleted,-1))
        GROUP BY s.AccountId""", ((today - timedelta(days=90)).isoformat(),))}
    by_acct = {}
    for e in ev_out:
        by_acct.setdefault(e["AccountId"], []).append(e)
    ea = cfg["exposure_account"]
    acc_out = []
    for aid in set(by_acct) | {a for a, b in bal.items() if (b.get("FullBalance") or 0) > 0
                               or (b.get("SalesForUpcomingEvents") or 0) > 0}:
        a = accounts.get(aid, {})
        b = bal.get(aid, {})
        evs = by_acct.get(aid, [])
        held = sum(e["Held"] for e in evs)
        largest = max((e["Held"] for e in evs), default=0)
        near = sum(e["Held"] for e in evs if e["DaysToSession"] is not None and e["DaysToSession"] <= 14)
        upcoming = b.get("SalesForUpcomingEvents") if b.get("SalesForUpcomingEvents") is not None else held
        exposure = (_band_points(b.get("FullBalance"), ea["full_balance"]) + _band_points(upcoming, ea["upcoming_sales"])
                    + _band_points(largest, ea["largest_event"]) + _band_points(near, ea["next_14_days"])
                    + (ea["bank_changed_90d"] if aid in bank_changed else 0))
        exposure = min(100, exposure)
        worst = max(evs, key=lambda e: e["FraudScore"], default=None)
        mult, age = age_multiplier(a.get("DateTimeCreated"), today, cfg)
        fraud = worst["FraudScore"] if worst else 0.0
        reasons = []
        if worst and worst["Reasons"] != "-":
            reasons.append(f"{worst['EventName'] or worst['EventId']}: {worst['Reasons']}")
        if aid in bank_changed:
            reasons.append("bank account changed (90d)")
        if not worst and mult > 1:
            reasons.append(f"new account ({age}d)")
        pr, fb, eb = priority(fraud, exposure, cfg)
        at_risk = max(b.get("FullBalance") or 0, upcoming or 0, held)
        acc_out.append({
            "AccountId": aid, "AccountName": a.get("AccountName"), "Tier": a.get("TierSystem"),
            "AccountCreated": a.get("DateTimeCreated"), "AgeDays": age, "FullBalance": b.get("FullBalance"),
            "SalesForUpcomingEvents": b.get("SalesForUpcomingEvents"), "Exposure": b.get("Exposure"),
            "UpcomingEvents": len(evs), "Held": round(held, 2), "LargestEvent": round(largest, 2),
            "Next14Days": round(near, 2), "AmountAtRisk": round(at_risk, 2), "ExposureScore": exposure,
            "FraudScore": fraud, "Priority": pr, "FraudBand": fb, "ExposureBand": eb,
            "NextSession": min((e["NextSession"] for e in evs), default=None),
            "Reasons": "; ".join(reasons) or "-",
        })
    order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3, "-": 4}
    key = lambda r: (order[r["Priority"]], -r["FraudScore"], -(r.get("AmountAtRisk") or r.get("Held") or 0))  # noqa
    ev_out.sort(key=key)
    acc_out.sort(key=key)
    return {"events": ev_out, "accounts": acc_out, "snapshot": snap, "version": cfg["version"]}


def save(conn, run_date: str, result: dict) -> int:
    rows = [(run_date, "event", e["EventId"], e["AccountId"], e["ExposureScore"], e["FraudScore"], e["Priority"],
             e["Held"], e["Reasons"], result["version"]) for e in result["events"]]
    rows += [(run_date, "account", a["AccountId"], a["AccountId"], a["ExposureScore"], a["FraudScore"], a["Priority"],
              a["AmountAtRisk"], a["Reasons"], result["version"]) for a in result["accounts"]]
    with conn:
        conn.execute("DELETE FROM risk_daily WHERE RunDate = ?", (run_date,))
        conn.executemany("INSERT INTO risk_daily VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)
