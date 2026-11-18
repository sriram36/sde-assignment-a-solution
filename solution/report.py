"""Report derivation. Everything here is DERIVED from the ledger + current lead state at report
time, so late-arriving leads, overrides and duplicates are always reflected and no counter can
drift. Heavy lifting is done in SQLite (temp tables); only formatting happens in Python.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

from .parsing import mask_phone, us_to_ist_iso

BUCKETS = ("yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed")

_DERIVE_SQL = """
DROP TABLE IF EXISTS temp.t_phone_link; DROP TABLE IF EXISTS temp.t_call_lead;
DROP TABLE IF EXISTS temp.t_link; DROP TABLE IF EXISTS temp.t_le; DROP TABLE IF EXISTS temp.t_call;
DROP TABLE IF EXISTS temp.t_flags; DROP TABLE IF EXISTS temp.t_eff; DROP TABLE IF EXISTS temp.t_calls;
DROP TABLE IF EXISTS temp.t_lead_agg; DROP TABLE IF EXISTS temp.t_cur;

-- 1. non-override events link to a lead via (workflow_id, phone hash)
CREATE TEMP TABLE t_phone_link AS
SELECT e.event_id, l.id AS lead_id
FROM events e JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
WHERE e.event_type <> 'disposition.override';

-- 2. a call's lead = lead of its phone-linked events
CREATE TEMP TABLE t_call_lead AS
SELECT e.call_id, e.workflow_id, MIN(p.lead_id) AS lead_id
FROM t_phone_link p JOIN events e ON e.event_id = p.event_id
WHERE e.call_id IS NOT NULL
GROUP BY e.call_id, e.workflow_id;

-- 3. overrides (no phone) link through their call_id
CREATE TEMP TABLE t_link AS
SELECT event_id, lead_id FROM t_phone_link
UNION ALL
SELECT e.event_id, cl.lead_id
FROM events e JOIN t_call_lead cl ON cl.call_id = e.call_id AND cl.workflow_id = e.workflow_id
WHERE e.event_type = 'disposition.override';
CREATE UNIQUE INDEX temp.ix_link ON t_link (event_id);

-- 4. linked events annotated with the disposition catalogue
CREATE TEMP TABLE t_le AS
SELECT e.event_id, e.event_type, e.call_id, e.workflow_id, e.ts_us, k.lead_id, e.disposition_code,
       d.is_terminal, d.is_success, (d.code IS NOT NULL) AS known
FROM events e JOIN t_link k ON k.event_id = e.event_id
LEFT JOIN dispositions d ON d.workflow_id = e.workflow_id AND d.code = e.disposition_code;
CREATE INDEX temp.ix_le_call ON t_le (call_id, lead_id);

-- 5. calls: only those whose call.initiated is present AND linked
CREATE TEMP TABLE t_call AS
SELECT call_id, lead_id, workflow_id, MIN(ts_us) AS init_ts
FROM t_le WHERE event_type = 'call.initiated' AND call_id IS NOT NULL
GROUP BY call_id, lead_id, workflow_id;
CREATE UNIQUE INDEX temp.ix_call ON t_call (call_id, lead_id);

CREATE TEMP TABLE t_flags AS
SELECT call_id, lead_id,
       MAX(event_type = 'call.connected') AS connected,
       MAX(event_type = 'call.telephony_failed') AS tfailed
FROM t_le WHERE call_id IS NOT NULL GROUP BY call_id, lead_id;
CREATE UNIQUE INDEX temp.ix_flags ON t_flags (call_id, lead_id);

-- 6. effective disposition: latest override, else latest set (known codes only)
CREATE TEMP TABLE t_eff AS
SELECT call_id, lead_id, disposition_code AS code, is_terminal, is_success FROM (
  SELECT t_le.*, ROW_NUMBER() OVER (
           PARTITION BY call_id, lead_id
           ORDER BY (event_type = 'disposition.override') DESC, ts_us DESC, event_id DESC) AS rn
  FROM t_le
  WHERE event_type IN ('disposition.set', 'disposition.override') AND known = 1 AND call_id IS NOT NULL
) WHERE rn = 1;
CREATE UNIQUE INDEX temp.ix_eff ON t_eff (call_id, lead_id);

CREATE TEMP TABLE t_calls AS
SELECT c.workflow_id, c.lead_id, c.call_id, c.init_ts,
       COALESCE(f.connected, 0) AS connected, COALESCE(f.tfailed, 0) AS tfailed,
       x.code, x.is_terminal, x.is_success
FROM t_call c
LEFT JOIN t_flags f ON f.call_id = c.call_id AND f.lead_id = c.lead_id
LEFT JOIN t_eff x ON x.call_id = c.call_id AND x.lead_id = c.lead_id;
CREATE INDEX temp.ix_calls_lead ON t_calls (lead_id);

-- explicit INTEGER PRIMARY KEY so the lead_report join is a rowid lookup (a CREATE TABLE AS
-- column has no type affinity and the planner then fell back to scanning: O(n^2) on 140k leads)
CREATE TEMP TABLE t_lead_agg (lead_id INTEGER PRIMARY KEY, total_calls INTEGER, connected_calls INTEGER,
    last_init INTEGER, any_terminal INTEGER, all_failed INTEGER, ever_success INTEGER);
