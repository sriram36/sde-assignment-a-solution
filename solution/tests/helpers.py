import csv
import json
from pathlib import Path

from solution import db, ingest, report
from solution.schema import load_dispositions, load_schemas

from .conftest import SCHEMAS

SCH = load_schemas(SCHEMAS)
DISP_ROWS, DISP_SHA = load_dispositions(SCHEMAS / "dispositions.csv")
MR_HEADER = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount", "preferred_language"]


def open_db(tmp: Path, name="t.db"):
    conn, crypto = db.connect(tmp / name)
    db.sync_dispositions(conn, DISP_ROWS, DISP_SHA)
    return conn, crypto


def make_drop(root: Path, name: str, lead_files: dict = None, events: list = None, wf="motor_renewal",
              raw_event_lines: list = None) -> Path:
    """lead_files: {'batch_001': [header, row, ...]}  events: list of dicts."""
    d = root / name
    for stem, rows in (lead_files or {}).items():
        p = d / "leads" / wf / f"{stem}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
    if events is not None or raw_event_lines is not None:
        p = d / "events" / "events_0001.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e) for e in (events or [])] + (raw_event_lines or [])
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


_n = [0]


def ev(etype, call, phone="9876543210", at="2026-10-05T10:00:00Z", payload=None, wf="motor_renewal", eid=None):
    _n[0] += 1
    e = {"event_id": eid or f"evt_{_n[0]:06d}", "event_type": etype, "workflow_id": wf, "call_id": call,
         "occurred_at": at, "payload": payload or {}}
    if phone is not None:
        e["phone"] = phone
    return e


def lead_row(name="A B", phone="9876543210", ref="MOT/1", expiry="", premium="", lang=""):
    return [name, phone, ref, expiry, premium, lang]


def run_reports(conn, out: Path) -> dict:
    dq = report.build_reports(conn, out)
    rd = lambda n: list(csv.reader(open(out / n, newline="", encoding="utf-8")))[1:]
    return {"dq": dq, "lead_report": rd("lead_report.csv"), "batch_summary": rd("batch_summary.csv"),
            "daily_overview": rd("daily_overview.csv"), "rejections": rd("rejections.csv")}


def ingest_all(conn, crypto, drops, kinds=("leads", "events")):
    for d in drops:
        ingest.ingest_drop(conn, crypto, SCH, d, kinds)
