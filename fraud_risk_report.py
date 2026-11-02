#!/usr/bin/env python3
"""
Fraud Risk Report.

Builds a per-account and per-event fraud risk table from the warehouse and
emails it as an Excel workbook sorted by risk score. Scoring logic is explained
in docs/fraud_risk_report.md.

Lives in trybookinguk/RiskManagement and uses the shared warehouse, email,
SharePoint and S3 code from an s3reporting checkout (see fraud/__init__.py:
$S3REPORTING_DIR, ../s3reporting, or /root/s3reporting).

Steps
-----
1. Chargebacks: each --chargeback-file that exists is upserted into the
   warehouse ``chargebacks`` table (new disputes inserted, status/outcome
   changes updated and logged to ``chargeback_history``). PayPal "All Dispute
   Cases" and Stripe "Disputes export" files are auto-detected. Missing files
   are skipped with a warning and the existing table is used.
2. Chargebacks without a booking are matched to the warehouse ``bookings``
   (Invoice ID / Stripe Description GUID -> booking ID; gateway transaction ID
   as fallback) to get the Event Organiser Account Number and Event ID, and
   every chargeback's Account Tier is refreshed from the tier snapshot.
3. Optional supplementary data (--signals-file, --logins-file) is upserted for
   the tests whose inputs are not in the S3 exports (Stripe risk score, card
   issue country, browser time zone, failed emails, login IPs).
4. The S3 RiskReport (balances) is loaded — or --risk-report-file — and stored
   as ``risk_exposure``.
5. Risk tests (last 100 bookings per event / per account, last 10 logins per
   account), chargeback roll-ups and exposure are scored 0-100 and written to
   ``risk_fraud`` (+ ``risk_fraud_history``).
6. <date>_FraudRiskReport.xlsx is written to REPORTS_DIR and emailed to
   henry@trybooking.co.uk (auto-CCs Kathryn).

Usage (source .env first for AWS + Azure email credentials):
    set -a && source /root/s3reporting/.env && set +a
    cd /root/RiskManagement
    python3 fraud_risk_report.py \\
        --chargeback-file AllDisputeCases_20231102000000_20261031235959_O_01.CSV \\
        --chargeback-file Disputes_Export_Query_2014-07-31_to_2026-11-01.csv
    python3 fraud_risk_report.py                       # re-score with existing chargebacks
    python3 fraud_risk_report.py --load-only --chargeback-file new_disputes.csv
    python3 fraud_risk_report.py --risk-report-file 202611-RiskReport-TBUK.csv --no-email
    TEST_MODE=true python3 fraud_risk_report.py        # [TEST] subject, safe dry run
"""
import argparse
import io
import logging
import os
import sys
from datetime import date, datetime

import pandas as pd

from fraud import S3REPORTING_DIR
from fraud import chargebacks as cbmod
from fraud import risk as fr

DEFAULT_EMAIL = "henry@trybooking.co.uk"
DEFAULT_SHAREPOINT_FOLDER = "Platform Data/Users and Accounts"
# Graph sendMail caps the whole request at ~4 MB, and attachments travel base64
# (+33%). Bigger files need the draft/upload-session path, which needs
# Mail.ReadWrite — the app only has Mail.Send. So a workbook over 2.5 MB raw
# (~3.3 MB encoded) is linked from SharePoint instead of attached.
MAX_ATTACHMENT_BYTES = int(2.5 * 1024 * 1024)
XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")

log = logging.getLogger("fraud_risk_report")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _normalise_risk_report(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lstrip("﻿") for c in df.columns]
    if "AccountId" not in df.columns and "AccountID" in df.columns:
        df["AccountId"] = df["AccountID"]
    if "AccountId" not in df.columns:
        raise ValueError("RiskReport has no AccountId column.")
    df["AccountId"] = pd.to_numeric(df["AccountId"], errors="coerce").astype("Int64")
    for c in ("FullBalance", "Balance", "SalesForUpcomingEvents", "Exposure", "FutureDays"):
        if c not in df.columns:
            df[c] = 0
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(r"[£,]", "", regex=True),
                              errors="coerce").fillna(0)
    if "LastEventDate" in df.columns:
        df["LastEventDate"] = df["LastEventDate"].astype(str)
    return df


