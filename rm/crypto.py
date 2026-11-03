"""Hashing of personal data, the card-number guard, and backup encryption.

Personal data is never stored in clear. Each value is normalised (so
"Jane@X.com " and "jane@x.com" match) and stored as a keyed hash:

    HMAC-SHA256(hash_key, "<kind>:" + normalised value)  -> first 32 hex chars

The hash key comes from RISK_ENCRYPTION_PASSPHRASE via scrypt (with a random
salt created once and kept in the warehouse ``meta`` table) and then HKDF, so
the passphrase itself is never used directly. A keyed hash cannot be reversed
by guessing likely emails the way a plain SHA-256 can — without the
passphrase there is nothing to compare guesses against.

The passphrase must never change: hashes made with a different passphrase
don't match, and the original values aren't kept, so nothing can be re-hashed.
``db.init_db`` stores a check value and refuses to start with a different one.

Backup encryption (``encrypt_stream`` / ``decrypt_stream``) is the streaming
Fernet format from trybookinguk/s3reporting modules/utils/backup_crypto.py,
with its own magic header.
"""
import base64
import hashlib
import hmac
import io
import ipaddress
import os
import re
import struct

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

SALT_LEN = 16
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
_CHECK_MESSAGE = b"riskmanagement-passphrase-check-v1"


class CryptoError(RuntimeError):
    """Missing passphrase, wrong passphrase, or corrupt encrypted data."""


class SensitiveDataError(ValueError):
    """Input holds data the warehouse must never store (e.g. a full card number)."""


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def _scrypt(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise CryptoError("RISK_ENCRYPTION_PASSPHRASE is empty")
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def derive_hash_key(passphrase: str, salt: bytes) -> bytes:
    master = _scrypt(passphrase, salt)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"riskmanagement-hash-v1").derive(master)


