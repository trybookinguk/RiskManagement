#!/usr/bin/env python3
"""Load the S3 exports into the risk warehouse.

Nightly (DSM Task Scheduler, 04:10):
    docker exec riskmanagement python3 bin/ingest_s3.py

Loads, from the current and previous month's folders:
    BookingData                bookings (upsert; lifetime table, never pruned)
    Accounts                   accounts + today's account_snapshots row
    users                      users (username hashed)
    RiskReport                 balances (one row per account, dated by the file's S3 date)
    AccountMovementDaily       balance_movements (each daily file not loaded yet)
    TransactionReconciliation  gateway_recon (card last 4 only; 1,100-day retention)
    transfers                  bank_transfers (payouts per account; lifetime)
then rebuilds events (incl. EventCreated), account_lifetime and event_lifetime.

First run, and whenever lifetime history needs rebuilding:
    docker exec riskmanagement python3 bin/ingest_s3.py --full-refresh
streams the newest non-empty BookingDataAll (~2 GB, one chunk in memory at a
time) plus every BookingData from its month on, and every
TransactionReconciliation inside the retention window, every transfers file,
every month's RiskReport
(month-end exposure history) and every AccountMovementDaily in the window.

Other options:
    --dry-run            list the files it would load and stop
    --only Accounts,RiskReport   just these reports
    --force              reload files even if this exact version was loaded before
    --local-dir PATH     read a local folder laid out like the bucket (YYYY/MM/...)
    --env-file PATH      load settings from a .env (outside the container)

Exit code 1 if any report failed (DSM emails the run details).
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rm import config, crypto, db, s3  # noqa: E402
from rm.ingest import s3_reports as rep  # noqa: E402

log = logging.getLogger("ingest_s3")
REPORTS = ["BookingData", "Accounts", "users", "RiskReport", "AccountMovementDaily", "TransactionReconciliation",
           "transfers"]
SNAPSHOT_LOOKBACK_MONTHS = 7   # how far back to look for a non-empty BookingDataAll


def file_date(obj, tz) -> str:
    """The date a file's data is for. Daily files: the YYYYMMDD in the name.
    Monthly files (rewritten daily): the day S3 last wrote them, in UK time, so a
    file TryBooking didn't refresh isn't stored as today's figures."""
    period = obj.get("period") or (s3.parse_key(obj["Key"]) or {}).get("period", "")
    if len(period) == 8:
        return f"{period[:4]}-{period[4:6]}-{period[6:]}"
    when = datetime.strptime(obj["LastModified"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    return when.astimezone(tz).date().isoformat()


def plan(objects, today: date, full_refresh: bool, retention_days: int, only=None, tz=None) -> list:
    """[(report, key, etag, file_date)] in the order to load."""
    tz = tz or ZoneInfo("Europe/London")
    only = {o.lower() for o in only} if only else None
    want = lambda r: only is None or r.lower() in only  # noqa: E731
    recent = {p for p in s3.month_prefixes(today, 2)}
    in_recent = [o for o in objects if any(o["Key"].startswith(p) for p in recent)]
    steps = []
    add = lambda report, f: steps.append((report, f["Key"], f.get("ETag"), file_date(f, tz)))  # noqa: E731
    cutoff_day = (today - timedelta(days=retention_days)).strftime("%Y%m%d")

    if want("BookingData") or want("BookingDataAll"):
        if full_refresh:
            snap = s3.latest(objects, "BookingDataAll")
            if snap:
                add("BookingDataAll", snap)
                since = snap["period"][:6]
            else:
                log.warning("No non-empty BookingDataAll found in the last %d months; loading BookingData only",
                            SNAPSHOT_LOOKBACK_MONTHS)
                since = "000000"
            for f in s3.files_for(objects, "BookingData"):
                if f["period"][:6] >= since:
                    add("BookingData", f)
        else:
            for f in s3.files_for(in_recent, "BookingData"):
                add("BookingData", f)

    for report in ("Accounts", "users"):
        if want(report):
            f = s3.latest(in_recent, report)
            if f:
                add(report, f)
            else:
                log.warning("No non-empty %s file in %s", report, sorted(recent))

    if want("RiskReport"):
        if full_refresh:   # every month's final balances = month-end history (+ the current month)
            for f in s3.files_for(objects, "RiskReport"):
                if f["period"][:6] >= cutoff_day[:6]:
                    add("RiskReport", f)
        else:
            f = s3.latest(in_recent, "RiskReport")
            if f:
                add("RiskReport", f)
            else:
                log.warning("No non-empty RiskReport file in %s", sorted(recent))

    if want("AccountMovementDaily"):
        for f in s3.files_for(objects if full_refresh else in_recent, "AccountMovementDaily"):
            if f["period"] >= cutoff_day:
                add("AccountMovementDaily", f)

    if want("TransactionReconciliation"):
        pool = objects if full_refresh else in_recent
        cutoff = (today - timedelta(days=retention_days)).strftime("%Y%m")
        for f in s3.files_for(pool, "TransactionReconciliation"):
            if f["period"][:6] >= cutoff:
                add("TransactionReconciliation", f)

    if want("transfers"):   # lifetime: every month on a full refresh, else this and last month
        for f in s3.files_for(objects if full_refresh else in_recent, "transfers"):
            add("transfers", f)
    return steps


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load S3 exports into the risk warehouse.")
    p.add_argument("--full-refresh", action="store_true", help="Reload lifetime bookings from BookingDataAll.")
    p.add_argument("--only", help="Comma-separated reports, e.g. Accounts,RiskReport.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Reload files already loaded at this version.")
    p.add_argument("--local-dir", help="Read a local folder laid out like the bucket instead of S3.")
    p.add_argument("--env-file")
    p.add_argument("--chunksize", type=int, default=100_000)
    p.add_argument("--today", help=argparse.SUPPRESS)  # tests
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.env_file:
        config.load_env_file(args.env_file)
    try:
        settings = config.get_settings().require("risk_passphrase")
        if not args.local_dir:
            settings.require("aws_access_key_id", "aws_secret_access_key")
    except config.ConfigError as e:
        print(f"Error: {e}")
        return 1

    tz = ZoneInfo(settings.timezone)
    today = date.fromisoformat(args.today) if args.today else datetime.now(tz).date()
    source = (s3.LocalSource(args.local_dir) if args.local_dir else
              s3.S3Source(settings.s3_bucket, access_key=settings.aws_access_key_id,
                          secret_key=settings.aws_secret_access_key))
    months = max(SNAPSHOT_LOOKBACK_MONTHS, settings.payment_retention_days // 28 + 2) if args.full_refresh else 2
    try:
        objects = source.list(s3.month_prefixes(today, months))
    except Exception as e:
        print(f"Error listing {source.ref('')}: {e}")
        return 1
    steps = plan(objects, today, args.full_refresh, settings.payment_retention_days,
                 [o.strip() for o in args.only.split(",")] if args.only else None, tz)

    print(f"{'Full refresh' if args.full_refresh else 'Nightly'} load for {today}: {len(steps)} file(s)")
    for report, key, _, fdate in steps:
        print(f"  {report:<26} {fdate}  {source.ref(key)}")
    if args.dry_run or not steps:
        return 0

    conn = db.connect(settings.risk_db)
    try:
        db.init_db(conn, settings.risk_passphrase)
        hasher = db.get_hasher(conn, settings.risk_passphrase)
    except (crypto.CryptoError, db.DatabaseError) as e:
        print(f"Error: {e}")
        return 1

    cutoff = (today - timedelta(days=settings.payment_retention_days)).isoformat()
    failures = []
    for report, key, etag, fdate in steps:
        checksum = f"{key}|{etag}" if etag else None
        source_name = f"s3:{report}"
        if checksum and not args.force and db.already_imported(conn, source_name, checksum):
            print(f"  = {report}: {key} unchanged since last load, skipped")
            continue
        try:
            with db.import_run(conn, source_name, key, checksum) as run:
                if report in ("BookingDataAll", "BookingData"):
                    stats = rep.load_bookings(conn, hasher, source, key, args.chunksize)
                elif report == "Accounts":
                    stats = rep.load_accounts(conn, source, key, fdate)
                elif report == "users":
                    stats = rep.load_users(conn, hasher, source, key)
                elif report == "RiskReport":
                    stats = rep.load_balances(conn, source, key, fdate)
                elif report == "AccountMovementDaily":
                    stats = rep.load_movements(conn, source, key, fdate)
                elif report == "transfers":
                    stats = rep.load_transfers(conn, source, key)
                else:
                    stats = rep.load_recon(conn, source, key, cutoff)
                run.update(stats)
            print(f"  + {report}: {stats['rows_read']:,} read, {stats['rows_written']:,} written")
        except crypto.SensitiveDataError as e:
            failures.append(report)
            print(f"  ! {report}: REJECTED {key}: {e}. Nothing from this file was stored.")
        except Exception as e:
            failures.append(report)
            log.exception("%s failed", key)
            print(f"  ! {report}: FAILED {key}: {type(e).__name__}: {e}")

    try:
        with db.import_run(conn, "derive", "events+lifetime") as run:
            n_events = rep.rebuild_events(conn)
            life = rep.rebuild_lifetime(conn)
            pruned = rep.prune_payments(conn, cutoff) + rep.prune_movements(conn, cutoff)
            run.update(rows_written=n_events, message=f"lifetime {life}, pruned {pruned}")
        print(f"Derived: {n_events:,} events, {life['accounts']:,} account lifetimes; "
              f"pruned {pruned:,} recon/movement rows before {cutoff}")
        n_bookings = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        if n_bookings and not args.full_refresh and not conn.execute(
                "SELECT 1 FROM import_log WHERE source='s3:BookingDataAll' AND status='ok' LIMIT 1").fetchone():
            print("Note: lifetime history hasn't been loaded yet - run once with --full-refresh.")
    except Exception as e:
        failures.append("derive")
        log.exception("derive failed")
        print(f"  ! derive: FAILED: {e}")
    finally:
        conn.close()

    if failures:
        print(f"Finished with failures: {', '.join(failures)}")
        return 1
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
