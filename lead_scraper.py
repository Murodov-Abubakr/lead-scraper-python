#!/usr/bin/env python3
"""
Local Business Lead Scraper + Full Auditor

Scrapes dental clinics via Google Places API and runs a full website
audit on each one — detecting every problem that can be sold as a service.

Usage:
    python lead_scraper.py                         # scrape all cities
    python lead_scraper.py --city "Austin TX"      # single city
    python lead_scraper.py --test                  # 1 search only
"""

import argparse
import csv
import logging
import os
import re
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

PLACES_API_KEY    = os.getenv("PLACES_API_KEY")
PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY")

RESULTS_FILE = "dental_leads.csv"
TARGET       = 1000

SEARCH_QUERIES = [
    ("dentist",         "San Antonio TX"),
    ("dental clinic",   "San Antonio TX"),
    ("dental office",   "San Antonio TX"),
    ("family dentist",  "San Antonio TX"),
    ("cosmetic dentist","San Antonio TX"),
    ("dentist",         "El Paso TX"),
    ("dental clinic",   "El Paso TX"),
    ("dental office",   "El Paso TX"),
    ("family dentist",  "El Paso TX"),
    ("dentist",         "Oklahoma City OK"),
    ("dental clinic",   "Oklahoma City OK"),
    ("dental office",   "Oklahoma City OK"),
    ("family dentist",  "Oklahoma City OK"),
    ("dentist",         "Tulsa OK"),
    ("dental clinic",   "Tulsa OK"),
    ("dental office",   "Tulsa OK"),
    ("dentist",         "Memphis TN"),
    ("dental clinic",   "Memphis TN"),
    ("dental office",   "Memphis TN"),
    ("family dentist",  "Memphis TN"),
    ("dentist",         "Albuquerque NM"),
    ("dental clinic",   "Albuquerque NM"),
    ("dental office",   "Albuquerque NM"),
    ("dentist",         "Fresno CA"),
    ("dental clinic",   "Fresno CA"),
    ("dental office",   "Fresno CA"),
    ("dentist",         "Louisville KY"),
    ("dental clinic",   "Louisville KY"),
    ("dental office",   "Louisville KY"),
    ("dentist",         "Corpus Christi TX"),
    ("dental clinic",   "Corpus Christi TX"),
    ("dentist",         "Lubbock TX"),
    ("dental clinic",   "Lubbock TX"),
    ("dentist",         "Shreveport LA"),
    ("dental clinic",   "Shreveport LA"),
]

CHAIN_KEYWORDS = [
    "aspen dental", "bright now", "western dental", "pacific dental",
    "smile direct", "castle dental", "gentle dental", "affordable dentures",
    "great expressions", "midwest dental", "dental works", "altus dental",
    "coast dental", "heartland dental", "comfort dental",
]

CHATBOT_MARKERS = [
    "tidio", "intercom", "drift.com", "zendesk", "livechat",
    "tawk.to", "hubspot", "freshchat", "olark", "crisp.chat",
    "liveperson", "boldchat", "snapengage", "chatra",
]

BOOKING_MARKERS = [
    "book appointment", "book online", "schedule appointment",
    "schedule online", "request appointment", "online booking",
    "book now", "schedule now", "reserve appointment",
]

THIRD_PARTY_BOOKING = [
    "zocdoc", "booksy", "healthgrades booking", "patientpop",
    "demandforce", "solutionreach",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ═══════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# GOOGLE PLACES API
# ═══════════════════════════════════════════════════════════════════

def search_places(query, city, page_token=None):
    params = {"query": f"{query} in {city}", "key": PLACES_API_KEY}
    if page_token:
        params = {"pagetoken": page_token, "key": PLACES_API_KEY}
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/place/textsearch/json",
                         params=params, timeout=15)
        return r.json()
    except Exception as e:
        log.error("Search failed: %s", e)
        return {}


def get_details(place_id):
    params = {
        "place_id": place_id,
        "fields": "name,formatted_phone_number,website,rating,user_ratings_total,business_status,formatted_address",
        "key": PLACES_API_KEY,
    }
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/place/details/json",
                         params=params, timeout=15)
        return r.json().get("result", {})
    except Exception as e:
        log.error("Details failed: %s", e)
        return {}

