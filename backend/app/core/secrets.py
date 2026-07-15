"""Secrets vault — envelope encryption (plan §11 design, activated by the
first keyed adapter: AirNow).

Random 256-bit DEK encrypts each secret (AES-GCM, per-secret nonce). The DEK
is wrapped and stored in kv.

AIDEV-CAUTION — deliberate threat-model deviation, documented: the plan's
pure design wraps the DEK with the admin-password-derived key, but an
appliance must poll keyed sources UNATTENDED AFTER POWER LOSS, when no
password is available to unwrap. So the DEK is wrapped with a key derived
from the device-local app_secret instead. This protects backups/exports and
casual file reads; it does NOT protect against full-disk theft — that
upgrade is TPM-backed wrapping on V1.2 hardware (plan V2 trust features).
Secrets remain excluded from logs, backups, and diagnostics (tested).
"""

import hashlib
import os
import sqlite3
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core import security, snapshots


def _wrap_key() -> bytes:
    return hashlib.sha256(("dek-wrap:" + security.app_secret()).encode()).digest()


def _dek(conn: sqlite3.Connection) -> bytes:
    wrapped = snapshots.kv_get(conn, "dek_wrapped", None)
    if wrapped is None:
        dek = os.urandom(32)
        nonce = os.urandom(12)
        ct = AESGCM(_wrap_key()).encrypt(nonce, dek, None)
        snapshots.kv_set(conn, "dek_wrapped",
                         {"nonce": nonce.hex(), "ct": ct.hex()})
        return dek
    return AESGCM(_wrap_key()).decrypt(bytes.fromhex(wrapped["nonce"]),
                                       bytes.fromhex(wrapped["ct"]), None)


def store(conn: sqlite3.Connection, service: str, value: str,
          label: str = "") -> None:
    nonce = os.urandom(12)
    ct = AESGCM(_dek(conn)).encrypt(nonce, value.encode(), None)
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM secret WHERE service_name=?", (service,))
        conn.execute("INSERT INTO secret (service_name, label, ciphertext, nonce,"
                     " created_at) VALUES (?, ?, ?, ?, ?)",
                     (service, label, ct, nonce, now))


def retrieve(conn: sqlite3.Connection, service: str) -> str | None:
    row = conn.execute("SELECT ciphertext, nonce FROM secret WHERE service_name=?",
                       (service,)).fetchone()
    if row is None:
        return None
    with conn:
        conn.execute("UPDATE secret SET last_used_at=? WHERE service_name=?",
                     (datetime.now().isoformat(timespec="seconds"), service))
    return AESGCM(_dek(conn)).decrypt(row["nonce"], row["ciphertext"], None).decode()


def delete(conn: sqlite3.Connection, service: str) -> None:
    with conn:
        conn.execute("DELETE FROM secret WHERE service_name=?", (service,))


def exists(conn: sqlite3.Connection, service: str) -> bool:
    return conn.execute("SELECT 1 FROM secret WHERE service_name=?",
                        (service,)).fetchone() is not None


def clean_pasted_key(raw: str) -> str:
    """Paste-anything tolerance (approved smoother): strip whitespace and
    common prefixes users copy along with the key."""
    key = raw.strip().strip('"').strip("'")
    for prefix in ("api_key=", "API_KEY=", "key=", "KEY="):
        if key.startswith(prefix):
            key = key[len(prefix):]
    return key.strip()
