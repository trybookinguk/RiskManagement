import sqlite3

import pytest

from rm import config, crypto, db, schema


def test_init_creates_schema_and_salt(warehouse):
    assert db.schema_version(warehouse) == schema.LATEST_VERSION
    assert db.get_meta(warehouse, "hash_salt")
    for t in ("bookings", "events", "accounts", "booking_transactions", "stripe_charges", "api_audit"):
        assert t in db.table_counts(warehouse)


def test_init_is_idempotent(env, warehouse):
    s = config.get_settings()
    salt = db.get_meta(warehouse, "hash_salt")
    r = db.init_db(warehouse, s.risk_passphrase)
    assert r == {"applied": [], "created_salt": False}
    assert db.get_meta(warehouse, "hash_salt") == salt


def test_wrong_passphrase_refused(warehouse):
    with pytest.raises(crypto.CryptoError):
        db.init_db(warehouse, "a different passphrase")
    with pytest.raises(crypto.CryptoError):
        db.get_hasher(warehouse, "a different passphrase")


def test_hasher_stable_across_connections(env, warehouse):
    s = config.get_settings()
    h1 = db.get_hasher(warehouse, s.risk_passphrase).email("a@b.com")
    conn2 = db.connect(s.risk_db)
    assert db.get_hasher(conn2, s.risk_passphrase).email("A@B.com ") == h1
    conn2.close()


def test_upsert_insert_then_update(warehouse):
    with warehouse:
        db.upsert(warehouse, "accounts", [{"AccountId": 1, "AccountName": "A", "Country": "GB"}], ["AccountId"])
        db.upsert(warehouse, "accounts", [{"AccountId": 1, "AccountName": "B", "Country": "GB"},
                                          {"AccountId": 2, "AccountName": "C", "Country": None}], ["AccountId"])
    rows = warehouse.execute("SELECT AccountId, AccountName FROM accounts ORDER BY 1").fetchall()
    assert [tuple(r) for r in rows] == [(1, "B"), (2, "C")]


def test_upsert_composite_key_and_update_cols(warehouse):
    with warehouse:
        db.upsert(warehouse, "balances", [{"AccountId": 1, "SnapshotDate": "2026-11-01", "Balance": 1.0,
                                           "Exposure": 5.0}], ["AccountId", "SnapshotDate"])
        db.upsert(warehouse, "balances", [{"AccountId": 1, "SnapshotDate": "2026-11-01", "Balance": 2.0,
                                           "Exposure": 9.0}], ["AccountId", "SnapshotDate"],
                  update_cols=["Balance"])
    r = warehouse.execute("SELECT Balance, Exposure FROM balances").fetchone()
    assert (r["Balance"], r["Exposure"]) == (2.0, 5.0)


def test_upsert_rejects_bad_identifiers(warehouse):
    with pytest.raises(ValueError):
        db.upsert(warehouse, "accounts; DROP TABLE x", [{"AccountId": 1}], ["AccountId"])


def test_import_run_ok_and_failed(warehouse):
    with db.import_run(warehouse, "s3", "file.csv", "abc") as run:
        run["rows_read"] = 10
        run["rows_written"] = 9
    assert db.already_imported(warehouse, "s3", "abc")
    with pytest.raises(RuntimeError):
        with db.import_run(warehouse, "s3", "bad.csv", "def"):
            raise RuntimeError("boom")
    assert not db.already_imported(warehouse, "s3", "def")
    r = warehouse.execute("SELECT status, message FROM import_log WHERE ref='bad.csv'").fetchone()
    assert r["status"] == "failed" and "boom" in r["message"]


def test_readonly_connection_cannot_write(env, warehouse):
    s = config.get_settings()
    ro = db.connect(s.risk_db, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    ro.close()


def test_newer_schema_refused(warehouse):
    warehouse.execute(f"PRAGMA user_version = {schema.LATEST_VERSION + 1}")
    with pytest.raises(db.DatabaseError):
        db.migrate(warehouse)


def test_failed_migration_rolls_back(env, monkeypatch):
    s = config.get_settings()
    monkeypatch.setattr(db, "MIGRATIONS", schema.MIGRATIONS + [(99, "bad", "CREATE TABLE ok1(x); SELEC broken;")])
    monkeypatch.setattr(db, "LATEST_VERSION", 99)
    conn = db.connect(s.risk_db)
    with pytest.raises(sqlite3.Error):
        db.migrate(conn)
    assert db.schema_version(conn) == schema.LATEST_VERSION
    assert "ok1" not in db.table_counts(conn)
    conn.close()


def test_upgrade_v1_to_v2_keeps_data(env, monkeypatch):
    s = config.get_settings()
    conn = db.connect(s.risk_db)
    monkeypatch.setattr(db, "MIGRATIONS", schema.MIGRATIONS[:1])
    monkeypatch.setattr(db, "LATEST_VERSION", 1)
    db.init_db(conn, s.risk_passphrase)
    full = "a" * 32 + "b" * 32
    with conn:
        conn.execute("INSERT INTO bookings(BookingTransactionId, AccountId, EventId, EventName, IPHash, UpdatedAt) "
                     "VALUES (1, 5, 50, 'Gala', ?, 'x')", (full,))
        conn.execute("INSERT INTO users(UserId, UsernameHash) VALUES (1, ?)", (full,))
    monkeypatch.setattr(db, "MIGRATIONS", schema.MIGRATIONS)
    monkeypatch.setattr(db, "LATEST_VERSION", schema.LATEST_VERSION)
    assert db.migrate(conn) == list(range(2, schema.LATEST_VERSION + 1))
    b = conn.execute("SELECT * FROM bookings").fetchone()
    assert b["IPHash"] == "a" * 32 and "EventName" not in b.keys() and "UpdatedAt" not in b.keys()
    assert tuple(conn.execute("SELECT EventId, AccountId, EventName FROM event_names").fetchone()) == (50, 5, "Gala")
    assert conn.execute("SELECT UsernameHash FROM users").fetchone()[0] == "a" * 32
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='bookings'")}
    assert {"ix_bookings_ip", "ix_bookings_urlid", "ix_bookings_account"} <= idx
    conn.close()
