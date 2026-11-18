"""Ingestion of lead CSVs and event JSONL files.

Idempotency: every source file is recorded in ``ingested_files`` (drop name + relative path +
sha256) in the SAME transaction as its data. Re-running ingest skips files already recorded; a
file whose name is known but whose content changed is refused (nothing is written).
Event rows are additionally de-duplicated by ``event_id`` (primary key).
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .crypto import Crypto
from .parsing import normalise_header, normalise_phone, parse_timestamp_us, parse_value
from .schema import ConfigError, Schema


class IngestConflict(Exception):
    """A previously ingested file name now has different content."""


# ===================================================================== leads
def build_column_map(schema: Schema, header: list[str]) -> dict[str, Optional[int]]:
    """field name -> index of the first column whose normalised header matches name/alias."""
    normalised = [normalise_header(h) for h in header]
    out: dict[str, Optional[int]] = {}
    for f in schema.fields:
        keys = f.header_keys
        out[f.name] = next((i for i, h in enumerate(normalised) if h in keys), None)
    return out


def validate_lead_row(schema: Schema, colmap: dict[str, Optional[int]], row: list[str]):
    """Returns (reject_reason | None, parsed_values, warning_count).

    Fields are walked in schema order; the first failure is the rejection reason.
    ``parsed_values`` maps field name -> normalised value or None (missing / invalid optional).
    """
    parsed: dict[str, Optional[str]] = {}
    warnings = 0
    identity = schema.identity_field.name
    for f in schema.fields:
        idx = colmap[f.name]
        raw = row[idx].strip() if idx is not None and idx < len(row) else ""
        if raw == "":
            if f.required:
                return f"missing_required:{f.name}", {}, 0
            parsed[f.name] = None
            continue
        ok, val = parse_value(f.type, raw, list(f.values))
        if ok:
            parsed[f.name] = val
        elif f.name == identity:
            return "invalid_phone", {}, 0
        elif f.required:
            return f"invalid_value:{f.name}", {}, 0
        else:
            parsed[f.name] = None
            warnings += 1
    return None, parsed, warnings


def _split_fields(schema: Schema, values: dict):
    pii = {f.name: values[f.name] for f in schema.fields if f.pii or f.name == schema.identity_field.name}
    plain = {f.name: values[f.name] for f in schema.fields if f.name not in pii}
    return plain, pii


def ingest_lead_file(conn: sqlite3.Connection, crypto: Crypto, schema: Schema, path: Path,
                     drop_name: str) -> dict:
    batch_id = path.stem
    stats = {"rows_total": 0, "rows_rejected": 0, "merged_duplicates": 0, "field_warnings": 0,
             "leads_created": 0, "rejections_by_reason": {}}
    reasons: Counter = Counter()
    ref_name = schema.reference_field.name
    ident = schema.identity_field.name
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return stats
        colmap = build_column_map(schema, header)
        row_no = 0
        for row in reader:
            if not any(c.strip() for c in row):
                continue  # blank row: not counted
            row_no += 1
            stats["rows_total"] += 1
            reason, parsed, warnings = validate_lead_row(schema, colmap, row)
            if reason:
                stats["rows_rejected"] += 1
                reasons[reason] += 1
                conn.execute("INSERT INTO rejections VALUES (?,?,?,?,?)",
                             (drop_name, schema.workflow_id, batch_id, row_no, reason))
                continue
            stats["field_warnings"] += warnings
            phone = parsed[ident]
            phash = crypto.phone_hash(phone)
            existing = conn.execute(
                "SELECT id, fields_json, pii_enc FROM leads WHERE workflow_id=? AND phone_hash=?",
                (schema.workflow_id, phash)).fetchone()
            if existing is None:
                for f in schema.fields:  # defaults fill whatever is still null
                    if parsed[f.name] is None and f.default is not None:
                        parsed[f.name] = f.default
                plain, pii = _split_fields(schema, parsed)
                conn.execute(
                    "INSERT INTO leads (workflow_id, phone_hash, batch_id, external_ref, phone_last4,"
                    " fields_json, pii_enc) VALUES (?,?,?,?,?,?,?)",
                    (schema.workflow_id, phash, batch_id, parsed[ref_name], phone[-4:],
                     json.dumps(plain, sort_keys=True, ensure_ascii=False), crypto.encrypt_json(pii)))
                stats["leads_created"] += 1
            else:
                lead_id, fields_json, pii_enc = existing
                stats["merged_duplicates"] += 1
                plain_cur = json.loads(fields_json)
                pii_cur = crypto.decrypt_json(pii_enc)
                new_plain, new_pii = _split_fields(schema, parsed)
                plain_cur.update({k: v for k, v in new_plain.items() if v is not None})
                pii_cur.update({k: v for k, v in new_pii.items() if v is not None})
                conn.execute(
                    "UPDATE leads SET fields_json=?, pii_enc=?, external_ref=? WHERE id=?",
                    (json.dumps(plain_cur, sort_keys=True, ensure_ascii=False),
                     crypto.encrypt_json(pii_cur), plain_cur.get(ref_name), lead_id))
    stats["rejections_by_reason"] = dict(sorted(reasons.items()))
    return stats


# ===================================================================== events
def _as_id(v) -> Optional[str]:
    if isinstance(v, str):
        return v if v.strip() else None
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return None


def prepare_event(obj: dict, crypto: Crypto) -> Optional[tuple]:
    """Validated dict -> row tuple (without drop/source/line), or None if not storable."""
    eid = _as_id(obj.get("event_id"))
    if eid is None:
        return None
    ts = parse_timestamp_us(obj.get("occurred_at"))
    if ts is None:
        return None
    etype = obj.get("event_type")
    wf = obj.get("workflow_id")
    call = _as_id(obj.get("call_id"))
    canon = normalise_phone(obj.get("phone"))
    payload = obj.get("payload")
    code = payload.get("disposition_code") if isinstance(payload, dict) else None
    return (eid, etype if isinstance(etype, str) else "", wf if isinstance(wf, str) else None, call,
            ts, str(obj.get("occurred_at")), crypto.phone_hash(canon) if canon else None,
            code if isinstance(code, str) else None,
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


_INSERT_EVENT = ("INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, ts_us,"
                 " occurred_at_raw, phone_hash, disposition_code, payload_json, drop_name, source_file,"
                 " line_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")


def ingest_event_file(conn: sqlite3.Connection, crypto: Crypto, path: Path, drop_name: str,
                      relpath: str, chunk: int = 20000) -> dict:
    stats = {"events_total": 0, "malformed": 0, "duplicates": 0, "stored": 0}
    batch: list[tuple] = []

    def flush():
        if not batch:
            return
        before = conn.total_changes
        conn.executemany(_INSERT_EVENT, batch)
        inserted = conn.total_changes - before
        stats["stored"] += inserted
        stats["duplicates"] += len(batch) - inserted
        batch.clear()

    with open(path, "rb") as fh:  # binary: split on \n only (JSON may hold U+2028 etc.)
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            stats["events_total"] += 1
            try:
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                stats["malformed"] += 1
                continue
            if not isinstance(obj, dict):
                stats["malformed"] += 1
                continue
            row = prepare_event(obj, crypto)
            if row is None:
                stats["malformed"] += 1
                continue
            batch.append(row + (drop_name, relpath, line_no))
            if len(batch) >= chunk:
                flush()
    flush()
    return stats


# ===================================================================== orchestration
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


@dataclass
class Planned:
    kind: str
    relpath: str
    path: Path
    sha: str
    workflow_id: Optional[str] = None
    skip: bool = False


def plan_drop(conn: sqlite3.Connection, drop: Path, schemas: dict[str, Schema],
              kinds=("leads", "events")) -> list[Planned]:
    leads_dir, events_dir = drop / "leads", drop / "events"
    if not leads_dir.is_dir() and not events_dir.is_dir():
        raise ConfigError(f"{drop} is not a drop directory (expected leads/ and/or events/)")
    plan: list[Planned] = []
    if "leads" in kinds and leads_dir.is_dir():
        found = []
        for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
            if wf_dir.name not in schemas:
                raise ConfigError(f"{wf_dir}: no schema for workflow '{wf_dir.name}'")
            found += [(f.name, wf_dir.name, f) for f in wf_dir.glob("*.csv")]
        for name, wf, f in sorted(found):  # filename order
            plan.append(Planned("leads", f"leads/{wf}/{name}", f, _sha256(f), wf))
    if "events" in kinds and events_dir.is_dir():
        for f in sorted(events_dir.glob("*.jsonl")):
            plan.append(Planned("events", f"events/{f.name}", f, _sha256(f)))
    for p in plan:
        row = conn.execute("SELECT sha256 FROM ingested_files WHERE drop_name=? AND kind=? AND relpath=?",
                           (drop.name, p.kind, p.relpath)).fetchone()
        if row is not None:
            if row[0] != p.sha:
                raise IngestConflict(
                    f"{drop.name}/{p.relpath} was ingested before with different content. Refusing to "
                    f"ingest anything. Use a fresh --db for a different dataset (or `reset`).")
            p.skip = True
    return plan


def ingest_drop(conn: sqlite3.Connection, crypto: Crypto, schemas: dict[str, Schema], drop_path,
                kinds=("leads", "events"), log: Callable[[str], None] = lambda s: None) -> dict:
    drop = Path(drop_path).resolve()
    plan = plan_drop(conn, drop, schemas, kinds)
    summary = {"drop": drop.name, "files_ingested": 0, "files_skipped": 0}
    for p in plan:
        if p.skip:
            summary["files_skipped"] += 1
            log(f"  skip   {drop.name}/{p.relpath} (already ingested)")
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            if p.kind == "leads":
                stats = ingest_lead_file(conn, crypto, schemas[p.workflow_id], p.path, drop.name)
            else:
                stats = ingest_event_file(conn, crypto, p.path, drop.name, p.relpath)
            conn.execute("INSERT INTO ingested_files (drop_name, kind, relpath, sha256, stats_json)"
                         " VALUES (?,?,?,?,?)",
                         (drop.name, p.kind, p.relpath, p.sha, json.dumps(stats, sort_keys=True)))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        summary["files_ingested"] += 1
        log(f"  ingest {drop.name}/{p.relpath}: " + ", ".join(
            f"{k}={v}" for k, v in stats.items() if not isinstance(v, dict)))
    return summary
