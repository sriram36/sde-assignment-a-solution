# AI Usage Disclosure

## Tools used
Claude (Anthropic), in a chat interface with a code sandbox.

## What I used them for
Claude read the assignment repo and README, proposed the design, and wrote the implementation
(`solution/`), the 106 tests, the naive reference implementation, `SUBMISSION.md` and this file's
draft. It also ran the generator, the tests, and the large-dataset timing in its sandbox.

## What I wrote or decided myself
- Evaluated both problem statements (Assignment A vs. Assignment B) and decided to tackle Assignment A based on its deterministic grading contract and architectural fit.
- Reviewed and audited each module (`schema.py`, `parsing.py`, `crypto.py`, `db.py`, `ingest.py`, and `report.py`) against the assignment specification.
- Evaluated architectural decisions for ambiguous edge cases:
  - Ensured report-time dynamic aggregation rather than stored counters to maintain consistency across late-arriving events.
  - Validated tie-breaking rules on effective disposition timestamps using `event_id` and call initiated times using `call_id`.
  - Confirmed HMAC-SHA256 hashing for phone numbers with key segregation outside the database.
- Executed the test suite, verified edge case handling (orphan event resolution, disposition override linkage solely via `call_id`, and IST timezone conversions).

## Where the AI was wrong or unhelpful
- First draft of `report.py` used column orders and `data_quality.json` key names written from memory,
  not from the README contract (e.g. `events_orphaned` instead of `orphan_events`, `lead_status` instead
  of `bucket`). Caught by re-reading the output contract before running it; fixed.
- First version of the lead_report query was O(n²) on the large dataset (> 5 min) because temp tables
  created with `CREATE TABLE AS` have no type affinity, so SQLite did not use the index. Found by
  profiling each SQL statement; fixed with explicit `INTEGER PRIMARY KEY` tables (11 s).

## How I verified AI-generated code
- 106 automated tests, including property tests on generated data (idempotency, ingest-order
  independence, no plaintext PII in the DB file) and an independent naive reference implementation
  compared against the SQL pipeline on three seeds.
- Ran the full `--scale large` dataset and verified timings and record counts.
- Checked output against the README contract examples (`0.5625` connectivity, `2026-10-14T17:55:24+05:30`).
- Inspected database tables directly to confirm zero plaintext PII and verified that append-only triggers reject unauthorized UPDATE and DELETE operations.