def load_risk_report(path: str, use_s3: bool) -> pd.DataFrame:
    if path:
        log.info("Reading RiskReport from %s", path)
        return _normalise_risk_report(pd.read_csv(path, encoding="utf-8-sig"))
    if not use_s3:
        return None
    try:
        from modules.utils.data_loader import get_loader
        df = get_loader().load_risk_report()
        log.info("Loaded RiskReport from S3: %d accounts", len(df))
        return _normalise_risk_report(df)
    except Exception as e:
        log.warning("Could not load RiskReport from S3 (%s) — balances will fall back to "
                    "upcoming sales from bookings.", e)
        return None


# ---------------------------------------------------------------------------
# Workbook
# ---------------------------------------------------------------------------

def _test_columns():
    cols = {}
    for t in fr.RISK_TESTS:
        unit = "" if t["key"] == "avg_risk_score" else " %"
        cols[f"{t['key']}__value"] = f"{t['name']}{unit}"
        cols[f"{t['key']}__result"] = f"{t['name']} Result"
    return cols


ACCOUNT_COLUMNS = {
    "AccountId": "Account Number", "AccountName": "Account Name", "AccountTier": "Account Tier",
    "AccountStatus": "Account Status", "AccountCreated": "Account Created",
    "RiskScore": "Risk Score", "RiskBand": "Risk Band", "TestPoints": "Test Points",
    "ChargebackPoints": "Chargeback Points", "ExposurePoints": "Exposure Points",
    "FailedTests": "Failed Tests", "TestsEvaluated": "Tests Evaluated",
    "Chargebacks": "Chargebacks", "CB_Lost": "CB Lost", "CB_Refunded": "CB Refunded",
    "CB_Open": "CB Open", "CB_Won": "CB Won", "CB_FraudClaims": "CB Fraud Claims",
    "CB_Amount": "CB Amount £", "CB_Last365": "CB Last 365d", "CB_RatioPct": "Dispute Ratio %",
    "LastChargebackDate": "Last Chargeback",
    "FullBalance": "Full Balance £", "Balance": "Balance £",
    "SalesForUpcomingEvents": "Sales Upcoming Events £", "Exposure": "Exposure £",
    "UpcomingSales": "Upcoming Sales (bookings) £", "UpcomingEvents": "Upcoming Events",
    "AmountAtRisk": "Amount At Risk £", "PaidBookings365": "Paid Bookings 365d",
    "GrossSales365": "Gross Sales 365d £", "failed_transactions__n": "Sample Bookings",
    "account_logins__n": "Sample Logins",
}
ACCOUNT_COLUMNS.update(_test_columns())

EVENT_COLUMNS = {
    "EventId": "Event ID", "EventName": "Event Name", "AccountId": "Account Number",
    "AccountName": "Account Name", "AccountTier": "Account Tier",
    "FirstEventDate": "First Session", "LastEventDate": "Last Session",
    "RiskScore": "Risk Score", "RiskBand": "Risk Band", "TestPoints": "Test Points",
    "ChargebackPoints": "Chargeback Points", "ExposurePoints": "Exposure Points",
    "FailedTests": "Failed Tests", "TestsEvaluated": "Tests Evaluated",
    "Chargebacks": "Chargebacks", "CB_Lost": "CB Lost", "CB_Refunded": "CB Refunded",
    "CB_Open": "CB Open", "CB_Won": "CB Won", "CB_FraudClaims": "CB Fraud Claims",
    "CB_Amount": "CB Amount £", "CB_RatioPct": "Dispute Ratio %",
    "LastChargebackDate": "Last Chargeback", "Bookings": "Bookings",
    "PaidBookings": "Paid Bookings", "Tickets": "Tickets", "GrossSales": "Gross Sales £",
    "AmountAtRisk": "Amount At Risk £", "failed_transactions__n": "Sample Bookings",
}
EVENT_COLUMNS.update(_test_columns())

CHARGEBACK_COLUMNS = {
    "CustomerEmail": "Customer Email", "DisputeDate": "Dispute Date", "InvoiceId": "Invoice ID",
    "DisputeStatus": "Dispute Status", "AccountId": "Event Organiser Account Number",
    "EventId": "Event ID", "AccountTier": "Account Tier", "Source": "Source",
    "DisputeStatusRaw": "Status (raw)", "DisputeReason": "Reason", "IsFraudClaim": "Fraud Claim",
    "DisputeAmount": "Amount £", "BookingTransactionId": "Booking Transaction ID",
    "MatchMethod": "Matched On", "DisputeId": "Dispute ID", "GatewayRef": "Gateway Ref",
    "SourceFile": "Source File", "FirstLoadedAt": "First Loaded", "LastUpdatedAt": "Last Updated",
}


