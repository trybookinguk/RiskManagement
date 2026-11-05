import csv
import importlib.util
import io
import pathlib

import pytest

from rm import config, db
from rm.graph import Graph, GraphError
from rm.ingest import sharepoint_transactions as bt

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = ["Transaction Date", "Gateway ID", "Booking ID", "Merchant Ref ID", "Card Type", "Card Number", "Cvv Check",
          "Amount", "Transaction", "Event Organiser", "Event Name", "Gateway Message", "Gateway Message Description",
          "Status", "Booking First Name", "Booking Last Name", "Booking Email", "Booking Phone", "Waited In Queue",
          "Wait Time", "Starting Position"]
FOLDER = "Platform Data/Booking Transactions"


def row(bid, when, desc="Approved", status="Success", card="************4242", email="jane@example.com", **kw):
    r = {"Transaction Date": when, "Gateway ID": f"ch_{bid}", "Booking ID": bid, "Merchant Ref ID": f"guid-{bid}",
         "Card Type": "VISA", "Card Number": card, "Cvv Check": "pass", "Amount": "25.00", "Transaction": "Payment",
         "Event Organiser": "Theatre Co", "Event Name": "Gala", "Gateway Message": "00",
         "Gateway Message Description": desc, "Status": status, "Booking First Name": "Jane",
         "Booking Last Name": "Doe", "Booking Email": email, "Booking Phone": "07700 900123",
         "Waited In Queue": "Yes", "Wait Time": "00:03:10", "Starting Position": "120"}
    r.update(kw)
    return r


def to_csv(rows, header=HEADER, crlf=False) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n" if crlf else "\n")
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(h, "") for h in header])
    return buf.getvalue().encode("utf-8-sig")


class FakeGraph:
    """In-memory stand-in for rm.graph.Graph (same methods)."""

    def __init__(self, files=None):
        self.items = {}            # id -> {name, folder, data, modified}
        self._next = 0
        self.mail = []
        self.fail_delete = False
        for i, (name, data) in enumerate((files or {}).items()):
            self.add(name, data, modified=f"2026-11-0{1 + i % 9}T00:00:00Z")

    def add(self, name, data, folder=FOLDER, modified="2026-11-01T00:00:00Z"):
        self._next += 1
        iid = f"id{self._next}"
        self.items[iid] = {"name": name, "folder": folder, "data": data, "modified": modified}
        return iid

    def in_folder(self, folder):
        return {i["name"]: i for i in self.items.values() if i["folder"] == folder}

    def list_folder(self, folder):
        out = [{"id": k, "name": v["name"], "size": len(v["data"]), "is_folder": False, "modified": v["modified"]}
               for k, v in self.items.items() if v["folder"] == folder]
        if any(v["folder"] == f"{folder}/Failed" for v in self.items.values()):
            out.append({"id": "fld", "name": "Failed", "size": 0, "is_folder": True, "modified": None})
        return out

    def download(self, iid):
        return self.items[iid]["data"]

    def delete(self, iid):
        if self.fail_delete:
            raise GraphError("DELETE -> 503")
        del self.items[iid]

    def ensure_folder(self, parent, name):
        pass

    def move(self, iid, dest, new_name=None):
        self.items[iid]["folder"] = dest
        if new_name:
            self.items[iid]["name"] = new_name

    def upload_small(self, folder, name, data):
        self.add(name, data, folder=folder)

    def send_mail(self, to, subject, body):
        self.mail.append((to, subject, body))


