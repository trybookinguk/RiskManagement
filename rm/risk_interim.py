"""Interim risk scoring for the nightly report (until phase 2's models.yaml).

Two scores, as in docs/architecture.md:
  Exposure score (0-100)  how much money is at stake
  Fraud score    (0-100)  how suspicious the activity looks, using only the
                          conditions that already have data:
                            N12 compressed timeline (account created -> event
                                created -> first session, each gap < 7 days)
                            Booking Outside UK (S3 IPCountry, last 100 bookings)
                            Declines (SharePoint Booking Transactions, last 30 days)
                            Night-time account creation (new accounts)
                            Imminent event (new event, first session days away)
                            High ticket price (average paid per ticket)
                            Immediate sales (value sold in the first 24h of sales)
                            No phone numbers (SharePoint bookings without a phone)
                            Duplicate event (same name, same dates, same account)
                          x M1 account-age multiplier
                          x trust: bank transfers (payouts) received, past events
                            and their value. No transfers raises the score; over
                            5 transfers and a history of events lowers it.
Review priority (P1-P4) comes from the matrix in architecture.md section 6.4.

Every number lives in CONFIG so it can be tuned; each run records
CONFIG["version"] next to the scores (risk_daily.ModelVersion).
"""
import re
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
        "days_min_held": 250,     # session-date points only when at least this much is held
    },
    "fraud": {
        "timeline": {"weight": 8, "min_gap_days": 7, "per_gap": [[1, 2.5], [3, 2.0], [7, 1.5]],
                     "upper_bound_factor": 0.5},
        "outside_uk_ip": {"weight": 6, "threshold_pct": 20, "sample": 100, "min_evidence": 5},
        "declines": {"weight": 5, "threshold_pct": 20, "days": 30, "min_evidence": 5},
        "night_created": {"weight": 3, "from_hour": 0, "to_hour": 5,      # created 00:00-04:59 UK time
                          "max_account_age_days": 90, "times_are": "Europe/London"},   # or "UTC"
        "imminent_event": {"weight": 5, "max_event_age_days": 14,         # event created in the last 14 days
                           "days_to_session": [[2, 1.0], [4, 0.7], [7, 0.4]]},   # [[within N days, share of weight]]
        "high_ticket_price": {"weight": 4, "min_tickets": 5,              # average paid per ticket
                              "bands": [[100, 1.0], [60, 0.6], [40, 0.3]]},       # [[at least £, share of weight]]
        "immediate_sales": {"weight": 6, "window_hours": 24, "max_days_since_first_sale": 30,
                            "bands": [[1000, 1.0], [500, 0.6], [250, 0.3]]},     # £ sold in the first 24h of sales
        "no_phone": {"weight": 3, "threshold_pct": 50, "min_evidence": 5},  # % of bookings without a phone
        # Same account, another upcoming event with the same name (ignoring case, spaces and punctuation)
        # whose next session is within max_days of this one - e.g. "MIDNIGHT MADNESS" / "MIDNIGHT MADNESS.".
        # A copied event for a different date (a weekly class, a pantomime run) doesn't count.
        "duplicate_event": {"weight": 3, "max_days": 1},
        "points_for_100": 25,      # signal points that make a score of 100 (before multipliers)
        "age_multiplier": [[30, 1.5], [90, 1.3], [365, 1.15]],   # [[under N days, x], ...]
    },
    "trust": {     # multiplies the fraud score; account history and payouts
        "no_transfers": 1.3,                                   # no bank transfer yet = high risk
        "transfers": [[21, 0.6], [6, 0.8]],                    # [[at least N transfers, x]]; 1-5 -> 1.0
        "past_events": [[20, 0.8], [5, 0.9]],                  # events whose last session has passed
        "past_gross": [[100000, 0.7], [20000, 0.8], [5000, 0.9]],   # £ sold for those events
        "floor": 0.4,                                          # trust never cuts a score by more than 60%
        # A new account has no transfers by definition: apply the larger of the age
        # multiplier and the no-transfers multiplier, not both.
        "new_account_combine": "max",                          # or "multiply"
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
_PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2, "P4": 3, "-": 4}


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


