"""Nightly risk report: changes, top 10 risk accounts/events, import health,
and the Booking Transactions import check. Rendered as HTML (email) and text (log).

No personal data appears in the report: accounts, events, money, counts and
gateway message texts only.
"""
import html
from datetime import date, datetime, timedelta, timezone

from . import risk_interim

TOP_N = 10


def _q(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args)]


def _one(conn, sql, *args):
    r = conn.execute(sql, args).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def exposure_changes(conn, today: date) -> dict:
    t = today.isoformat()
    d0 = _one(conn, "SELECT MAX(SnapshotDate) FROM balances WHERE SnapshotDate <= ?", t)
    if not d0:
        return {"latest": None}
    d1 = _one(conn, "SELECT MAX(SnapshotDate) FROM balances WHERE SnapshotDate < ?", d0)
    dm = _one(conn, "SELECT MAX(SnapshotDate) FROM balances WHERE SnapshotDate <= ?",
              (date.fromisoformat(d0) - timedelta(days=28)).isoformat())

    def totals(d):
        if not d:
            return None
        r = conn.execute("SELECT COUNT(*) n, SUM(COALESCE(FullBalance,0)) fb, SUM(COALESCE(Balance,0)) b, "
                         "SUM(COALESCE(SalesForUpcomingEvents,0)) su, SUM(COALESCE(Exposure,0)) ex "
                         "FROM balances WHERE SnapshotDate = ?", (d,)).fetchone()
        return {"date": d, "accounts": r["n"], "FullBalance": r["fb"], "Balance": r["b"],
                "SalesForUpcomingEvents": r["su"], "Exposure": r["ex"]}

    rises = []
    if d1:
        rises = _q(conn, """
            SELECT c.AccountId, a.AccountName, COALESCE(p.FullBalance,0) AS Before, c.FullBalance AS After,
                   c.FullBalance - COALESCE(p.FullBalance,0) AS Change
            FROM balances c LEFT JOIN balances p ON p.AccountId = c.AccountId AND p.SnapshotDate = ?
            LEFT JOIN accounts a ON a.AccountId = c.AccountId
            WHERE c.SnapshotDate = ? ORDER BY Change DESC LIMIT 5""", d1, d0)
        rises = [r for r in rises if (r["Change"] or 0) > 0]
    stale_days = (today - date.fromisoformat(d0)).days
    return {"latest": totals(d0), "previous": totals(d1), "month_ago": totals(dm), "rises": rises,
            "stale_days": stale_days}


def movements_yesterday(conn, today: date) -> dict:
    d = _one(conn, "SELECT MAX(MovementDate) FROM balance_movements WHERE MovementDate < ?", today.isoformat())
    if not d:
        return {"date": None}
    r = conn.execute("""SELECT COUNT(*) n, SUM(COALESCE(TicketValue,0)) tv, SUM(COALESCE(TicketRefundedAmount,0)) rf,
                               SUM(COALESCE(FundTransfer,0)) ft, SUM(COALESCE(StripeTransfers,0)) st,
                               SUM(COALESCE(Pending,0)) pe
                        FROM balance_movements WHERE MovementDate = ?""", (d,)).fetchone()
    return {"date": d, "accounts": r["n"], "TicketValue": r["tv"], "Refunds": r["rf"], "FundTransfer": r["ft"],
            "StripeTransfers": r["st"], "Pending": r["pe"]}


def new_activity(conn, today: date) -> dict:
    week = (today - timedelta(days=7)).isoformat()
    yday = (today - timedelta(days=1)).isoformat()
    new_accounts = _q(conn, """
        SELECT a.AccountId, a.AccountName, a.DateTimeCreated, COUNT(DISTINCT b.EventId) AS UpcomingEvents
        FROM accounts a LEFT JOIN bookings b ON b.AccountId = a.AccountId AND b.EventDate >= ?
        WHERE a.DateTimeCreated >= ? GROUP BY a.AccountId ORDER BY a.DateTimeCreated DESC""", today.isoformat(), week)
    events_created = _one(conn, "SELECT COUNT(*) FROM events WHERE EventCreated >= ? AND EventCreated < ?",
                          yday, today.isoformat())
    return {"new_accounts": new_accounts, "events_created_yesterday": events_created}