def run(graph=None, *args, today="2026-11-05"):
    spec = importlib.util.spec_from_file_location("ingest_sharepoint", ROOT / "bin" / "ingest_sharepoint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(list(args) + ["--today", today], graph=graph)


def open_db():
    return db.connect(config.get_settings().risk_db)


# ------------------------------------------------------------------ parse ---

@pytest.fixture
def hasher():
    from rm import crypto
    return crypto.Hasher(crypto.derive_hash_key("pw", b"s" * 16))


def test_parse_maps_hashes_and_flags(hasher):
    df = bt.parse(to_csv([row(1, "2026-11-01 10:00:00"),
                          row(2, "01/11/2026 11:00", desc="Do Not Honour", status="Declined", card="1111")]), hasher)
    r = df.set_index("BookingId").to_dict("index")
    assert r[1]["CardLast4"] == "4242" and r[2]["CardLast4"] == "1111"
    assert r[1]["IsDecline"] == 0 and r[2]["IsDecline"] == 1
    assert r[2]["TransactionDate"] == "2026-11-01 11:00:00"
    assert r[1]["EmailHash"] == hasher.email("JANE@example.com ")
    assert r[1]["PhoneHash"] == hasher.phone("+447700900123")
    assert r[1]["WaitedInQueue"] == 1 and r[1]["StartingPosition"] == 120
    for v in r[1].values():                       # no personal data in clear
        assert v not in ("Jane", "Doe", "jane@example.com", "07700 900123")


def test_parse_tolerates_header_spelling(hasher):
    header = [h.upper().replace(" ", "_") for h in HEADER]
    rows = [{h.upper().replace(" ", "_"): v for h, v in row(5, "2026-11-01").items()}]
    assert bt.parse(to_csv(rows, header), hasher)["BookingId"].tolist() == [5]


def test_parse_cp1252_and_crlf(hasher):
    data = to_csv([row(1, "2026-11-01", **{"Event Name": "Café"})], crlf=True).decode("utf-8-sig").encode("cp1252")
    assert bt.parse(data, hasher)["EventName"].tolist() == ["Café"]


@pytest.mark.parametrize("data,reason", [
    (b"", "empty"),
    (b"Booking ID,Amount\n1,2\n", "missing required column"),
    (to_csv([]), "no rows"),
    (to_csv([row("abc", "2026-11-01")]), "not a number"),
])
def test_parse_rejects(hasher, data, reason):
    with pytest.raises(bt.FileRejected) as e:
        bt.parse(data, hasher)
    assert reason in str(e.value)


def test_full_card_number_rejected_without_echoing_it(hasher):
    with pytest.raises(bt.FileRejected) as e:
        bt.parse(to_csv([row(1, "2026-11-01"), row(2, "2026-11-01", card="4111111111111111")]), hasher)
    assert "row 3" in str(e.value) and "4111" not in str(e.value)


@pytest.mark.parametrize("desc,status,expected", [
    ("Approved", "Success", 0), ("Insufficient funds", "", 1), ("Card expired", "", 1),
    ("", "Declined", 1), ("Authorised", "", 0), ("", "", None), ("Some new message", "Pending", None),
])
def test_is_decline(desc, status, expected):
    assert bt.is_decline(desc, status) == expected


# -------------------------------------------------------------- end to end ---

def test_import_then_delete(env):
    g = FakeGraph({"export1.csv": to_csv([row(1, "2026-11-01 10:00"), row(2, "2026-11-01 11:00")])})
    assert run(g) == 0
    assert g.in_folder(FOLDER) == {}                                  # deleted after commit
    conn = open_db()
    assert conn.execute("SELECT COUNT(*) FROM booking_transactions").fetchone()[0] == 2
    log = conn.execute("SELECT status, ref FROM import_log WHERE source='sharepoint'").fetchone()
    assert tuple(log) == ("ok", "export1.csv")
    conn.close()


def test_empty_folder_is_fine(env, capsys):
    assert run(FakeGraph()) == 0
    assert "Nothing to import" in capsys.readouterr().out


def test_duplicate_file_deleted_without_reimport(env):
    data = to_csv([row(1, "2026-11-01 10:00")])
    assert run(FakeGraph({"a.csv": data})) == 0
    g = FakeGraph({"a (copy).csv": data})
    assert run(g) == 0
    assert g.in_folder(FOLDER) == {}
    conn = open_db()
    assert conn.execute("SELECT COUNT(*) FROM import_log WHERE source='sharepoint'").fetchone()[0] == 1
    conn.close()


def test_bad_file_moved_to_failed_with_note_and_alert(env):
    g = FakeGraph({"good.csv": to_csv([row(1, "2026-11-01")]),
                   "bad.csv": to_csv([row(2, "2026-11-01", card="4111 1111 1111 1111")]),
                   "notes.xlsx": b"PK..."})
    assert run(g) == 1
    assert g.in_folder(FOLDER) == {}                                  # good deleted, bad + xlsx moved
    failed = g.in_folder(FOLDER + "/Failed")
    names = sorted(failed)
    assert any(n.startswith("bad-") and n.endswith(".csv") for n in names)
    assert any(n.startswith("notes-") and n.endswith(".xlsx") for n in names)
    note = [v["data"].decode() for n, v in failed.items() if n.startswith("bad-") and n.endswith(".error.txt")][0]
    assert "last 4 digits" in note and "4111" not in note
    assert len(g.mail) == 1 and "bad.csv" in g.mail[0][2] and "4111" not in g.mail[0][2]
    conn = open_db()
    assert [r[0] for r in conn.execute("SELECT BookingId FROM booking_transactions")] == [1]
    conn.close()


def test_newer_row_wins_across_files_and_history_keeps_decline(env):
    first = to_csv([row(7, "2026-11-01 10:00", desc="Insufficient funds", status="Declined"),
                    row(7, "2026-11-01 10:05", desc="Approved", status="Success")])
    assert run(FakeGraph({"day1.csv": first})) == 0
    older = to_csv([row(7, "2026-10-31 09:00", desc="Card expired", status="Declined")])
    assert run(FakeGraph({"late.csv": older})) == 0
    conn = open_db()
    latest = conn.execute("SELECT Status, TransactionDate FROM booking_transactions WHERE BookingId=7").fetchone()
    assert tuple(latest) == ("Success", "2026-11-01 10:05:00")        # older file didn't overwrite
    hist = conn.execute("SELECT IsDecline FROM booking_transaction_history WHERE BookingId=7 "
                        "ORDER BY TransactionDate").fetchall()
    assert [r[0] for r in hist] == [1, 1, 0]
    first_seen = conn.execute("SELECT FirstSeenAt FROM booking_transactions WHERE BookingId=7").fetchone()[0]
    conn.close()
    newer = to_csv([row(7, "2026-11-02 08:00", desc="Approved", status="Refunded")])
    assert run(FakeGraph({"day2.csv": newer})) == 0
    conn = open_db()
    r = conn.execute("SELECT Status, FirstSeenAt FROM booking_transactions WHERE BookingId=7").fetchone()
    assert (r[0], r[1]) == ("Refunded", first_seen)                   # FirstSeenAt kept
    conn.close()


def test_retention_prunes_old_rows(env):
    g = FakeGraph({"x.csv": to_csv([row(1, "2023-01-01 10:00"), row(2, "2026-11-01 10:00")])})
    assert run(g) == 0
    conn = open_db()
    assert [r[0] for r in conn.execute("SELECT BookingId FROM booking_transactions")] == [2]
    assert [r[0] for r in conn.execute("SELECT BookingId FROM booking_transaction_history")] == [2]
    conn.close()


def test_dry_run_changes_nothing(env, capsys):
    g = FakeGraph({"a.csv": to_csv([row(1, "2026-11-01")]), "b.csv": b"junk,\n"})
    assert run(g, "--dry-run") == 1                                   # b.csv would fail
    assert sorted(g.in_folder(FOLDER)) == ["a.csv", "b.csv"] and g.mail == []
    conn = open_db()
    assert conn.execute("SELECT COUNT(*) FROM booking_transactions").fetchone()[0] == 0
    conn.close()
    assert "dry run" in capsys.readouterr().out


def test_delete_failure_keeps_data_and_retries_next_run(env, capsys):
    g = FakeGraph({"a.csv": to_csv([row(1, "2026-11-01")])})
    g.fail_delete = True
    assert run(g) == 0
    assert "will be removed next run" in capsys.readouterr().out
    g.fail_delete = False
    assert run(g) == 0
    assert g.in_folder(FOLDER) == {}


def test_local_file_option(env, tmp_path):
    f = tmp_path / "local.csv"
    f.write_bytes(to_csv([row(3, "2026-11-01")]))
    assert run(None, "--file", str(f)) == 0
    assert f.exists()                                                 # local file left alone
    conn = open_db()
    assert conn.execute("SELECT BookingId FROM booking_transactions").fetchone()[0] == 3
    conn.close()


def test_missing_graph_settings(env, capsys):
    assert run(None) == 1
    assert "AZURE_TENANT_ID" in capsys.readouterr().out


# ------------------------------------------------------------- Graph client ---

class FakeResp:
    def __init__(self, status, body=None, content=b"", headers=None):
        self.status_code, self._body, self.content = status, body, content
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def test_graph_list_pages_and_404():
    s = FakeSession([
        FakeResp(200, {"value": [{"id": "1", "name": "a.csv", "size": 3, "file": {}}],
                       "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}),
        FakeResp(200, {"value": [{"id": "2", "name": "Failed", "folder": {}}]}),
        FakeResp(404, {}),
    ])
    g = Graph("t", "c", "s", drive_id="D", session=s, token="tok", sleep=lambda *_: None)
    items = g.list_folder("Platform Data/Booking Transactions")
    assert [(i["name"], i["is_folder"]) for i in items] == [("a.csv", False), ("Failed", True)]
    assert "/drives/D/root:/Platform%20Data/Booking%20Transactions:/children" in s.calls[0][1]
    assert s.calls[0][2]["headers"]["Authorization"] == "Bearer tok"
    assert g.list_folder("Nope") == []


def test_graph_retries_throttling_then_raises():
    s = FakeSession([FakeResp(429, headers={"Retry-After": "1"}), FakeResp(200, content=b"data")])
    slept = []
    g = Graph("t", "c", "s", drive_id="D", session=s, token="tok", sleep=slept.append)
    assert g.download("X") == b"data" and slept == [1]
    s2 = FakeSession([FakeResp(503)] * 4)
    with pytest.raises(GraphError):
        Graph("t", "c", "s", drive_id="D", session=s2, token="tok", sleep=lambda *_: None).delete("X")


def test_graph_move_delete_mail_shapes():
    s = FakeSession([FakeResp(200, {}), FakeResp(204), FakeResp(202)])
    g = Graph("t", "c", "s", drive_id="D", sender="reports@x.com", session=s, token="tok")
    g.move("ID", "Platform Data/Booking Transactions/Failed", "a-1.csv")
    g.delete("ID")
    g.send_mail("henry@x.com, kathryn@x.com", "Subj", "Body")
    assert s.calls[0][2]["json"] == {"parentReference": {"path": "/drive/root:/Platform Data/Booking Transactions/Failed"},
                                     "name": "a-1.csv"}
    assert s.calls[1][0] == "DELETE"
    msg = s.calls[2][2]["json"]["message"]
    assert [r["emailAddress"]["address"] for r in msg["toRecipients"]] == ["henry@x.com", "kathryn@x.com"]
    assert s.calls[2][1].endswith("/users/reports%40x.com/sendMail")
