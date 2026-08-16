#!/usr/bin/env python3
"""
Analytics tracker — conversations, bookings, peak hours, daily stats.
Uses Supabase PostgreSQL when DATABASE_URL is set; falls back to analytics.json locally.
"""

import json, logging, os, threading
from datetime import datetime

from db import DATABASE_URL, get_db

logger = logging.getLogger(__name__)

_FILE = os.path.join(os.path.dirname(__file__), "analytics.json")
_LOCK = threading.Lock()


# ── File-based helpers (local dev fallback) ────────────────────────────────────

def _load():
    if not os.path.exists(_FILE):
        return {
            "total_conversations": 0,
            "total_bookings":      0,
            "today_conversations": 0,
            "today_bookings":      0,
            "last_reset_date":     datetime.now().strftime("%Y-%m-%d"),
            "hourly":              {str(h): 0 for h in range(24)},
            "daily":               {},
        }
    with open(_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _FILE)


def _reset_if_new_day(data):
    today = datetime.now().strftime("%Y-%m-%d")
    if data.get("last_reset_date") != today:
        data["today_conversations"] = 0
        data["today_bookings"]      = 0
        data["last_reset_date"]     = today
    return data


def _file_track_conversation():
    with _LOCK:
        data  = _reset_if_new_day(_load())
        today = datetime.now().strftime("%Y-%m-%d")
        hour  = str(datetime.now().hour)
        data["total_conversations"]   += 1
        data["today_conversations"]   += 1
        data["hourly"][hour]           = data["hourly"].get(hour, 0) + 1
        data["daily"][today]           = data["daily"].get(today, 0) + 1
        _save(data)


def _file_track_booking():
    with _LOCK:
        data = _reset_if_new_day(_load())
        data["total_bookings"] += 1
        data["today_bookings"] += 1
        _save(data)


def _file_get_stats() -> dict:
    with _LOCK:
        data       = _reset_if_new_day(_load())
        total_conv = data["total_conversations"]
        total_book = data["total_bookings"]
        hourly     = data.get("hourly", {})
        daily      = data.get("daily", {})
        peak_hour  = max(hourly, key=lambda h: hourly.get(h, 0)) if hourly else "9"
        last7      = dict(sorted(daily.items())[-7:])
        return {
            "total_conversations": total_conv,
            "total_bookings":      total_book,
            "today_conversations": data["today_conversations"],
            "today_bookings":      data["today_bookings"],
            "conversion_rate":     round(total_book / total_conv * 100, 1) if total_conv > 0 else 0,
            "peak_hour":           f"{peak_hour}:00",
            "hourly":              hourly,
            "daily_last7":         last7,
        }


# ── Database helpers (production / Supabase) ───────────────────────────────────