# ═══════════════════════════════════════════════════════════════════
# PAGESPEED / SEO
# ═══════════════════════════════════════════════════════════════════

def check_pagespeed(url):
    try:
        r = requests.get(
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
            params={"url": url, "strategy": "mobile", "key": PAGESPEED_API_KEY},
            timeout=25,
        )
        data = r.json()
        cats = data.get("lighthouseResult", {}).get("categories", {})
        perf = cats.get("performance", {}).get("score")
        seo  = cats.get("seo", {}).get("score")
        return (
            int(perf * 100) if perf is not None else -1,
            int(seo  * 100) if seo  is not None else -1,
        )
    except Exception:
        return -1, -1

# ═══════════════════════════════════════════════════════════════════
# WEBSITE AUDIT
# ═══════════════════════════════════════════════════════════════════

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\."
    r"(?:com|net|org|io|co|info|us|ca|dental|health|clinic|care)(?![a-zA-Z])",
    re.IGNORECASE,
)


def fetch_page(url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT},
                         timeout=15, allow_redirects=True)
        return r
    except Exception:
        return None


def extract_email(soup, base_url):
    # Try contact page first
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "contact" in href.lower():
            contact_url = urljoin(base_url, href)
            resp = fetch_page(contact_url)
            if resp:
                contact_soup = BeautifulSoup(resp.text, "html.parser")
                for tag in contact_soup(["script", "style"]):
                    tag.decompose()
                emails = _EMAIL_RE.findall(contact_soup.get_text(" "))
                if emails:
                    return emails[0].lower()

    # Fall back to main page
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            email = a["href"][7:].split("?")[0].split(",")[0].strip()
            if _EMAIL_RE.match(email):
                return email.lower()
    return ""


def get_copyright_year(text):
    m = re.search(r"©\s*(\d{4})", text) or re.search(r"copyright\s*(\d{4})", text, re.I)
    return int(m.group(1)) if m else None


def audit_website(url):
    """Returns dict of audit results for a business website."""
    result = {
        "has_website":    bool(url),
        "has_https":      False,
        "has_mobile":     False,
        "has_chatbot":    False,
        "has_booking":    False,
        "uses_third_party_booking": False,
        "third_party_name": "",
        "copyright_year": "",
        "pagespeed":      -1,
        "seo_score":      -1,
        "email":          "",
        "problems":       [],
        "problem_count":  0,
    }

    if not url:
        result["problems"].append("no_website")
        result["problem_count"] = 1
        return result

    # HTTPS check
    result["has_https"] = url.startswith("https")
    if not result["has_https"]:
        result["problems"].append("no_https")

    # Fetch main page
    resp = fetch_page(url)
    if not resp:
        result["problems"].append("site_unreachable")
        result["problem_count"] = len(result["problems"])
        return result

    html  = resp.text
    soup  = BeautifulSoup(html, "html.parser")

    # Mobile viewport
    viewport = soup.find("meta", attrs={"name": re.compile("viewport", re.I)})
    result["has_mobile"] = bool(viewport)
    if not result["has_mobile"]:
        result["problems"].append("no_mobile_support")

    # Copyright year
    for tag in soup(["script", "style"]):
        tag.decompose()
    page_text = soup.get_text(" ", strip=True)
    yr = get_copyright_year(page_text)
    if yr:
        result["copyright_year"] = yr
        if yr < 2020:
            result["problems"].append(f"old_design_{yr}")

    # Chatbot detection
    html_lower = html.lower()
    result["has_chatbot"] = any(m in html_lower for m in CHATBOT_MARKERS)
    if not result["has_chatbot"]:
        result["problems"].append("no_ai_chatbot")

    # Booking detection
    text_lower = page_text.lower()
    result["has_booking"] = any(m in text_lower for m in BOOKING_MARKERS)
    if not result["has_booking"]:
        result["problems"].append("no_online_booking")

    # Third-party booking (Zocdoc etc.)
    for tp in THIRD_PARTY_BOOKING:
        if tp in html_lower:
            result["uses_third_party_booking"] = True
            result["third_party_name"] = tp
            result["problems"].append(f"uses_{tp}")
            break

    # Email extraction
    result["email"] = extract_email(soup, url)

    # PageSpeed + SEO
    log.info("      PageSpeed check...")
    ps, seo = check_pagespeed(url)
    result["pagespeed"]  = ps
    result["seo_score"]  = seo
    if ps != -1 and ps < 70:
        result["problems"].append(f"slow_site_{ps}")
    if seo != -1 and seo < 70:
        result["problems"].append(f"bad_seo_{seo}")

    result["problem_count"] = len(result["problems"])
    return result

