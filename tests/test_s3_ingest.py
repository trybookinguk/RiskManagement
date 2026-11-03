import csv
import importlib.util
import os
import pathlib

import pandas as pd
import pytest

from rm import config, db, s3
from rm.ingest import s3_reports as rep

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Real export headers (docs/s3_inventory 2026-11-01 / s3reporting schema doc).
BOOKING_HEADER = ["BookingId", "BookingTransactionId", "AccountId", "AccountName", "DateTimeCreated", "EventId",
                  "EventName", "DonationCampaignId", "DonationCampaignName", "TransactionDate", "BookingUrlId",
                  "PaymentReceived", "TicketQuantity", "BookingFee", "CardFee", "ProcessingFee", "Surcharge",
                  "ProcessingFeeSurcharge", "TicketFee", "TransactionType", "PaymentType", "EventPostcode",
                  "AccountPostcode", "EventDate", "Industry", "SubIndustry", "GatewayGroup", "DGRStatus",
                  "CustomerId", "IPCountry", "Status", "Wallet", "GatewayName", "GatewayId", "GatewayReference",
                  "GiftCertificateTypeName", "GiftCertificateId", "BookingCountryCode", "IPAddress"]
ACCOUNT_HEADER = ["Id", "AccountName", "AccountStatus", "DateTimeCreated", "IsVerified", "BankAccountIsDeleted",
                  "BankAccountName", "BSB", "AccNumber", "BankAccountStatusId", "LastLogIn", "FirstEventCreation",
                  "LastEventCreation", "Industry", "SubIndustry", "GatewayGroup", "State", "Postcode", "Country"]
USER_HEADER = ["UserId", "RoleId", "RoleName", "Username", "FirstName", "LastName", "IsDeleted", "AccountId",
               "Unsubscribed", "LastLoginDatetime"]
RISK_HEADER = ["AccountId", "AccountName", "FullBalance", "Balance", "SalesForUpcomingEvents", "Exposure",
               "FutureDays", "LastEventDate"]
RECON_HEADER = ["BookingTransactionId", "TransactionDate", "GatewayId", "BookingUrlId", "CardType", "CardNo",
                "Amount", "Transaction", "GatewayMessage", "PayoutId", "ArrivalDate", "Provider", "AccountName"]


def write(root, key, header, rows, preamble=None):
    path = pathlib.Path(root) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        if preamble:
            f.write(preamble + "\n")
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(h, "") for h in header])
    return key


def booking(bt, acct, ev, txn, evdate, **kw):
    return {"BookingId": bt + 100000, "BookingTransactionId": bt, "AccountId": acct, "EventId": ev,
            "EventName": f"Event {ev}", "TransactionDate": txn, "EventDate": evdate,
            "BookingUrlId": f"guid-{bt}", "PaymentReceived": "10.00", "TicketQuantity": "2",
            "Status": "Success", "TransactionType": "Online", "PaymentType": "Card",
            "IPCountry": kw.get("ip_country", "GB"), "IPAddress": kw.get("ip", "81.2.69.160"),
            "BookingCountryCode": "GBR", "EventPostcode": "SW1A 1AA", "GatewayName": "Stripe",
            "GatewayId": f"ch_{bt}", **{k: v for k, v in kw.items() if k not in ("ip_country", "ip")}}


