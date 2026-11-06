"""Fund transfer requests pasted from the admin portal: parsing and the per-row risk check.

All rows here are made up (example.test addresses, invented names)."""
from datetime import datetime

from rm import fund_requests as fr

HEADERS = "\n".join(h + "\t " for h in [
    "Trust Level", "Event Organiser", "Comment", "Bank Account", "Amount (GBP)", "Date Requested",
    "Date Account Created", "Date Last Event Created", "Date of Event", "Exposure", "Remaining",
    "# Previous Payments", "% Foreign Cards", "% Overseas Bookings", "% Failed Card Payments", "Account ID",
    "Email Address"]) + "\n \t\n"


def portal_row(amount, tail, trust="Untrusted", organiser="Test Organiser", comment="Add a comment",
               bank="Test Club"):
    return f"{trust}\n{organiser}\t\n{comment}\n{bank}\t\n{amount}\n{tail}\n"


QUIET = "6Nov26 10:19 PM\t18Jan26\t20Oct26\t22Nov26\t0.00\t162.20\t11\t1.01\t2.02\t4.55\t18967\tquiet@example.test"
OLD = "6Nov26 6:58 PM\t19Sep17\t16Sep26\t6Nov26\t28.00\t28.00\t14\t0.47\t0.87\t2.54\t388\told@example.test"
HOT = "6Nov26 2:05 AM\t1Nov26\t3Nov26\t20Nov26\t0.00\t0.00\t0\t62.00\t35.00\t31.00\t30\thot@example.test"
PASTE = HEADERS + portal_row("30.00", QUIET) + portal_row("6811.48", OLD) + portal_row("4,950.00", HOT)


def test_parse_portal_copy():
    out = fr.parse(PASTE)
    assert out["problems"] == []
    a, b, c = out["rows"]
    assert a["account_id"] == 18967 and a["amount"] == 30.0 and a["trust_level"] == "Untrusted"
    assert a["requested"] == datetime(2026, 11, 6, 22, 19) and a["account_created"] == datetime(2026, 1, 18)
    assert a["event_date"] == datetime(2026, 11, 22) and a["previous_payments"] == 11
    assert (a["foreign_cards_pct"], a["overseas_bookings_pct"], a["failed_payments_pct"]) == (1.01, 2.02, 4.55)
    assert b["account_id"] == 388 and b["amount"] == 6811.48 and b["account_created"] == datetime(2017, 9, 19)
    assert b["requested"] == datetime(2026, 11, 6, 18, 58)
    assert c["amount"] == 4950.0 and c["requested"] == datetime(2026, 11, 6, 2, 5)
    for r in out["rows"]:     # personal fields are never kept
        assert not {"organiser", "comment", "bank_account", "email"} & set(r)


def test_parse_one_line_per_row_and_garbage():
    line = "\t".join(["Untrusted", "Test Organiser", "", "Test Club", "30.00", QUIET])
    out = fr.parse(line + "\n")
    assert out["rows"][0]["account_id"] == 18967 and out["rows"][0]["amount"] == 30.0
    assert fr.parse("hello\nworld")["problems"][0].startswith("No requests found")
    assert fr.parse_date("12 PM") is None and fr.parse_date("6Nov26 12:30 AM") == datetime(2026, 11, 6, 0, 30)


def test_evaluate_levels():
    rows = fr.parse(PASTE)["rows"]
    quiet, old, hot = (fr.evaluate(r, {"risk": None, "past_events": 0, "past_gross": 0}) for r in rows)
    assert quiet["level"] == "Low" and old["level"] == "Low"
    assert "trusted: 14 bank transfers (x0.80)" in old["reasons"]
    assert hot["level"] == "High" and hot["score"] == 100
    for why in ("account 5 days old (+25)", "first payout (no previous payments) (+15)",
                "£4,950 paid before the event (14 days away) (+12)", "62% foreign cards (+25)",
                "31% failed card payments (+25)"):
        assert why in hot["reasons"], why
    missing = fr.evaluate(rows[0], None)
    assert "account not in the warehouse yet (+10)" in missing["reasons"]
