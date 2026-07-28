#!/usr/bin/env python3
"""
Local Business Lead Scraper
Finds qualified local businesses via Google Places API,
checks their website speed via PageSpeed API,
and outputs a ranked lead list.

Usage:
    python local_leads_scraper.py                    # scrape all configured searches
    python local_leads_scraper.py --test             # test with 1 search only
    python local_leads_scraper.py --niche dentist    # override niche
    python local_leads_scraper.py --city "Austin TX" # override city
"""

import argparse
import csv
import json
import logging
import os
import time
import re
import random
import requests
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

PLACES_API_KEY   = os.getenv("PLACES_API_KEY")
PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY")

RESULTS_FILE = "qualified_leads.csv"
TARGET_LEADS = 1000

# Search combinations — each yields up to 60 results (3 pages × 20)
SEARCH_QUERIES = [
    # Dental — San Antonio TX (primary target)
    ("dental clinic",  "San Antonio TX"),
    ("dental office",  "San Antonio TX"),
    ("dentist",        "San Antonio TX"),
    ("family dentist", "San Antonio TX"),
    ("cosmetic dentist","San Antonio TX"),
    # Dental — El Paso TX
    ("dental clinic",  "El Paso TX"),
    ("dental office",  "El Paso TX"),
    ("dentist",        "El Paso TX"),
    ("family dentist", "El Paso TX"),
    # Dental — Oklahoma City OK
    ("dental clinic",  "Oklahoma City OK"),
    ("dental office",  "Oklahoma City OK"),
    ("dentist",        "Oklahoma City OK"),
    ("family dentist", "Oklahoma City OK"),
    # Dental — Tulsa OK
    ("dental clinic",  "Tulsa OK"),
    ("dental office",  "Tulsa OK"),
    ("dentist",        "Tulsa OK"),
    # Dental — Memphis TN
    ("dental clinic",  "Memphis TN"),
    ("dental office",  "Memphis TN"),
    ("dentist",        "Memphis TN"),
    ("family dentist", "Memphis TN"),
    # Dental — Albuquerque NM
    ("dental clinic",  "Albuquerque NM"),
    ("dental office",  "Albuquerque NM"),
    ("dentist",        "Albuquerque NM"),
    # Dental — Fresno CA
    ("dental clinic",  "Fresno CA"),
    ("dental office",  "Fresno CA"),
    ("dentist",        "Fresno CA"),
    # Dental — Louisville KY
    ("dental clinic",  "Louisville KY"),
    ("dental office",  "Louisville KY"),
    ("dentist",        "Louisville KY"),
]

# Chains to skip — they have corporate IT teams
CHAIN_KEYWORDS = [
    "aspen dental", "bright now", "western dental", "pacific dental",
    "smile direct", "1-800-dentist", "castle dental", "gentle dental",
    "affordable dentures", "great expressions", "midwest dental",
    "family dentistry chain", "dental works", "altus dental",
    "coast dental", "smiledirectclub", "heartland dental",
]

# Qualification filters
MIN_REVIEWS    = 10     # established enough to pay
MAX_REVIEWS    = 400    # not a big chain
MIN_RATING     = 3.0    # not a dying business
MAX_PAGESPEED  = 70     # below this = has a real speed problem (most clinics score 50-70)
PAGESPEED_TIMEOUT = 20  # seconds

# ═══════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# GOOGLE PLACES API
# ═══════════════════════════════════════════════════════════════════

PLACES_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
PLACES_DETAIL_URL = "https://maps.googleapis.com/maps/api/place/details/json"


def search_places(query: str, city: str, page_token: str = None) -> dict:
    params = {
        "query": f"{query} in {city}",
        "key":   PLACES_API_KEY,
        "type":  "dentist",
    }
    if page_token:
        params = {"pagetoken": page_token, "key": PLACES_API_KEY}
    try:
        resp = requests.get(PLACES_SEARCH_URL, params=params, timeout=15)
        return resp.json()
    except Exception as e:
        log.error("Places search failed: %s", e)
        return {}