@pytest.fixture
def bucket(tmp_path):
    root = tmp_path / "bucket"
    # Lifetime snapshot (Sept) - account 1 (old), account 2 (new, compressed timeline)
    write(root, "2026/09/20260930-BookingDataAll-TBUK.csv", BOOKING_HEADER, [
        booking(1, 1, 10, "2024-01-05 10:00:00", "2024-02-01 19:00:00"),
        booking(2, 1, 11, "2025-06-01 12:00:00.123", "2025-07-01 19:00:00"),
        booking(3, 1, 11, "2025-06-02 09:00:00", "2025-07-01 19:00:00", ip_country="US", ip="8.8.8.8"),
    ])
    write(root, "2026/08/20260831-BookingDataAll-TBUK.csv", BOOKING_HEADER, [])  # older, ignored
    (root / "2026/10").mkdir(parents=True)
    (root / "2026/10/20261031-BookingDataAll-TBUK.csv").write_bytes(b"")          # empty -> skipped
    write(root, "2026/10/202610-BookingData-TBUK.csv", BOOKING_HEADER, [
        booking(3, 1, 11, "2025-06-02 09:00:00", "2025-07-01 19:00:00", ip_country="US", ip="8.8.8.8",
                Status="Refunded"),                                               # update
        booking(4, 2, 20, "2026-10-29 08:00:00", "2026-11-03 20:00:00"),
    ])
    write(root, "2026/11/202611-BookingData-TBUK.csv", BOOKING_HEADER, [
        booking(5, 2, 20, "2026-11-01 08:00:00", "2026-11-03 20:00:00"),
    ])
    write(root, "2026/11/202611-Accounts-TBUK.csv", ACCOUNT_HEADER, [
        {"Id": 1, "AccountName": "Old Theatre", "AccountStatus": "Active", "DateTimeCreated": "2023-12-01 09:00:00",
         "IsVerified": "1", "FirstEventCreation": "2023-12-20 10:00:00", "LastEventCreation": "2025-05-20 10:00:00",
         "BSB": "123456", "AccNumber": "12345678", "Postcode": "M1 1AE", "Country": "GB"},
        {"Id": 2, "AccountName": "New Promoter", "AccountStatus": "Active", "DateTimeCreated": "28/10/2026 09:00",
         "IsVerified": "0", "FirstEventCreation": "2026-10-28 15:00:00", "LastEventCreation": "2026-10-28 15:00:00",
         "Postcode": "not a postcode"},
    ])
    write(root, "2026/11/202611-users-TBUK.csv", USER_HEADER, [
        {"UserId": 7, "RoleName": "Admin", "Username": "Owner@Example.com", "FirstName": "Jo", "AccountId": 2,
         "IsDeleted": "False", "LastLoginDatetime": "2026-11-02T08:00:00Z"},
    ])
    write(root, "2026/11/202611-RiskReport-TBUK.csv", RISK_HEADER, [
        {"AccountId": 2, "AccountName": "New Promoter", "FullBalance": "1,250.50", "Balance": "1000",
         "SalesForUpcomingEvents": "1250.50", "Exposure": "1250.50", "FutureDays": "1",
         "LastEventDate": "2026-11-03"},
    ])
    write(root, "2026/11/202611-TransactionReconciliation-TBUK.csv", RECON_HEADER, [
        {"BookingTransactionId": 5, "TransactionDate": "2026-11-01 08:00:00", "GatewayId": "ch_5",
         "CardType": "VISA", "CardNo": "************4242", "Amount": "10", "Transaction": "Payment",
         "GatewayMessage": "Approved"},
        {"BookingTransactionId": 5, "TransactionDate": "2026-11-01 08:05:00", "GatewayId": "ch_5",
         "CardType": "VISA", "CardNo": "4242", "Amount": "-10", "Transaction": "Refund"},
    ], preamble="First Gateway Id,ch_1,Start Booking Transaction Id,1")
    return root


