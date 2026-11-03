import io
import os

import pytest

from rm import crypto


@pytest.fixture(scope="module")
def hasher():
    return crypto.Hasher(crypto.derive_hash_key("pass", b"0" * 16))


def test_email_normalised(hasher):
    assert hasher.email(" Jane.Doe@Example.COM ") == hasher.email("jane.doe@example.com")
    assert len(hasher.email("a@b.c")) == 32


def test_kinds_do_not_collide(hasher):
    assert hasher.name("SMITH") != hasher.address("SMITH")


def test_blank_is_none(hasher):
    for v in (None, "", "   ", float("nan")):
        assert hasher.email(v) is None


@pytest.mark.parametrize("a,b", [
    ("07700 900123", "+447700900123"),
    ("0044 7700 900123", "+447700900123"),
    ("447700900123", "+447700900123"),
    ("+44 (0)7700-900123", "+4407700900123"),  # kept as typed after +
])
def test_phone(a, b):
    assert crypto.norm_phone(a) == b


def test_phone_hash_matches_formats(hasher):
    assert hasher.phone("07700 900123") == hasher.phone("+44 7700 900123")


def test_different_passphrase_different_hash():
    h1 = crypto.Hasher(crypto.derive_hash_key("one", b"s" * 16))
    h2 = crypto.Hasher(crypto.derive_hash_key("two", b"s" * 16))
    assert h1.email("x@y.z") != h2.email("x@y.z")


def test_different_salt_different_hash():
    h1 = crypto.Hasher(crypto.derive_hash_key("one", b"a" * 16))
    h2 = crypto.Hasher(crypto.derive_hash_key("one", b"b" * 16))
    assert h1.email("x@y.z") != h2.email("x@y.z")


@pytest.mark.parametrize("pc,out", [
    ("SW1A 1AA", "SW1A"), ("sw1a1aa", "SW1A"), ("M1 1AE", "M1"), ("B33 8TH", "B33"),
    ("CR2 6XH", "CR2"), ("DN55 1PT", "DN55"), ("EC1A 1BB", "EC1A"),
    ("12345", None), ("", None), (None, None), ("NOT A PC", None),
])
def test_postcode_outward(pc, out):
    assert crypto.postcode_outward(pc) == out


@pytest.mark.parametrize("value,last4", [
    ("1234", "1234"), ("************1234", "1234"), ("XXXX-XXXX-XXXX-1234", "1234"),
    ("4111 11** **** 1234", "1234"), ("", None), (None, None), ("12", None),
])
def test_card_last4(value, last4):
    assert crypto.card_last4(value) == last4


@pytest.mark.parametrize("value", ["4111111111111111", "4111 1111 1111 1111", "12345",
                                   "4111-1111-1111-1234", "4111 1111 11** 1234 5678"])
def test_card_guard_rejects(value):
    with pytest.raises(crypto.SensitiveDataError):
        crypto.card_last4(value)


def test_backup_roundtrip_multiframe(monkeypatch):
    monkeypatch.setattr(crypto, "CHUNK_SIZE", 1000)
    data = os.urandom(5500)
    blob = crypto.encrypt_bytes(data, "pw")
    assert blob.startswith(crypto.MAGIC) and data not in blob
    assert crypto.decrypt_bytes(blob, "pw") == data


def test_backup_wrong_passphrase():
    blob = crypto.encrypt_bytes(b"secret", "pw")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(blob, "other")


def test_backup_truncated():
    blob = crypto.encrypt_bytes(b"x" * 100, "pw")
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_bytes(blob[:-5], "pw")


def test_backup_bad_header():
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_stream(io.BytesIO(b"garbage"), io.BytesIO(), "pw")