def get_place_details(place_id: str) -> dict:
    params = {
        "place_id": place_id,
        "fields":   "name,formatted_phone_number,website,rating,user_ratings_total,business_status,formatted_address",
        "key":      PLACES_API_KEY,
    }
    try:
        resp = requests.get(PLACES_DETAIL_URL, params=params, timeout=15)
        return resp.json().get("result", {})
    except Exception as e:
        log.error("Place details failed: %s", e)
        return {}


# ═══════════════════════════════════════════════════════════════════
# PAGESPEED API
# ═══════════════════════════════════════════════════════════════════

PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def check_pagespeed(website: str) -> int:
    if not website:
        return -1
    if not website.startswith("http"):
        website = "https://" + website
    try:
        resp = requests.get(
            PAGESPEED_URL,
            params={"url": website, "strategy": "mobile", "key": PAGESPEED_API_KEY},
            timeout=PAGESPEED_TIMEOUT,
        )
        data = resp.json()
        score = data.get("lighthouseResult", {}).get("categories", {}).get("performance", {}).get("score")
        return int(score * 100) if score is not None else -1
    except Exception:
        return -1


# ═══════════════════════════════════════════════════════════════════
# QUALIFICATION
# ═══════════════════════════════════════════════════════════════════

def is_chain(name: str) -> bool:
    name_lower = name.lower()
    return any(chain in name_lower for chain in CHAIN_KEYWORDS)


def lead_score(row: dict) -> int:
    score = 0
    ps = row.get("pagespeed_score", -1)

    if row.get("has_website") == "no":
        score += 100          # no website = easiest pitch
    elif ps != -1:
        if ps < 20:   score += 90
        elif ps < 35: score += 80
        elif ps < 50: score += 70
        else:         score += 0   # site is fine, skip

    reviews = row.get("reviews", 0)
    rating  = row.get("rating",  0)

    if 50 <= reviews <= 200: score += 15
    elif reviews > 200:      score += 5

    if 3.5 <= rating <= 4.5: score += 10

    if row.get("phone"):     score += 10

    return score


def is_qualified(row: dict) -> bool:
    if row.get("has_website") == "no":
        return True
    ps = row.get("pagespeed_score", -1)
    return ps != -1 and ps < MAX_PAGESPEED


# ═══════════════════════════════════════════════════════════════════
# CSV
# ═══════════════════════════════════════════════════════════════════

FIELDS = [
    "business_name", "address", "phone", "website",
    "rating", "reviews", "has_website", "pagespeed_score",
    "lead_score", "city", "niche",
]


