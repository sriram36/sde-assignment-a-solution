"""SQLite storage: DDL, connection, catalogue sync."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .crypto import Crypto, load_or_create_master_key
from .schema import ConfigError, DispositionRow

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- One row per ingested source file. Makes ingest idempotent and stores per-file counters.
CREATE TABLE IF NOT EXISTS ingested_files (
    drop_name   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('leads','events')),
    relpath     TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    stats_json  TEXT NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (drop_name, kind, relpath)
);

-- Current state of each lead. PII only as ciphertext; phone matched via keyed hash.
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    phone_hash   TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    external_ref TEXT,
    phone_last4  TEXT NOT NULL,
    fields_json  TEXT NOT NULL,   -- non-PII fields (incl. defaults)
    pii_enc      BLOB NOT NULL,   -- Fernet(JSON of pii fields incl. phone)
    UNIQUE (workflow_id, phone_hash)
);
CREATE INDEX IF NOT EXISTS ix_leads_batch ON leads (workflow_id, batch_id);

CREATE TABLE IF NOT EXISTS rejections (
    drop_name   TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (drop_name, workflow_id, batch_id, row_number)
);

-- The interaction ledger: append-only (triggers below). No raw phone stored, only the hash.
CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    event_type       TEXT NOT NULL,
    workflow_id      TEXT,
    call_id          TEXT,
    ts_us            INTEGER NOT NULL,
    occurred_at_raw  TEXT NOT NULL,
    phone_hash       TEXT,
    disposition_code TEXT,
    payload_json     TEXT NOT NULL,
    drop_name        TEXT NOT NULL,
    source_file      TEXT NOT NULL,
    line_no          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_link ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call ON events (call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code        TEXT NOT NULL,
    label       TEXT NOT NULL,
    is_terminal INTEGER NOT NULL,
    is_success  INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, code)
);
"""


def connect(db_path: Path, create_key: bool = True) -> tuple[sqlite3.Connection, Crypto]:
    db_path = Path(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit transactions
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(DDL)
    crypto = Crypto(load_or_create_master_key(db_path, create=create_key))
    row = conn.execute("SELECT value FROM meta WHERE key='key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('key_fingerprint', ?)", (crypto.fingerprint,))
    elif row[0] != crypto.fingerprint:
        raise ConfigError("PII key does not match this database (wrong LEDGER_PII_KEY / key file). "
                          "Refusing to continue: phones could no longer be matched.")
    return conn, crypto


def sync_dispositions(conn: sqlite3.Connection, rows: list[DispositionRow], sha: str) -> None:
    cur = conn.execute("SELECT value FROM meta WHERE key='dispositions_sha'").fetchone()
    if cur and cur[0] == sha:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM dispositions")
        conn.executemany("INSERT INTO dispositions VALUES (?,?,?,?,?)",
                         [(r.workflow_id, r.code, r.label, int(r.is_terminal), int(r.is_success)) for r in rows])
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('dispositions_sha', ?)", (sha,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
