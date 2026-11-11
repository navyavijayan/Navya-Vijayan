#!/usr/bin/env python3
"""
Real-Time Parking Spot Rotation Calendar Server
Schedule Range: Monday, Nov 9, 2026 – Tuesday, Aug 31, 2027
Participants: Navya & Kulwinder (alternating weekly, Monday–Sunday)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import sqlite3
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "parking_schedule.db"
STATIC_DIR = APP_DIR / "static"
ICS_EXPORT_ALL_PATH = APP_DIR / "parking_rotation_all_2026_2027.ics"
ICS_EXPORT_NAVYA_PATH = APP_DIR / "parking_rotation_navya_2026_2027.ics"

SCHEDULE_START = dt.date(2026, 11, 9)   # Monday of Week 1 (Navya)
SCHEDULE_END = dt.date(2027, 8, 31)     # Tuesday of Week 43
PARTICIPANTS = ("Navya", "Kulwinder")

# Connected SSE client queues
_sse_lock = threading.Lock()
_sse_clients: list[queue.Queue[str]] = []


def broadcast_sse(event_type: str, payload: dict[str, Any]) -> None:
    """Broadcast a real-time Server-Sent Event to all connected browser clients."""
    msg = f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
    with _sse_lock:
        stale: list[queue.Queue[str]] = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                stale.append(q)
        for q in stale:
            if q in _sse_clients:
                _sse_clients.remove(q)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS day_overrides (
                date_iso TEXT PRIMARY KEY,
                override_owner TEXT NOT NULL,
                note TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS swap_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        defaults = {
            "spot_label": "Shared Parking Spot",
            "user_a": "Navya",
            "user_b": "Kulwinder",
            "handoff_day": "Monday 12:00 AM",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, v),
            )
        conn.commit()


def baseline_owner_for_date(d: dt.date) -> str:
    """Return baseline owner ('Navya' for even weeks from 2026-11-09, 'Kulwinder' for odd)."""
    week_idx = (d - SCHEDULE_START).days // 7
    return PARTICIPANTS[week_idx % 2]


def week_number_for_date(d: dt.date) -> int:
    """1-based rotation week number starting Nov 9, 2026."""
    return ((d - SCHEDULE_START).days // 7) + 1


def week_bounds_for_date(d: dt.date) -> tuple[dt.date, dt.date]:
    """Return (Monday, Sunday) clipped to [SCHEDULE_START, SCHEDULE_END]."""
    week_idx = (d - SCHEDULE_START).days // 7
    mon = SCHEDULE_START + dt.timedelta(days=week_idx * 7)
    sun = min(mon + dt.timedelta(days=6), SCHEDULE_END)
    return mon, sun


def build_schedule_state(ref_date: dt.date | None = None) -> dict[str, Any]:
    """Compute the full schedule, live status, fairness balance, and history."""
    if ref_date is None:
        sys_today = dt.date.today()
        ref_date = sys_today if SCHEDULE_START <= sys_today <= SCHEDULE_END else dt.date(2026, 11, 10)

    with get_db() as conn:
        settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
        settings = {r["key"]: r["value"] for r in settings_rows}

        override_rows = conn.execute(
            "SELECT date_iso, override_owner, note, updated_at FROM day_overrides"
        ).fetchall()
        overrides = {
            r["date_iso"]: {
                "override_owner": r["override_owner"],
                "note": r["note"],
                "updated_at": r["updated_at"],
            }
            for r in override_rows
        }

        history_rows = conn.execute(
            "SELECT id, action_type, summary, details_json, created_at "
            "FROM swap_history ORDER BY id DESC LIMIT 50"
        ).fetchall()
        history = [
            {
                "id": r["id"],
                "action_type": r["action_type"],
                "summary": r["summary"],
                "details": json.loads(r["details_json"]),
                "created_at": r["created_at"],
            }
            for r in history_rows
        ]

    days: list[dict[str, Any]] = []
    weeks_map: dict[int, dict[str, Any]] = {}

    baseline_counts = {"Navya": 0, "Kulwinder": 0}
    effective_counts = {"Navya": 0, "Kulwinder": 0}
    weekday_baseline = {"Navya": 0, "Kulwinder": 0}
    weekday_effective = {"Navya": 0, "Kulwinder": 0}
    navya_took_from_kulwinder = 0
    kulwinder_took_from_navya = 0

    cur = SCHEDULE_START
    while cur <= SCHEDULE_END:
        iso = cur.isoformat()
        w_num = week_number_for_date(cur)
        base_owner = baseline_owner_for_date(cur)
        ov = overrides.get(iso)
        eff_owner = ov["override_owner"] if ov else base_owner
        is_overridden = eff_owner != base_owner
        note = ov["note"] if ov else ""

        baseline_counts[base_owner] += 1
        effective_counts[eff_owner] += 1
        if cur.weekday() < 5:
            weekday_baseline[base_owner] += 1
            weekday_effective[eff_owner] += 1

        if is_overridden:
            if base_owner == "Kulwinder" and eff_owner == "Navya":
                navya_took_from_kulwinder += 1
            elif base_owner == "Navya" and eff_owner == "Kulwinder":
                kulwinder_took_from_navya += 1

        day_entry = {
            "date": iso,
            "Year": cur.year,
            "month": cur.month,
            "day": cur.day,
            "weekday": cur.weekday(),  # 0=Mon .. 6=Sun
            "weekday_short": cur.strftime("%a"),
            "week_number": w_num,
            "baseline_owner": base_owner,
            "effective_owner": eff_owner,
            "is_overridden": is_overridden,
            "has_note": bool(note),
            "note": note,
            "updated_at": ov["updated_at"] if ov else None,
        }
        days.append(day_entry)

        if w_num not in weeks_map:
            mon, sun = week_bounds_for_date(cur)
            weeks_map[w_num] = {
                "week_number": w_num,
                "start_date": mon.isoformat(),
                "end_date": sun.isoformat(),
                "label": f"{mon.strftime('%b %d, %Y')} – {sun.strftime('%b %d, %Y')}",
                "baseline_owner": base_owner,
                "days": [],
                "override_count": 0,
            }
        weeks_map[w_num]["days"].append(day_entry)
        if is_overridden:
            weeks_map[w_num]["override_count"] += 1

        cur += dt.timedelta(days=1)

    for w in weeks_map.values():
        nav_days = sum(1 for d in w["days"] if d["effective_owner"] == "Navya")
        kul_days = sum(1 for d in w["days"] if d["effective_owner"] == "Kulwinder")
        w["navya_days"] = nav_days
        w["kulwinder_days"] = kul_days
        if nav_days == len(w["days"]):
            w["dominant_owner"] = "Navya"
        elif kul_days == len(w["days"]):
            w["dominant_owner"] = "Kulwinder"
        else:
            w["dominant_owner"] = "Split"

    # Fairness / Swap balance calculation
    # Positive net_delta_for_navya means Kulwinder has used more of Navya's baseline days than vice versa,
    # so Kulwinder owes Navya days.
    net_owed_to_navya = kulwinder_took_from_navya - navya_took_from_kulwinder
    if net_owed_to_navya == 0:
        balance_status = "All swaps are evenly balanced (0 days owed)"
        owed_by = None
        owed_to = None
        owed_days = 0
    elif net_owed_to_navya > 0:
        owed_by = "Kulwinder"
        owed_to = "Navya"
        owed_days = net_owed_to_navya
        balance_status = f"Kulwinder owes Navya {owed_days} day{'s' if owed_days != 1 else ''}"
    else:
        owed_by = "Navya"
        owed_to = "Kulwinder"
        owed_days = abs(net_owed_to_navya)
        balance_status = f"Navya owes Kulwinder {owed_days} day{'s' if owed_days != 1 else ''}"

    # Determine current/active status for ref_date
    clamped_date = min(max(ref_date, SCHEDULE_START), SCHEDULE_END)
    days_by_iso = {d["date"]: d for d in days}
    today_entry = days_by_iso[clamped_date.isoformat()]
    current_week = weeks_map[today_entry["week_number"]]
    next_week = weeks_map.get(today_entry["week_number"] + 1)

    # Next handoff date (first future day where effective_owner != today_entry['effective_owner'])
    next_handoff_day = None
    for d in days:
        if d["date"] > clamped_date.isoformat() and d["effective_owner"] != today_entry["effective_owner"]:
            next_handoff_day = d
            break

    total_days = len(days)
    elapsed_days = max(0, min(total_days, (ref_date - SCHEDULE_START).days + 1))

    return {
        "schedule_meta": {
            "start_date": SCHEDULE_START.isoformat(),
            "end_date": SCHEDULE_END.isoformat(),
            "total_days": total_days,
            "total_weeks": len(weeks_map),
            "elapsed_days": elapsed_days,
            "remaining_days": max(0, total_days - elapsed_days),
            "participants": list(PARTICIPANTS),
            "settings": settings,
            "server_timestamp": dt.datetime.now().isoformat(),
        },
        "live_status": {
            "reference_date": ref_date.isoformat(),
            "in_schedule_window": SCHEDULE_START <= ref_date <= SCHEDULE_END,
            "today": today_entry,
            "current_week": current_week,
            "next_week": next_week,
            "next_handoff": next_handoff_day,
        },
        "fairness": {
            "baseline_days": baseline_counts,
            "effective_days": effective_counts,
            "weekday_baseline_days": weekday_baseline,
            "weekday_effective_days": weekday_effective,
            "navya_took_from_kulwinder": navya_took_from_kulwinder,
            "kulwinder_took_from_navya": kulwinder_took_from_navya,
            "total_overridden_days": navya_took_from_kulwinder + kulwinder_took_from_navya,
            "owed_by": owed_by,
            "owed_to": owed_to,
            "owed_days": owed_days,
            "balance_status": balance_status,
        },
        "weeks": list(weeks_map.values()),
        "days": days,
        "history": history,
    }


def generate_ics_content(owner_filter: str = "all") -> str:
    """
    Generate an RFC 5545 iCalendar (.ics) file covering 2026-11-09 through 2027-08-31.
    Consecutive days with the same effective owner and override state are grouped into
    clean all-day events so the calendar stays readable while accurately reflecting swaps.
    """
    state = build_schedule_state()
    days = state["days"]
    spot_label = state["schedule_meta"]["settings"].get("spot_label", "Shared Parking Spot")

    filtered_days = [
        d for d in days
        if owner_filter.lower() == "all" or d["effective_owner"].lower() == owner_filter.lower()
    ]

    # Group contiguous days with same effective_owner, week_number, and is_overridden
    spans: list[dict[str, Any]] = []
    for d in filtered_days:
        d_date = dt.date.fromisoformat(d["date"])
        if (
            spans
            and spans[-1]["owner"] == d["effective_owner"]
            and spans[-1]["is_overridden"] == d["is_overridden"]
            and spans[-1]["note"] == d["note"]
            and (d_date - spans[-1]["end_date"]).days == 1
        ):
            spans[-1]["end_date"] = d_date
            spans[-1]["day_count"] += 1
        else:
            spans.append(
                {
                    "start_date": d_date,
                    "end_date": d_date,
                    "owner": d["effective_owner"],
                    "baseline_owner": d["baseline_owner"],
                    "week_number": d["week_number"],
                    "is_overridden": d["is_overridden"],
                    "note": d["note"],
                    "day_count": 1,
                }
            )

    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cal_title = (
        f"Parking Rotation ({spot_label})"
        if owner_filter.lower() == "all"
        else f"Parking Spot - {owner_filter.title()}"
    )

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Navya & Kulwinder//Parking Spot Rotation Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{cal_title}",
        "X-WR-TIMEZONE:America/Los_Angeles",
    ]

    for idx, sp in enumerate(spans, start=1):
        dtstart = sp["start_date"].strftime("%Y%m%d")
        # In RFC 5545, DTEND for VALUE=DATE is non-inclusive (+1 day)
        dtend = (sp["end_date"] + dt.timedelta(days=1)).strftime("%Y%m%d")
        swap_tag = " [SWAPPED]" if sp["is_overridden"] else ""
        summary = f"🚗 Parking: {sp['owner']}{swap_tag} ({spot_label})"
        desc_parts = [
            f"Assigned Holder: {sp['owner']}",
            f"Rotation Week #{sp['week_number']}",
            f"Span: {sp['start_date'].isoformat()} to {sp['end_date'].isoformat()} ({sp['day_count']} day(s))",
        ]
        if sp["is_overridden"]:
            desc_parts.append(f"Override: Swapped from baseline holder ({sp['baseline_owner']})")
        if sp["note"]:
            desc_parts.append(f"Note: {sp['note']}")
        description = "\\n".join(desc_parts)
        uid = f"parking-rot-2026-2027-{dtstart}-{sp['owner'].lower()}-{idx}@parking.local"

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now_utc}",
                f"DTSTART;VALUE=DATE:{dtstart}",
                f"DTEND;VALUE=DATE:{dtend}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{description}",
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def sync_static_ics_files() -> None:
    """Keep static .ics files on disk up to date whenever changes occur."""
    ICS_EXPORT_ALL_PATH.write_text(generate_ics_content("all"), encoding="utf-8")
    ICS_EXPORT_NAVYA_PATH.write_text(generate_ics_content("Navya"), encoding="utf-8")


def validate_date_in_range(date_iso: str) -> dt.date:
    d = dt.date.fromisoformat(date_iso)
    if not (SCHEDULE_START <= d <= SCHEDULE_END):
        raise ValueError(
            f"Date {date_iso} is outside schedule range ({SCHEDULE_START} to {SCHEDULE_END})."
        )
    return d


class ParkingRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Clean concise logging
        pass

    def _send_json(self, data: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/schedule":
            ref_str = query.get("ref_date", [None])[0]
            ref_date = dt.date.fromisoformat(ref_str) if ref_str else None
            state = build_schedule_state(ref_date)
            self._send_json(state)
            return

        if path == "/api/calendar.ics":
            owner_filter = query.get("owner", ["all"])[0]
            ics_text = generate_ics_content(owner_filter)
            ics_bytes = ics_text.encode("utf-8")
            filename = (
                "parking_rotation_all_2026_2027.ics"
                if owner_filter.lower() == "all"
                else f"parking_rotation_{owner_filter.lower()}_2026_2027.ics"
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            self.send_header("Content-Length", str(len(ics_bytes)))
            self.end_headers()
            self.wfile.write(ics_bytes)
            return

        if path == "/api/stream":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            client_q: queue.Queue[str] = queue.Queue(maxsize=32)
            with _sse_lock:
                _sse_clients.append(client_q)

            try:
                # Send initial connected handshake
                init_msg = (
                    f"event: connected\ndata: "
                    f"{json.dumps({'ts': dt.datetime.now().isoformat()})}\n\n"
                )
                self.wfile.write(init_msg.encode("utf-8"))
                self.wfile.flush()

                while True:
                    try:
                        msg = client_q.get(timeout=15.0)
                        self.wfile.write(msg.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        heartbeat = f": heartbeat {int(time.time())}\n\n"
                        self.wfile.write(heartbeat.encode("utf-8"))
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with _sse_lock:
                    if client_q in _sse_clients:
                        _sse_clients.remove(client_q)
            return

        # Serve static frontend files
        if path in ("/", "/index.html"):
            index_file = STATIC_DIR / "index.html"
            content = index_file.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json({"error": f"Invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return

        now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            if path == "/api/override-day":
                date_iso = str(payload.get("date", "")).strip()
                d = validate_date_in_range(date_iso)
                base_owner = baseline_owner_for_date(d)
                requested_owner = payload.get("owner")
                note = str(payload.get("note", "")).strip()

                with get_db() as conn:
                    existing = conn.execute(
                        "SELECT override_owner, note FROM day_overrides WHERE date_iso = ?",
                        (date_iso,),
                    ).fetchone()
                    current_eff = existing["override_owner"] if existing else base_owner

                    if requested_owner == "toggle" or not requested_owner:
                        new_owner = "Kulwinder" if current_eff == "Navya" else "Navya"
                    elif requested_owner == "reset":
                        new_owner = base_owner
                    elif requested_owner in PARTICIPANTS:
                        new_owner = requested_owner
                    else:
                        raise ValueError(f"Unsupported owner value: {requested_owner}")

                    if new_owner == base_owner and not note:
                        conn.execute(
                            "DELETE FROM day_overrides WHERE date_iso = ?", (date_iso,)
                        )
                        summary = f"Reset {date_iso} ({d.strftime('%a, %b %d, %Y')}) to baseline ({base_owner})"
                        action_type = "reset_day"
                    else:
                        conn.execute(
                            """
                            INSERT INTO day_overrides (date_iso, override_owner, note, updated_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(date_iso) DO UPDATE SET
                                override_owner = excluded.override_owner,
                                note = excluded.note,
                                updated_at = excluded.updated_at
                            """,
                            (date_iso, new_owner, note, now_iso),
                        )
                        if new_owner != base_owner:
                            summary = (
                                f"Assigned {d.strftime('%a, %b %d, %Y')} to {new_owner} "
                                f"(baseline: {base_owner})"
                            )
                            action_type = "override_day"
                        else:
                            summary = f"Added note on {d.strftime('%a, %b %d, %Y')} ({new_owner})"
                            action_type = "note_day"
                        if note:
                            summary += f" — “{note}”"

                    conn.execute(
                        """
                        INSERT INTO swap_history (action_type, summary, details_json, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            action_type,
                            summary,
                            json.dumps(
                                {
                                    "date": date_iso,
                                    "baseline_owner": base_owner,
                                    "new_owner": new_owner,
                                    "note": note,
                                }
                            ),
                            now_iso,
                        ),
                    )
                    conn.commit()

                sync_static_ics_files()
                state = build_schedule_state()
                broadcast_sse("schedule_updated", {"summary": summary, "ts": now_iso})
                self._send_json({"ok": True, "summary": summary, "state": state})
                return

            if path == "/api/swap-week":
                week_number = int(payload.get("week_number", 0))
                mode = str(payload.get("mode", "swap")).strip()  # "swap" or "reset"
                note = str(payload.get("note", "")).strip()

                if not (1 <= week_number <= 43):
                    raise ValueError("Week number must be between 1 and 43.")

                mon = SCHEDULE_START + dt.timedelta(days=(week_number - 1) * 7)
                sun = min(mon + dt.timedelta(days=6), SCHEDULE_END)
                base_owner = baseline_owner_for_date(mon)
                other_owner = "Kulwinder" if base_owner == "Navya" else "Navya"

                with get_db() as conn:
                    cur = mon
                    affected_dates: list[str] = []
                    while cur <= sun:
                        iso = cur.isoformat()
                        affected_dates.append(iso)
                        if mode == "reset":
                            conn.execute(
                                "DELETE FROM day_overrides WHERE date_iso = ?", (iso,)
                            )
                        else:
                            target_owner = (
                                payload.get("target_owner")
                                if payload.get("target_owner") in PARTICIPANTS
                                else other_owner
                            )
                            if target_owner == base_owner and not note:
                                conn.execute(
                                    "DELETE FROM day_overrides WHERE date_iso = ?", (iso,)
                                )
                            else:
                                conn.execute(
                                    """
                                    INSERT INTO day_overrides (date_iso, override_owner, note, updated_at)
                                    VALUES (?, ?, ?, ?)
                                    ON CONFLICT(date_iso) DO UPDATE SET
                                        override_owner = excluded.override_owner,
                                        note = excluded.note,
                                        updated_at = excluded.updated_at
                                    """,
                                    (
                                        iso,
                                        target_owner,
                                        note or f"Week #{week_number} override",
                                        now_iso,
                                    ),
                                )
                        cur += dt.timedelta(days=1)

                    if mode == "reset":
                        summary = (
                            f"Reset Week #{week_number} ({mon.strftime('%b %d')} – "
                            f"{sun.strftime('%b %d, %Y')}) to baseline ({base_owner})"
                        )
                        action_type = "reset_week"
                    else:
                        target_owner = (
                            payload.get("target_owner")
                            if payload.get("target_owner") in PARTICIPANTS
                            else other_owner
                        )
                        summary = (
                            f"Assigned Week #{week_number} ({mon.strftime('%b %d')} – "
                            f"{sun.strftime('%b %d, %Y')}) to {target_owner}"
                        )
                        if note:
                            summary += f" — “{note}”"
                        action_type = "swap_week"

                    conn.execute(
                        """
                        INSERT INTO swap_history (action_type, summary, details_json, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            action_type,
                            summary,
                            json.dumps(
                                {
                                    "week_number": week_number,
                                    "dates": affected_dates,
                                    "mode": mode,
                                    "note": note,
                                }
                            ),
                            now_iso,
                        ),
                    )
                    conn.commit()

                sync_static_ics_files()
                state = build_schedule_state()
                broadcast_sse("schedule_updated", {"summary": summary, "ts": now_iso})
                self._send_json({"ok": True, "summary": summary, "state": state})
                return

            if path == "/api/trade-days":
                # 1-for-1 trade between two dates (or two weeks) so balance stays 0!
                date_a_iso = str(payload.get("date_a", "")).strip()
                date_b_iso = str(payload.get("date_b", "")).strip()
                note = str(payload.get("note", "")).strip()

                d_a = validate_date_in_range(date_a_iso)
                d_b = validate_date_in_range(date_b_iso)
                if d_a == d_b:
                    raise ValueError("Please select two different dates to trade.")

                with get_db() as conn:
                    row_a = conn.execute(
                        "SELECT override_owner FROM day_overrides WHERE date_iso = ?",
                        (date_a_iso,),
                    ).fetchone()
                    row_b = conn.execute(
                        "SELECT override_owner FROM day_overrides WHERE date_iso = ?",
                        (date_b_iso,),
                    ).fetchone()

                    eff_a = row_a["override_owner"] if row_a else baseline_owner_for_date(d_a)
                    eff_b = row_b["override_owner"] if row_b else baseline_owner_for_date(d_b)

                    new_a = eff_b if eff_a != eff_b else ("Kulwinder" if eff_a == "Navya" else "Navya")
                    new_b = eff_a if eff_a != eff_b else ("Navya" if new_a == "Kulwinder" else "Kulwinder")

                    trade_note = note or f"1:1 Trade ({d_a.strftime('%b %d')} ↔ {d_b.strftime('%b %d')})"

                    for d_obj, iso_str, target_owner in (
                        (d_a, date_a_iso, new_a),
                        (d_b, date_b_iso, new_b),
                    ):
                        conn.execute(
                            """
                            INSERT INTO day_overrides (date_iso, override_owner, note, updated_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(date_iso) DO UPDATE SET
                                override_owner = excluded.override_owner,
                                note = excluded.note,
                                updated_at = excluded.updated_at
                            """,
                            (iso_str, target_owner, trade_note, now_iso),
                        )

                    summary = (
                        f"Traded {d_a.strftime('%a %b %d')} (→ {new_a}) ↔ "
                        f"{d_b.strftime('%a %b %d')} (→ {new_b})"
                    )
                    if note:
                        summary += f" — “{note}”"

                    conn.execute(
                        """
                        INSERT INTO swap_history (action_type, summary, details_json, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            "trade_days",
                            summary,
                            json.dumps(
                                {
                                    "date_a": date_a_iso,
                                    "new_owner_a": new_a,
                                    "date_b": date_b_iso,
                                    "new_owner_b": new_b,
                                    "note": trade_note,
                                }
                            ),
                            now_iso,
                        ),
                    )
                    conn.commit()

                sync_static_ics_files()
                state = build_schedule_state()
                broadcast_sse("schedule_updated", {"summary": summary, "ts": now_iso})
                self._send_json({"ok": True, "summary": summary, "state": state})
                return

            if path == "/api/settings":
                spot_label = str(payload.get("spot_label", "")).strip()
                if not spot_label:
                    raise ValueError("Spot label cannot be empty.")
                with get_db() as conn:
                    conn.execute(
                        """
                        INSERT INTO settings (key, value) VALUES ('spot_label', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (spot_label,),
                    )
                    conn.commit()
                sync_static_ics_files()
                state = build_schedule_state()
                broadcast_sse("schedule_updated", {"summary": f"Updated spot label to {spot_label}", "ts": now_iso})
                self._send_json({"ok": True, "state": state})
                return

            if path == "/api/reset-all":
                with get_db() as conn:
                    conn.execute("DELETE FROM day_overrides")
                    conn.execute(
                        """
                        INSERT INTO swap_history (action_type, summary, details_json, created_at)
                        VALUES ('reset_all', 'Reset entire schedule to baseline weekly rotation', '{}', ?)
                        """,
                        (now_iso,),
                    )
                    conn.commit()
                sync_static_ics_files()
                state = build_schedule_state()
                broadcast_sse("schedule_updated", {"summary": "Reset all overrides", "ts": now_iso})
                self._send_json({"ok": True, "state": state})
                return

            self._send_json({"error": "Unknown endpoint"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": f"Server error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)


def get_lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def run_server(host: str = "0.0.0.0", port: int = 8765) -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    sync_static_ics_files()
    server = ThreadingHTTPServer((host, port), ParkingRequestHandler)
    lan_ip = get_lan_ip()
    print(f"Parking Rotation Server listening on:", flush=True)
    print(f"  • Local:   http://127.0.0.1:{port}", flush=True)
    print(f"  • Network: http://{lan_ip}:{port}  (Share this with Kulwinder on the same network)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    host_addr = os.environ.get("HOST", "0.0.0.0")
    port_num = int(os.environ.get("PORT", "8765"))
    run_server(host=host_addr, port=port_num)