# ═══════════════════════════════════════════════════════════════════
# SCRIPT GENERATION (template-based, no AI API needed)
# ═══════════════════════════════════════════════════════════════════

def generate_pitch(business_name, problems, audit):
    lines = [
        f"Hi, I was searching for a dentist in your area and came across {business_name}.",
        "I ran a quick audit of your online presence and found a few things that might be costing you patients.",
    ]

    if "no_website" in problems:
        lines.append("You currently have no website. In 2025, over 80 percent of patients search online before choosing a dentist — you are completely invisible to them.")

    if "no_https" in problems:
        lines.append("Your website shows Not Secure in every browser. Patients see this warning and immediately go to your competitor instead.")

    old_design = [p for p in problems if p.startswith("old_design_")]
    if old_design:
        yr = old_design[0].split("_")[-1]
        lines.append(f"Your website was last updated in {yr}. An outdated site signals to patients that your practice may be behind on modern dental technology as well.")

    if "no_mobile_support" in problems:
        lines.append("Your site is not mobile-friendly. Over 70 percent of patients search on their phone — they will leave immediately.")

    if "no_online_booking" in problems:
        lines.append("Patients who visit your site after 6pm have no way to book an appointment. They simply go to whoever lets them book online right now.")

    zocdoc_used = any("zocdoc" in p for p in problems)
    if zocdoc_used:
        lines.append("You are using Zocdoc which charges a fee per acquired patient. We can replace that with your own AI booking system on your website for a flat monthly fee — you keep 100 percent of every patient.")

    if "no_ai_chatbot" in problems:
        lines.append("There is no chat or AI assistant on your site. Patient questions go unanswered until the next business day, and those patients book elsewhere.")

    slow = [p for p in problems if p.startswith("slow_site_")]
    if slow:
        score = slow[0].split("_")[-1]
        lines.append(f"Your website scores {score} out of 100 for speed on mobile. Most visitors leave if a page takes more than 3 seconds to load.")

    bad_seo = [p for p in problems if p.startswith("bad_seo_")]
    if bad_seo:
        score = bad_seo[0].split("_")[-1]
        lines.append(f"Your website scores {score} out of 100 for SEO, meaning new patients searching Google cannot easily find you.")

    lines.append(f"I specialize in helping dental practices fix exactly these issues — website, AI booking, speed, and SEO.")
    lines.append("I would love to show you what is possible in a free 15-minute call. My contact details are at the end of this video.")

    return " ".join(lines)

# ═══════════════════════════════════════════════════════════════════
# CSV
# ═══════════════════════════════════════════════════════════════════

FIELDS = [
    "business_name", "address", "city", "phone", "email",
    "website", "rating", "reviews",
    "has_https", "has_mobile", "has_chatbot", "has_booking",
    "uses_third_party_booking", "third_party_name",
    "copyright_year", "pagespeed", "seo_score",
    "problems", "problem_count", "lead_score", "pitch",
]


