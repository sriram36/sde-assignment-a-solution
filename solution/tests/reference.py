"""Deliberately naive, in-memory re-implementation of the spec, used ONLY to cross-check the
SQL pipeline in tests. It shares the low-level parsers (tested separately) but none of the
ingest / linking / derivation logic."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from solution.ingest import build_column_map, validate_lead_row
from solution.parsing import normalise_phone, parse_timestamp_us, us_to_ist_iso

IST_OFF = 19800


def run(drops: list[Path], schemas: dict, dispositions: list) -> dict:
    disp = {(d.workflow_id, d.code): d for d in dispositions}
    leads: dict = {}          # (wf, phone) -> dict
    rejections, dq = [], defaultdict(int)
    reasons: dict = defaultdict(int)
    events: dict = {}
    for drop in drops:
        files = []
        for wf_dir in sorted((drop / "leads").iterdir()):
            files += [(f.name, wf_dir.name, f) for f in wf_dir.glob("*.csv")]
        for name, wf, f in sorted(files):
            schema = schemas[wf]
            with open(f, newline="", encoding="utf-8-sig") as fh:
                rd = csv.reader(fh)
                colmap = build_column_map(schema, next(rd))
                n = 0
                for row in rd:
                    if not any(c.strip() for c in row):
                        continue
                    n += 1
                    dq["lead_rows_total"] += 1
                    reason, parsed, warn = validate_lead_row(schema, colmap, row)
                    if reason:
                        dq["lead_rows_rejected"] += 1
                        reasons[reason] += 1
                        rejections.append((wf, f.stem, n, reason))
                        continue
                    dq["field_warnings"] += warn
                    key = (wf, parsed[schema.identity_field.name])
                    if key in leads:
                        dq["duplicate_lead_rows_merged"] += 1
                        for k, v in parsed.items():
                            if v is not None:
                                leads[key]["vals"][k] = v
                    else:
                        leads[key] = {"batch": f.stem, "vals": dict(parsed), "wf": wf,
                                      "ref_name": schema.reference_field.name}
        for ef in sorted((drop / "events").glob("*.jsonl")):
            for line in open(ef, "rb"):
                if not line.strip():
                    continue
                dq["events_total"] += 1
                try:
                    obj = json.loads(line)
                    eid = obj["event_id"]
                    if not eid:
                        raise KeyError
                except Exception:
                    dq["malformed_events"] += 1
                    continue
                if eid in events:
                    dq["duplicate_events_dropped"] += 1
                    continue
                events[eid] = obj

    # link
    link, call_lead = {}, {}
    for eid, e in events.items():
        if e["event_type"] == "disposition.override":
            continue
        p = normalise_phone(e.get("phone"))
        if p and (e["workflow_id"], p) in leads:
            link[eid] = (e["workflow_id"], p)
            call_lead[(e["workflow_id"], e["call_id"])] = (e["workflow_id"], p)
    for eid, e in events.items():
        if e["event_type"] == "disposition.override":
            k = call_lead.get((e["workflow_id"], e["call_id"]))
            if k:
                link[eid] = k
    dq["orphan_events"] = len(events) - len(link)
    dq["unknown_disposition_events"] = 0

    calls: dict = {}  # (lead_key, call_id) -> info
    for eid, lk in link.items():
        e = events[eid]
        t = e["event_type"]
        ts = parse_timestamp_us(e["occurred_at"])
        c = calls.setdefault((lk, e["call_id"]), {"init": None, "conn": False, "fail": False, "sets": [], "ovr": []})
        if t == "call.initiated":
            c["init"] = ts if c["init"] is None else min(c["init"], ts)
        elif t == "call.connected":
            c["conn"] = True
        elif t == "call.telephony_failed":
            c["fail"] = True
        elif t.startswith("disposition."):
            code = (e.get("payload") or {}).get("disposition_code")
            if (lk[0], code) not in disp:
                dq["unknown_disposition_events"] += 1
                continue
            (c["ovr"] if t == "disposition.override" else c["sets"]).append((ts, eid, code))
    by_lead = defaultdict(list)
    for (lk, cid), c in calls.items():
        if c["init"] is None:
            continue
        pool = c["ovr"] or c["sets"]
        c["code"] = max(pool)[2] if pool else None
        c["id"] = cid
        by_lead[lk].append(c)

    lead_rows, batches = [], defaultdict(lambda: defaultdict(int))
    for lk, L in leads.items():
        cs = by_lead.get(lk, [])
        if not cs:
            bucket = "yet_to_reach_out"
        elif any(c["code"] and disp[(lk[0], c["code"])].is_terminal for c in cs):
            bucket = "completed"
        elif all(c["fail"] for c in cs):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        have = [c for c in cs if c["code"]]
        cur = max(have, key=lambda c: (c["init"], c["id"]))["code"] if have else ""
        ever = any(c["code"] and disp[(lk[0], c["code"])].is_success for c in cs)
        lead_rows.append([lk[0], L["vals"][L["ref_name"]], L["batch"], "+91******" + lk[1][-4:], bucket,
                          len(cs), sum(c["conn"] for c in cs),
                          us_to_ist_iso(max(c["init"] for c in cs)) if cs else "", cur,
                          "true" if ever else "false"])
        batches[(lk[0], L["batch"])][bucket] += 1
    lead_rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    daily = defaultdict(lambda: [0, 0, set()])
    for lk, cs in by_lead.items():
        for c in cs:
            d = (lk[0], __import__("datetime").datetime.fromtimestamp(c["init"] // 10**6 + IST_OFF,
                 __import__("datetime").timezone.utc).strftime("%Y-%m-%d"))
            daily[d][0] += 1
            daily[d][1] += c["conn"]
            if c["code"] and disp[(lk[0], c["code"])].is_success:
                daily[d][2].add(lk)
    bs = [[wf, b, sum(v.values()), v["yet_to_reach_out"], v["in_progress_telephony_failed"],
           v["in_progress_dialed"], v["completed"]] for (wf, b), v in sorted(batches.items())]
    do = [[wf, d, t, c, f"{c / t:.4f}", len(s)] for (wf, d), (t, c, s) in sorted(daily.items())]
    dq["rejections_by_reason"] = dict(sorted(reasons.items()))
    return {"lead_report": lead_rows, "batch_summary": bs, "daily_overview": do,
            "rejections": sorted(rejections), "dq": dict(dq)}
