# Assignment A: Interaction Ledger & Reporting Pipeline

A high-performance, idempotent data ingestion and reporting pipeline built for outbound voice campaigns. Stores events in an append-only interaction ledger with database-enforced immutability, secures PII at rest via HMAC-SHA256 and Fernet encryption, and dynamically derives all analytics reports at report time via SQL window functions.

---

## 1. Quickstart

### Prerequisites
- Python 3.11+
- SQLite >= 3.25 (with window function support)
- Dependencies: `pip install -r requirements.txt` (`cryptography>=42`, `pytest>=8.0`)

### End-to-End Execution
```bash
# 1. Generate synthetic dataset (from repository root)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 2. Ingest lead and event drops incrementally
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2

# 3. Generate the 5 output reports
python -m solution report --out reports/

# 4. Run test suite (106 tests, ~9 seconds)
pytest

# 5. Reset environment between different seeds or datasets
python -m solution reset --yes
```

---

## 2. Architecture & Design Principles

```
                  ┌──────────────────────────────────────────┐
                  │               Data Sources               │
                  │  Leads: CSV files | Events: JSONL streams│
                  └─────────────────────┬────────────────────┘
                                        │
                                        ▼
                  ┌──────────────────────────────────────────┐
                  │        Ingestion Engine (ingest.py)       │
                  │  - Header & E.164 phone normalisation    │
                  │  - IST/Day-first date parsing            │
                  │  - File-level transaction idempotency    │
                  └─────────────────────┬────────────────────┘
                                        │
                         ┌──────────────┴──────────────┐
                         ▼                             ▼
       ┌────────────────────────────────┐  ┌────────────────────────────────┐
       │   Fernet Tokenization (PII)    │  │    HMAC-SHA256 Phone Hash      │
       │   Name & raw phone encrypted   │  │   Searchable non-reversible    │
       └────────────────┬───────────────┘  └───────────────┬────────────────┘
                        │                                  │
                        └───────────────┬──────────────────┘
                                        │
                                        ▼
                  ┌──────────────────────────────────────────┐
                  │      Append-Only Ledger (db.py SQLite)   │
                  │  - Triggers reject UPDATE & DELETE       │
                  │  - File SHA-256 deduplication tracking  │
                  └─────────────────────┬────────────────────┘
                                        │
                                        ▼
                  ┌──────────────────────────────────────────┐
                  │       Report Derivation (report.py)      │
                  │  - Dynamic SQL window functions          │
                  │  - Zero stored counters (self-healing)   │
                  │  - Late lead/event orphan resolution     │
                  └─────────────────────┬────────────────────┘
                                        │
                                        ▼
                  ┌──────────────────────────────────────────┐
                  │             Output Contract              │
                  │  1. lead_report.csv                      │
                  │  2. performance_summary.csv              │
                  │  3. daily_performance.csv                │
                  │  4. data_quality.json                    │
                  │  5. rejections.csv                       │
                  └──────────────────────────────────────────┘
```

### Core Tenets
1. **Dynamic SQL Derivation Over Stored Counters**:
   No counters or report metrics are maintained during ingestion. All metrics (call counts, connection rates, bucket allocations, orphan links) are computed at report time from the raw ledger. This guarantees that:
   - Events arriving before their leads (orphans) are seamlessly linked once the lead arrives in a subsequent drop.
   - Ingesting drop-by-drop produces byte-for-byte identical reports to ingesting everything in a single batch.
2. **Append-Only Immutability**:
   Events are immutable facts. The `events` table is protected by SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers that raise abort exceptions on any modification.
3. **PII Protection at Rest**:
   Fields marked `pii: true` (`customer_name`, `phone`) are stored strictly as encrypted Fernet tokens. Identity matching is performed using `HMAC-SHA256(canonical_phone, master_key)`. Raw SHA-256 is vulnerable to rainbow table attacks given the constrained space of 10-digit mobile numbers; HMAC provides cryptographic security against offline dictionary attacks.
4. **Crash-Safe File Idempotency**:
   Ingestion processes each file inside its own atomic database transaction alongside a record in `ingested_files` containing the file's SHA-256 hash. Re-running ingestion on already processed files is a safe no-op. If a file with an existing path is modified, ingestion halts immediately with exit code 2.

---

## 3. Database Schema

| Table | Primary / Unique Key | Purpose |
| :--- | :--- | :--- |
| `leads` | `UNIQUE(workflow_id, phone_hash)` | Stores current lead state. PII payload is encrypted; `batch_id` is permanently assigned from the first valid row. |
| `events` | `PRIMARY KEY(event_id)` | Append-only interaction ledger. Enforces event deduplication across all drops. Protected by update/delete triggers. |
| `rejections` | `PRIMARY KEY(drop_name, workflow_id, batch_id, row_number)` | Records invalid lead rows with rejection reasons without logging PII. |
| `ingested_files` | `PRIMARY KEY(drop_name, kind, relpath)` | Stores SHA-256 checksums, timestamps, and row counts to ensure idempotent re-runs. |
| `dispositions` | `PRIMARY KEY(workflow_id, code)` | Disposition catalogue defining terminal and successful call outcomes per workflow. |
| `meta` | `PRIMARY KEY(key)` | Houses PII master key fingerprint and catalogue integrity checksums. |

---

## 4. Codebase Structure

