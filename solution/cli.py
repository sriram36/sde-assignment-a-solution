"""Command line interface.

    python -m solution ingest data/drop_1 [data/drop_2 ...]
    python -m solution report --out reports/
    python -m solution reset --yes
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import db, ingest, report
from .schema import ConfigError, load_dispositions, load_schemas

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMAS = REPO_ROOT / "assignment-a-data" / "schemas"


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=os.environ.get("LEDGER_DB", "ledger.db"),
                   help="SQLite file (default: ./ledger.db or $LEDGER_DB)")
    p.add_argument("--schemas", default=os.environ.get("LEDGER_SCHEMAS", str(DEFAULT_SCHEMAS)),
                   help="directory with workflow schemas + dispositions.csv")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m solution", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="ingest one or more drop directories, in the order given")
    _common(p)
    p.add_argument("drops", nargs="+")
    p.add_argument("--only", choices=["leads", "events"], help="ingest just one kind of file")
    p = sub.add_parser("report", help="derive and write the five report files")
    _common(p)
    p.add_argument("--out", required=True)
    p = sub.add_parser("reset", help="delete the database and its PII key file")
    _common(p)
    p.add_argument("--yes", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    if args.cmd == "reset":
        if not args.yes:
            print("refusing to reset without --yes", file=sys.stderr)
            return 2
        for suffix in ("", "-wal", "-shm", ".key"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        print(f"removed {db_path} and its key file")
        return 0
    try:
        schemas = load_schemas(Path(args.schemas))
        disp_rows, disp_sha = load_dispositions(Path(args.schemas) / "dispositions.csv")
        conn, crypto = db.connect(db_path)
        db.sync_dispositions(conn, disp_rows, disp_sha)
        t0 = time.time()
        if args.cmd == "ingest":
            kinds = (args.only,) if args.only else ("leads", "events")
            for d in args.drops:
                s = ingest.ingest_drop(conn, crypto, schemas, d, kinds, log=print)
                print(f"{s['drop']}: {s['files_ingested']} file(s) ingested, {s['files_skipped']} skipped")
        else:
            dq = report.build_reports(conn, args.out)
            print(f"wrote reports to {args.out}: {dq}")
        print(f"done in {time.time() - t0:.1f}s")
        return 0
    except (ConfigError, ingest.IngestConflict, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
