"""Reading the TryBooking S3 exports.

Adapted from trybookinguk/s3reporting (data_loader key discovery,
warehouse_backfill column streaming, s3_inventory header detection), cut down
to what the warehouse needs.

Files are named ``YYYY/MM/<period>-<Report>-TBUK.csv`` where period is
YYYYMM (monthly, rewritten daily) or YYYYMMDD (daily snapshot). TryBooking
sometimes writes 0-byte files, so "latest" always means latest NON-EMPTY.
"""
import csv
import io
import os
import re
from datetime import date, datetime, timezone

import pandas as pd

KEY_RE = re.compile(r"(?:^|/)(?P<period>\d{8}|\d{6})-(?P<report>[A-Za-z]+)-TBUK\.csv$", re.I)
_PREAMBLE_MARKERS = ("first gateway id", "start booking transaction id")


def parse_key(key: str):
    """{'report', 'period'} for an export key, or None."""
    m = KEY_RE.search(key)
    if not m:
        return None
    return {"report": m.group("report"), "period": m.group("period")}


def month_prefixes(today: date, months: int) -> list:
    """['YYYY/MM/', ...] for ``months`` months ending with today's month, oldest first."""
    y, m = today.year, today.month
    out = []
    for _ in range(months):
        out.append(f"{y:04d}/{m:02d}/")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def files_for(objects, report: str) -> list:
    """Non-empty files of one report type, oldest period first (ties: LastModified)."""
    out = []
    for o in objects:
        info = parse_key(o["Key"])
        if info and info["report"].lower() == report.lower() and int(o.get("Size", 0)) > 0:
            out.append({**o, **info})
    return sorted(out, key=lambda o: (o["period"], str(o.get("LastModified", ""))))


def latest(objects, report: str):
    files = files_for(objects, report)
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class S3Source:
    def __init__(self, bucket: str, client=None, access_key: str = None, secret_key: str = None):
        if client is None:
            import boto3
            kw = {}
            if access_key and secret_key:
                kw = {"aws_access_key_id": access_key, "aws_secret_access_key": secret_key}
            client = boto3.client("s3", **kw)
        self.client = client
        self.bucket = bucket

    def ref(self, key):
        return f"s3://{self.bucket}/{key}"

    def list(self, prefixes) -> list:
        out = []
        pager = self.client.get_paginator("list_objects_v2")
        for prefix in prefixes:
            for page in pager.paginate(Bucket=self.bucket, Prefix=prefix):
                for o in page.get("Contents", []):
                    out.append({"Key": o["Key"], "Size": int(o["Size"]), "ETag": o.get("ETag", "").strip('"'),
                                "LastModified": o["LastModified"].strftime("%Y-%m-%d %H:%M:%S")})
        return out

    def head_bytes(self, key: str, n: int) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{n - 1}")["Body"].read()

    def open(self, key: str):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]


