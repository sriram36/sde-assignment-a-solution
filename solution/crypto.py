"""PII protection.

* Phone numbers are matched through a keyed hash (HMAC-SHA256). A plain hash of a 10-digit
  number is reversible by brute force (10^10 candidates), so the key matters.
* Everything marked ``pii: true`` (name, phone) is stored only as a Fernet token.
* The master key lives outside the database (env LEDGER_PII_KEY, or a 0600 key file beside it).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet


class Crypto:
    def __init__(self, master: bytes):
        if len(master) < 16:
            raise ValueError("PII master key too short")
        self._hmac_base = hmac.new(hmac.new(master, b"ledger/phone-hash", hashlib.sha256).digest(),
                                   digestmod=hashlib.sha256)
        self._fernet = Fernet(base64.urlsafe_b64encode(
            hmac.new(master, b"ledger/pii-encryption", hashlib.sha256).digest()))
        self.fingerprint = hmac.new(master, b"ledger/key-check", hashlib.sha256).hexdigest()

    def phone_hash(self, canonical_phone: str) -> str:
        h = self._hmac_base.copy()
        h.update(canonical_phone.encode())
        return h.hexdigest()

    def encrypt_json(self, obj: Any) -> bytes:
        return self._fernet.encrypt(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode())

    def decrypt_json(self, token: bytes) -> Any:
        return json.loads(self._fernet.decrypt(bytes(token)).decode())


def load_or_create_master_key(db_path: Path, create: bool = True) -> bytes:
    env = os.environ.get("LEDGER_PII_KEY")
    if env:
        return base64.urlsafe_b64decode(env.encode())
    key_path = Path(str(db_path) + ".key")
    if key_path.exists():
        return base64.urlsafe_b64decode(key_path.read_bytes().strip())
    if not create:
        raise FileNotFoundError(key_path)
    key = os.urandom(32)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.urlsafe_b64encode(key))
    return key
