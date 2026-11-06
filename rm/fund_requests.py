"""Fund transfer requests pasted from the TryBooking admin portal, with a risk check per row.

The portal's "fund transfer requests" table is copied to the clipboard and pasted
into the dashboard (/fund-requests). Nothing pasted is stored or logged: the
page parses it, looks each account up in the warehouse, and shows a verdict.

The copied text looks like this (headers first, one per line; then each row
spread over several lines, its numbers on one tab-separated line):

    Trust Level / Event Organiser / Comment / Bank Account / Amount (GBP) /
    Date Requested / Date Account Created / Date Last Event Created / Date of Event /
    Exposure / Remaining / # Previous Payments / % Foreign Cards /
    % Overseas Bookings / % Failed Card Payments / Account ID / Email Address

    Untrusted
    <organiser>
    Add a comment
    <bank account name>
    30.00
    6Nov26 10:19 PM<TAB>18Jan26<TAB>20Oct26<TAB>22Nov26<TAB>0.00<TAB>162.20<TAB>11<TAB>1.01<TAB>2.02<TAB>4.55<TAB>18967<TAB><email>

A row is found by its "Date Requested" cell (a date with a time). The cells from
there on are fixed; the lines before it are trust level, organiser, comment,
bank account and amount. A row pasted as one tab-separated line (e.g. copied
back out of Excel) works the same way.

Organiser, comment, bank account name and email are read only to find the
columns: they are never shown, returned or kept.
"""
import re
from datetime import datetime

from rm import risk_interim

CONFIG = {
    "version": "fund-2026-11-06",
    # [[under N days old when requested, points]]
    "account_age": [[30, 25], [90, 10]],
    "first_payout": 15,                       # no previous payments
    # paid out before the event has happened: [[amount at least £, points]]
    "before_event": [[5000, 20], [1000, 12], [250, 5]],
    "large_amount": [[10000, 10], [5000, 5]],  # any request
    "new_event": {"days": 14, "points": 10},  # last event created this close to the request
    "foreign_cards": [[50, 25], [20, 15], [10, 5]],         # %
    "overseas_bookings": [[50, 20], [20, 12], [10, 5]],     # %
    "failed_payments": [[30, 25], [15, 15], [8, 5]],        # % (card testing)
    "model_priority": {"P1": 30, "P2": 20, "P3": 10},       # today's risk report
    "not_in_warehouse": 10,
    # x trust (risk_interim CONFIG["trust"]): previous payments, past events and their value
    "levels": [[50, "High"], [25, "Medium"]],               # [[score at least, level]]; else Low
    "advice": {"High": "Hold and investigate before paying", "Medium": "Check before paying", "Low": "No concerns"},
}

FIELDS = ["requested", "account_created", "last_event_created", "event_date", "exposure", "remaining",
          "previous_payments", "foreign_cards_pct", "overseas_bookings_pct", "failed_payments_pct", "account_id"]
HEADERS = {"trust level", "event organiser", "comment", "bank account", "amount (gbp)", "amount", "date requested",
           "date account created", "date last event created", "date of event", "exposure", "remaining",
           "# previous payments", "% foreign cards", "% overseas bookings", "% failed card payments",
           "account id", "email address"}
_REQUESTED = re.compile(r"^\d{1,2}\s?[A-Za-z]{3}\s?\d{2,4}\s+\d{1,2}:\d{2}(\s*[AaPp][Mm])?$")
MAX_ROWS = 500


def parse_date(s):
    """'6Nov26 10:19 PM' or '18Jan26' -> datetime, else None."""
    s = (s or "").strip()
    if not s or s == "-":
        return None
    m = re.match(r"^(\d{1,2})\s?([A-Za-z]{3})\s?(\d{2}|\d{4})(?:\s+(\d{1,2}):(\d{2})(?:\s*([AaPp])[Mm])?)?$", s)
    if not m:
        return None
    day, mon, year, hh, mm, ap = m.groups()
    try:
        d = datetime.strptime(f"{day} {mon.title()} {year}", "%d %b %y" if len(year) == 2 else "%d %b %Y")
    except ValueError:
        return None
    if hh:
        h = int(hh) % 12 + (12 if ap and ap.upper() == "P" else 0) if ap else int(hh)
        if h > 23 or int(mm) > 59:
            return None
        d = d.replace(hour=h, minute=int(mm))
    return d