def _band_value(value, bands, default=0.0):
    """First [[at least X, v], ...] band met, highest first."""
    if value is None:
        return default
    for limit, v in bands:
        if value >= limit:
            return v
    return default


def night_created(created, cfg=CONFIG):
    """Hour (UK time) if the account was created in the night window, else None."""
    n = cfg["fraud"]["night_created"]
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(str(created)[:19])
    except ValueError:
        return None
    if len(str(created)) <= 10:          # date only: no time to judge
        return None
    if n.get("times_are", "Europe/London").upper() == "UTC":
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/London")).replace(tzinfo=None)
    return dt if n["from_hour"] <= dt.hour < n["to_hour"] else None


def trust_multiplier(transfers, past_events, past_gross, transfers_seen, cfg=CONFIG):
    """(multiplier, reason). ``transfers_seen`` is False when the transfers data can't
    show this account's history (it predates the first transfers file and has past events)."""
    t = cfg["trust"]
    if not transfers:
        if transfers_seen:
            return t["no_transfers"], "no bank transfers yet"
        mult, why = 1.0, []
    else:
        mult = _band_value(transfers, t["transfers"], 1.0)
        why = [f"{transfers} bank transfers"]
    mult *= _band_value(past_events, t["past_events"], 1.0) * _band_value(past_gross, t["past_gross"], 1.0)
    if past_events:
        why.append(f"{past_events} past events {_money(past_gross or 0)}")
    mult = max(t["floor"], mult)
    return mult, (("trusted: " if mult < 1 else "history: ") + ", ".join(why)) if why else ""


def priority(fraud_score, exposure_score, cfg=CONFIG):
    fb = _band(fraud_score, cfg["bands"]["fraud"])
    eb = _band(exposure_score, cfg["bands"]["exposure"])
    return cfg["priority"][fb][_EXPOSURE_ORDER.index(eb)], fb, eb


def _pts(v):
    return f"+{round(v, 1):g}"


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


