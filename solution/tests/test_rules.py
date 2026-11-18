"""Hand-built scenarios for the rules most likely to break."""
import json

import pytest

from .helpers import MR_HEADER, ev, ingest_all, lead_row, make_drop, open_db, run_reports


def build(tmp_path, leads, events, **kw):
    conn, crypto = open_db(tmp_path)
    d = make_drop(tmp_path, "d1", {"batch_001": [MR_HEADER, *leads]}, events, **kw)
    ingest_all(conn, crypto, [d])
    return run_reports(conn, tmp_path / "out")


def lead(res):
    return res["lead_report"][0]  # wf, ref, batch, masked, bucket, calls, conn, last, cur, ever


def test_bucket_no_calls(tmp_path):
    r = build(tmp_path, [lead_row()], [])
    assert lead(r)[4] == "yet_to_reach_out" and lead(r)[7] == "" and lead(r)[8] == ""


def test_bucket_precedence_completed_beats_all_failed(tmp_path):
    ev1 = [ev("call.initiated", "c1"), ev("call.telephony_failed", "c1", at="2026-10-05T10:00:05Z"),
           ev("call.initiated", "c2", at="2026-10-06T10:00:00Z"), ev("call.connected", "c2", at="2026-10-06T10:00:09Z"),
           ev("disposition.set", "c2", at="2026-10-06T10:05:00Z", payload={"disposition_code": "renewed"})]
    r = build(tmp_path, [lead_row()], ev1)
    assert lead(r)[4] == "completed" and lead(r)[9] == "true" and lead(r)[8] == "renewed"


def test_bucket_all_calls_telephony_failed(tmp_path):
    events = [ev("call.initiated", "c1"), ev("call.telephony_failed", "c1", at="2026-10-05T10:00:05Z"),
              ev("call.initiated", "c2", at="2026-10-06T10:00:00Z"),
              ev("call.telephony_failed", "c2", at="2026-10-06T10:00:03Z")]
    assert lead(build(tmp_path, [lead_row()], events))[4] == "in_progress_telephony_failed"


def test_bucket_one_failed_one_not_is_dialed(tmp_path):
    events = [ev("call.initiated", "c1"), ev("call.telephony_failed", "c1", at="2026-10-05T10:00:05Z"),
              ev("call.initiated", "c2", at="2026-10-06T10:00:00Z")]
    assert lead(build(tmp_path, [lead_row()], events))[4] == "in_progress_dialed"


def test_non_terminal_disposition_stays_in_progress(tmp_path):
    events = [ev("call.initiated", "c1"), ev("call.connected", "c1"),
              ev("disposition.set", "c1", payload={"disposition_code": "callback_requested"})]
    r = build(tmp_path, [lead_row()], events)
    assert lead(r)[4] == "in_progress_dialed" and lead(r)[8] == "callback_requested"


def test_override_beats_later_set_and_ledger_keeps_both(tmp_path):
    events = [ev("call.initiated", "c1"),
              ev("disposition.set", "c1", at="2026-10-05T10:10:00Z", payload={"disposition_code": "not_interested"}),
              ev("disposition.override", "c1", phone=None, at="2026-10-05T11:00:00Z",
                 payload={"disposition_code": "renewed", "overridden_by": "s1", "reason": "qa"}),
              ev("disposition.set", "c1", at="2026-10-05T12:00:00Z", payload={"disposition_code": "dnd"})]
    conn, crypto = open_db(tmp_path)
    d = make_drop(tmp_path, "d1", {"batch_001": [MR_HEADER, lead_row()]}, events)
    ingest_all(conn, crypto, [d])
    r = run_reports(conn, tmp_path / "out")
    assert lead(r)[8] == "renewed" and lead(r)[9] == "true"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4  # nothing deleted/edited


def test_latest_of_two_overrides_wins(tmp_path):
    ov = lambda at, code: ev("disposition.override", "c1", phone=None, at=at,
                             payload={"disposition_code": code, "overridden_by": "s", "reason": "r"})
    events = [ev("call.initiated", "c1"), ov("2026-10-05T12:00:00Z", "dnd"), ov("2026-10-05T11:00:00Z", "renewed")]
    assert lead(build(tmp_path, [lead_row()], events))[8] == "dnd"


def test_unknown_override_is_ignored_and_original_stands(tmp_path):
    events = [ev("call.initiated", "c1"),
              ev("disposition.set", "c1", payload={"disposition_code": "renewed"}),
              ev("disposition.override", "c1", phone=None, at="2026-10-06T00:00:00Z",
                 payload={"disposition_code": "BOGUS", "overridden_by": "s", "reason": "r"})]
    r = build(tmp_path, [lead_row()], events)
    assert lead(r)[8] == "renewed" and r["dq"]["unknown_disposition_events"] == 1


