#!/usr/bin/env python3
"""Import Booking Transactions CSVs from SharePoint "Platform Data/Booking Transactions".

Nightly (DSM Task Scheduler, 04:20):
    bash /volume1/docker/RiskManagement/deploy/synology/rmctl.sh ingest-sharepoint

For each CSV in the folder:
  - already imported (same bytes)  -> deleted, nothing re-imported
  - imports cleanly                -> rows committed, THEN the file is deleted
  - can't be imported              -> nothing stored; the file is moved to
                                      Booking Transactions/Failed/ with a
                                      <name>.error.txt saying why; alert email
No files is fine (exit 0). Exit 1 if any file failed.

Options:
    --dry-run          check every file and report, change nothing (no DB writes, no moves/deletes)
    --file PATH        import a local CSV instead (repeatable; the local file is left alone)
    --env-file PATH    load settings from a .env (outside the container)
"""
import argparse
import hashlib
import logging
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rm import config, crypto, db  # noqa: E402
from rm.graph import Graph, GraphError  # noqa: E402
from rm.ingest import sharepoint_transactions as bt  # noqa: E402

log = logging.getLogger("ingest_sharepoint")
SOURCE = "sharepoint"
FAILED = "Failed"


def failed_name(name: str, now: datetime) -> str:
    stem, dot, ext = name.rpartition(".")
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return f"{stem}-{stamp}.{ext}" if dot else f"{name}-{stamp}"


def import_bytes(conn, hasher, data: bytes, name: str, cutoff: str, dry_run: bool) -> dict:
    """Parse + store one file. Raises bt.FileRejected. Returns stats (+ 'duplicate')."""
    checksum = hashlib.sha256(data).hexdigest()
    if db.already_imported(conn, SOURCE, checksum):
        return {"duplicate": True, "checksum": checksum}
    df = bt.parse(data, hasher, name)
    if dry_run:
        return {"duplicate": False, "rows_read": len(df), "rows_written": 0, "bookings": df["BookingGuid"].nunique(), "date_order": df.attrs.get("date_order"),
                "declines": int(sum(1 for v in df["IsDecline"] if v == 1)), "checksum": checksum}
    run_id = db.start_import(conn, SOURCE, name, checksum)
    try:
        with conn:
            stats = bt.store(conn, df, name)
            stats["pruned"] = bt.prune(conn, cutoff)
    except Exception as e:
        db.finish_import(conn, run_id, "failed", message=f"{type(e).__name__}: {e}")
        raise
    db.finish_import(conn, run_id, "ok", stats["rows_read"], stats["rows_written"],
                     f"{stats['bookings']} bookings, {stats['new_versions']} new versions, {stats['declines']} declines")
    stats.update(duplicate=False, checksum=checksum, date_order=df.attrs.get("date_order"))
    return stats


def alert(graph, settings, failures):
    if not failures or graph is None:
        return
    lines = [f"{len(failures)} Booking Transactions file(s) could not be imported and were moved to",
             f"SharePoint: {settings.sharepoint_transactions_folder}/{FAILED}/", ""]
    for name, reason in failures:
        lines += [f"- {name}", f"  {reason}", ""]
    lines.append("Nothing from these files was stored. Fix and re-upload them to the Booking Transactions folder.")
    try:
        graph.send_mail(settings.alert_email, "RiskManagement: Booking Transactions import failed", "\n".join(lines))
    except Exception as e:  # alerting must never hide the real failure
        log.warning("Could not send the alert email: %s", e)


