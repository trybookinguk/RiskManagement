"""SQLite warehouse: connections, migrations, passphrase check, upsert helpers.

Only the worker container writes ``risk.db``; the web container opens it with
``connect(path, readonly=True)``. WAL mode lets the dashboard read while a
nightly job writes.
"""
import base64
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import crypto
from .schema import LATEST_VERSION, MIGRATIONS  # noqa: F401  (LATEST_VERSION re-exported)


class DatabaseError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        if not os.path.exists(path):
            raise DatabaseError(f"warehouse not found: {path} (run: rm_admin.py init-db)")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30,
                               check_same_thread=False)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def schema_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn) -> list:
    """Apply pending migrations; returns the versions applied."""
    current = schema_version(conn)
    if current > LATEST_VERSION:
        raise DatabaseError(f"warehouse schema v{current} is newer than this code (v{LATEST_VERSION}); "
                            "update the code before running jobs")
    applied = []
    for version, _name, sql in MIGRATIONS:
        if version <= current:
            continue
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {int(version)};\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise
        applied.append(version)
    return applied


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def init_db(conn, passphrase: str) -> dict:
    """Migrate, then create (first run) or verify (later runs) the hashing key.

    Returns {"applied": [...], "created_salt": bool}. Raises CryptoError if the
    passphrase differs from the one the warehouse was created with.
    """
    applied = migrate(conn)
    created = False
    with conn:
        salt_b64 = get_meta(conn, "hash_salt")
        if salt_b64 is None:
            salt = crypto.new_salt()
            key = crypto.derive_hash_key(passphrase, salt)
            set_meta(conn, "hash_salt", base64.b64encode(salt).decode())
            set_meta(conn, "hash_check", crypto.check_value(key))
            set_meta(conn, "created_at", utcnow())
            created = True
    if not created:
        get_hasher(conn, passphrase)  # verifies
    return {"applied": applied, "created_salt": created}


def get_hasher(conn, passphrase: str) -> crypto.Hasher:
    """Hasher for this warehouse. Refuses a passphrase that doesn't match."""
    salt_b64 = get_meta(conn, "hash_salt")
    if salt_b64 is None:
        raise DatabaseError("warehouse not initialised (run: rm_admin.py init-db)")
    key = crypto.derive_hash_key(passphrase, base64.b64decode(salt_b64))
    if crypto.check_value(key) != get_meta(conn, "hash_check"):
        raise crypto.CryptoError(
            "RISK_ENCRYPTION_PASSPHRASE does not match the one this warehouse was created with. "
            "Hashes would not match existing rows, so nothing has been written.")
    return crypto.Hasher(key)


def upsert(conn, table: str, rows, key_cols, update_cols=None) -> int:
    """INSERT … ON CONFLICT(key) DO UPDATE for a list of dicts. Returns rows sent.

    ``update_cols`` limits which columns change on conflict (default: all
    non-key columns present in the first row). Columns are taken from the first
    row, so every row must have the same keys.
    """
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    for c in [table, *cols, *key_cols]:
        if not c.replace("_", "").isalnum():
            raise ValueError(f"bad identifier {c!r}")
    upd = [c for c in (update_cols or cols) if c not in key_cols]
    placeholders = ", ".join("?" for _ in cols)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT ({', '.join(key_cols)}) ")
    sql += ("DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in upd)) if upd else "DO NOTHING"
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


# ---------------------------------------------------------------------------
# Import log
# ---------------------------------------------------------------------------

def start_import(conn, source: str, ref: str = None, checksum: str = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO import_log(source, ref, checksum, started_at, status) VALUES (?,?,?,?, 'running')",
            (source, ref, checksum, utcnow()))
    return cur.lastrowid


def finish_import(conn, run_id: int, status: str, rows_read: int = 0, rows_written: int = 0,
                  message: str = None) -> None:
    if status not in ("ok", "failed", "skipped"):
        raise ValueError(status)
    with conn:
        conn.execute("UPDATE import_log SET finished_at=?, status=?, rows_read=?, rows_written=?, message=? "
                     "WHERE id=?", (utcnow(), status, rows_read, rows_written,
                                    (message or "")[:2000] or None, run_id))


@contextmanager
def import_run(conn, source: str, ref: str = None, checksum: str = None):
    """Log a job run; yields a dict to fill with rows_read / rows_written / message.

    Marks the run failed (and re-raises) on any exception.
    """
    run_id = start_import(conn, source, ref, checksum)
    stats = {"rows_read": 0, "rows_written": 0, "message": None, "status": "ok", "id": run_id}
    try:
        yield stats
    except BaseException as e:
        finish_import(conn, run_id, "failed", stats["rows_read"], stats["rows_written"],
                      f"{type(e).__name__}: {e}")
        raise
    finish_import(conn, run_id, stats["status"], stats["rows_read"], stats["rows_written"], stats["message"])


def already_imported(conn, source: str, checksum: str) -> bool:
    return conn.execute("SELECT 1 FROM import_log WHERE source=? AND checksum=? AND status='ok' LIMIT 1",
                        (source, checksum)).fetchone() is not None


def table_counts(conn) -> dict:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