# Where each test's data comes from. Only the booking-based ones have an S3
# source; the rest stay "No data" until the matching optional file is loaded.
TEST_DATA_SOURCE = {
    "account_logins": "--logins-file (AccountId, LoginDateTime, IPCountry)",
    "avg_risk_score": "--signals-file RiskScore column",
    "browser_timezone": "--signals-file BrowserTimeZone column",
    "card_location": "--signals-file CardCountry column",
    "card_outside_uk": "--signals-file CardCountry column",
    "failed_email": "--signals-file EmailDeliveryFailed column",
    "failed_transactions": "S3 BookingData (Status)",
    "ip_location": "S3 BookingData (IPCountry vs BookingCountryCode)",
    "outside_uk": "S3 BookingData (IPCountry)",
    "quicksale": "S3 BookingData (PaymentType) or --signals-file IsQuicksale",
}


def _tests_without_data(df) -> list:
    """Test keys where no row on this frame was evaluated (all "No data")."""
    if df is None or not len(df):
        return [t["key"] for t in fr.RISK_TESTS]
    return [t["key"] for t in fr.RISK_TESTS
            if f"{t['key']}__result" not in df.columns
            or not df[f"{t['key']}__result"].isin(["Pass", "Fail"]).any()]


def _pick(df, mapping, hide_tests=()):
    hidden = {f"{k}__{part}" for k in hide_tests for part in ("value", "result", "n")}
    cols = [c for c in mapping if c in df.columns and c not in hidden]
    return df[cols].rename(columns=mapping)