def _name_key(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def duplicate_events(events, meta, max_days):
    """{EventId: [other EventIds]} for upcoming events of the same account with the same
    name (ignoring case, spaces, punctuation) and next sessions within ``max_days``."""
    groups = {}
    for eid, e in events.items():
        key = _name_key((meta.get(eid) or {}).get("EventName"))
        if key:
            groups.setdefault((e["AccountId"], key), []).append(eid)
    out = {}
    for ids in groups.values():
        for a in ids:
            near = [b for b in ids if b != a and (lambda d: d is not None and abs(d) <= max_days)(
                _days(events[a]["NextSession"], events[b]["NextSession"]))]
            if near:
                out[a] = sorted(near)
    return out


def _load_ev(conn, event_ids):
    conn.execute("DROP TABLE IF EXISTS temp._ev")
    conn.execute("CREATE TEMP TABLE _ev (EventId INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO temp._ev VALUES (?)", [(e,) for e in event_ids])


_PAID = ("COALESCE(PaymentReceived,0) > 0 AND LOWER(COALESCE(Status,'')) NOT LIKE '%refund%' "
         "AND LOWER(COALESCE(TransactionType,'')) NOT LIKE '%refund%'")


def _event_sales(conn, window_hours):
    """{EventId: (first sale, £ in the first window, £ total, tickets)} for temp._ev events."""
    rows = conn.execute(f"""
        WITH paid AS (SELECT EventId, TransactionDate, PaymentReceived, TicketQuantity FROM bookings
                      WHERE EventId IN (SELECT EventId FROM temp._ev) AND {_PAID}),
             first AS (SELECT EventId, MIN(TransactionDate) AS f FROM paid GROUP BY EventId)
        SELECT p.EventId, f.f AS first_sale,
               SUM(CASE WHEN p.TransactionDate < datetime(f.f, ?) THEN p.PaymentReceived ELSE 0 END) AS early,
               SUM(p.PaymentReceived) AS gross, SUM(COALESCE(p.TicketQuantity,0)) AS tickets
        FROM paid p JOIN first f ON f.EventId = p.EventId GROUP BY p.EventId""",
                        (f"+{int(window_hours)} hours",)).fetchall()
    return {r["EventId"]: (r["first_sale"], r["early"] or 0, r["gross"] or 0, r["tickets"] or 0) for r in rows}


def _no_phone(conn):
    """{EventId: (bookings, without a phone)} from SharePoint transactions (needs temp._txn_event)."""
    rows = conn.execute("""
        SELECT m.EventId, COUNT(*) AS n, SUM(CASE WHEN t.PhoneHash IS NULL THEN 1 ELSE 0 END) AS missing
        FROM booking_transactions t JOIN temp._txn_event m ON m.BookingGuid = t.BookingGuid
        WHERE m.EventId IS NOT NULL AND LOWER(COALESCE(t.TransactionType,'')) NOT LIKE '%refund%'
        GROUP BY m.EventId""").fetchall()
    return {r["EventId"]: (r["n"], r["missing"]) for r in rows}


def _trust_data(conn, today: str):
    transfers = {r[0]: r[1] for r in conn.execute(
        "SELECT AccountId, COUNT(*) FROM bank_transfers WHERE DateCreated < ? GROUP BY AccountId", (today,))}
    since = conn.execute("SELECT MIN(DateCreated) FROM bank_transfers").fetchone()[0]
    past = {r[0]: (r[1], r[2] or 0) for r in conn.execute("""
        SELECT e.AccountId, COUNT(*), SUM(COALESCE(l.Gross, 0)) FROM events e
        LEFT JOIN event_lifetime l ON l.EventId = e.EventId
        WHERE e.LastSessionDate < ? AND e.AccountId IS NOT NULL GROUP BY e.AccountId""", (today,))}
    return transfers, since, past


def _account_trust(aid, account, transfers, since, past, cfg):
    n = transfers.get(aid, 0)
    pe, pg = past.get(aid, (0, 0))
    created = (account or {}).get("DateTimeCreated")
    # Zero transfers only counts against an account if the transfers data covers its life,
    # or it has no event history to vouch for it.
    seen = bool(since) and (not pe or (created is not None and str(created) >= since))
    mult, why = trust_multiplier(n, pe, pg, seen, cfg)
    return mult, why, n, pe, pg


def _ip_share(conn, event_ids, uk, sample):
    if not event_ids:
        return {}
    _load_ev(conn, event_ids)
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
    """Map SharePoint transaction rows to events: by booking GUID (S3 BookingUrlId), else by a
    unique (event name, organiser name). Fills temp table _txn_event(BookingGuid, EventId, How)."""
    conn.execute("DROP TABLE IF EXISTS temp._txn_event")
    conn.execute("""
        CREATE TEMP TABLE _txn_event AS
        WITH ids AS (SELECT DISTINCT BookingGuid FROM booking_transaction_history),
             by_guid AS (SELECT BookingUrlId AS BookingGuid, MAX(EventId) AS EventId FROM bookings
                         WHERE BookingUrlId IN (SELECT BookingGuid FROM ids) GROUP BY BookingUrlId),
             names AS (SELECT LOWER(TRIM(n.EventName)) AS ev, LOWER(TRIM(a.AccountName)) AS org,
                              MIN(n.EventId) AS EventId, COUNT(*) AS n
                       FROM event_names n JOIN accounts a ON a.AccountId = n.AccountId GROUP BY 1, 2),
             latest AS (SELECT BookingGuid, LOWER(TRIM(EventName)) AS ev, LOWER(TRIM(EventOrganiser)) AS org
                        FROM booking_transactions)
        SELECT i.BookingGuid,
               COALESCE(bg.EventId, CASE WHEN nm.n = 1 THEN nm.EventId END) AS EventId,
               CASE WHEN bg.EventId IS NOT NULL THEN 'booking_guid'
                    WHEN nm.n = 1 THEN 'event_name' ELSE 'unmatched' END AS How
        FROM ids i
        LEFT JOIN by_guid bg ON bg.BookingGuid = i.BookingGuid
        LEFT JOIN latest l ON l.BookingGuid = i.BookingGuid
        LEFT JOIN names nm ON nm.ev = l.ev AND nm.org = l.org""")


def _declines(conn, since: str):
    transaction_event_map(conn)
    rows = conn.execute("""
        SELECT m.EventId, COUNT(*) AS attempts, SUM(CASE WHEN h.IsDecline = 1 THEN 1 ELSE 0 END) AS declines
        FROM booking_transaction_history h JOIN temp._txn_event m ON m.BookingGuid = h.BookingGuid
        WHERE m.EventId IS NOT NULL AND h.TransactionDate >= ?
          AND LOWER(COALESCE(h.TransactionType,'')) NOT LIKE '%refund%'
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
        "SELECT EventId, EventName, EventCreated, EventCreatedSource, FirstSessionDate, FirstBookingDate FROM events")} if events else {}
    accounts = {r["AccountId"]: dict(r) for r in conn.execute(
        "SELECT a.AccountId, a.AccountName, a.DateTimeCreated, a.AccountStatus, a.TierSystem "
        "FROM accounts a")}
    ip = _ip_share(conn, list(events), cfg["uk_values"], f["outside_uk_ip"]["sample"])
    sales = _event_sales(conn, f["immediate_sales"]["window_hours"]) if events else {}
    dec = _declines(conn, (today - timedelta(days=f["declines"]["days"])).isoformat())
    phone = _no_phone(conn)
    transfers, transfers_since, past = _trust_data(conn, t)
    twins = duplicate_events(events, meta, f["duplicate_event"]["max_days"])
    trust_cache = {}

    def trust(aid):
        if aid not in trust_cache:
            trust_cache[aid] = _account_trust(aid, accounts.get(aid), transfers, transfers_since, past, cfg)
        return trust_cache[aid]

    ev_out = []
    for eid, e in events.items():
        m = meta.get(eid, {})
        a = accounts.get(e["AccountId"], {})
        reasons, pts = [], 0.0
        tl, gaps = timeline_points(a.get("DateTimeCreated"), m.get("EventCreated"), m.get("FirstSessionDate"),
                                   m.get("EventCreatedSource"), cfg)
        if tl:
            pts += tl
            short = [f"{n} {g:.0f}d" for n, g in zip(("account→event", "event→session", "account→session"), gaps)
                     if g is not None and 0 <= g < f["timeline"]["min_gap_days"]]
            reasons.append(f"short timeline: {', '.join(short)} {_pts(tl)}")
        known, foreign = ip.get(eid, (0, 0))
        ip_pct = 100.0 * foreign / known if known >= f["outside_uk_ip"]["min_evidence"] else None
        s = _severity(ip_pct, f["outside_uk_ip"]["threshold_pct"], f["outside_uk_ip"]["weight"])
        if s:
            pts += s
            reasons.append(f"{ip_pct:.0f}% overseas IPs {_pts(s)}")
        att, dn = dec.get(eid, (0, 0))
        dec_pct = 100.0 * dn / att if att >= f["declines"]["min_evidence"] else None
        s = _severity(dec_pct, f["declines"]["threshold_pct"], f["declines"]["weight"])
        if s:
            pts += s
            reasons.append(f"{dec_pct:.0f}% declined ({dn}/{att}, {f['declines']['days']}d) {_pts(s)}")
        mult, age = age_multiplier(a.get("DateTimeCreated"), today, cfg)
        days_to = _days(t, e["NextSession"])

        nc = f["night_created"]
        night = night_created(a.get("DateTimeCreated"), cfg)
        if night and age is not None and age <= nc["max_account_age_days"]:
            pts += nc["weight"]
            reasons.append(f"account created {night:%H:%M} (night) {_pts(nc['weight'])}")

        ie = f["imminent_event"]
        created = m.get("EventCreated") or m.get("FirstBookingDate")
        ev_age = _days(created, t)
        share = 0.0
        whole_days = None if days_to is None else int(days_to)       # calendar days, as DaysToSession
        if ev_age is not None and ev_age <= ie["max_event_age_days"] and whole_days is not None:
            for limit, sh in ie["days_to_session"]:
                if whole_days <= limit:
                    share = sh
                    break
        if share:
            pts += ie["weight"] * share
            reasons.append(f"event in {max(whole_days, 0)}d, created {int(ev_age)}d ago {_pts(ie['weight'] * share)}")

        first_sale, early, gross, tickets = sales.get(eid, (None, 0, 0, 0))
        hp = f["high_ticket_price"]
        avg_price = gross / tickets if tickets >= hp["min_tickets"] else None
        share = _band_value(avg_price, hp["bands"])
        if share:
            pts += hp["weight"] * share
            reasons.append(f"avg ticket {_money(avg_price)} {_pts(hp['weight'] * share)}")

        im = f["immediate_sales"]
        since_first = _days(first_sale, t)
        share = (_band_value(early, im["bands"])
                 if since_first is not None and since_first <= im["max_days_since_first_sale"] else 0)
        if share:
            pts += im["weight"] * share
            reasons.append(f"{_money(early)} sold in first {im['window_hours']}h {_pts(im['weight'] * share)}")

        npn = f["no_phone"]
        pn, pmiss = phone.get(eid, (0, 0))
        nophone_pct = 100.0 * pmiss / pn if pn >= npn["min_evidence"] else None
        s = _severity(nophone_pct, npn["threshold_pct"], npn["weight"])
        if s:
            pts += s
            reasons.append(f"{nophone_pct:.0f}% bookings without a phone {_pts(s)}")

        dup = twins.get(eid)
        if dup:
            pts += f["duplicate_event"]["weight"]
            reasons.append(f"duplicate of event {', '.join(str(x) for x in dup)} {_pts(f['duplicate_event']['weight'])}")

        tmult, twhy, n_transfers, past_n, past_gross = trust(e["AccountId"])
        if tmult > 1 and cfg["trust"].get("new_account_combine", "max") == "max":
            combined = max(mult, tmult)
            if mult > 1:
                reasons.append(f"new account ({age}d){'' if mult < tmult else f' x{mult:g}'}")
            if twhy:
                reasons.append(f"{twhy}{'' if tmult <= mult else f' x{tmult:g}'}")
        else:
            combined = mult * tmult
            if mult > 1:
                reasons.append(f"new account ({age}d) x{mult:g}")
            if twhy:
                reasons.append(f"{twhy} x{tmult:.2f}".rstrip("0").rstrip("."))
        fraud = round(min(100.0, 100.0 * pts / f["points_for_100"] * combined), 1) if pts else 0.0
        days_pts = 0
        for limit, p in cfg["exposure_event"]["days_to_session"]:
            if (e["Held"] or 0) < cfg["exposure_event"].get("days_min_held", 0):
                break
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
            "AvgTicketPrice": None if avg_price is None else round(avg_price, 2), "FirstDaySales": round(early, 2),
            "NoPhonePct": nophone_pct, "Transfers": n_transfers, "PastEvents": past_n,
            "PastGross": round(past_gross, 2), "TrustMultiplier": round(tmult, 2),
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
        tmult, twhy, n_transfers, past_n, past_gross = trust(aid)
        if not worst and mult > 1:
            reasons.append(f"new account ({age}d)")
        if not worst and twhy:
            reasons.append(twhy)
        pr, fb, eb = priority(fraud, exposure, cfg)
        if worst and _PRIORITY_ORDER[worst["Priority"]] < _PRIORITY_ORDER[pr]:
            pr = worst["Priority"]           # an account is never lower priority than its worst event
        at_risk = max(b.get("FullBalance") or 0, upcoming or 0, held)
        acc_out.append({
            "AccountId": aid, "AccountName": a.get("AccountName"), "Tier": a.get("TierSystem"),
            "AccountCreated": a.get("DateTimeCreated"), "AgeDays": age, "FullBalance": b.get("FullBalance"),
            "SalesForUpcomingEvents": b.get("SalesForUpcomingEvents"), "Exposure": b.get("Exposure"),
            "UpcomingEvents": len(evs), "Held": round(held, 2), "LargestEvent": round(largest, 2),
            "Next14Days": round(near, 2), "AmountAtRisk": round(at_risk, 2), "ExposureScore": exposure,
            "FraudScore": fraud, "Priority": pr, "FraudBand": fb, "ExposureBand": eb,
            "NextSession": min((e["NextSession"] for e in evs), default=None),
            "Transfers": n_transfers, "PastEvents": past_n, "PastGross": round(past_gross, 2),
            "TrustMultiplier": round(tmult, 2),
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
