#!/usr/bin/env python3
"""Verification tests for the Parking Spot Rotation Calendar application."""

import datetime as dt
import os
from pathlib import Path

import server


def run_tests() -> None:
    if server.DB_PATH.exists():
        server.DB_PATH.unlink()
    server.init_db()
    server.sync_static_ics_files()

    state = server.build_schedule_state(dt.date(2026, 11, 10))
    meta = state["schedule_meta"]
    assert meta["start_date"] == "2026-11-09"
    assert meta["end_date"] == "2027-08-31"
    assert meta["total_days"] == 296
    assert meta["total_weeks"] == 43

    # Verify Week 1 (Nov 9-15, 2026) is Navya
    w1 = state["weeks"][0]
    assert w1["week_number"] == 1
    assert w1["start_date"] == "2026-11-09"
    assert w1["end_date"] == "2026-11-15"
    assert w1["baseline_owner"] == "Navya"
    assert w1["dominant_owner"] == "Navya"

    # Verify Week 2 (Nov 16-22, 2026) is Kulwinder
    w2 = state["weeks"][1]
    assert w2["week_number"] == 2
    assert w2["start_date"] == "2026-11-16"
    assert w2["end_date"] == "2026-11-22"
    assert w2["baseline_owner"] == "Kulwinder"
    assert w2["dominant_owner"] == "Kulwinder"

    # Verify final week (Week 43: Mon Aug 30 – Tue Aug 31, 2027) is Navya (week_idx=42, even)
    w43 = state["weeks"][-1]
    assert w43["week_number"] == 43
    assert w43["start_date"] == "2027-08-30"
    assert w43["end_date"] == "2027-08-31"
    assert w43["baseline_owner"] == "Navya"
    assert len(w43["days"]) == 2

    # Verify baseline counts: 22 Navya weeks (21*7 + 2 = 149 days), 21 Kulwinder weeks (21*7 = 147 days)
    fairness = state["fairness"]
    assert fairness["baseline_days"]["Navya"] == 149
    assert fairness["baseline_days"]["Kulwinder"] == 147
    assert fairness["owed_days"] == 0

    # Verify .ics files generated cleanly
    ics_all = server.ICS_EXPORT_ALL_PATH.read_text(encoding="utf-8")
    assert "BEGIN:VCALENDAR" in ics_all
    assert "SUMMARY:🚗 Parking: Navya (Shared Parking Spot)" in ics_all
    assert "SUMMARY:🚗 Parking: Kulwinder (Shared Parking Spot)" in ics_all
    assert "DTEND;VALUE=DATE:20270901" in ics_all  # Non-inclusive end date for Aug 31, 2027

    print("ALL VERIFICATION CHECKS PASSED!")


if __name__ == "__main__":
    run_tests()
