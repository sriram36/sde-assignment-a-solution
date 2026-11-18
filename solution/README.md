# Assignment A: Interaction Ledger & Reporting Pipeline

Python 3.11+, SQLite (>= 3.25, for window functions), one third-party dependency (`cryptography`).

```bash
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution ingest data/drop_1            # run from the repo root
python -m solution ingest data/drop_2            # (or: ingest data/drop_1 data/drop_2)
python -m solution report --out reports/
pytest                                           # 106 tests, ~10 s

python -m solution reset --yes                   # wipe ledger.db + its key (new dataset / new seed)
```

Options (all subcommands): `--db ledger.db` (or `$LEDGER_DB`), `--schemas <dir>` (default
`assignment-a-data/schemas`). `ingest --only leads|events` ingests one kind of file.
`$LEDGER_PII_KEY` (urlsafe-base64, 32 bytes) overrides the key file `<db>.key`.

Use a **fresh `--db` (or `reset`) per dataset**: ingest refuses a file whose name was ingested before with
different content (e.g. `drop_1` from a different seed) rather than silently mixing two datasets.

## Layout
| File | Role |
|---|---|
| `parsing.py` | pure normalisers: header, phone, date, decimal, enum, timestamp |
| `schema.py` | workflow schemas + disposition catalogue loading |
| `crypto.py` | HMAC phone hash, Fernet encryption, key handling |
| `db.py` | DDL (incl. append-only triggers), connection, key check |
| `ingest.py` | lead CSV + event JSONL ingestion, file-level idempotency |
| `report.py` | SQL derivation of calls / dispositions / buckets, writes the 5 files |
| `tests/` | unit + scenario tests, generated-data property tests, naive reference implementation |