def check_value(hash_key: bytes) -> str:
    """Fingerprint of the hash key, stored in meta to detect a changed passphrase."""
    return hmac.new(hash_key, _CHECK_MESSAGE, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_SPACES = re.compile(r"\s+")


def _blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return str(value).strip() == ""


def norm_email(value):
    return None if _blank(value) else str(value).strip().lower()


def norm_phone(value):
    """Best-effort E.164; UK numbers without a country code become +44."""
    if _blank(value):
        return None
    text = str(value).strip()
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if not plus and digits.startswith("00"):
        digits, plus = digits[2:], True
    if plus:
        return "+" + digits
    if digits.startswith("0") and len(digits) in (10, 11):
        return "+44" + digits[1:]
    if digits.startswith("44") and len(digits) in (11, 12):
        return "+" + digits
    return digits


def norm_name(value):
    return None if _blank(value) else _SPACES.sub(" ", str(value).strip()).upper()


def norm_ip(value):
    if _blank(value):
        return None
    text = str(value).strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return text.lower()


def norm_postcode(value):
    """Upper case, no spaces: 'sw1a 1aa' -> 'SW1A1AA'."""
    if _blank(value):
        return None
    pc = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    return pc or None


def postcode_outward(value):
    """UK outward code ('SW1A 1AA' -> 'SW1A'); None if it doesn't look like a UK postcode.

    Safe to keep in clear: an outward code covers thousands of addresses.
    """
    pc = norm_postcode(value)
    if not pc or not 5 <= len(pc) <= 7:
        return None
    outward, inward = pc[:-3], pc[-3:]
    if not (re.fullmatch(r"[A-Z]{1,2}[0-9][A-Z0-9]?", outward) and re.fullmatch(r"[0-9][A-Z]{2}", inward)):
        return None
    return outward


NORMALISERS = {
    "email": norm_email,
    "phone": norm_phone,
    "name": norm_name,
    "ip": norm_ip,
    "postcode": norm_postcode,
    "address": norm_name,
}


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

# Stored hashes are the first 128 bits of the HMAC (32 hex chars): collision-safe
# for billions of values, and half the space of the full digest on large tables.
HASH_HEX_LEN = 32


class Hasher:
    """Keyed hashing of personal values. Get one from ``db.get_hasher(conn, settings)``."""

    def __init__(self, hash_key: bytes):
        if len(hash_key) != 32:
            raise CryptoError("hash key must be 32 bytes")
        self._key = hash_key

    def hash(self, kind: str, value):
        """32-char hex hash of the normalised value, or None for a blank value."""
        if kind not in NORMALISERS:
            raise ValueError(f"unknown kind {kind!r}")
        norm = NORMALISERS[kind](value)
        if norm is None:
            return None
        msg = f"{kind}:{norm}".encode("utf-8")
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()[:HASH_HEX_LEN]

    def email(self, v):
        return self.hash("email", v)

    def phone(self, v):
        return self.hash("phone", v)

    def name(self, v):
        return self.hash("name", v)

    def ip(self, v):
        return self.hash("ip", v)

    def postcode(self, v):
        return self.hash("postcode", v)

    def address(self, v):
        return self.hash("address", v)


# ---------------------------------------------------------------------------
# Card guard
# ---------------------------------------------------------------------------

_MASK_CHARS = set("*xX•#")


def card_last4(value):
    """Return the last 4 digits of a card field, or None if blank.

    Accepted: '1234', '************1234', '4111 11** **** 1234', 'XXXX-1234'.
    Rejected (SensitiveDataError): any field with more than 4 digits and no
    masking characters — that is, a full or partial card number in clear.
    """
    if _blank(value):
        return None
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    masked = any(c in _MASK_CHARS for c in text)
    if len(digits) > 4 and not masked:
        raise SensitiveDataError("card field holds more than 4 unmasked digits")
    if len(digits) > 10:  # masked but still more than first-6 + last-4
        raise SensitiveDataError("card field holds more digits than a masked card number allows")
    return digits[-4:] if len(digits) >= 4 else None


# ---------------------------------------------------------------------------
# Backup encryption (streaming, bounded memory)
# ---------------------------------------------------------------------------

MAGIC = b"RMBKP1\n"
CHUNK_SIZE = 4 * 1024 * 1024
_LEN = struct.Struct(">I")


def _fernet(passphrase: str, salt: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_scrypt(passphrase, salt)))


def encrypt_stream(src, dst, passphrase: str) -> None:
    """Encrypt binary file-like ``src`` into ``dst`` in independent 4 MB frames."""
    salt = new_salt()
    f = _fernet(passphrase, salt)
    dst.write(MAGIC)
    dst.write(salt)
    while True:
        chunk = src.read(CHUNK_SIZE)
        if not chunk:
            break
        token = f.encrypt(chunk)
        dst.write(_LEN.pack(len(token)))
        dst.write(token)


def decrypt_stream(src, dst, passphrase: str) -> None:
    """Reverse of encrypt_stream. Raises CryptoError on a wrong passphrase or damage."""
    if src.read(len(MAGIC)) != MAGIC:
        raise CryptoError("not a RiskManagement backup (bad header)")
    salt = src.read(SALT_LEN)
    if len(salt) != SALT_LEN:
        raise CryptoError("truncated header")
    f = _fernet(passphrase, salt)
    while True:
        head = src.read(_LEN.size)
        if not head:
            return
        if len(head) != _LEN.size:
            raise CryptoError("truncated frame length")
        (n,) = _LEN.unpack(head)
        token = src.read(n)
        if len(token) != n:
            raise CryptoError("truncated frame")
        try:
            dst.write(f.decrypt(token))
        except Exception as e:
            raise CryptoError("decrypt failed: wrong passphrase or corrupt backup") from e


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    out = io.BytesIO()
    encrypt_stream(io.BytesIO(data), out, passphrase)
    return out.getvalue()


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    out = io.BytesIO()
    decrypt_stream(io.BytesIO(blob), out, passphrase)
    return out.getvalue()