def top_changes(conn, run_date: str, result: dict) -> dict:
    prev = _one(conn, "SELECT MAX(RunDate) FROM risk_daily WHERE RunDate < ?", run_date)
    out = {"previous_run": prev, "new_accounts": [], "new_events": [], "priority_counts": {}, "previous_counts": {}}
    for level, rows, idk in (("account", result["accounts"], "AccountId"), ("event", result["events"], "EventId")):
        counts = {}
        for r in rows:
            counts[r["Priority"]] = counts.get(r["Priority"], 0) + 1
        out["priority_counts"][level] = counts
        if prev:
            pc = {r["Priority"]: r["n"] for r in _q(conn, "SELECT Priority, COUNT(*) n FROM risk_daily "
                                                         "WHERE RunDate=? AND Level=? GROUP BY Priority", prev, level)}
            out["previous_counts"][level] = pc
            prev_ids = [r["Id"] for r in _q(conn, """
                SELECT Id FROM risk_daily WHERE RunDate=? AND Level=?
                ORDER BY CASE Priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 WHEN 'P3' THEN 2 WHEN 'P4' THEN 3 ELSE 4 END,
                         FraudScore DESC, AmountAtRisk DESC LIMIT ?""", prev, level, TOP_N)]
            now_ids = [r[idk] for r in rows[:TOP_N]]
            out["new_" + level + "s"] = [i for i in now_ids if i not in prev_ids]
    return out


def import_health(conn, now_utc: datetime) -> list:
    rows = _q(conn, """SELECT source, status, ref, started_at, finished_at, rows_read, rows_written, message
                       FROM import_log WHERE id IN (SELECT MAX(id) FROM import_log GROUP BY source)
                       ORDER BY source""")
    for r in rows:
        try:
            age = (now_utc - datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))
            r["age_hours"] = round(age.total_seconds() / 3600, 1)
        except (TypeError, ValueError):
            r["age_hours"] = None
        r["flag"] = ("FAILED" if r["status"] == "failed" else
                     "STALE" if r["age_hours"] is not None and r["age_hours"] > 30 and r["source"].startswith("s3:")
                     else "")
    return rows


REFUND_SQL = "LOWER(COALESCE(TransactionType,'')) LIKE '%refund%'"


def check_transactions(conn, today: date) -> dict:
    """Quality check of the SharePoint Booking Transactions import."""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    files = _q(conn, """SELECT status, COUNT(*) n, SUM(rows_read) rows FROM import_log
                        WHERE source='sharepoint' AND started_at >= ? GROUP BY status""", since)
    last_files = _q(conn, """SELECT ref, status, started_at, rows_read, message FROM import_log
                             WHERE source='sharepoint' ORDER BY id DESC LIMIT 10""")
    n = _one(conn, "SELECT COUNT(*) FROM booking_transactions") or 0
    if not n:
        return {"rows": 0, "files": files, "last_files": last_files}
    attempts = conn.execute(f"""SELECT COUNT(*) n, SUM(CASE WHEN IsDecline=1 THEN 1 ELSE 0 END) dec,
                                      SUM(CASE WHEN IsDecline IS NULL THEN 1 ELSE 0 END) unk
                               FROM booking_transaction_history WHERE NOT ({REFUND_SQL})""").fetchone()
    summary = conn.execute("""SELECT MIN(TransactionDate) d0, MAX(TransactionDate) d1,
                                     SUM(CASE WHEN EmailHash IS NOT NULL THEN 1 ELSE 0 END) em,
                                     SUM(CASE WHEN PhoneHash IS NOT NULL THEN 1 ELSE 0 END) ph,
                                     SUM(CASE WHEN CardLast4 IS NOT NULL THEN 1 ELSE 0 END) c4,
                                     SUM(CASE WHEN WaitedInQueue=1 THEN 1 ELSE 0 END) q
                              FROM booking_transactions""").fetchone()
    versions = _one(conn, "SELECT COUNT(*) FROM booking_transaction_history") or 0
    risk_interim.transaction_event_map(conn)
    match = {r["How"]: r["n"] for r in _q(conn, "SELECT How, COUNT(*) n FROM temp._txn_event GROUP BY How")}
    gw = _one(conn, """SELECT COUNT(*) FROM booking_transactions t
                       WHERE t.GatewayId IS NOT NULL AND EXISTS (SELECT 1 FROM bookings b WHERE b.GatewayId = t.GatewayId)""")

    def values(col, limit=15):
        return _q(conn, f"SELECT COALESCE({col}, '(blank)') AS value, COUNT(*) n FROM booking_transaction_history "
                        f"GROUP BY 1 ORDER BY n DESC LIMIT ?", limit)

    messages = _q(conn, """SELECT COALESCE(GatewayMessageDescription,'(blank)') AS value,
                                  COALESCE(Status,'(blank)') AS status,
                                  CASE IsDecline WHEN 1 THEN 'decline' WHEN 0 THEN 'approved' ELSE 'UNCLASSIFIED' END AS class,
                                  COUNT(*) n
                           FROM booking_transaction_history GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 40""")
    refunds = _one(conn, "SELECT COUNT(*) FROM booking_transaction_history WHERE " + REFUND_SQL) or 0
    hint = None
    if not match.get("booking_guid"):   # nothing linked to S3: show what's loaded and the GUID shapes
        nb = _one(conn, "SELECT COUNT(*) FROM bookings") or 0
        s3_guid = _one(conn, "SELECT BookingUrlId FROM bookings WHERE BookingUrlId IS NOT NULL "
                             "ORDER BY TransactionDate DESC LIMIT 1")
        bt_guid = _one(conn, "SELECT BookingGuid FROM booking_transactions LIMIT 1")
        overlap = _one(conn, "SELECT MIN(TransactionDate) || ' to ' || MAX(TransactionDate) FROM bookings")
        from .ingest.sharepoint_transactions import shape
        hint = {"s3_bookings": nb, "s3_dates": overlap,
                "s3_guid_shape": shape(s3_guid) if s3_guid else None,
                "export_guid_shape": shape(bt_guid) if bt_guid else None}
    return {
        "rows": n, "versions": versions, "files": files, "last_files": last_files,
        "date_from": summary["d0"], "date_to": summary["d1"],
        "attempts": attempts["n"], "declines": attempts["dec"] or 0, "unclassified": attempts["unk"] or 0,
        "fill": {"email hash": summary["em"], "phone hash": summary["ph"], "card last 4": summary["c4"],
                 "waited in queue": summary["q"]},
        "match": match, "gateway_id_matches": gw,
        "status": values("Status"), "transaction": values("TransactionType"), "cvv": values("CvvCheck"),
        "card_type": values("CardType"), "messages": messages, "refunds": refunds, "match_hint": hint,
    }