def _summary_rows(as_of, accounts, events, cb, cb_stats, match_stats, rr_loaded, coverage):
    rows = [("Fraud Risk Report", f"As of {as_of.isoformat()}"),
            ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")), ("", "")]
    for label, df in (("Accounts", accounts), ("Events", events)):
        counts = df["RiskBand"].value_counts().to_dict() if len(df) else {}
        rows.append((f"{label} scored", len(df)))
        for band in ("Critical", "High", "Medium", "Low"):
            rows.append((f"  {band}", counts.get(band, 0)))
    rows += [("", ""), ("Chargebacks in table", len(cb)),
             ("  matched to a booking", int(cb["AccountId"].notna().sum()) if len(cb) else 0),
             ("  unmatched", int(cb["AccountId"].isna().sum()) if len(cb) else 0)]
    for s in cb_stats:
        rows.append((f"  loaded {s['file']}",
                     f"{s['source']}: {s['read']} read, {s['new']} new, {s['changed']} changed, "
                     f"{s['unchanged']} unchanged"))
    if match_stats:
        rows.append(("  matched this run", f"{match_stats['matched']} of {match_stats['pending']} pending"))
    rows += [("", ""), ("RiskReport balances", "loaded" if rr_loaded else
                        "NOT loaded — Amount At Risk uses upcoming sales from bookings"),
             ("", ""), ("Test data coverage (event rows evaluated)", "")]
    for t in fr.RISK_TESTS:
        rows.append((f"  {t['name']}{'' if t['active'] else ' (inactive)'}", coverage.get(t["key"], 0)))
    missing = [t for t in fr.RISK_TESTS if not coverage.get(t["key"])]
    if missing:
        rows += [("", ""), ("Tests with no data (columns hidden, score 0)", "Data needed")]
        rows += [(f"  {t['name']}{'' if t['active'] else ' (inactive)'}",
                  TEST_DATA_SOURCE.get(t["key"], "")) for t in missing]
    unknown = sorted(fr.UNRECOGNISED_COUNTRIES.items(), key=lambda kv: -kv[1])
    rows += [("", ""), ("Unrecognised country values (counted as foreign)",
                        ", ".join(f"{k} ({v})" for k, v in unknown[:15]) if unknown else "none")]
    rows += [("", ""), ("Scoring", "Risk Score = Test Points (max 40) + Chargeback Points (max 40) "
                                   "+ Exposure Points (max 20). See docs/fraud_risk_report.md in RiskManagement."),
             ("Bands", "Critical >= 60, High >= 40, Medium >= 20, Low < 20")]
    return pd.DataFrame(rows, columns=["Item", "Value"])


def build_workbook(as_of, accounts, events, cb, summary_df) -> bytes:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    tests_df = pd.DataFrame([{"Name": t["name"], "Description": t["description"],
                              "Threshold": t["threshold"], "Active": t["active"],
                              "Weight (points)": t["weight"] if t["active"] else 0,
                              "Sample": f"last {fr.SAMPLE_LOGINS} logins" if t["scope"] == "logins"
                              else f"last {fr.SAMPLE_BOOKINGS} bookings"} for t in fr.RISK_TESTS])
    cb_sorted = cb.sort_values(["DisputeDate"], ascending=False) if len(cb) else cb
    sheets = [
        ("Summary", summary_df),
        ("Account Risk", _pick(accounts, ACCOUNT_COLUMNS, _tests_without_data(accounts))),
        ("Event Risk", _pick(events, EVENT_COLUMNS, _tests_without_data(events))),
        ("Chargebacks", _pick(cb_sorted, CHARGEBACK_COLUMNS)),
        ("Risk Tests", tests_df),
    ]
    fills = {"Critical": PatternFill("solid", fgColor="F8696B"),
             "High": PatternFill("solid", fgColor="FFB366"),
             "Medium": PatternFill("solid", fgColor="FFE699"),
             "Fail": PatternFill("solid", fgColor="F8CBAD")}
    header_fill = PatternFill("solid", fgColor="1F3864")

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets:
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = header_fill
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            if name == "Summary":
                ws.column_dimensions["A"].width = 45
                ws.column_dimensions["B"].width = 110
                continue
            ws.freeze_panes = "C2" if name in ("Account Risk", "Event Risk") else "A2"
            if ws.max_row > 1:
                ws.auto_filter.ref = ws.dimensions
            headers = [c.value for c in ws[1]]
            for idx, h in enumerate(headers, start=1):
                letter = get_column_letter(idx)
                sample = [str(v) for v in df.iloc[:200, idx - 1].tolist()] if len(df) else []
                width = max([len(str(h)) * 0.9] + [len(s) for s in sample]) + 2
                ws.column_dimensions[letter].width = min(max(width, 9), 60)
                if str(h).endswith("£"):
                    for (cell,) in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
                        cell.number_format = "£#,##0.00"
            band_idx = headers.index("Risk Band") + 1 if "Risk Band" in headers else None
            result_idx = [i + 1 for i, h in enumerate(headers) if str(h).endswith(" Result")]
            for row in ws.iter_rows(min_row=2):
                if band_idx and row[band_idx - 1].value in fills:
                    row[band_idx - 1].fill = fills[row[band_idx - 1].value]
                for i in result_idx:
                    if row[i - 1].value == "Fail":
                        row[i - 1].fill = fills["Fail"]
            ws.row_dimensions[1].height = 45
    return buf.getvalue()


def _email_body(as_of, accounts, events, summary_df, filename, web_url=None, attached=True,
                local_path=None):
    from modules.utils.email_utils import create_html_table
    top_a = _pick(accounts.head(10), {k: ACCOUNT_COLUMNS[k] for k in (
        "AccountId", "AccountName", "AccountTier", "RiskScore", "RiskBand", "Chargebacks",
        "CB_Lost", "AmountAtRisk", "FailedTests")})
    top_e = _pick(events.head(10), {k: EVENT_COLUMNS[k] for k in (
        "EventId", "EventName", "AccountId", "RiskScore", "RiskBand", "Chargebacks",
        "CB_Lost", "AmountAtRisk", "FailedTests")})
    style = "border-collapse: collapse; font-family: Arial; font-size: 12px;"
    bands = accounts["RiskBand"].value_counts().to_dict() if len(accounts) else {}
    if web_url and attached:
        where = (f'is attached ({filename}) and saved to SharePoint: '
                 f'<a href="{web_url}">{filename}</a>')
    elif web_url:
        where = (f'is on SharePoint: <a href="{web_url}">{filename}</a> '
                 f'(too large to attach)')
    elif attached:
        where = f"is attached ({filename})"
    else:
        where = (f"was too large to attach and could not be uploaded to SharePoint; "
                 f"it is on the Pi at {local_path}")
    return (
        f"<p>The Fraud Risk Report for {as_of.isoformat()} {where}, "
        f"sorted by risk score.</p>"
        f"<p><b>Accounts:</b> {len(accounts):,} scored — Critical {bands.get('Critical', 0)}, "
        f"High {bands.get('High', 0)}, Medium {bands.get('Medium', 0)}. "
        f"<b>Events:</b> {len(events):,} scored.</p>"
        f"<h3>Top 10 accounts</h3>{create_html_table(top_a.fillna(''), style=style)}"
        f"<h3>Top 10 events</h3>{create_html_table(top_e.fillna(''), style=style)}"
        f"<p>Risk Score (0-100) = risk tests (max 40) + chargebacks (max 40) + exposure "
        f"(max 20). Tests without data score 0 and their columns are hidden; "
        f"the Summary sheet lists them with the file each one needs.</p>"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Build and email the Fraud Risk Report.")
    p.add_argument("--db", help="SQLite warehouse path (default: warehouse.default_db_path()).")
    p.add_argument("--as-of", help="Treat this date as today (YYYY-MM-DD).")
    p.add_argument("--chargeback-file", action="append", default=[],
                   help="PayPal or Stripe dispute export to load (repeatable). Loaded if it exists.")
    p.add_argument("--signals-file", action="append", default=[],
                   help="Optional per-booking signals CSV (Stripe payments export etc.).")
    p.add_argument("--logins-file", action="append", default=[],
                   help="Optional account login history CSV (AccountId, LoginDateTime, IPAddress, IPCountry).")
    p.add_argument("--risk-report-file", help="Local RiskReport CSV instead of S3.")
    p.add_argument("--no-s3", action="store_true", help="Don't fetch the RiskReport from S3.")
    p.add_argument("--event-lookback-days", type=int, default=90,
                   help="Score events with a session on/after today minus N days (default 90).")
    p.add_argument("--booking-lookback-days", type=int, default=365,
                   help="Account-level booking sample window in days (default 365).")
    p.add_argument("--rematch", action="store_true", help="Re-match every chargeback, not just unmatched.")
    p.add_argument("--load-only", action="store_true", help="Load/match chargebacks and stop.")
    p.add_argument("--output-dir", help="Where to save the xlsx (default: REPORTS_DIR).")
    p.add_argument("--email", default=DEFAULT_EMAIL, help=f"Recipient (default {DEFAULT_EMAIL}).")
    p.add_argument("--no-email", action="store_true", help="Skip the email.")
    p.add_argument("--sharepoint-folder", default=DEFAULT_SHAREPOINT_FOLDER,
                   help=f"SharePoint folder for the workbook (default '{DEFAULT_SHAREPOINT_FOLDER}').")
    p.add_argument("--no-upload", action="store_true",
                   help="Don't upload to SharePoint (a workbook over 3 MB then can't be emailed).")
    args = p.parse_args()
    if not S3REPORTING_DIR:
        print("Error: s3reporting checkout not found (needs modules/warehouse.py). Clone "
              "trybookinguk/s3reporting to /root/s3reporting or next to this repo, or set "
              "S3REPORTING_DIR.")
        return 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.load_only:
        import importlib.util
        if importlib.util.find_spec("openpyxl") is None:  # needed for the xlsx; fail fast
            print("Error: openpyxl is not installed — run: "
                  "pip install --break-system-packages openpyxl")
            return 1
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()

    from modules import warehouse
    db_path = args.db or warehouse.default_db_path()
    if not os.path.exists(db_path):
        print(f"Error: warehouse database not found: {db_path}")
        return 1
    conn = warehouse.connect(db_path)
    try:
        cbmod.ensure_tables(conn)
        cb_stats = []
        for path in args.chargeback_file:
            if not os.path.exists(path):
                log.warning("Chargeback file not found, skipping: %s", path)
                continue
            cb_stats.append(cbmod.load_file(conn, path))
        for path in args.signals_file:
            if os.path.exists(path):
                fr.load_signals_file(conn, path)
            else:
                log.warning("Signals file not found, skipping: %s", path)
        for path in args.logins_file:
            if os.path.exists(path):
                fr.load_logins_file(conn, path)
            else:
                log.warning("Logins file not found, skipping: %s", path)

        match_stats = cbmod.match_to_bookings(conn, rematch=args.rematch)
        tiers = cbmod.load_tier_lookup()
        cbmod.refresh_tiers(conn, tiers)
        for s in cb_stats:
            print(f"Chargebacks {s['file']} ({s['source']}): {s['read']} read, {s['new']} new, "
                  f"{s['changed']} changed, {s['unchanged']} unchanged")
        print(f"Chargeback matching: {match_stats['matched']} of {match_stats['pending']} pending matched.")
        unmatched = conn.execute(
            "SELECT Source, COUNT(*) FROM chargebacks WHERE AccountId IS NULL OR AccountId = '' "
            "GROUP BY Source").fetchall()
        if unmatched:
            print("Unmatched chargebacks by source: "
                  + ", ".join(f"{src} {n}" for src, n in unmatched))
        if args.load_only:
            return 0

        rr = load_risk_report(args.risk_report_file, use_s3=not args.no_s3)
        if rr is not None:
            warehouse.replace_snapshot(conn, "risk_exposure", rr, key=None)

        accounts, events = fr.build_risk_fraud(
            conn, as_of, risk_report=rr, tiers=tiers,
            event_lookback_days=args.event_lookback_days,
            booking_lookback_days=args.booking_lookback_days)
        if fr.UNRECOGNISED_COUNTRIES:
            top = sorted(fr.UNRECOGNISED_COUNTRIES.items(), key=lambda kv: -kv[1])[:10]
            log.warning("Unrecognised country values (add to fraud/countries.py): %s", top)
        n = fr.write_risk_fraud(conn, accounts, events, as_of)
        print(f"risk_fraud: {len(accounts):,} accounts + {len(events):,} events ({n:,} rows).")
        cb = fr.chargeback_frame(conn)
    finally:
        conn.close()

    coverage = {t["key"]: int(events[f"{t['key']}__result"].isin(["Pass", "Fail"]).sum())
                if len(events) else 0 for t in fr.RISK_TESTS}
    coverage["account_logins"] = int(accounts["account_logins__result"].isin(["Pass", "Fail"]).sum()) \
        if len(accounts) else 0
    summary_df = _summary_rows(as_of, accounts, events, cb, cb_stats, match_stats,
                               rr is not None, coverage)
    xlsx = build_workbook(as_of, accounts, events, cb, summary_df)
    filename = f"{as_of.isoformat()}_FraudRiskReport.xlsx"

    out_dir = args.output_dir
    if not out_dir:
        from modules.utils.config import REPORTS_DIR
        out_dir = REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)
    with open(out_path, "wb") as f:
        f.write(xlsx)
    print(f"Wrote {out_path}")

    web_url = None
    if not args.no_upload:
        web_url = upload_to_sharepoint(filename, xlsx, args.sharepoint_folder)

    if not args.no_email:
        attach = len(xlsx) <= MAX_ATTACHMENT_BYTES
        if not attach:
            print(f"Workbook is {len(xlsx) / 2**20:.1f} MB (over the "
                  f"{MAX_ATTACHMENT_BYTES / 2**20:.0f} MB attachment limit): emailing "
                  + ("the SharePoint link instead of attaching it." if web_url
                     else "a summary only — no SharePoint link is available."))
        try:
            from modules.utils.email_utils import send_html_email
            send_html_email(to=args.email, subject=f"Fraud Risk Report {as_of.isoformat()}",
                            html_content=_email_body(as_of, accounts, events, summary_df, filename,
                                                     web_url=web_url, attached=attach,
                                                     local_path=out_path),
                            attachments=[(filename, xlsx) + XLSX_MIME] if attach else None)
        except Exception as e:
            print(f"Error sending email: {e}")
            return 1
        how = "attached" if attach else ("SharePoint link" if web_url else "summary only")
        print(f"Emailed the report ({how}) to {args.email}")
        if not attach and not web_url:
            return 1  # the recipient didn't get the workbook — make the failure visible
    return 0


def upload_to_sharepoint(filename: str, data: bytes, folder: str):
    """Upload the workbook to SharePoint; return its webUrl, or None on failure."""
    try:
        from modules.utils import sharepoint
        from modules.utils.config import SHAREPOINT_DRIVE_ID
        if not SHAREPOINT_DRIVE_ID:
            print("SharePoint upload skipped: SHAREPOINT_DRIVE_ID not set.")
            return None
        token = sharepoint.authenticate_graph()
        if not token:
            print("SharePoint upload skipped: could not authenticate to Microsoft Graph.")
            return None
        if not sharepoint.upload(token, SHAREPOINT_DRIVE_ID, filename, data, folder):
            print(f"SharePoint upload of {filename} to '{folder}' failed.")
            return None
        url = sharepoint.get_web_url(token, SHAREPOINT_DRIVE_ID, filename, folder)
        print(f"Uploaded {filename} to SharePoint '{folder}'.")
        return url
    except Exception as e:
        print(f"SharePoint upload failed: {e}")
        return None


if __name__ == "__main__":
    sys.exit(main())
