#!/usr/bin/env python3
"""RiskManagement admin tool.

    rm_admin.py init-db                 create / migrate risk.db and set up hashing (first run)
    rm_admin.py status                  schema version, size, row counts, last import per source
    rm_admin.py add-user EMAIL [--role viewer|admin]
    rm_admin.py remove-user EMAIL
    rm_admin.py list-users
    rm_admin.py create-token NAME       API token for scripts (shown once)
    rm_admin.py revoke-token TOKEN_ID
    rm_admin.py list-tokens

On the NAS:  sudo docker exec riskmanagement python3 bin/rm_admin.py status
Elsewhere:   python3 bin/rm_admin.py --env-file /path/to/.env status
"""
import argparse
import hashlib
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rm import config, db  # noqa: E402
from rm.crypto import CryptoError  # noqa: E402


def _open(settings, readonly=False):
    if not readonly or os.path.exists(settings.risk_db):
        return db.connect(settings.risk_db, readonly=readonly)
    raise db.DatabaseError(f"warehouse not found: {settings.risk_db} (run: rm_admin.py init-db)")


def cmd_init_db(settings, args):
    settings.require("risk_passphrase")
    conn = _open(settings)
    try:
        result = db.init_db(conn, settings.risk_passphrase)
    finally:
        conn.close()
    print(f"Warehouse: {settings.risk_db}")
    print(f"Schema: v{db.LATEST_VERSION}" + (f" (applied {result['applied']})" if result["applied"] else " (up to date)"))
    if result["created_salt"]:
        print("Hashing key created from RISK_ENCRYPTION_PASSPHRASE. Keep the passphrase in the password "
              "manager: it must never change.")
    else:
        print("Passphrase check: OK")
    return 0


def cmd_status(settings, args):
    conn = _open(settings, readonly=True)
    try:
        print(f"Warehouse: {settings.risk_db}  ({os.path.getsize(settings.risk_db) / 1e6:,.1f} MB)")
        print(f"Schema: v{db.schema_version(conn)} (code v{db.LATEST_VERSION})")
        print(f"Created: {db.get_meta(conn, 'created_at', '-')}")
        if settings.risk_passphrase:
            try:
                db.get_hasher(conn, settings.risk_passphrase)
                print("Passphrase check: OK")
            except (CryptoError, db.DatabaseError) as e:
                print(f"Passphrase check: FAILED - {e}")
        print("\nRows:")
        for name, n in db.table_counts(conn).items():
            print(f"  {name:<30} {n:>12,}")
        print("\nLast import per source:")
        rows = conn.execute(
            "SELECT source, ref, status, started_at, finished_at, rows_read, rows_written, message "
            "FROM import_log WHERE id IN (SELECT MAX(id) FROM import_log GROUP BY source) ORDER BY source").fetchall()
        if not rows:
            print("  (none yet)")
        for r in rows:
            print(f"  {r['source']:<12} {r['status']:<8} {r['started_at']}  read {r['rows_read']:,} "
                  f"written {r['rows_written']:,}  {r['ref'] or ''} {('- ' + r['message']) if r['message'] else ''}")
    finally:
        conn.close()
    return 0


def cmd_add_user(settings, args):
    email = args.email.strip().lower()
    if "@" not in email:
        print("Error: give the person's work email address")
        return 1
    conn = _open(settings)
    with conn:
        conn.execute("INSERT INTO dashboard_users(Email, Role, Active, AddedAt, AddedBy) VALUES (?,?,1,?,?) "
                     "ON CONFLICT(Email) DO UPDATE SET Role=excluded.Role, Active=1",
                     (email, args.role, db.utcnow(), os.environ.get("USER", "rm_admin")))
    conn.close()
    print(f"{email} can sign in to the dashboard as {args.role}.")
    return 0


def cmd_remove_user(settings, args):
    conn = _open(settings)
    with conn:
        n = conn.execute("UPDATE dashboard_users SET Active=0 WHERE Email=?", (args.email.strip().lower(),)).rowcount
    conn.close()
    print("Removed." if n else "No such user.")
    return 0 if n else 1


def cmd_list_users(settings, args):
    conn = _open(settings, readonly=True)
    rows = conn.execute("SELECT Email, Role, Active, AddedAt FROM dashboard_users ORDER BY Email").fetchall()
    conn.close()
    for r in rows:
        print(f"  {r['Email']:<40} {r['Role']:<7} {'active' if r['Active'] else 'removed':<8} {r['AddedAt']}")
    if not rows:
        print("  (no users)")
    return 0


def cmd_create_token(settings, args):
    token_id = "tok_" + secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full = f"{token_id}.{secret}"
    conn = _open(settings)
    with conn:
        conn.execute("INSERT INTO api_tokens(TokenId, Name, TokenHash, CreatedAt) VALUES (?,?,?,?)",
                     (token_id, args.name, hashlib.sha256(full.encode()).hexdigest(), db.utcnow()))
    conn.close()
    print(f"Token for {args.name!r} (shown once - store it in the password manager):\n\n  {full}\n")
    print("Use it as:  Authorization: Bearer <token>")
    return 0


def cmd_revoke_token(settings, args):
    conn = _open(settings)
    with conn:
        n = conn.execute("UPDATE api_tokens SET Revoked=1 WHERE TokenId=?", (args.token_id,)).rowcount
    conn.close()
    print("Revoked." if n else "No such token.")
    return 0 if n else 1


def cmd_list_tokens(settings, args):
    conn = _open(settings, readonly=True)
    rows = conn.execute("SELECT TokenId, Name, CreatedAt, LastUsedAt, Revoked FROM api_tokens ORDER BY CreatedAt").fetchall()
    conn.close()
    for r in rows:
        print(f"  {r['TokenId']}  {r['Name']:<25} created {r['CreatedAt']}  last used {r['LastUsedAt'] or '-'}"
              f"{'  REVOKED' if r['Revoked'] else ''}")
    if not rows:
        print("  (no tokens)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="RiskManagement warehouse admin.")
    p.add_argument("--env-file", help="Load settings from this .env file (variables already set win).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="Create or migrate the warehouse and set up hashing.")
    sub.add_parser("status", help="Show schema, row counts and last imports.")
    a = sub.add_parser("add-user", help="Allow a dashboard sign-in.")
    a.add_argument("email")
    a.add_argument("--role", choices=["viewer", "admin"], default="viewer")
    r = sub.add_parser("remove-user", help="Remove a dashboard sign-in.")
    r.add_argument("email")
    sub.add_parser("list-users")
    t = sub.add_parser("create-token", help="Create an API token for scripts.")
    t.add_argument("name")
    rv = sub.add_parser("revoke-token")
    rv.add_argument("token_id")
    sub.add_parser("list-tokens")
    args = p.parse_args(argv)

    if args.env_file:
        config.load_env_file(args.env_file)
    try:
        settings = config.get_settings()
        handler = globals()["cmd_" + args.cmd.replace("-", "_")]
        return handler(settings, args)
    except (config.ConfigError, db.DatabaseError, CryptoError) as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
