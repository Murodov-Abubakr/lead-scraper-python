#!/usr/bin/env python3
"""
Push local lead data to Supabase so the phone dashboard can read it.
Run this after each scraping/outreach session.

    python dashboard/sync_to_supabase.py

Requires in .env:
    SUPABASE_URL      — https://xxxx.supabase.co
    SUPABASE_ANON_KEY — eyJhbGci...
"""
import csv
import json
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

SUPABASE_URL      = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
DATA_DIR          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_HEADERS = {
    "apikey":        SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates,return=minimal",
}


def _check_credentials():
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        print("ERROR: Add SUPABASE_URL and SUPABASE_ANON_KEY to your .env file.")
        print("       Get them from Supabase → Project Settings → API")
        sys.exit(1)


def _read_json(filename, fallback=None):
    try:
        with open(os.path.join(DATA_DIR, filename)) as f:
            return json.load(f)
    except Exception:
        return fallback if fallback is not None else {}


def _load_leads():
    path = os.path.join(DATA_DIR, "dental_leads.csv")
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _compute_stage(sent_info, seq_step, reply_info):
    if reply_info.get("stage"):
        return reply_info["stage"]
    status = sent_info.get("status", "")
    if status == "no_email":
        return "no_email"
    if status == "failed":
        return "failed"
    if status == "sent":
        return "followed_up" if int(seq_step or 0) > 1 else "emailed"
    return "scraped"


def _enrich(leads, sent, seq, owner, replied):
    rows = []
    for lead in leads:
        name = (lead.get("business_name") or "").lower().strip()
        s    = sent.get(name, {})
        sq   = seq.get(name, {})
        o    = owner.get(name, {})
        r    = replied.get(name, {})

        stage = _compute_stage(s, sq.get("step"), r)

        try:
            rating = float(lead.get("rating") or 0) or None
        except (ValueError, TypeError):
            rating = None
        try:
            reviews = int(lead.get("reviews") or 0) or None
        except (ValueError, TypeError):
            reviews = None

        rows.append({
            "business_name":  lead.get("business_name", ""),
            "city":           lead.get("city", ""),
            "phone":          lead.get("phone", ""),
            "email":          lead.get("email", ""),
            "website":        lead.get("website", ""),
            "rating":         rating,
            "reviews":        reviews,
            "problems":       lead.get("problems", ""),
            "problem_count":  int(lead.get("problem_count") or 0),
            "lead_score":     int(lead.get("lead_score") or 0),
            "pipeline_stage": stage,
            "sent_email":     s.get("email") or lead.get("email", ""),
            "sent_at":        s.get("timestamp"),
            "sequence_step":  int(sq.get("step") or 0),
            "owner_name":     o.get("owner_name", ""),
            "owner_email":    o.get("email", ""),
        })
    return rows


def _upsert_batch(rows):
    chunk_size = 100
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        resp  = requests.post(
            f"{SUPABASE_URL}/rest/v1/outreach_leads",
            headers=_HEADERS,
            json=chunk,
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            print(f"\n  ERROR chunk {i // chunk_size}: {resp.status_code}")
            print(f"  {resp.text[:300]}")
            return False
        done = min(i + chunk_size, len(rows))
        print(f"  {done}/{len(rows)} synced...", end="\r")
    return True


def main():
    _check_credentials()

    print("\nVicere — Sync to Supabase")
    print("─" * 38)

    leads   = _load_leads()
    sent    = _read_json("outreach_sent.json")
    seq     = _read_json("email_sequence.json")
    owner   = _read_json("owner_outreach.json")
    replied = _read_json("replied.json")

    if not leads:
        print("  No leads found — run the scraper first.")
        return

    print(f"  Leads in CSV  : {len(leads)}")
    rows = _enrich(leads, sent, seq, owner, replied)
    stages = {}
    for r in rows:
        stages[r["pipeline_stage"]] = stages.get(r["pipeline_stage"], 0) + 1
    for stage, count in sorted(stages.items()):
        print(f"    {stage:<14}: {count}")
    print(f"  Pushing to Supabase...")

    if _upsert_batch(rows):
        print(f"\n  Done — {len(rows)} leads synced.")
        print("  Open the phone app and pull to refresh.\n")
    else:
        print("\n  Sync failed — see error above.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