def build(conn, today: date) -> dict:
    result = risk_interim.score(conn, today)
    run_date = today.isoformat()
    changes = top_changes(conn, run_date, result)
    risk_interim.save(conn, run_date, result)
    return {
        "date": run_date, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "model": result["version"], "accounts": result["accounts"], "events": result["events"],
        "exposure": exposure_changes(conn, today), "movements": movements_yesterday(conn, today),
        "activity": new_activity(conn, today), "top_changes": changes,
        "imports": import_health(conn, datetime.now(timezone.utc)), "transactions": check_transactions(conn, today),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _m(v):
    return "-" if v is None else f"£{v:,.0f}"


def _delta(a, b):
    if a is None or b is None:
        return "-"
    d = a - b
    return f"{'+' if d >= 0 else '-'}£{abs(d):,.0f}"


def subject(r) -> str:
    c = r["top_changes"]["priority_counts"]
    p1 = c.get("account", {}).get("P1", 0)
    p1e = c.get("event", {}).get("P1", 0)
    exp = (r["exposure"].get("latest") or {}).get("FullBalance")
    flags = sum(1 for i in r["imports"] if i["flag"])
    return (f"Risk report {r['date']}: {p1} P1 account(s), {p1e} P1 event(s), balances {_m(exp)}"
            + (f", {flags} import issue(s)" if flags else ""))


_STYLE = ("font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#1f2937",
          "border-collapse:collapse;margin:6px 0 16px 0",
          "border:1px solid #d1d5db;padding:4px 8px;text-align:left;vertical-align:top",
          "background:#1f3864;color:#fff")
_PCOL = {"P1": "#f8696b", "P2": "#ffb366", "P3": "#ffe699", "P4": "#e5e7eb"}


def _table(headers, rows, prio_col=None):
    e = html.escape
    h = "".join(f'<th style="{_STYLE[2]};{_STYLE[3]}">{e(str(x))}</th>' for x in headers)
    body = []
    for r in rows:
        cells = []
        for i, v in enumerate(r):
            bg = f";background:{_PCOL.get(v, '')}" if prio_col == i and v in _PCOL else ""
            cells.append(f'<td style="{_STYLE[2]}{bg}">{e("" if v is None else str(v))}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table style="{_STYLE[1]}"><tr>{h}</tr>{"".join(body)}</table>'


def _account_rows(r):
    return [[a["Priority"], a["AccountId"], a["AccountName"], a["Tier"], f"{a['FraudScore']:.0f}",
             f"{a['ExposureScore']:.0f}", _m(a["AmountAtRisk"]), _m(a["FullBalance"]), a["UpcomingEvents"],
             (a["NextSession"] or "")[:10], a["AgeDays"], a["Reasons"]] for a in r["accounts"][:TOP_N]]


_ACC_H = ["Priority", "Account", "Name", "Tier", "Fraud", "Exposure", "At risk", "Full balance", "Upcoming events",
          "Next session", "Age (days)", "Why"]


def _event_rows(r):
    return [[e["Priority"], e["EventId"], e["EventName"], e["AccountName"], f"{e['FraudScore']:.0f}",
             f"{e['ExposureScore']:.0f}", _m(e["Held"]), (e["NextSession"] or "")[:10], e["DaysToSession"],
             e["FutureBookings"], e["Reasons"]] for e in r["events"][:TOP_N]]


_EV_H = ["Priority", "Event", "Name", "Account", "Fraud", "Exposure", "Held", "Next session", "Days", "Bookings", "Why"]


def render_html(r) -> str:
    e = html.escape
    x = r["exposure"]
    parts = [f'<div style="{_STYLE[0]}">', f"<h2>Risk report {e(r['date'])}</h2>",
             f"<p>Interim scoring <code>{e(r['model'])}</code>: exposure from balances and upcoming events; "
             "fraud from the compressed timeline, overseas IPs and declines, times the new-account multiplier. "
             "Priority P1 is the most urgent.</p>"]
    # Changes
    parts.append("<h3>What changed</h3>")
    if x.get("latest"):
        L, P, M = x["latest"], x.get("previous"), x.get("month_ago")
        rows = [[k, _m(L[k]), _delta(L[k], P and P[k]), _delta(L[k], M and M[k])]
                for k in ("FullBalance", "Balance", "SalesForUpcomingEvents", "Exposure")]
        parts.append(f"<p>Balances as of {e(L['date'])} ({L['accounts']:,} accounts)"
                     + (f" — <b>RiskReport not refreshed for {x['stale_days']} days</b>" if x["stale_days"] > 1 else "")
                     + "</p>")
        parts.append(_table(["", "Total", f"vs {P['date'] if P else '-'}", f"vs {M['date'] if M else '-'}"], rows))
        if x["rises"]:
            parts.append("<p>Largest balance increases:</p>" + _table(
                ["Account", "Name", "Before", "After", "Change"],
                [[a["AccountId"], a["AccountName"], _m(a["Before"]), _m(a["After"]), _delta(a["After"], a["Before"])]
                 for a in x["rises"]]))
    else:
        parts.append("<p>No RiskReport balances loaded yet.</p>")
    mv = r["movements"]
    if mv.get("date"):
        parts.append(f"<p>Movements on {e(mv['date'])}: ticket value {_m(mv['TicketValue'])}, refunds {_m(mv['Refunds'])}, "
                     f"fund transfers {_m(mv['FundTransfer'])}, Stripe transfers {_m(mv['StripeTransfers'])}.</p>")
    tc = r["top_changes"]
    pc = tc["priority_counts"]
    pv = tc.get("previous_counts", {})
    parts.append(_table(["", "P1", "P2", "P3", "P4"],
                        [[lvl.title() + "s", *[f"{pc.get(lvl, {}).get(p, 0)}"
                                               + (f" ({pv[lvl].get(p, 0)})" if lvl in pv else "") for p in ("P1", "P2", "P3", "P4")]]
                         for lvl in ("account", "event")]))
    if tc.get("previous_run"):
        parts.append(f"<p>Brackets: {e(tc['previous_run'])}. New in the top {TOP_N}: "
                     f"{len(tc['new_accounts'])} account(s), {len(tc['new_events'])} event(s).</p>")
    act = r["activity"]
    parts.append(f"<p>New accounts in the last 7 days: {len(act['new_accounts'])} "
                 f"({sum(1 for a in act['new_accounts'] if a['UpcomingEvents'])} with upcoming events). "
                 f"Events created yesterday: {act['events_created_yesterday'] or 0}.</p>")
    # Top 10s
    parts.append(f"<h3>Top {TOP_N} risk accounts</h3>" + (_table(_ACC_H, _account_rows(r), 0) if r["accounts"]
                                                          else "<p>None scored.</p>"))
    parts.append(f"<h3>Top {TOP_N} risk events (upcoming)</h3>" + (_table(_EV_H, _event_rows(r), 0) if r["events"]
                                                                   else "<p>No upcoming events.</p>"))
    # Imports
    parts.append("<h3>Imports</h3>" + _table(
        ["Source", "Status", "Started (UTC)", "Read", "Written", "", "Note"],
        [[i["source"], i["status"], i["started_at"], i["rows_read"], i["rows_written"], i["flag"],
          (i["message"] or "")[:120]] for i in r["imports"]]))
    # Booking Transactions check
    parts.append(_transactions_html(r["transactions"]))
    parts.append("</div>")
    return "\n".join(parts)


def _transactions_html(t) -> str:
    e = html.escape
    out = ["<h3>Booking Transactions import check</h3>"]
    files = ", ".join(f"{f['n']} {f['status']}" for f in t["files"]) or "none"
    out.append(f"<p>Files in the last 30 days: {e(files)}.</p>")
    if not t["rows"]:
        out.append("<p>No transactions imported yet.</p>")
        return "".join(out)
    m = t["match"]
    total = sum(m.values()) or 1
    out.append(f"<p>{t['rows']:,} bookings ({t['versions']:,} row versions), {e(str(t['date_from']))[:10]} to "
               f"{e(str(t['date_to']))[:10]}. Of {t['attempts']:,} payment attempts (refunds excluded): {t['declines']:,} declined, "
               f"{t['unclassified']:,} unclassified.</p>")
    out.append(_table(["Matched to an event by", "Bookings", "%"],
                      [[k, v, f"{100 * v / total:.1f}%"] for k, v in sorted(m.items())]))
    out.append(_table(["Field filled", "Rows", "%"],
                      [[k, v, f"{100 * v / t['rows']:.1f}%"] for k, v in t["fill"].items()]
                      + [["Gateway ID found in S3 bookings", t["gateway_id_matches"],
                          f"{100 * t['gateway_id_matches'] / t['rows']:.1f}%"]]))
    if t.get("match_hint"):
        h = t["match_hint"]
        out.append(f"<p><b>No transactions linked to S3 bookings.</b> S3 bookings loaded: {h['s3_bookings']:,} "
                   f"({e(str(h['s3_dates']))}). GUID format in S3: {e(str(h['s3_guid_shape']))}; in the export: "
                   f"{e(str(h['export_guid_shape']))}. If S3 bookings is 0 or doesn't cover these dates, run the "
                   "full refresh.</p>")
    out.append(f"<p>{t['refunds']:,} refund rows are excluded from the decline figures.</p>")
    out.append("<p>Gateway Message Description by Status, as classified. Tell me how any <b>UNCLASSIFIED</b> ones "
               "should be treated:</p>" + _table(["Message", "Status", "Treated as", "Rows"],
                                                 [[x["value"], x["status"], x["class"], x["n"]] for x in t["messages"]]))
    for label, key in (("Status", "status"), ("Transaction", "transaction"), ("Cvv Check", "cvv"),
                       ("Card Type", "card_type")):
        out.append(f"<p>{label} values:</p>" + _table(["Value", "Rows"], [[x["value"], x["n"]] for x in t[key]]))
    return "".join(out)


def render_text(r) -> str:
    lines = [subject(r), f"Model: {r['model']}", ""]
    x = r["exposure"]
    if x.get("latest"):
        L, P = x["latest"], x.get("previous")
        lines.append(f"Balances {L['date']} ({L['accounts']:,} accounts): full {_m(L['FullBalance'])} "
                     f"({_delta(L['FullBalance'], P and P['FullBalance'])} vs {P['date'] if P else '-'}), "
                     f"upcoming sales {_m(L['SalesForUpcomingEvents'])}, exposure {_m(L['Exposure'])}")
        if x["stale_days"] > 1:
            lines.append(f"WARNING: RiskReport not refreshed for {x['stale_days']} days")
    lines += ["", f"Top {TOP_N} risk accounts:"]
    for a in r["accounts"][:TOP_N]:
        lines.append(f"  {a['Priority']:<3} {a['AccountId']:<8} {str(a['AccountName'])[:30]:<30} fraud {a['FraudScore']:>5.1f} "
                     f"exposure {a['ExposureScore']:>3}  at risk {_m(a['AmountAtRisk']):>10}  {a['Reasons'][:90]}")
    lines += ["", f"Top {TOP_N} risk events:"]
    for ev in r["events"][:TOP_N]:
        lines.append(f"  {ev['Priority']:<3} {ev['EventId']:<8} {str(ev['EventName'])[:30]:<30} fraud {ev['FraudScore']:>5.1f} "
                     f"held {_m(ev['Held']):>10}  {(ev['NextSession'] or '')[:10]}  {ev['Reasons'][:80]}")
    lines += ["", "Imports:"]
    for i in r["imports"]:
        lines.append(f"  {i['source']:<30} {i['status']:<7} {i['started_at']}  {i['flag']}")
    t = r["transactions"]
    lines += ["", f"Booking Transactions: {t['rows']:,} bookings"
              + (f"; {t['attempts']:,} attempts, {t['declines']:,} declined, {t['unclassified']:,} unclassified; "
                 f"matched to events {t['match']}"
                 if t["rows"] else "")]
    return "\n".join(lines)