def test_disposition_code_is_per_workflow(tmp_path):
    # 'feedback_captured' exists only for policy_feedback, so it is unknown for motor_renewal
    events = [ev("call.initiated", "c1"), ev("disposition.set", "c1", payload={"disposition_code": "feedback_captured"})]
    r = build(tmp_path, [lead_row()], events)
    assert r["dq"]["unknown_disposition_events"] == 1 and lead(r)[8] == ""


def test_call_without_initiated_is_not_counted(tmp_path):
    events = [ev("call.connected", "c1"), ev("disposition.set", "c1", payload={"disposition_code": "renewed"})]
    r = build(tmp_path, [lead_row()], events)
    assert lead(r)[4] == "yet_to_reach_out" and r["daily_overview"] == [] and lead(r)[9] == "false"


def test_current_disposition_is_most_recent_call_that_has_one(tmp_path):
    events = [ev("call.initiated", "c1", at="2026-10-05T10:00:00Z"),
              ev("disposition.set", "c1", payload={"disposition_code": "callback_requested"}),
              ev("call.initiated", "c2", at="2026-10-06T10:00:00Z")]  # newer call, no disposition
    r = build(tmp_path, [lead_row()], events)
    assert lead(r)[8] == "callback_requested" and lead(r)[5] == "2"


def test_ist_date_boundary_and_connectivity(tmp_path):
    events = [ev("call.initiated", "c1", at="2026-10-05T18:29:59Z"),      # 23:59:59 IST on the 5th
              ev("call.initiated", "c2", at="2026-10-05T18:30:00Z"),      # 00:00:00 IST on the 6th
              ev("call.connected", "c2", at="2026-10-05T18:30:10Z"),
              ev("call.initiated", "c3", at="2026-10-06 10:00:00")]       # naive => IST, 6th
    r = build(tmp_path, [lead_row()], events)
    assert r["daily_overview"] == [["motor_renewal", "2026-10-05", "1", "0", "0.0000", "0"],
                                   ["motor_renewal", "2026-10-06", "2", "1", "0.5000", "0"]]
    assert lead(r)[7] == "2026-10-06T10:00:00+05:30"


def test_success_leads_counts_distinct_leads_per_day(tmp_path):
    ren = {"disposition_code": "renewed"}
    events = [ev("call.initiated", "c1"), ev("disposition.set", "c1", payload=ren),
              ev("call.initiated", "c2", at="2026-10-05T11:00:00Z"), ev("disposition.set", "c2", at="2026-10-05T11:05:00Z", payload=ren)]
    assert build(tmp_path, [lead_row()], events)["daily_overview"][0][-1] == "1"


def test_event_phone_formats_link_to_lead_and_others_orphan(tmp_path):
    events = [ev("call.initiated", "c1", phone="+91 98765 43210"), ev("call.initiated", "c2", phone="N/A"),
              ev("call.initiated", "c3", phone="9000000000"), ev("call.initiated", "c4", phone="9876543210", wf="policy_feedback")]
    r = build(tmp_path, [lead_row()], events)
    assert lead(r)[5] == "1" and r["dq"]["orphan_events"] == 3


def test_override_with_unlinkable_call_is_orphan(tmp_path):
    events = [ev("disposition.override", "nope", phone=None, payload={"disposition_code": "renewed"})]
    assert build(tmp_path, [lead_row()], events)["dq"]["orphan_events"] == 1


def test_orphan_event_counts_once_lead_arrives_in_later_drop(tmp_path):
    conn, crypto = open_db(tmp_path)
    d1 = make_drop(tmp_path, "d1", {"batch_001": [MR_HEADER, lead_row("X Y", "9111111111", "MOT/9")]},
                   [ev("call.initiated", "c1", phone="+91 98765 43210")])
    d2 = make_drop(tmp_path, "d2", {"batch_002": [MR_HEADER, lead_row()]}, [])
    ingest_all(conn, crypto, [d1])
    assert run_reports(conn, tmp_path / "o1")["dq"]["orphan_events"] == 1
    ingest_all(conn, crypto, [d2])
    r = run_reports(conn, tmp_path / "o2")
    assert r["dq"]["orphan_events"] == 0 and r["daily_overview"][0][2] == "1"