```
solution/
├── __init__.py           # Package marker
├── __main__.py           # python -m solution entrypoint
├── cli.py                # Command-line interface: ingest, report, reset
├── crypto.py             # HMAC-SHA256 hashing and Fernet encryption
├── db.py                 # SQLite DDL, connection manager, triggers, indexes
├── ingest.py             # CSV/JSONL parsing, schema validation, transaction control
├── parsing.py            # Normalisers: phone numbers, IST dates, money decimals, headers
├── report.py             # Analytical SQL queries generating all 5 report artifacts
├── schema.py             # Schema parsing for workflows and dispositions.csv
├── README.md             # This technical documentation
└── tests/
    ├── conftest.py       # Pytest fixtures and dataset generator hooks
    ├── helpers.py        # Drop and event generation helpers
    ├── reference.py      # Independent naive in-memory verification model
    ├── test_parsing.py   # Unit tests for normalisers and edge-case inputs
    ├── test_pipeline.py  # End-to-end integration and CLI pipeline tests
    └── test_rules.py     # Spec compliance, bucket precedence, and property tests
```

---

## 5. CLI Reference

```bash
# Ingest one or more drops
python -m solution ingest data/drop_1 [data/drop_2 ...] [--db ledger.db] [--schemas assignment-a-data/schemas]

# Ingest only leads or only events
python -m solution ingest data/drop_1 --only leads
python -m solution ingest data/drop_1 --only events

# Generate reports into target folder
python -m solution report --out reports/ [--db ledger.db] [--schemas assignment-a-data/schemas]

# Reset ledger database and associated encryption key
python -m solution reset --yes [--db ledger.db]
```

### Environment Variables
- `LEDGER_DB`: Path to the SQLite database file (default: `ledger.db`).
- `LEDGER_PII_KEY`: Base64-encoded 32-byte secret master key. If not provided, a secure key file (`<db_path>.key`) is automatically generated with `0600` permissions.

---

## 6. Output Artifacts Contract

When `report` is executed, five files are written to the target directory:

1. **`lead_report.csv`**: Comprehensive per-lead status report containing:
   - `workflow_id`, `reference_id`, `batch_id`, `phone_masked` (`******XXXX`), `bucket`
   - `total_calls_initiated`, `connectivity_rate` (4 decimal places)
   - `last_call_at` (IST ISO format: `YYYY-MM-DDTHH:MM:SS+05:30`), `current_disposition`, `ever_renewed` (`true`/`false`)
2. **`performance_summary.csv`**: Aggregated performance metrics broken down by workflow and batch.
3. **`daily_performance.csv`**: Daily timeline analysis categorized by IST calendar date of call initiation.
4. **`data_quality.json`**: Structured audit metrics: total rows, rejected rows, duplicate leads merged, total events, duplicate events dropped, orphan events, and malformed lines.
5. **`rejections.csv`**: Detailed audit trail of rejected rows (`workflow_id`, `batch_id`, `row_number`, `reason`).

---

## 7. Edge Cases & Spec Compliance

| Edge Case | Solution Behavior |
| :--- | :--- |
| **Late Lead / Orphan Events** | Events occurring before their lead arrives are stored in the ledger. When the lead is ingested in a subsequent drop, report-time SQL joins automatically link them without manual intervention. |
| **Disposition Override** | `disposition.override` events lack phone numbers. The query resolves them through `call_id` linkage and prioritizes overrides over regular `disposition.set` events. |
| **Timestamp Collisions** | Ties in effective disposition timestamps are deterministically broken using the greater `event_id`. Ties in call initiation times are broken using the greater `call_id`. |
| **Indian Phone Formats** | Handles 10-digit, 11-digit (leading 0), and 12-digit (country code 91) variants, requiring leading digits 6, 7, 8, or 9, and normalizes them to canonical E.164 (`+91XXXXXXXXXX`). |
| **IST Timezone Standards** | All naive and UTC timestamps are converted to Indian Standard Time (`UTC+05:30`), ensuring correct calendar grouping for daily reports. |

---

## 8. Verification & Test Suite

The test suite contains **106 automated tests** providing comprehensive coverage:

```bash
pytest -v
```

### Coverage Highlights
- **Normaliser Unit Tests**: Strict validation of E.164 phone numbers, ISO/day-first dates (`DD/MM/YYYY`, `DD-Mon-YYYY`), currency amounts with symbol stripping, and header variations.
- **Spec Rule Scenarios**: Exhaustive verification of lead bucket precedence (`completed` > `in_progress_connected` > `in_progress_telephony_failed` > `yet_to_reach_out`), override hierarchies, and unknown disposition quarantining.
- **Property & Idempotency Tests**: Verifies that re-ingesting datasets does not alter database state or reports, and that ingesting in reverse or fragmented order produces identical output.
- **Security & PII Audits**: Directly scans database binary files to verify zero plaintext phone numbers or names exist unencrypted.
- **Cross-Check with Independent Reference**: Compares the SQL derivation engine against an independent in-memory reference model (`tests/reference.py`) across randomized seeds.

---

## 9. Performance Benchmark

Evaluated on the synthetic large dataset scale (`--scale large --seed 42`: 143,990 lead rows, 632,602 event lines):

| Operation | Execution Time | Output Integrity |
| :--- | :--- | :--- |
| Ingest `drop_1` + `drop_2` | ~45 s | Zero data loss, 100% deduplication |
| Full Report Generation | ~11 s | All 5 report files computed and formatted |
| Re-ingest (Idempotent Skip) | 0.2 s | Immediate no-op confirmation |
| **Total Pipeline Runtime** | **~56 s** *(well within < 2 min requirement)* | Database size: ~307 MB |