INSERT INTO t_lead_agg
SELECT lead_id, COUNT(*), SUM(connected), MAX(init_ts), COALESCE(MAX(is_terminal), 0), MIN(tfailed),
       COALESCE(MAX(is_success), 0)
FROM t_calls GROUP BY lead_id;

CREATE TEMP TABLE t_cur (lead_id INTEGER PRIMARY KEY, code TEXT);
INSERT INTO t_cur
SELECT lead_id, code FROM (
  SELECT lead_id, code, ROW_NUMBER() OVER (PARTITION BY lead_id ORDER BY init_ts DESC, call_id DESC) AS rn
  FROM t_calls WHERE code IS NOT NULL) WHERE rn = 1;
"""

_LEAD_SQL = """
SELECT l.workflow_id, l.external_ref, l.phone_last4, l.batch_id,
       COALESCE(a.total_calls, 0), COALESCE(a.connected_calls, 0), a.last_init, c.code,
       COALESCE(a.ever_success, 0),
       CASE WHEN a.lead_id IS NULL THEN 'yet_to_reach_out'
            WHEN a.any_terminal = 1 THEN 'completed'
            WHEN a.all_failed = 1 THEN 'in_progress_telephony_failed'
            ELSE 'in_progress_dialed' END AS bucket
FROM leads l LEFT JOIN t_lead_agg a ON a.lead_id = l.id LEFT JOIN t_cur c ON c.lead_id = l.id
ORDER BY l.workflow_id, l.external_ref, l.batch_id, l.phone_hash
"""

_DAILY_SQL = """
SELECT workflow_id, date(init_ts / 1000000 + 19800, 'unixepoch') AS d, COUNT(*), SUM(connected),
       COUNT(DISTINCT CASE WHEN is_success = 1 THEN lead_id END)
FROM t_calls GROUP BY workflow_id, d ORDER BY workflow_id, d
"""


def derive(conn: sqlite3.Connection) -> None:
    conn.executescript(_DERIVE_SQL)


def _write_csv(path: Path, header: list, rows) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


def _tf(b) -> str:
    return "true" if b else "false"


def build_reports(conn: sqlite3.Connection, out_dir) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    derive(conn)

    # ---- lead_report + batch_summary (one pass over leads)
    lead_rows, batch = [], {}
    for wf, ref, last4, batch_id, tot, conn_c, last_init, cur, ever, bucket in conn.execute(_LEAD_SQL):
        lead_rows.append([wf, ref or "", batch_id, "+91******" + last4, bucket, tot, conn_c,
                          us_to_ist_iso(last_init) if last_init is not None else "", cur or "",
                          _tf(ever)])
        b = batch.setdefault((wf, batch_id), dict.fromkeys(BUCKETS, 0))
        b[bucket] += 1
    _write_csv(out / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"], lead_rows)
    _write_csv(out / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", *BUCKETS],
               [[wf, bid, sum(b.values()), *(b[k] for k in BUCKETS)] for (wf, bid), b in sorted(batch.items())])

    # ---- daily overview
    _write_csv(out / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
               [[wf, d, t, c, f"{c / t:.4f}", s] for wf, d, t, c, s in conn.execute(_DAILY_SQL)])

    # ---- rejections
    _write_csv(out / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               conn.execute("SELECT workflow_id, batch_id, row_number, reason FROM rejections"
                            " ORDER BY workflow_id, batch_id, row_number"))

    # ---- data quality
    dq = data_quality(conn)
    tmp = out / "data_quality.json.tmp"
    tmp.write_text(json.dumps(dq, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out / "data_quality.json")
    return dq


def data_quality(conn: sqlite3.Connection) -> dict:
    lead = {"rows_total": 0, "rows_rejected": 0, "merged_duplicates": 0, "field_warnings": 0}
    ev = {"events_total": 0, "malformed": 0, "duplicates": 0}
    reasons: dict = {}
    for kind, sj in conn.execute("SELECT kind, stats_json FROM ingested_files"):
        s = json.loads(sj)
        if kind == "leads":
            for k in lead:
                lead[k] += s[k]
            for r, n in s["rejections_by_reason"].items():
                reasons[r] = reasons.get(r, 0) + n
        else:
            for k in ev:
                ev[k] += s[k]
    orphans = conn.execute("SELECT COUNT(*) FROM events e LEFT JOIN t_link k ON k.event_id = e.event_id"
                           " WHERE k.event_id IS NULL").fetchone()[0]
    unknown = conn.execute("SELECT COUNT(*) FROM t_le WHERE substr(event_type, 1, 12) = 'disposition.'"
                           " AND known = 0").fetchone()[0]
    return {
        "lead_rows_total": lead["rows_total"],
        "lead_rows_rejected": lead["rows_rejected"],
        "rejections_by_reason": dict(sorted(reasons.items())),
        "duplicate_lead_rows_merged": lead["merged_duplicates"],
        "field_warnings": lead["field_warnings"],
        "events_total": ev["events_total"],
        "malformed_events": ev["malformed"],
        "duplicate_events_dropped": ev["duplicates"],
        "orphan_events": orphans,
        "unknown_disposition_events": unknown,
    }
