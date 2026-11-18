"""Whole-pipeline properties on generated data: idempotency, incrementality, PII, append-only,
and an independent naive reference implementation."""
import sqlite3

import pytest

from solution import ingest

from . import reference
from .conftest import SCHEMAS
from .helpers import DISP_ROWS, SCH, ingest_all, open_db, run_reports

SEEDS = [42, 7, 2026]


def drops(root):
    return [root / "drop_1", root / "drop_2"]


def snapshot(conn):
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1,2,3").fetchall()
            for t in ("leads", "events", "rejections", "ingested_files")}


@pytest.mark.parametrize("seed", SEEDS)
def test_matches_naive_reference(seed, gen_data, tmp_path):
    root = gen_data(seed)
    conn, crypto = open_db(tmp_path)
    ingest_all(conn, crypto, drops(root))
    got = run_reports(conn, tmp_path / "out")
    exp = reference.run(drops(root), SCH, DISP_ROWS)
    for k in ("lead_report", "batch_summary", "daily_overview", "rejections"):
        assert got[k] == [[str(c) for c in r] for r in exp[k]], k
    assert {k: v for k, v in got["dq"].items()} == exp["dq"]


def test_reingest_changes_nothing(gen_data, tmp_path):
    conn, crypto = open_db(tmp_path)
    ingest_all(conn, crypto, drops(gen_data(42)))
    before, rep1 = snapshot(conn), run_reports(conn, tmp_path / "o1")
    ingest_all(conn, crypto, drops(gen_data(42)))
    assert snapshot(conn) == before and run_reports(conn, tmp_path / "o2") == rep1


def test_incremental_equals_all_at_once_and_any_arrival_order_of_kinds(gen_data, tmp_path):
    root = gen_data(7)
    a_conn, a_cr = open_db(tmp_path, "a.db")
    ingest_all(a_conn, a_cr, drops(root))                        # normal: drop1 then drop2
    b_conn, b_cr = open_db(tmp_path, "b.db")                      # all leads first, then all events
    ingest_all(b_conn, b_cr, drops(root), kinds=("leads",))
    ingest_all(b_conn, b_cr, drops(root), kinds=("events",))
    c_conn, c_cr = open_db(tmp_path, "c.db")                      # events first (every event arrives before its lead)
    ingest_all(c_conn, c_cr, drops(root), kinds=("events",))
    ingest_all(c_conn, c_cr, drops(root), kinds=("leads",))
    ra, rb, rc = (run_reports(x, tmp_path / n) for x, n in ((a_conn, "ra"), (b_conn, "rb"), (c_conn, "rc")))
    assert ra == rb == rc


def test_drop1_only_orphans_shrink_after_drop2(gen_data, tmp_path):
    conn, crypto = open_db(tmp_path)
    ingest_all(conn, crypto, [gen_data(42) / "drop_1"])
    o1 = run_reports(conn, tmp_path / "o1")["dq"]["orphan_events"]
    ingest_all(conn, crypto, [gen_data(42) / "drop_2"])
    assert run_reports(conn, tmp_path / "o2")["dq"]["orphan_events"] > 0 and o1 > 0


def test_changed_file_under_same_name_is_refused_atomically(gen_data, tmp_path):
    import shutil
    root = tmp_path / "copy"
    shutil.copytree(gen_data(42) / "drop_1", root / "drop_1")
    conn, crypto = open_db(tmp_path)
    ingest_all(conn, crypto, [root / "drop_1"])
    before = snapshot(conn)
    f = root / "drop_1" / "events" / "events_0001.jsonl"
    f.write_text(f.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ingest.IngestConflict):
        ingest_all(conn, crypto, [root / "drop_1"])
    assert snapshot(conn) == before


def test_ledger_is_append_only(gen_data, tmp_path):
    conn, crypto = open_db(tmp_path)
    ingest_all(conn, crypto, [gen_data(42) / "drop_1"])
    for stmt in ("UPDATE events SET call_id='x'", "DELETE FROM events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(stmt)


def test_no_pii_in_database_file(gen_data, tmp_path):
    import csv
    root = gen_data(42)
    conn, crypto = open_db(tmp_path)
    ingest_all(conn, crypto, drops(root))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = (tmp_path / "t.db").read_bytes()
    names, phones = set(), set()
    for f in (root / "drop_1" / "leads" / "motor_renewal").glob("*.csv"):
        for row in csv.DictReader(open(f, encoding="utf-8")):
            n = row.get("customer_name") or row.get("Insured Name") or ""
            names.add(n.strip())
            phones.add("".join(ch for ch in (row.get("phone") or row.get("Mobile No") or "") if ch.isdigit())[-10:])
    names.discard("")
    phones = {p for p in phones if len(p) == 10}
    assert names and phones
    assert not [n for n in names if n.encode() in blob], "customer name stored in plaintext"
    assert not [p for p in phones if p.encode() in blob], "phone stored in plaintext"


def test_wrong_key_is_refused(gen_data, tmp_path, monkeypatch):
    from solution import db
    from solution.schema import ConfigError
    open_db(tmp_path)
    (tmp_path / "t.db.key").write_text("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    with pytest.raises(ConfigError, match="PII key"):
        db.connect(tmp_path / "t.db")


def test_cli_end_to_end(gen_data, tmp_path, capsys):
    from solution.cli import main
    root = gen_data(42)
    args = ["--db", str(tmp_path / "cli.db")]
    assert main(["ingest", str(root / "drop_1"), *args]) == 0
    assert main(["ingest", str(root / "drop_2"), *args]) == 0
    assert main(["report", "--out", str(tmp_path / "rep"), *args]) == 0
    assert sorted(p.name for p in (tmp_path / "rep").iterdir()) == [
        "batch_summary.csv", "daily_overview.csv", "data_quality.json", "lead_report.csv", "rejections.csv"]
