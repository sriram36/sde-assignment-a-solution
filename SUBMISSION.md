# Submission

## Candidate
- **Name:** Sriram Kolli
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~5 hours (architecture review, edge-case analysis, testing, and verification)

## How to run
```bash
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports/
pytest
```
Use a fresh `--db` or `python -m solution reset --yes` between datasets. More in `solution/README.md`.

## Design
**Principle: the ledger is the source of truth and reports are derived from it at report time.** Nothing
in a report is a stored counter, so a late lead, a late override or a duplicate can never leave stale
numbers behind. This is what makes "orphan after drop_1, linked after drop_2" and "incremental ==
all-at-once" true by construction.

SQLite, one file. Tables:

| Table | Purpose | Key constraints |
|---|---|---|
| `leads` | current state of each lead | `UNIQUE(workflow_id, phone_hash)` = lead identity; `batch_id` fixed at creation |
| `events` | the append-only interaction ledger | `PRIMARY KEY(event_id)` dedupes; `BEFORE UPDATE/DELETE` triggers abort |
| `rejections` | rejected lead rows (no PII) | PK `(drop, workflow, batch, row_number)` |
| `ingested_files` | one row per source file: sha256 + counters | PK `(drop, kind, relpath)`; written in the **same transaction** as the file's data |
| `dispositions` | catalogue from `dispositions.csv` | PK `(workflow_id, code)` |
| `meta` | PII-key fingerprint, catalogue hash | |

**Idempotency.** Each file is ingested in one transaction together with its `ingested_files` row, so a crash
leaves either all of the file or none of it. A re-run skips files already recorded. A known file name with
different content is refused before anything is written (exit code 2). Events are *also* idempotent
independently, through the `event_id` primary key.

**PII.** `customer_name` and `phone` (everything `pii:true`, plus the phone identity) are stored only as a
Fernet token. Matching events to leads uses `HMAC-SHA256(key, canonical_phone)`: a plain hash of a
10-digit number can be brute-forced in seconds, the keyed one cannot without the key. Events keep only
that hash (the raw phone is never stored). The key lives outside the DB (`$LEDGER_PII_KEY` or a `0600`
file beside it), and a fingerprint in `meta` stops the pipeline from running with the wrong key. A test
greps the database file for every name and phone in the input. `phone_last4` is stored in clear because
the report shows it masked anyway.

**Report derivation (all in SQL, `report.py`).** Link events to leads, derive each lead's calls from linked
`call.initiated` events, pick each call's effective disposition with `ROW_NUMBER()` (override first, then
latest), then aggregate. Formatting only in Python.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Report-time derivation instead of maintained link/counter tables | Always correct for late data; simplest to reason about | Materialised `event_links` updated on ingest (faster reports, more state to keep consistent) |
| Ties on effective-disposition timestamps broken by larger `event_id` | Deterministic and independent of ingest order | Ingest sequence (would break incremental == bulk) |
| Last call tie (same initiated time) broken by larger `call_id` | same | – |
| Same call_id seen in `call.initiated` twice → one call, earliest time | A call is one thing | Count twice |
| Override links only through `call_id` (+ same workflow), never by phone | Spec says so | Fall back to phone if present |
| Event with valid JSON + `event_id` but unparseable `occurred_at` counts as **malformed** | Cannot be placed on a timeline; the spec has no other bucket. Doesn't occur in generated data | Store with NULL time |
| Required non-phone field present but invalid → `invalid_value:<field>` rejection | Spec defines only 2 reasons; neither schema has such a field | Store null |
| Dates accept 1-digit day/month (`1/2/2026`) | Unambiguous day-first, friendlier | Strict 2-digit |
| Header normalisation gets one extra trailing strip (`_phone_` → `phone`) | The literal order leaves stray spaces that can never match | Literal |
| If two columns match one field, the first column wins | Deterministic | Error |
| `rejections_by_reason` lists only reasons that occurred | Counter semantics | List zeros |
| `batch_summary` has a row only for batches that own at least one lead | Grouped from leads | Also all-duplicate/all-rejected files |
| Disposition codes matched exactly (case-sensitive, no trim) | `RENEWD`/`call_back` are the intended unknowns | Case-insensitive |
| Decimal rounding half-up (money) | Conventional | Banker's |
| `connectivity_rate` uses Python `f"{x:.4f}"` | – | `Decimal` half-up (differs only on exact ties) |
| Unparseable/missing `call_id` on an event: stored, can't belong to a call, so it has no report effect | Pathological | Treat as malformed |

## Failure modes considered
- **Re-run / crash mid-ingest:** per-file transaction + file hash, tested (`test_reingest_changes_nothing`,
  changed-file refusal leaves the DB byte-for-byte equal).
- **Order of arrival:** tested that leads-first, events-first and drop-by-drop produce identical reports.
- **Bad input:** malformed/truncated JSON, non-object JSON, missing id, blank lines, blank CSV rows, BOM,
  short/long rows, unknown columns, unknown workflow directory (fails before writing anything).
- **Wrong or lost PII key:** refused loudly via fingerprint. If the key is lost the data cannot be
  re-matched (by design).
- **Not handled:** concurrent writers (single-writer assumption); a file whose content is *corrected*
  under the same name (refused, needs a reset or a new drop name); payloads are stored verbatim, so
  PII placed in a payload by an upstream system would be stored as-is; `phone_last4` is plaintext.

## Testing
106 tests, ~10 s (`pytest`):
- parsing tables: phones, dates, decimals, enums, headers, timestamps (incl. IST boundaries)
- hand-built scenarios: bucket precedence, override vs later set, unknown/overridden codes, per-workflow
  catalogue, call without `initiated`, orphan-then-lead, event line accounting, rejection order, merge rules
- generated data (seeds 42, 7, 2026): idempotency, incremental vs all-at-once vs events-first, append-only
  triggers, no plaintext PII in the DB file, CLI end to end
- **independent naive in-memory reference implementation** (`tests/reference.py`) cross-checked against the
  SQL pipeline on all three seeds
Not tested: Postgres (SQLite only), concurrent runs, very large single files beyond the large dataset run.
Caveat: the reference implementation shares my reading of the spec, so it catches bugs, not misreadings.

## Results
`--scale large --seed 42` (143,990 lead rows, 632,602 event lines), one laptop-class container core:

| Step | Time |
|---|---|
| ingest drop_1 + drop_2 | 45 s |
| report | 11 s |
| re-ingest of both drops | 0.2 s |
| total (ingest + report) | ~56 s (target < 2 min); DB 307 MB |

Surprise found while measuring: my first report took > 5 minutes because `CREATE TABLE AS` columns have no
type affinity and SQLite scanned a temp table per lead (O(n²)); explicit `INTEGER PRIMARY KEY` temp tables
fixed it (11 s).

## If I had another day
1. Truly incremental reports (materialised `event_links` + per-call facts updated on ingest, touching only
   affected leads); trade-off: more state that must stay consistent with report-time truth.
2. Postgres backend (`docker-compose.yml`) with `COPY` bulk loading.
3. Production note, 100k leads/day: land files in object storage, ingest per file as an idempotent job
   (the file hash is the idempotency key), run `report` after each landing + nightly full rebuild as a
   reconciliation check, alert on orphan-rate and malformed-rate spikes, and back-fill by re-ingesting
   into a fresh DB and diffing reports.
4. Reject/quarantine inspection command (orphans by reason: bad phone, unknown lead, override without call).
