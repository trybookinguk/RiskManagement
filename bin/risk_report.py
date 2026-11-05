#!/usr/bin/env python3
"""Nightly risk report: changes, top 10 risk accounts and events, import health,
and the Booking Transactions import check.

Nightly (DSM Task Scheduler, 05:00, after the imports):
    bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh report

Scores every upcoming event and every account with a balance or upcoming event
(interim model: rm/risk_interim.py), saves the scores in risk_daily, writes
<data>/reports/<date>_RiskReport.html and emails it to REPORT_EMAIL.

Options:
    --no-email              write the HTML and print the summary only
    --to a@x.com,b@y.com    send to these instead of REPORT_EMAIL
    --check-transactions    print only the Booking Transactions import check (no scoring, no email)
    --env-file PATH
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rm import config, db, report  # noqa: E402
from rm.graph import Graph, GraphError  # noqa: E402

log = logging.getLogger("risk_report")


def print_check(t):
    print("Booking Transactions import check")
    files = ", ".join(f"{f['n']} {f['status']}" for f in t["files"]) or "none"
    print(f"  Files in the last 30 days: {files}")
    for f in t["last_files"]:
        print(f"    {f['started_at']}  {f['status']:<7} {f['ref']}  {f['rows_read'] or 0:,} rows  {f['message'] or ''}")
    if not t["rows"]:
        print("  No transactions imported yet.")
        return
    print(f"  {t['rows']:,} bookings, {t['versions']:,} row versions, {str(t['date_from'])[:10]} to {str(t['date_to'])[:10]}")
    print(f"  {t['attempts']:,} payment attempts: {t['declines']:,} declined, {t['unclassified']:,} unclassified")
    total = sum(t["match"].values()) or 1
    print("  Matched to an event by: " + ", ".join(f"{k} {v:,} ({100 * v / total:.1f}%)" for k, v in sorted(t["match"].items())))
    print("  Filled: " + ", ".join(f"{k} {100 * v / t['rows']:.0f}%" for k, v in t["fill"].items())
          + f", Gateway ID in S3 bookings {100 * t['gateway_id_matches'] / t['rows']:.0f}%")
    print("  Gateway Message Description (treated as):")
    for x in t["messages"]:
        print(f"    {x['n']:>8,}  {x['class']:<12} {x['value']}")
    for label, key in (("Status", "status"), ("Transaction", "transaction"), ("Cvv Check", "cvv"),
                       ("Card Type", "card_type")):
        print(f"  {label}: " + ", ".join(f"{x['value']} ({x['n']:,})" for x in t[key]))


def main(argv=None, graph=None) -> int:
    p = argparse.ArgumentParser(description="Nightly risk report.")
    p.add_argument("--no-email", action="store_true")
    p.add_argument("--to")
    p.add_argument("--check-transactions", action="store_true")
    p.add_argument("--env-file")
    p.add_argument("--today", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.env_file:
        config.load_env_file(args.env_file)
    try:
        settings = config.get_settings()
    except config.ConfigError as e:
        print(f"Error: {e}")
        return 1
    today = date.fromisoformat(args.today) if args.today else datetime.now(ZoneInfo(settings.timezone)).date()
    if not os.path.exists(settings.risk_db):
        print(f"Error: warehouse not found: {settings.risk_db}")
        return 1
    conn = db.connect(settings.risk_db)
    try:
        db.migrate(conn)
        if args.check_transactions:
            print_check(report.check_transactions(conn, today))
            return 0
        r = report.build(conn, today)
    finally:
        conn.close()

    out_dir = os.path.join(settings.data_dir, "reports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{r['date']}_RiskReport.html")
    page = report.render_html(r)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset='utf-8'><title>Risk report {r['date']}</title>{page}")
    print(report.render_text(r))
    print(f"\nSaved {path}  (File Station: docker > riskmanagement-data > reports)")

    if args.no_email:
        return 0
    to = args.to or settings.report_email
    try:
        if graph is None:
            settings.require("azure_tenant_id", "azure_client_id", "azure_client_secret", "azure_sender_mailbox")
            graph = Graph.from_settings(settings)
        graph.send_mail(to, report.subject(r), page, html=True)
    except (config.ConfigError, GraphError) as e:
        print(f"Error: report written but not emailed: {e}")
        return 1
    print(f"Emailed to {to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