def parse_number(s):
    s = (s or "").strip().replace("£", "").replace(",", "").replace("%", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse(text: str) -> dict:
    """{'rows': [...], 'problems': [...]} from the pasted portal table."""
    rows, problems, pending, seen_row = [], [], [], False
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cells = [c.strip() for c in line.split("\t")]
        k = next((i for i, c in enumerate(cells) if _REQUESTED.match(c)), None)
        if k is None:
            pending.extend(c for c in cells if c)
            continue
        pending.extend(c for c in cells[:k] if c)
        if not seen_row:   # drop the header block before the first row
            last = max((i for i, c in enumerate(pending) if c.lower() in HEADERS), default=-1)
            pending = pending[last + 1:]
        seen_row = True
        rows.append(_row(pending, cells[k:], len(rows) + 1, problems))
        pending = []
        if len(rows) >= MAX_ROWS:
            problems.append(f"Only the first {MAX_ROWS} rows were read.")
            break
    if not rows:
        problems.append("No requests found. Copy the whole table from the portal, including the "
                        "Date Requested column (e.g. \"6Nov26 10:19 PM\").")
    return {"rows": rows, "problems": problems}


def _row(prefix, tail, n, problems):
    tail = tail + [""] * (len(FIELDS) + 1 - len(tail))
    raw = dict(zip(FIELDS, tail))
    amount_i = next((i for i in range(len(prefix) - 1, -1, -1) if parse_number(prefix[i]) is not None), None)
    row = {
        "n": n,
        "trust_level": prefix[0] if prefix and amount_i != 0 else None,
        "amount": parse_number(prefix[amount_i]) if amount_i is not None else None,
        "requested": parse_date(raw["requested"]),
        "account_created": parse_date(raw["account_created"]),
        "last_event_created": parse_date(raw["last_event_created"]),
        "event_date": parse_date(raw["event_date"]),
        "exposure": parse_number(raw["exposure"]),
        "remaining": parse_number(raw["remaining"]),
        "previous_payments": parse_number(raw["previous_payments"]),
        "foreign_cards_pct": parse_number(raw["foreign_cards_pct"]),
        "overseas_bookings_pct": parse_number(raw["overseas_bookings_pct"]),
        "failed_payments_pct": parse_number(raw["failed_payments_pct"]),
        "account_id": None,
    }
    aid = raw["account_id"].strip()
    if aid.isdigit():
        row["account_id"] = int(aid)
    else:
        problems.append(f"Row {n}: no account ID found.")
    if row["amount"] is None:
        problems.append(f"Row {n}: no amount found.")
    if row["previous_payments"] is not None:
        row["previous_payments"] = int(row["previous_payments"])
    return row


# --------------------------------------------------------------- evaluate ---

def _band(value, bands, default=0):
    return risk_interim._band_value(value, bands, default)


def warehouse_context(conn, account_ids, today: str) -> dict:
    """Per account: name, today's risk report row, past events and their value."""
    out = {}
    ids = sorted({a for a in account_ids if a is not None})
    if not ids:
        return out
    run = conn.execute("SELECT MAX(RunDate) FROM risk_daily").fetchone()[0]
    for aid in ids:
        a = conn.execute("SELECT AccountName, DateTimeCreated FROM accounts WHERE AccountId=?", (aid,)).fetchone()
        if not a:
            continue
        r = conn.execute("SELECT Priority, FraudScore, ExposureScore, AmountAtRisk, Reasons FROM risk_daily "
                         "WHERE RunDate=? AND Level='account' AND Id=?", (run, aid)).fetchone() if run else None
        past = conn.execute("""SELECT COUNT(*), COALESCE(SUM(l.Gross),0) FROM events e
                               LEFT JOIN event_lifetime l ON l.EventId = e.EventId
                               WHERE e.AccountId=? AND e.LastSessionDate < ?""", (aid, today)).fetchone()
        out[aid] = {"name": a[0], "created": a[1], "run": run, "risk": dict(r) if r else None,
                    "past_events": past[0], "past_gross": past[1] or 0}
    return out


def evaluate(row: dict, ctx: dict = None, cfg=CONFIG) -> dict:
    """Score 0-100, level (High/Medium/Low), advice and the reasons with their points."""
    reasons, pts = [], 0
    req = row.get("requested")

    def add(p, why):
        nonlocal pts
        if p:
            pts += p
            reasons.append(f"{why} (+{p:g})")

    def days(a, b):
        return None if not a or not b else (b - a).total_seconds() / 86400

    age = days(row.get("account_created"), req)
    if age is not None:
        add(_band_neg(age, cfg["account_age"]), f"account {int(age)} days old")
    prev = row.get("previous_payments")
    if prev == 0:
        add(cfg["first_payout"], "first payout (no previous payments)")
    amount = row.get("amount") or 0
    ev = row.get("event_date")
    if ev and req and ev.date() > req.date():
        add(_band(amount, cfg["before_event"]),
            f"£{amount:,.0f} paid before the event ({int(days(req, ev) + 0.999)} days away)")
    add(_band(amount, cfg["large_amount"]), f"large request £{amount:,.0f}")
    since = days(row.get("last_event_created"), req)
    if since is not None and 0 <= since < cfg["new_event"]["days"]:
        add(cfg["new_event"]["points"], f"event created {int(since)} days before the request")
    for key, label in (("foreign_cards_pct", "foreign cards"), ("overseas_bookings_pct", "overseas bookings"),
                       ("failed_payments_pct", "failed card payments")):
        v = row.get(key)
        add(_band(v, cfg[key.replace("_pct", "")]), f"{v:g}% {label}" if v is not None else label)
    risk = (ctx or {}).get("risk")
    if ctx is None:
        add(cfg["not_in_warehouse"], "account not in the warehouse yet")
    elif risk and risk["Priority"] in cfg["model_priority"]:
        add(cfg["model_priority"][risk["Priority"]], f"{risk['Priority']} in today's risk report")
    past_n, past_gross = (ctx or {}).get("past_events", 0), (ctx or {}).get("past_gross", 0)
    # previous payments come from the portal, so a count of 0 is real; first payout is scored above
    mult, why = risk_interim.trust_multiplier(prev or 0, past_n, past_gross, transfers_seen=False)
    if mult != 1:
        reasons.append(f"{why} (x{mult:.2f})")
    score = round(min(100.0, pts * mult), 1)
    level = next((name for limit, name in cfg["levels"] if score >= limit), "Low")
    return {"score": score, "level": level, "advice": cfg["advice"][level], "reasons": reasons,
            "multiplier": round(mult, 2)}


def _band_neg(value, bands):
    """First [[under N, points], ...] band met."""
    for limit, p in bands:
        if value < limit:
            return p
    return 0


def assess(conn, text: str, today: str) -> dict:
    """Parse the pasted table and evaluate every row. Returns only what the page shows:
    no organiser, comment, bank account name or email."""
    parsed = parse(text)
    ctx = warehouse_context(conn, [r["account_id"] for r in parsed["rows"]], today) if conn else {}
    out = []
    for r in parsed["rows"]:
        c = ctx.get(r["account_id"])
        ev = evaluate(r, c)
        out.append({**{k: v for k, v in r.items()}, **ev,
                    "account_name": c["name"] if c else None,
                    "report": c["risk"] if c else None,
                    "past_events": c["past_events"] if c else None,
                    "past_gross": c["past_gross"] if c else None})
    order = {"High": 0, "Medium": 1, "Low": 2}
    summary = {lvl: {"n": sum(1 for r in out if r["level"] == lvl),
                     "amount": round(sum(r["amount"] or 0 for r in out if r["level"] == lvl), 2)}
               for lvl in ("High", "Medium", "Low")}
    return {"rows": sorted(out, key=lambda r: (order[r["level"]], -r["score"], r["n"])),
            "problems": parsed["problems"], "summary": summary, "version": CONFIG["version"]}
