"""Fraud-list pattern from 30 Oct: account opened in the night, fireworks event two days
away, £100 tickets, £1,000 sold straight away, no phone numbers, no bank transfers -
against a long-standing organiser running the same kind of event."""
from datetime import date

import pytest

from rm import config, db, risk_interim
from test_s3_ingest import ACCOUNT_HEADER, BOOKING_HEADER, booking, run as run_s3, stamp, write
from test_sharepoint_ingest import FakeGraph, row, run as run_sp, to_csv

TODAY = "2026-10-30"
TRANSFER_HEADER = ["Amount", "DateCreated", "Filename", "AccountId"]


@pytest.fixture
def scenario(env, tmp_path):
    root = tmp_path / "bucket"
    rows = []
    # Fraud: account 30, created Fri 01:30, event 300 on Sun, 10 x £100 tickets sold 03:00-07:30.
    for i in range(10):
        rows.append(booking(3000 + i, 30, 300, f"2026-10-30 0{3 + i // 2}:{(i % 2) * 30:02d}:00",
                            "2026-11-01 19:00:00", PaymentReceived="100.00", TicketQuantity="1",
                            EventName="Fireworks Spectacular"))
    # Trusted: account 40 since 2018, 12 past events, then the same pattern for event 400.
    bt = 4000
    for ev in range(401, 413):
        for _ in range(5):
            bt += 1
            rows.append(booking(bt, 40, ev, f"2025-{ev - 400:02d}-01 10:00:00", f"2025-{ev - 400:02d}-20 19:00:00",
                                PaymentReceived="1000.00", TicketQuantity="20"))
    for i in range(10):
        rows.append(booking(4500 + i, 40, 400, f"2026-10-30 0{3 + i // 2}:{(i % 2) * 30:02d}:00",
                            "2026-11-01 19:00:00", PaymentReceived="100.00", TicketQuantity="1"))
    write(root, "2026/10/202610-BookingData-TBUK.csv", BOOKING_HEADER, rows)
    write(root, "2026/10/202610-Accounts-TBUK.csv", ACCOUNT_HEADER, [
        {"Id": 30, "AccountName": "Sparkle Events", "DateTimeCreated": "2026-10-30 01:30:00",
         "FirstEventCreation": "2026-10-30 02:10:00", "LastEventCreation": "2026-10-30 02:10:00"},
        {"Id": 40, "AccountName": "Village Bonfire Society", "DateTimeCreated": "2018-03-01 02:00:00",
         "FirstEventCreation": "2018-03-02 10:00:00", "LastEventCreation": "2026-10-28 10:00:00"},
    ])
    write(root, "2026/10/202610-transfers-TBUK.csv", TRANSFER_HEADER,
          [{"Amount": "500.00", "DateCreated": f"2025-{m:02d}-{d:02d} 09:00:00", "Filename": f"payout{m}{d}.aba",
            "AccountId": 40} for m in range(1, 13) for d in (5, 20)])
    stamp(root, "2026-10-30 05:00")
    assert run_s3("--local-dir", str(root), "--full-refresh", today=TODAY) == 0
    sp = [row(3000 + i, f"10/30/2026 0{3 + i // 2}:{(i % 2) * 30:02d}:00", desc="", status="Successful",
              **{"Booking Phone": ""}) for i in range(10)]
    sp += [row(4500 + i, f"10/30/2026 0{3 + i // 2}:{(i % 2) * 30:02d}:00", desc="", status="Successful")
           for i in range(10)]
    assert run_sp(FakeGraph({"30_Oct_26_BookingTransactionsReport.csv": to_csv(sp)}), today=TODAY) == 0
    conn = db.connect(config.get_settings().risk_db)
    yield conn
    conn.close()


def test_transfers_loaded(scenario):
    n = scenario.execute("SELECT COUNT(*) FROM bank_transfers WHERE AccountId=40").fetchone()[0]
    assert n == 24


def test_fraud_pattern_is_p1_and_trusted_organiser_is_not(scenario):
    r = risk_interim.score(scenario, date.fromisoformat(TODAY))
    ev = {e["EventId"]: e for e in r["events"]}
    bad, good = ev[300], ev[400]
    assert bad["Priority"] == "P1" and bad["FraudScore"] == 100.0
    for text in ("account created 01:30 (night)", "event in 2d", "avg ticket £100", "£1,000 sold in first 24h",
                 "100% bookings without a phone", "no bank transfers yet", "new account"):
        assert text in bad["Reasons"], text
    assert bad["Transfers"] == 0 and bad["TrustMultiplier"] == 1.3

    assert good["TrustMultiplier"] == 0.43                     # 24 transfers 0.6 x 12 past events 0.9 x £60k 0.8
    assert "trusted: 24 bank transfers, 12 past events £60,000" in good["Reasons"]
    assert "night" not in good["Reasons"] and "without a phone" not in good["Reasons"]
    assert good["FraudScore"] < 40 and good["Priority"] == "P2"          # still reviewed: £1,000 held, 2 days out
    acc = {a["AccountId"]: a for a in r["accounts"]}
    assert acc[30]["Priority"] == "P1" and acc[40]["PastEvents"] == 12


def test_signal_helpers():
    assert risk_interim.night_created("2026-10-30 01:30:00").hour == 1
    assert risk_interim.night_created("2026-10-30 09:30:00") is None
    assert risk_interim.night_created("2026-10-30") is None                     # no time
    utc = dict(risk_interim.CONFIG, fraud=dict(risk_interim.CONFIG["fraud"],
               night_created=dict(risk_interim.CONFIG["fraud"]["night_created"], times_are="UTC")))
    assert risk_interim.night_created("2026-07-01 04:30:00", utc) is None       # 05:30 BST
    assert risk_interim.trust_multiplier(0, 0, 0, True)[0] == 1.3
    assert risk_interim.trust_multiplier(0, 8, 9000, False) == (pytest.approx(0.81), "trusted: 8 past events £9,000")
    assert risk_interim.trust_multiplier(3, 0, 0, True) == (1.0, "history: 3 bank transfers")
    assert risk_interim.trust_multiplier(6, 0, 0, True)[0] == 0.8


def test_reasons_show_points(scenario):
    r = risk_interim.score(scenario, date.fromisoformat(TODAY))
    bad = {e["EventId"]: e for e in r["events"]}[300]
    assert "account created 01:30 (night) +3" in bad["Reasons"] and "avg ticket £100 +4" in bad["Reasons"]
    assert "new account (0d) x1.5" in bad["Reasons"] and "x1.3" not in bad["Reasons"]   # larger multiplier only
    good = {e["EventId"]: e for e in r["events"]}[400]
    assert good["Reasons"].endswith("trusted: 24 bank transfers, 12 past events £60,000 x0.43")
    print("\n" + bad["Reasons"] + "\n" + good["Reasons"], bad["FraudScore"], good["FraudScore"], good["Priority"])