def load_existing() -> set:
    seen = set()
    if not os.path.exists(RESULTS_FILE):
        return seen
    with open(RESULTS_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            seen.add(r.get("business_name", "").lower().strip())
    return seen


def save_lead(row: dict):
    is_new = not os.path.exists(RESULTS_FILE) or os.path.getsize(RESULTS_FILE) == 0
    with open(RESULTS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(row)


def count_leads() -> int:
    if not os.path.exists(RESULTS_FILE):
        return 0
    with open(RESULTS_FILE, encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


# ═══════════════════════════════════════════════════════════════════
# MAIN SCRAPE LOOP
# ═══════════════════════════════════════════════════════════════════

def scrape(queries=None, target=TARGET_LEADS):
    if not PLACES_API_KEY:
        log.error("PLACES_API_KEY not set in .env")
        return

    queries = queries or SEARCH_QUERIES
    seen_names = load_existing()
    seen_place_ids = set()
    total_found = count_leads()

    log.info("Starting — %d qualified leads already saved, target: %d", total_found, target)

    for niche, city in queries:
        if total_found >= target:
            log.info("Target of %d reached — done.", target)
            break

        log.info("Searching: %s in %s", niche, city)
        page_token = None
        page = 0

        while page < 3:   # max 3 pages = 60 results per search
            if page > 0:
                time.sleep(2)   # Google requires delay before using next_page_token

            data = search_places(niche, city, page_token)
            results = data.get("results", [])

            if not results:
                break

            for place in results:
                if total_found >= target:
                    break

                place_id = place.get("place_id", "")
                if place_id in seen_place_ids:
                    continue
                seen_place_ids.add(place_id)

                name = place.get("name", "").strip()
                if not name or name.lower() in seen_names:
                    continue
                if is_chain(name):
                    log.info("  skip chain: %s", name)
                    continue

                rating  = place.get("rating", 0)
                reviews = place.get("user_ratings_total", 0)

                if reviews < MIN_REVIEWS or reviews > MAX_REVIEWS:
                    log.info("  skip %s — reviews: %d", name, reviews)
                    continue
                if rating < MIN_RATING:
                    log.info("  skip %s — rating: %.1f", name, rating)
                    continue
                if place.get("business_status") != "OPERATIONAL":
                    continue

                # Get full details (phone + website)
                log.info("  [%d/%d] %s — getting details...", total_found + 1, target, name)
                details = get_place_details(place_id)
                time.sleep(0.3)

                phone   = details.get("formatted_phone_number", "")
                website = details.get("website", "")
                address = details.get("formatted_address", place.get("formatted_address", ""))
                status  = details.get("business_status", "OPERATIONAL")

                if status != "OPERATIONAL":
                    continue

                has_website = "yes" if website else "no"

                # PageSpeed check
                ps_score = -1
                if website:
                    log.info("    checking PageSpeed for %s...", website)
                    ps_score = check_pagespeed(website)
                    log.info("    score: %d", ps_score)

                row = {
                    "business_name":  name,
                    "address":        address,
                    "phone":          phone,
                    "website":        website,
                    "rating":         rating,
                    "reviews":        reviews,
                    "has_website":    has_website,
                    "pagespeed_score": ps_score,
                    "city":           city,
                    "niche":          niche,
                }
                row["lead_score"] = lead_score(row)

                if not is_qualified(row):
                    log.info("    %s — site is fast enough, skipping", name)
                    continue

                if not phone:
                    log.info("    %s — no phone, skipping", name)
                    continue

                save_lead(row)
                seen_names.add(name.lower())
                total_found += 1
                log.info("  ✓ LEAD #%d: %s | score=%d | ps=%d | phone=%s",
                         total_found, name, row["lead_score"], ps_score, phone)

            page_token = data.get("next_page_token")
            if not page_token:
                break
            page += 1

        time.sleep(random.uniform(1, 2))

    log.info("Done. Total qualified leads saved: %d", count_leads())
    log.info("Results in: %s", RESULTS_FILE)
    print_summary()


def print_summary():
    if not os.path.exists(RESULTS_FILE):
        return
    rows = list(csv.DictReader(open(RESULTS_FILE, encoding="utf-8")))
    no_website = sum(1 for r in rows if r["has_website"] == "no")
    slow_site  = sum(1 for r in rows if r["has_website"] == "yes" and int(r.get("pagespeed_score") or 0) < 50)
    has_phone  = sum(1 for r in rows if r.get("phone"))

    print("\n" + "="*50)
    print(f"  TOTAL QUALIFIED LEADS : {len(rows)}")
    print(f"  No website            : {no_website}")
    print(f"  Slow website (<50)    : {slow_site}")
    print(f"  Have phone number     : {has_phone}")
    print("="*50)
    print(f"\nTop 10 hottest leads (by score):")
    top = sorted(rows, key=lambda r: int(r.get("lead_score") or 0), reverse=True)[:10]
    for r in top:
        print(f"  {r['business_name'][:35]:<35} score={r['lead_score']} ps={r['pagespeed_score']} ph={r['phone']}")


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",  action="store_true", help="Run 1 search only")
    parser.add_argument("--niche", default=None, help="Override niche (e.g. dentist)")
    parser.add_argument("--city",  default=None, help="Override city (e.g. 'Austin TX')")
    args = parser.parse_args()

    if args.niche and args.city:
        queries = [(args.niche, args.city)]
    elif args.test:
        queries = [SEARCH_QUERIES[0]]
    else:
        queries = SEARCH_QUERIES

    scrape(queries=queries, target=TARGET_LEADS)


if __name__ == "__main__":
    main()