def load_seen():
    seen = set()
    if not os.path.exists(RESULTS_FILE):
        return seen
    with open(RESULTS_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            seen.add(r.get("business_name", "").lower().strip())
    return seen


def count_saved():
    if not os.path.exists(RESULTS_FILE):
        return 0
    with open(RESULTS_FILE, encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


def save(row):
    is_new = not os.path.exists(RESULTS_FILE) or os.path.getsize(RESULTS_FILE) == 0
    with open(RESULTS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(row)

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def is_chain(name):
    return any(c in name.lower() for c in CHAIN_KEYWORDS)


def scrape(queries=None, target=TARGET):
    queries = queries or SEARCH_QUERIES
    seen_names    = load_seen()
    seen_place_ids = set()
    total = count_saved()

    log.info("Starting — %d saved, target: %d", total, target)

    for niche, city in queries:
        if total >= target:
            break

        log.info("Searching: %s in %s", niche, city)
        page_token = None
        page = 0

        while page < 3:
            if page > 0:
                time.sleep(2)

            data = search_places(niche, city, page_token)
            results = data.get("results", [])
            if not results:
                break

            for place in results:
                if total >= target:
                    break

                pid  = place.get("place_id", "")
                name = place.get("name", "").strip()

                if not name or not pid:
                    continue
                if pid in seen_place_ids:
                    continue
                if name.lower() in seen_names:
                    continue
                if is_chain(name):
                    log.info("  skip chain: %s", name)
                    continue
                if place.get("business_status") != "OPERATIONAL":
                    continue

                seen_place_ids.add(pid)

                log.info("  [%d/%d] %s — getting details...", total + 1, target, name)
                det     = get_details(pid)
                phone   = det.get("formatted_phone_number", "")
                website = det.get("website", "")
                address = det.get("formatted_address", place.get("formatted_address", ""))
                rating  = place.get("rating", 0)
                reviews = place.get("user_ratings_total", 0)

                if not phone:
                    log.info("    no phone — skip")
                    continue

                log.info("    auditing website...")
                audit = audit_website(website)
                problems = audit["problems"]

                # Skip if no problems found (perfect website — rare but possible)
                if not problems:
                    log.info("    no problems found — skip")
                    continue

                pitch = generate_pitch(name, problems, audit)

                row = {
                    "business_name":  name,
                    "address":        address,
                    "city":           city,
                    "phone":          phone,
                    "email":          audit["email"],
                    "website":        website,
                    "rating":         rating,
                    "reviews":        reviews,
                    "has_https":      audit["has_https"],
                    "has_mobile":     audit["has_mobile"],
                    "has_chatbot":    audit["has_chatbot"],
                    "has_booking":    audit["has_booking"],
                    "uses_third_party_booking": audit["uses_third_party_booking"],
                    "third_party_name": audit["third_party_name"],
                    "copyright_year": audit["copyright_year"],
                    "pagespeed":      audit["pagespeed"],
                    "seo_score":      audit["seo_score"],
                    "problems":       " | ".join(problems),
                    "problem_count":  audit["problem_count"],
                    "lead_score":     audit["problem_count"] * 10,
                    "pitch":          pitch,
                }

                save(row)
                seen_names.add(name.lower())
                total += 1

                log.info("  ✓ #%d %s | %d problems: %s",
                         total, name, audit["problem_count"], ", ".join(problems))

                time.sleep(random.uniform(1, 2))

            page_token = data.get("next_page_token")
            if not page_token:
                break
            page += 1

        time.sleep(random.uniform(1, 2))

    log.info("Done. %d leads saved to %s", count_saved(), RESULTS_FILE)
    summary()


def summary():
    if not os.path.exists(RESULTS_FILE):
        return
    rows = list(csv.DictReader(open(RESULTS_FILE, encoding="utf-8")))
    print("\n" + "=" * 55)
    print(f"  TOTAL LEADS        : {len(rows)}")
    print(f"  No website         : {sum(1 for r in rows if 'no_website' in r['problems'])}")
    print(f"  No AI chatbot      : {sum(1 for r in rows if 'no_ai_chatbot' in r['problems'])}")
    print(f"  No online booking  : {sum(1 for r in rows if 'no_online_booking' in r['problems'])}")
    print(f"  No HTTPS           : {sum(1 for r in rows if 'no_https' in r['problems'])}")
    print(f"  Slow site          : {sum(1 for r in rows if 'slow_site' in r['problems'])}")
    print(f"  Bad SEO            : {sum(1 for r in rows if 'bad_seo' in r['problems'])}")
    print(f"  Have email         : {sum(1 for r in rows if r.get('email'))}")
    print(f"  Have phone         : {sum(1 for r in rows if r.get('phone'))}")
    print("=" * 55)
    top = sorted(rows, key=lambda r: int(r.get("problem_count") or 0), reverse=True)[:5]
    print("\nTop 5 hottest leads:")
    for r in top:
        print(f"  {r['business_name'][:40]:<40} {r['problem_count']} problems | {r['phone']}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test",  action="store_true")
    p.add_argument("--city",  default=None)
    p.add_argument("--niche", default="dentist")
    args = p.parse_args()

    if args.city:
        scrape(queries=[(args.niche, args.city)], target=TARGET)
    elif args.test:
        scrape(queries=[SEARCH_QUERIES[0]], target=20)
    else:
        scrape()


if __name__ == "__main__":
    main()