def main(argv=None, graph=None) -> int:
    p = argparse.ArgumentParser(description="Import Booking Transactions CSVs from SharePoint.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--file", action="append", default=[], help="Import a local CSV instead of SharePoint.")
    p.add_argument("--env-file")
    p.add_argument("--today", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.env_file:
        config.load_env_file(args.env_file)
    try:
        settings = config.get_settings().require("risk_passphrase")
        if not args.file and graph is None:
            settings.require("azure_tenant_id", "azure_client_id", "azure_client_secret", "sharepoint_drive_id")
    except config.ConfigError as e:
        print(f"Error: {e}")
        return 1

    tz = ZoneInfo(settings.timezone)
    today = date.fromisoformat(args.today) if args.today else datetime.now(tz).date()
    cutoff = (today - timedelta(days=settings.payment_retention_days)).isoformat()

    conn = db.connect(settings.risk_db)
    try:
        db.init_db(conn, settings.risk_passphrase)
        hasher = db.get_hasher(conn, settings.risk_passphrase)
    except (crypto.CryptoError, db.DatabaseError) as e:
        print(f"Error: {e}")
        return 1

    failures, imported = [], 0
    try:
        if args.file:
            for path in args.file:
                name = os.path.basename(path)
                try:
                    with open(path, "rb") as f:
                        s = import_bytes(conn, hasher, f.read(), name, cutoff, args.dry_run)
                    print(f"  = {name}: already imported" if s["duplicate"] else
                          f"  + {name}: {s['rows_read']:,} rows, {s['bookings']:,} bookings, {s['declines']:,} declines"
                          + (" (dry run)" if args.dry_run else ""))
                    imported += 0 if s["duplicate"] else 1
                except (bt.FileRejected, OSError) as e:
                    failures.append((name, str(e)))
                    print(f"  ! {name}: REJECTED - {e}")
            return _finish(failures, imported, args.dry_run)

        graph = graph or Graph.from_settings(settings)
        folder = settings.sharepoint_transactions_folder
        try:
            items = graph.list_folder(folder)
        except GraphError as e:
            print(f"Error: could not list SharePoint '{folder}': {e}")
            return 1
        files = sorted((i for i in items if not i["is_folder"]), key=lambda i: (i["modified"] or "", i["name"]))
        print(f"SharePoint '{folder}': {len(files)} file(s)")
        if not files:
            print("Nothing to import.")
            return 0

        for item in files:
            name = item["name"]
            try:
                if not name.lower().endswith(".csv"):
                    raise bt.FileRejected("not a .csv file - export Booking Transactions as CSV")
                data = graph.download(item["id"])
                s = import_bytes(conn, hasher, data, name, cutoff, args.dry_run)
            except bt.FileRejected as e:
                failures.append((name, str(e)))
                print(f"  ! {name}: REJECTED - {e}")
                if not args.dry_run:
                    _move_to_failed(graph, folder, item, str(e))
                continue
            except GraphError as e:
                failures.append((name, f"SharePoint error: {e}"))
                print(f"  ! {name}: SharePoint error - {e} (left in place, retried next run)")
                continue

            if s["duplicate"]:
                print(f"  = {name}: already imported - deleting the copy")
            else:
                imported += 1
                print(f"  + {name}: {s['rows_read']:,} rows, {s['bookings']:,} bookings, {s['declines']:,} declines, "
                      f"dates read as {'day/month' if s.get('date_order') == 'dmy' else 'month/day'}/year"
                      + (" (dry run - not stored, not deleted)" if args.dry_run else
                         f"; {s['new_versions']:,} new versions, {s['rows_written']:,} latest rows updated"))
            if not args.dry_run:
                try:
                    graph.delete(item["id"])
                except GraphError as e:  # data is safe; next run sees a duplicate and deletes it
                    print(f"    (could not delete {name} yet: {e}; it will be removed next run)")
        alert(None if args.dry_run else graph, settings, failures)
        return _finish(failures, imported, args.dry_run)
    finally:
        conn.close()


def _move_to_failed(graph, folder, item, reason):
    now = datetime.now()
    new = failed_name(item["name"], now)
    try:
        graph.ensure_folder(folder, FAILED)
        graph.move(item["id"], f"{folder}/{FAILED}", new)
        note = (f"{item['name']} could not be imported ({now:%Y-%m-%d %H:%M}).\n\n{reason}\n\n"
                "Nothing from this file was stored. Fix it and upload it to the Booking Transactions folder again.\n")
        graph.upload_small(f"{folder}/{FAILED}", new + ".error.txt", note.encode("utf-8"))
        print(f"    moved to {FAILED}/{new}")
    except GraphError as e:
        print(f"    could not move it to {FAILED}/ ({e}); it stays in the folder")


def _finish(failures, imported, dry_run) -> int:
    word = "would import" if dry_run else "imported"
    print(f"Done: {imported} file(s) {word}, {len(failures)} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