def run(*args, today="2026-11-02"):
    spec = importlib.util.spec_from_file_location("ingest_s3", ROOT / "bin" / "ingest_s3.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(list(args) + ["--today", today])


def open_db():
    return db.connect(config.get_settings().risk_db)


# ---------------------------------------------------------------- helpers ---

def test_header_offset_skips_preamble():
    head = b"\xef\xbb\xbfFirst Gateway Id,ch_1,Start Booking Transaction Id,1\nA,B,C\n1,2,3\n"
    assert s3.header_offset(head) == (1, ["A", "B", "C"])
    assert s3.header_offset(b"X\nA,B,C,D\n1,2,3,4\n")[0] == 1      # stray narrow line
    assert s3.header_offset(b"A,B\n1,2\n") == (0, ["A", "B"])


def test_to_iso_formats():
    out = s3.to_iso(pd.Series(["2026-11-02 10:01:02.123", "2026-11-02T10:01:02Z", "02/11/2026 10:01",
                               "2026-11-02", "", "NULL", "garbage"])).tolist()
    assert out == ["2026-11-02 10:01:02", "2026-11-02 10:01:02", "2026-11-02 10:01:00",
                   "2026-11-02 00:00:00", None, None, None]


def test_number_helpers():
    assert s3.to_float(pd.Series(["1,250.50", "£3", "", "x"])).tolist() == [1250.5, 3.0, None, None]
    assert s3.to_int(pd.Series(["7", "7.0", ""])).tolist() == [7, 7, None]
    assert s3.to_bool(pd.Series(["True", "0", "yes", ""])).tolist() == [1, 0, 1, None]


def test_plan_picks_files(bucket):
    objs = s3.LocalSource(str(bucket)).list([])
    from datetime import date
    spec = importlib.util.spec_from_file_location("ingest_s3", ROOT / "bin" / "ingest_s3.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    full = mod.plan(objs, date(2026, 11, 2), True, 1100)
    assert [s[0] for s in full][:3] == ["BookingDataAll", "BookingData", "BookingData"]
    assert full[0][1].endswith("20260930-BookingDataAll-TBUK.csv")       # newest NON-empty
    nightly = mod.plan(objs, date(2026, 11, 2), False, 1100)
    assert "BookingDataAll" not in [s[0] for s in nightly]
    assert {s[0] for s in nightly} == {"BookingData", "Accounts", "users", "RiskReport", "TransactionReconciliation"}


# ------------------------------------------------------------- end to end ---

def test_full_refresh_then_nightly(env, bucket, capsys):
    assert run("--local-dir", str(bucket), "--full-refresh") == 0
    conn = open_db()
    b = {r["BookingTransactionId"]: dict(r) for r in conn.execute("SELECT * FROM bookings")}
    assert set(b) == {1, 2, 3, 4, 5}
    assert b[3]["Status"] == "Refunded"                       # later monthly file wins
    assert b[2]["TransactionDate"] == "2025-06-01 12:00:00"
    assert b[1]["EventPostcodeOutward"] == "SW1A"
    assert b[1]["IPHash"] and len(b[1]["IPHash"]) == 32 and "81.2" not in b[1]["IPHash"]
    assert b[1]["IPHash"] == b[2]["IPHash"] != b[3]["IPHash"]

    a = {r["AccountId"]: dict(r) for r in conn.execute("SELECT * FROM accounts")}
    assert a[2]["DateTimeCreated"] == "2026-10-28 09:00:00"   # UK d/m/Y parsed
    assert a[1]["PostcodeOutward"] == "M1" and a[2]["PostcodeOutward"] is None
    assert "BSB" not in a[1] and "AccNumber" not in a[1]       # bank details never loaded

    u = conn.execute("SELECT * FROM users").fetchone()
    assert u["UsernameHash"] and "@" not in u["UsernameHash"]
    h = db.get_hasher(conn, config.get_settings().risk_passphrase)
    assert u["UsernameHash"] == h.email("owner@example.com")

    bal = conn.execute("SELECT * FROM balances").fetchone()
    assert (bal["SnapshotDate"], bal["FullBalance"]) == ("2026-11-02", 1250.5)

    recon = conn.execute("SELECT TxnType, CardLast4 FROM gateway_recon ORDER BY TxnType").fetchall()
    assert [tuple(r) for r in recon] == [("Payment", "4242"), ("Refund", "4242")]

    ev = {r["EventId"]: dict(r) for r in conn.execute("SELECT * FROM events")}
    assert ev[20]["FirstSessionDate"] == "2026-11-03 20:00:00"
    assert (ev[20]["EventCreated"], ev[20]["EventCreatedSource"]) == ("2026-10-28 15:00:00", "last_event")
    assert (ev[11]["EventCreated"], ev[11]["EventCreatedSource"]) == ("2025-05-20 10:00:00", "last_event")
    assert (ev[10]["EventCreated"], ev[10]["EventCreatedSource"]) == ("2023-12-20 10:00:00", "first_event")

    life = {r["AccountId"]: dict(r) for r in conn.execute("SELECT * FROM account_lifetime")}
    assert (life[1]["Events"], life[1]["Bookings"], life[1]["ForeignIpBookings"], life[1]["RefundedBookings"]) \
        == (2, 3, 1, 1)
    assert life[2]["Gross"] == 20.0
    conn.close()

    # Re-run the same day: unchanged files are skipped.
    capsys.readouterr()
    assert run("--local-dir", str(bucket)) == 0
    assert "unchanged since last load" in capsys.readouterr().out


def test_snapshot_gives_exact_event_created(env, bucket):
    assert run("--local-dir", str(bucket), "--full-refresh") == 0
    # Next day: account 2 created a new event (LastEventCreation moves on) and it starts selling.
    write(bucket, "2026/11/202611-Accounts-TBUK.csv", ACCOUNT_HEADER, [
        {"Id": 2, "AccountName": "New Promoter", "DateTimeCreated": "2026-10-28 09:00:00",
         "FirstEventCreation": "2026-10-28 15:00:00", "LastEventCreation": "2026-11-02 18:00:00"},
    ])
    write(bucket, "2026/11/202611-BookingData-TBUK.csv", BOOKING_HEADER, [
        booking(5, 2, 20, "2026-11-01 08:00:00", "2026-11-03 20:00:00"),
        booking(6, 2, 21, "2026-11-03 07:00:00", "2026-11-05 20:00:00"),
    ])
    os.utime(bucket / "2026/11/202611-Accounts-TBUK.csv", (2e9, 2e9))
    os.utime(bucket / "2026/11/202611-BookingData-TBUK.csv", (2e9, 2e9))
    assert run("--local-dir", str(bucket), today="2026-11-03") == 0
    conn = open_db()
    ev = {r["EventId"]: dict(r) for r in conn.execute("SELECT * FROM events WHERE AccountId=2")}
    assert (ev[21]["EventCreated"], ev[21]["EventCreatedSource"]) == ("2026-11-02 18:00:00", "snapshot")
    assert ev[20]["EventCreatedSource"] == "first_event"
    snaps = conn.execute("SELECT SnapshotDate, LastEventCreation FROM account_snapshots WHERE AccountId=2 "
                         "ORDER BY 1").fetchall()
    assert [tuple(r) for r in snaps] == [("2026-11-02", "2026-10-28 15:00:00"), ("2026-11-03", "2026-11-02 18:00:00")]
    conn.close()


def test_full_card_number_rejects_file(env, bucket, capsys):
    write(bucket, "2026/11/202611-TransactionReconciliation-TBUK.csv", RECON_HEADER, [
        {"BookingTransactionId": 9, "TransactionDate": "2026-11-01", "GatewayId": "ch_9",
         "CardNo": "************1111", "Transaction": "Payment"},
        {"BookingTransactionId": 8, "TransactionDate": "2026-11-01", "GatewayId": "ch_8",
         "CardNo": "4111111111111111", "Transaction": "Payment"},
    ])
    assert run("--local-dir", str(bucket), "--only", "TransactionReconciliation") == 1
    out = capsys.readouterr().out
    assert "REJECTED" in out and "4111111111111111" not in out
    conn = open_db()
    assert conn.execute("SELECT COUNT(*) FROM gateway_recon").fetchone()[0] == 0
    r = conn.execute("SELECT status FROM import_log WHERE source='s3:TransactionReconciliation'").fetchone()
    assert r["status"] == "failed"
    conn.close()


def test_recon_retention(env, bucket):
    write(bucket, "2023/09/202309-TransactionReconciliation-TBUK.csv", RECON_HEADER, [
        {"BookingTransactionId": 1, "TransactionDate": "2023-09-01", "GatewayId": "ch_1", "Transaction": "Payment"}])
    assert run("--local-dir", str(bucket), "--full-refresh") == 0
    conn = open_db()
    dates = [r[0] for r in conn.execute("SELECT TransactionDate FROM gateway_recon")]
    assert dates and all(d >= "2023-10-29" for d in dates)    # 1,100 days before 2026-11-02
    conn.close()


def test_dry_run_writes_nothing(env, bucket, capsys):
    assert run("--local-dir", str(bucket), "--full-refresh", "--dry-run") == 0
    assert "20260930-BookingDataAll" in capsys.readouterr().out
    assert not os.path.exists(config.get_settings().risk_db)


def test_missing_required_column_fails_cleanly(env, bucket, capsys):
    write(bucket, "2026/11/202611-RiskReport-TBUK.csv", ["Name", "Balance"], [{"Name": "x", "Balance": "1"}])
    assert run("--local-dir", str(bucket), "--only", "RiskReport") == 1
    assert "missing required columns" in capsys.readouterr().out


def test_wrong_passphrase_stops_before_loading(env, bucket, monkeypatch, capsys):
    assert run("--local-dir", str(bucket), "--only", "RiskReport") == 0
    monkeypatch.setenv("RISK_ENCRYPTION_PASSPHRASE", "different")
    assert run("--local-dir", str(bucket), "--only", "RiskReport", "--force") == 1
    assert "does not match" in capsys.readouterr().out


# ----------------------------------------------------------------- S3 API ---

def test_s3_source_with_moto(bucket, monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with moto.mock_aws():
        c = boto3.client("s3", region_name="eu-west-2")
        c.create_bucket(Bucket="rm-test", CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        for p in bucket.rglob("*.csv"):
            c.put_object(Bucket="rm-test", Key=str(p.relative_to(bucket)), Body=p.read_bytes())
        src = s3.S3Source("rm-test", client=c)
        objs = src.list(["2026/11/"])
        assert s3.latest(objs, "RiskReport")["Key"] == "2026/11/202611-RiskReport-TBUK.csv"
        chunks = list(s3.read_chunks(src, "2026/11/202611-TransactionReconciliation-TBUK.csv",
                                     rep.RECON_COLUMNS))
        assert list(chunks[0]["CardNo"]) == ["************4242", "4242"]
        assert "AccountName" not in chunks[0].columns