def test_event_line_accounting(tmp_path):
    good = ev("call.initiated", "c1", eid="evt_A")
    lines = ['{"event_id": "evt_A", "event_t', "not json", "[1,2]", '{"no_id": 1}', json.dumps(good), "   "]
    conn, crypto = open_db(tmp_path)
    d = make_drop(tmp_path, "d1", {"batch_001": [MR_HEADER, lead_row()]}, [good], raw_event_lines=lines)
    ingest_all(conn, crypto, [d])
    dq = run_reports(conn, tmp_path / "o")["dq"]
    assert dq["events_total"] == 6 and dq["malformed_events"] == 4 and dq["duplicate_events_dropped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_orphan_precedes_unknown_disposition_counting(tmp_path):
    events = [ev("disposition.set", "c1", phone="9000000001", payload={"disposition_code": "BOGUS"})]
    dq = build(tmp_path, [lead_row()], events)["dq"]
    assert dq["orphan_events"] == 1 and dq["unknown_disposition_events"] == 0


# ------------------------------------------------------------------ lead rules
def test_rejection_reason_is_first_failure_in_schema_order(tmp_path):
    rows = [lead_row(name="", phone="bad", ref=""),        # name is first in schema -> missing name
            lead_row(name="N", phone="bad", ref=""),       # phone before policy_number -> invalid_phone
            lead_row(name="N", phone="", ref="R"),         # missing phone
            lead_row(name="N", phone="9876543210", ref=""),
            ["   ", "", "", "", "", ""],                   # blank row: skipped, not counted
            lead_row(name="Ok", phone="9876543210", ref="R2")]
    r = build(tmp_path, rows, [])
    assert [x[2:] for x in r["rejections"]] == [["1", "missing_required:customer_name"], ["2", "invalid_phone"],
                                                 ["3", "missing_required:phone"], ["4", "missing_required:policy_number"]]
    assert r["dq"]["lead_rows_total"] == 5 and r["dq"]["lead_rows_rejected"] == 4
    assert len(r["lead_report"]) == 1


def test_row_numbers_skip_blank_rows(tmp_path):
    rows = [lead_row("A", "9876543210", "R1"), ["", "", "", "", "", ""],
            lead_row("", "9876543211", "R2")]
    r = build(tmp_path, rows, [])
    assert r["rejections"] == [["motor_renewal", "batch_001", "2", "missing_required:customer_name"]]


def test_field_warnings_only_for_non_rejected_rows(tmp_path):
    rows = [lead_row(expiry="soon", premium="N/A", lang="Hindi"),   # 3 warnings
            lead_row(name="", phone="9876543211", ref="R2", expiry="soon")]  # rejected: no warning counted
    dq = build(tmp_path, rows, [])["dq"]
    assert dq["field_warnings"] == 3 and dq["lead_rows_rejected"] == 1


def test_duplicate_merge_non_null_wins_first_batch_kept_defaults_last(tmp_path):
    conn, crypto = open_db(tmp_path)
    d1 = make_drop(tmp_path, "d1", {"batch_001": [MR_HEADER, lead_row("First", "09876543210", "MOT/1", "", "1000", "")]})
    d2 = make_drop(tmp_path, "d2", {"batch_002": [MR_HEADER, lead_row("Second", "+91 98765 43210", "MOT/1B", "05/10/2026", "", "TA")]})
    ingest_all(conn, crypto, [d1, d2])
    r = run_reports(conn, tmp_path / "o")
    assert len(r["lead_report"]) == 1 and r["lead_report"][0][1:3] == ["MOT/1B", "batch_001"]
    assert r["dq"]["duplicate_lead_rows_merged"] == 1
    fields = json.loads(conn.execute("SELECT fields_json FROM leads").fetchone()[0])
    assert fields["premium_amount"] == "1000.00" and fields["policy_expiry"] == "2026-10-05"
    assert fields["preferred_language"] == "ta"
    pii = crypto.decrypt_json(conn.execute("SELECT pii_enc FROM leads").fetchone()[0])
    assert pii == {"customer_name": "Second", "phone": "+919876543210"}


def test_default_language_applies_when_still_null(tmp_path):
    conn, crypto = open_db(tmp_path)
    ingest_all(conn, crypto, [make_drop(tmp_path, "d1", {"batch_001": [MR_HEADER, lead_row(lang="klingon")]})])
    assert json.loads(conn.execute("SELECT fields_json FROM leads").fetchone()[0])["preferred_language"] == "en"


def test_renamed_reordered_extra_columns(tmp_path):
    header = ["Language", "Insured Name", "Policy No", "Mobile No", "Expiry Date", "Premium", "agent_notes"]
    rows = [header, ["HI", "A B", "MOT/5", "(+91) 9876543210", "01-Dec-2026", "₹1,500", "VIP"]]
    conn, crypto = open_db(tmp_path)
    ingest_all(conn, crypto, [make_drop(tmp_path, "d1", {"batch_003": rows})])
    f = json.loads(conn.execute("SELECT fields_json FROM leads").fetchone()[0])
    assert f["preferred_language"] == "hi" and f["premium_amount"] == "1500.00" and f["policy_expiry"] == "2026-12-01"
    assert "agent_notes" not in f
