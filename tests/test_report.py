import importlib.util
import pathlib
from datetime import date

import pytest

from rm import config, db, risk_interim
from test_s3_ingest import BOOKING_HEADER, booking, bucket, run as run_s3, stamp, write  # noqa: F401
from test_sharepoint_ingest import FakeGraph, row, run as run_sp, to_csv

ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_report(*args, graph=None, today="2026-11-02"):
    spec = importlib.util.spec_from_file_location("risk_report", ROOT / "bin" / "risk_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(list(args) + ["--today", today], graph=graph)


@pytest.fixture
def loaded(env, bucket):
    """S3 fixture (account 2: created 28 Oct, event 20 created 28 Oct, first session 3 Nov) +
    SharePoint attempts on event 20's bookings: 2 approved, 3 declined (plus an unknown message)."""
    assert run_s3("--local-dir", str(bucket), "--full-refresh") == 0
    sp = to_csv([row(4, "2026-10-29 08:00"), row(5, "2026-11-01 08:00"),
                 row(4, "2026-10-29 07:58", desc="Insufficient funds", status="Declined"),
                 row(5, "2026-11-01 07:50", desc="Do Not Honour", status="Declined", email="x@spam.example"),
                 row(5, "2026-11-01 07:55", desc="Card expired", status="Declined"),
                 row(999999, "2026-11-01 07:00", desc="Gateway hiccup 42", status="Pending",
                     **{"Event Name": "Event 20", "Event Organiser": "New Promoter"})])
    assert run_sp(FakeGraph({"t.csv": sp}), today="2026-11-02") == 0
    return bucket


def test_interim_scoring(loaded):
    conn = db.connect(config.get_settings().risk_db)
    r = risk_interim.score(conn, date(2026, 11, 2))
    ev = {e["EventId"]: e for e in r["events"]}
    e20 = ev[20]
    assert e20["Gaps"][0] == pytest.approx(0.25) and all(g < 7 for g in e20["Gaps"])
    assert e20["DeclinePct"] == pytest.approx(3 / 6 * 100)       # 6 attempts mapped to event 20 (incl. by name)
    assert e20["AgeDays"] == 4
    # timeline 8 + declines 5*min(50/20,2)/2=5 -> 13/19*100 * 1.5 -> capped at 100
    assert e20["FraudScore"] == 100.0 and e20["Priority"] == "P1"
    assert "short timeline" in e20["Reasons"] and "declined" in e20["Reasons"] and "new account" in e20["Reasons"]
    acc = {a["AccountId"]: a for a in r["accounts"]}
    assert acc[2]["FraudScore"] == 100.0 and acc[2]["AmountAtRisk"] == 1250.5
    assert r["accounts"][0]["AccountId"] == 2 and r["events"][0]["EventId"] == 20
    assert 1 not in acc or acc[1]["Priority"] in ("-", "P4")           # old account, past events only
    conn.close()


def test_age_and_timeline_helpers():
    assert risk_interim.age_multiplier("2026-10-30 00:00:00", date(2026, 11, 2))[0] == 1.5
    assert risk_interim.age_multiplier("2025-01-01 00:00:00", date(2026, 11, 2))[0] == 1.0
    pts, gaps = risk_interim.timeline_points("2026-01-01", "2026-03-01", "2026-03-04", "last_event")
    assert pts == 1.5 and round(gaps[1]) == 3
    pts, _ = risk_interim.timeline_points("2026-01-01", "2026-03-01", "2026-03-04", "first_booking")
    assert pts == 0.75                                                   # upper-bound date: half
    assert risk_interim.timeline_points(None, "2026-03-01", "2026-03-04", "x")[0] == 0
    assert risk_interim.priority(65, 10) == ("P2", "Critical", "Low")
    assert risk_interim.priority(10, 90) == ("P3", "Low", "Critical")


def test_report_email_and_changes(loaded, capsys):
    g = FakeGraph()
    assert run_report(graph=g) == 0
    to, subj, page = g.mail[0]
    assert to == "henry@trybooking.co.uk"
    assert subj.startswith("Risk report 2026-11-02: 0 P1 account(s), 1 P1 event(s)")
    for text in ("Top 10 risk accounts", "Top 10 risk events", "New Promoter", "Booking Transactions import check",
                 "UNCLASSIFIED", "Gateway hiccup 42", "event_name"):
        assert text in page
    for pii in ("jane@example.com", "x@spam.example", "07700", "Jane", "Doe"):
        assert pii not in page
    out = capsys.readouterr().out
    assert "Emailed to henry@trybooking.co.uk" in out
    html_file = pathlib.Path(config.get_settings().data_dir) / "reports" / "2026-11-02_RiskReport.html"
    assert html_file.exists()

    # Next day: previous counts appear, stale RiskReport flagged, scores saved per day.
    assert run_report("--no-email", today="2026-11-04") == 0
    out = capsys.readouterr().out
    assert "RiskReport not refreshed for 2 days" in out
    conn = db.connect(config.get_settings().risk_db)
    days = [r[0] for r in conn.execute("SELECT DISTINCT RunDate FROM risk_daily ORDER BY 1")]
    assert days == ["2026-11-02", "2026-11-04"]
    conn.close()


def test_check_transactions_command(loaded, capsys):
    assert run_report("--check-transactions") == 0
    out = capsys.readouterr().out
    assert "Booking Transactions import check" in out
    assert "booking_guid" in out and "event_name" in out
    assert "UNCLASSIFIED" in out and "Gateway hiccup 42" in out
    assert "jane@example.com" not in out


def test_report_email_failure_is_an_error(loaded, capsys):
    class Broken(FakeGraph):
        def send_mail(self, *a, **k):
            from rm.graph import GraphError
            raise GraphError("403")
    assert run_report(graph=Broken()) == 1
    assert "not emailed" in capsys.readouterr().out


def test_empty_warehouse_report(warehouse, capsys):
    assert run_report("--no-email") == 0
    out = capsys.readouterr().out
    assert "Top 10 risk accounts" in out


def test_check_matches_real_export_mix(warehouse, capsys):
    """Shape of the 3 Nov export: blank messages on most rows, S1/S2 codes on failures, refunds, no S3 loaded."""
    rows = ([row(i, "11/03/2026 10:00", desc="", status="Successful") for i in range(1, 9)]
            + [row(20, "11/03/2026 10:01", desc="S1 - Unknown Reason", status="Failed"),
               row(21, "11/03/2026 10:02", desc="", status="Failed"),
               row(22, "11/03/2026 10:03", desc="", status="Unknown", **{"Gateway ID": ""}),
               row(23, "11/03/2026 10:04", desc="", status="Successful", **{"Transaction": "RefundedOnline"})])
    assert run_sp(FakeGraph({"3_Nov_26_BookingTransactionsReport.csv": to_csv(rows)}), today="2026-11-05") == 0
    assert run_report("--check-transactions", today="2026-11-05") == 0
    out = capsys.readouterr().out
    assert "11 payment attempts (refunds excluded): 2 declined, 1 unclassified; 1 refund rows" in out
    lines = [l for l in out.splitlines() if "(blank)" in l and ("approved" in l or "decline" in l)]
    assert any("approved" in l and "Successful" in l for l in lines)        # blank+Successful shown as approved
    assert any("decline" in l and "Failed" in l for l in lines)             # blank+Failed shown as decline
    assert "No transactions linked to S3 bookings. S3 bookings loaded: 0" in out
    assert "export: 99999999-9999-9999-9999-999999999999" in out