def _db_ensure_row(cur):
    cur.execute("INSERT INTO analytics (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def _db_reset_if_new_day(cur):
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT last_reset_date FROM analytics WHERE id = 1 FOR UPDATE")
    row = cur.fetchone()
    if row and str(row[0]) != today:
        cur.execute(
            "UPDATE analytics SET today_conversations=0, today_bookings=0, last_reset_date=%s WHERE id=1",
            (today,),
        )


def _db_track_conversation():
    today = datetime.now().strftime("%Y-%m-%d")
    hour  = str(datetime.now().hour)
    with get_db() as conn:
        cur = conn.cursor()
        _db_ensure_row(cur)
        _db_reset_if_new_day(cur)
        cur.execute("""
            UPDATE analytics
            SET total_conversations = total_conversations + 1,
                today_conversations = today_conversations + 1,
                hourly = jsonb_set(hourly, ARRAY[%s]::text[], to_jsonb(COALESCE((hourly->>%s)::int, 0) + 1)),
                daily  = jsonb_set(daily,  ARRAY[%s]::text[], to_jsonb(COALESCE((daily ->>%s)::int, 0) + 1))
            WHERE id = 1
        """, (hour, hour, today, today))


def _db_track_booking():
    with get_db() as conn:
        cur = conn.cursor()
        _db_ensure_row(cur)
        _db_reset_if_new_day(cur)
        cur.execute("""
            UPDATE analytics
            SET total_bookings = total_bookings + 1,
                today_bookings = today_bookings + 1
            WHERE id = 1
        """)


def _db_get_stats() -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        _db_ensure_row(cur)
        _db_reset_if_new_day(cur)
        cur.execute("""
            SELECT total_conversations, total_bookings,
                   today_conversations, today_bookings, hourly, daily
            FROM analytics WHERE id = 1
        """)
        row = cur.fetchone()

    total_conv, total_book, today_conv, today_book, hourly, daily = row
    hourly    = hourly or {}
    daily     = daily  or {}
    peak_hour = max(hourly, key=lambda h: hourly.get(h, 0)) if hourly else "9"
    last7     = dict(sorted(daily.items())[-7:])
    return {
        "total_conversations": total_conv,
        "total_bookings":      total_book,
        "today_conversations": today_conv,
        "today_bookings":      today_book,
        "conversion_rate":     round(total_book / total_conv * 100, 1) if total_conv > 0 else 0,
        "peak_hour":           f"{peak_hour}:00",
        "hourly":              hourly,
        "daily_last7":         last7,
    }


# ── Public API — dispatch based on DATABASE_URL ────────────────────────────────

def track_conversation():
    (_db_track_conversation if DATABASE_URL else _file_track_conversation)()


def track_booking():
    (_db_track_booking if DATABASE_URL else _file_track_booking)()


def get_stats() -> dict:
    return (_db_get_stats if DATABASE_URL else _file_get_stats)()


# ── Per-clinic analytics (DB only) ────────────────────────────────────────────

def _clinic_ensure_row(cur, clinic_id: str):
    cur.execute(
        "INSERT INTO clinic_analytics (clinic_id) VALUES (%s) ON CONFLICT (clinic_id) DO NOTHING",
        (clinic_id,)
    )


def _clinic_reset_if_new_day(cur, clinic_id: str):
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute(
        "SELECT last_reset_date FROM clinic_analytics WHERE clinic_id = %s FOR UPDATE",
        (clinic_id,)
    )
    row = cur.fetchone()
    if row and str(row[0]) != today:
        cur.execute(
            "UPDATE clinic_analytics SET today_conversations=0, today_bookings=0, last_reset_date=%s WHERE clinic_id=%s",
            (today, clinic_id),
        )


def track_clinic_conversation(clinic_id: str):
    if not DATABASE_URL or not clinic_id:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    hour  = str(datetime.now().hour)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            _clinic_ensure_row(cur, clinic_id)
            _clinic_reset_if_new_day(cur, clinic_id)
            cur.execute("""
                UPDATE clinic_analytics
                SET total_conversations = total_conversations + 1,
                    today_conversations = today_conversations + 1,
                    hourly = jsonb_set(hourly, ARRAY[%s]::text[], to_jsonb(COALESCE((hourly->>%s)::int, 0) + 1)),
                    daily  = jsonb_set(daily,  ARRAY[%s]::text[], to_jsonb(COALESCE((daily ->>%s)::int, 0) + 1))
                WHERE clinic_id = %s
            """, (hour, hour, today, today, clinic_id))
    except Exception as e:
        logger.warning("Clinic analytics tracking failed: %s", e)


def track_clinic_booking(clinic_id: str):
    if not DATABASE_URL or not clinic_id:
        return
    try:
        with get_db() as conn:
            cur = conn.cursor()
            _clinic_ensure_row(cur, clinic_id)
            _clinic_reset_if_new_day(cur, clinic_id)
            cur.execute("""
                UPDATE clinic_analytics
                SET total_bookings = total_bookings + 1,
                    today_bookings = today_bookings + 1
                WHERE clinic_id = %s
            """, (clinic_id,))
    except Exception as e:
        logger.warning("Clinic booking tracking failed: %s", e)


def get_clinic_stats(clinic_id: str) -> dict:
    empty = {
        "total_conversations": 0, "total_bookings": 0,
        "today_conversations": 0, "today_bookings": 0,
        "conversion_rate": 0, "peak_hour": "—", "hourly": {}, "daily_last7": {}
    }
    if not DATABASE_URL or not clinic_id:
        return empty
    try:
        with get_db() as conn:
            cur = conn.cursor()
            _clinic_ensure_row(cur, clinic_id)
            _clinic_reset_if_new_day(cur, clinic_id)
            cur.execute("""
                SELECT total_conversations, total_bookings,
                       today_conversations, today_bookings, hourly, daily
                FROM clinic_analytics WHERE clinic_id = %s
            """, (clinic_id,))
            row = cur.fetchone()
        if not row:
            return empty
        total_conv, total_book, today_conv, today_book, hourly, daily = row
        hourly    = hourly or {}
        daily     = daily  or {}
        peak_hour = max(hourly, key=lambda h: hourly.get(h, 0)) if hourly else "9"
        last7     = dict(sorted(daily.items())[-7:])
        return {
            "total_conversations": total_conv,
            "total_bookings":      total_book,
            "today_conversations": today_conv,
            "today_bookings":      today_book,
            "conversion_rate":     round(total_book / total_conv * 100, 1) if total_conv > 0 else 0,
            "peak_hour":           f"{peak_hour}:00",
            "hourly":              hourly,
            "daily_last7":         last7,
        }
    except Exception as e:
        logger.error("Clinic stats failed: %s", e)
        return empty