class LocalSource:
    """A folder laid out like the bucket (YYYY/MM/...csv). For tests and manual loads."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def ref(self, key):
        return os.path.join(self.root, key)

    def list(self, prefixes) -> list:
        out = []
        for dirpath, _, files in os.walk(self.root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                key = os.path.relpath(full, self.root).replace(os.sep, "/")
                if prefixes and not any(key.startswith(p) for p in prefixes):
                    continue
                st = os.stat(full)
                out.append({"Key": key, "Size": st.st_size, "ETag": f"{st.st_size}-{int(st.st_mtime)}",
                            "LastModified": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")})
        return out

    def head_bytes(self, key: str, n: int) -> bytes:
        with open(self.ref(key), "rb") as f:
            return f.read(n)

    def open(self, key: str):
        return open(self.ref(key), "rb")


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

def clean_col(name) -> str:
    return str(name).replace("﻿", "").replace("​", "").strip()


_DELIMITERS = (",", "\t", "|", ";")


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_col(name).lower())


def sniff(head: bytes, columns=()) -> tuple:
    """(lines to skip, header cells, delimiter) from the first bytes of an export.

    Tries comma, tab, pipe and semicolon. If ``columns`` are given, the first
    line (under any delimiter) holding the most of them is the header, so an
    unknown preamble or a lone ``NULL`` line is skipped. Otherwise it falls back
    to skipping gateway-batch preamble lines (AccountMovement*, GatewayMovement,
    TransactionReconciliation) and stray narrow lines before the real header.
    """
    text = head.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()[:12]
    if not lines:
        return 0, [], ","
    wanted = {_norm(c) for c in columns}
    if wanted:
        best = None
        for sep in _DELIMITERS:
            for i, r in enumerate(csv.reader(lines, delimiter=sep)):
                hits = sum(1 for c in r if _norm(c) in wanted)
                if hits and (best is None or hits > best[0]):
                    best = (hits, i, [clean_col(c) for c in r], sep)
        if best:
            return best[1], best[2], best[3]

    # No known column found: the delimiter giving the widest rows wins.
    sep = max(_DELIMITERS, key=lambda d: (max(len(r) for r in csv.reader(lines, delimiter=d)), d == ","))
    rows = [[clean_col(c) for c in r] for r in csv.reader(lines, delimiter=sep)]

    def filled(r):
        return sum(1 for c in r if c)

    def is_preamble(r):
        joined = ",".join(r).lower()
        return any(m in joined for m in _PREAMBLE_MARKERS)

    widest = max([filled(r) for r in rows if not is_preamble(r)] or [0])
    for i, r in enumerate(rows):
        if not filled(r) or is_preamble(r) or (widest >= 2 and filled(r) < max(2, widest // 2)):
            continue
        return i, [c for c in r], sep
    return 0, rows[0] if rows else [], sep


def header_offset(head: bytes) -> tuple:
    """(lines to skip, header cells) - comma files; see sniff()."""
    skip, header, _ = sniff(head)
    return skip, header


def _shape(cell: str) -> str:
    """A header cell as-is if it looks like a column name, else its shape (no data values)."""
    c = clean_col(cell)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,40}", c) and not re.search(r"\d{3}", c):
        return c
    return re.sub(r"[a-z]", "x", re.sub(r"[A-Z]", "X", re.sub(r"\d", "9", c)))[:20] or "(blank)"


def describe_head(head: bytes) -> str:
    """Safe one-line description of the first lines of a file, for errors: field
    counts per delimiter and the cells of each line, data values shown as shapes."""
    text = head.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()[:4]
    out = []
    for n, line in enumerate(lines, 1):
        counts = {repr(d): len(next(csv.reader([line], delimiter=d))) for d in _DELIMITERS}
        sep = max(_DELIMITERS, key=lambda d: counts[repr(d)])
        cells = next(csv.reader([line], delimiter=sep))
        shown = " | ".join(_shape(c) for c in cells[:16]) + (" | ..." if len(cells) > 16 else "")
        out.append(f"line {n}: fields per delimiter {counts}; {shown}")
    return "; ".join(out) or "file is empty"


def read_chunks(source, key: str, columns, chunksize: int = 100_000):
    """Yield DataFrames (all str) holding only ``columns`` that exist in the file.

    ``columns`` are canonical names; matching ignores case, BOMs, spaces and
    underscores, and output columns use the canonical spelling. The delimiter
    (comma, tab, pipe, semicolon) and any preamble are detected from the first
    64 KB. Missing columns are simply absent - callers decide which are required.
    """
    skip, header, sep = sniff(source.head_bytes(key, 64 * 1024), columns)
    by_norm = {_norm(c): c for c in columns}
    rename = {h: by_norm[_norm(h)] for h in header if _norm(h) in by_norm}
    body = source.open(key)
    try:
        reader = pd.read_csv(body, skiprows=skip, sep=sep, dtype=str, keep_default_na=False, chunksize=chunksize,
                             usecols=lambda c: clean_col(c) in rename, encoding="utf-8-sig",
                             on_bad_lines="warn", quoting=csv.QUOTE_MINIMAL)
        for chunk in reader:
            chunk.columns = [rename[clean_col(c)] for c in chunk.columns]
            yield chunk
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


# ---------------------------------------------------------------------------
# Value helpers (vectorised over pandas Series of str)
# ---------------------------------------------------------------------------

_NULLS = {"", "null", "none", "nan", "nat", "n/a"}


def text(s: pd.Series) -> pd.Series:
    s = s.astype(str).str.strip()
    return s.where(~s.str.lower().isin(_NULLS), None)


def to_int(s: pd.Series) -> pd.Series:
    n = pd.to_numeric(text(s), errors="coerce")
    return n.round().astype("Int64").astype(object).where(n.notna(), None)


def to_float(s: pd.Series) -> pd.Series:
    n = pd.to_numeric(text(s).str.replace(",", "", regex=False).str.replace("£", "", regex=False),
                      errors="coerce")
    return n.astype(object).where(n.notna(), None)


_TRUE = {"1", "true", "yes", "y", "t"}
_FALSE = {"0", "false", "no", "n", "f"}


def to_bool(s: pd.Series) -> pd.Series:
    low = text(s).str.lower()
    return low.map(lambda v: 1 if v in _TRUE else 0 if v in _FALSE else None).astype(object).where(
        low.isin(_TRUE | _FALSE), None)


_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_GUID36 = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def canonical_guid(value):
    """Booking GUIDs in one form: lower case with dashes. Other text is returned trimmed."""
    if value is None or (isinstance(value, float) and value != value):
        return None
    v = str(value).strip().strip("{}").strip()
    if not v or v.lower() in _NULLS:
        return None
    if _HEX32.match(v):
        v = f"{v[:8]}-{v[8:12]}-{v[12:16]}-{v[16:20]}-{v[20:]}"
    return v.lower() if _GUID36.match(v) else v


_DMY = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}")


def to_iso(s: pd.Series, date_only: bool = False, dayfirst: bool = True) -> pd.Series:
    """'YYYY-MM-DD HH:MM:SS' (or 'YYYY-MM-DD') from ISO or n/n/YYYY text
    (day first unless dayfirst=False).

    Offsets/Z are converted to UTC; naive values are kept as exported.
    """
    t = text(s)
    out = pd.Series([None] * len(t), index=t.index, dtype=object)
    present = t.notna()
    if not present.any():
        return out
    dmy = present & t.fillna("").str.match(_DMY)
    iso = present & ~dmy
    fmt = "%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M:%S"
    if iso.any():
        d = pd.to_datetime(t[iso], errors="coerce", utc=True, format="ISO8601")
        out[iso] = d.dt.strftime(fmt).where(d.notna(), None)
    if dmy.any():
        d = pd.to_datetime(t[dmy], errors="coerce", dayfirst=dayfirst, utc=True, format="mixed")
        out[dmy] = d.dt.strftime(fmt).where(d.notna(), None)
    return out.where(out.notna(), None)
