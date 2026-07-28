#!/usr/bin/env python3
"""
Shopify Agency Hiring Signal Scanner

Scans the agencies already collected in agencies.csv for a real, current
hiring signal (a live careers/jobs page mentioning a developer/engineer
role), and for agencies that show one, extracts phone number, WhatsApp
link, and social media links already published on their own site.

This deliberately does NOT scrape LinkedIn/Instagram/etc. directly — that
violates platform ToS and gets accounts blocked. It only picks up links
agencies have already published themselves (e.g. a LinkedIn icon in their
footer), which is just normal public-page reading.

The output is a prioritized CSV for YOU to manually call/message — phone
calls and DMs work best with a real personal touch, so this tool finds
the contact info and the "why I'm reaching out now" signal, but doesn't
automate the call/DM itself.

Usage:
    python shopify_hiring_scanner.py            # scan all agencies
    python shopify_hiring_scanner.py --domain X  # scan a single domain (debug)
"""

import argparse
import csv
import logging
import os
import random
import re
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

AGENCIES_FILE = "agencies.csv"          # reuses the existing agency list
RESULTS_FILE = "hiring_signals.csv"     # new output, separate from results.csv

SCRAPE_DELAY = (3, 8)
REQUEST_TIMEOUT = 15

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CAREERS_PATHS = [
    "/careers", "/jobs", "/careers/", "/jobs/", "/about/careers",
    "/work-with-us", "/join-us", "/join-the-team", "/team/careers",
    "/about-us/careers", "/company/careers", "/open-positions",
]

# Combinations checked in careers-page text to confirm there's a real,
# currently-open developer-type role (not just a generic "join our team"
# page with no openings).
DEV_ROLE_KEYWORDS = [
    "shopify developer", "frontend developer", "front-end developer",
    "backend developer", "back-end developer", "full stack developer",
    "full-stack developer", "software developer", "software engineer",
    "web developer", "ecommerce developer", "liquid developer",
    "javascript developer", "react developer",
]

OPENING_INDICATORS = [
    "we're hiring", "we are hiring", "open position", "open role",
    "join our team", "apply now", "current openings", "now hiring",
    "we're looking for", "we are looking for", "job opening",
]

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# HTTP SESSION — separate from the other tools, no shared state
# ═══════════════════════════════════════════════════════════════════════════════

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",   # no "br" — avoids the Brotli decode bug
        "Connection": "keep-alive",
    })
    return s


SESSION = make_session()
_robots_cache: dict = {}


def robots_allowed(domain: str, path: str) -> bool:
    if domain not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"https://{domain}/robots.txt")
        try:
            rp.read()
            _robots_cache[domain] = rp
        except Exception:
            _robots_cache[domain] = None
    rp = _robots_cache[domain]
    return True if rp is None else rp.can_fetch(USER_AGENT, path)


def _fetch(url: str):
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# HIRING SIGNAL DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def check_hiring_signal(domain: str):
    """
    Visits CAREERS_PATHS looking for a real developer-role opening.
    Returns (is_hiring: bool, careers_url: str, matched_role: str, soup_for_reuse).
    """
    for path in CAREERS_PATHS:
        if not robots_allowed(domain, path):
            continue
        resp = _fetch(f"https://{domain}{path}")
        if not resp:
            time.sleep(random.uniform(*SCRAPE_DELAY))
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True).lower()

        matched_role = next((kw for kw in DEV_ROLE_KEYWORDS if kw in text), None)
        has_opening_language = any(ind in text for ind in OPENING_INDICATORS)

        if matched_role and has_opening_language:
            return True, f"https://{domain}{path}", matched_role, soup

        time.sleep(random.uniform(*SCRAPE_DELAY))

    return False, None, None, None


# ═══════════════════════════════════════════════════════════════════════════════
# CONTACT EXTRACTION (phone / WhatsApp / social — only what's already published)
# ═══════════════════════════════════════════════════════════════════════════════

_PHONE_RE = re.compile(r"(\+?\d[\d\s().\-]{7,16}\d)")
_WHATSAPP_LINK_RE = re.compile(r"(?:wa\.me|api\.whatsapp\.com/send)\S*", re.IGNORECASE)

SOCIAL_DOMAINS = {
    "linkedin": "linkedin.com",
    "instagram": "instagram.com",
    "twitter": "twitter.com",
    "x": "x.com",
    "facebook": "facebook.com",
}


def extract_contacts(soup, page_text: str) -> dict:
    """Extracts phone, WhatsApp link, and social links already published on the page."""
    contacts = {"phone": "", "whatsapp": "", "linkedin": "", "instagram": "", "twitter": "", "facebook": ""}

    # Phone — prefer tel: links (most reliable), fall back to regex over text
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("tel:"):
            contacts["phone"] = href[4:].strip()
            break
    if not contacts["phone"]:
        m = _PHONE_RE.search(page_text)
        if m:
            contacts["phone"] = m.group(1).strip()

    # WhatsApp — wa.me links are the clearest signal
    for a in soup.find_all("a", href=True):
        if _WHATSAPP_LINK_RE.search(a["href"]):
            contacts["whatsapp"] = a["href"]
            break

    # Social links — only ones the agency already published themselves
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        for platform, marker in SOCIAL_DOMAINS.items():
            if marker in href and not contacts.get(platform):
                contacts[platform if platform != "x" else "twitter"] = a["href"]

    return contacts


# ═══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ═══════════════════════════════════════════════════════════════════════════════

RESULTS_FIELDS = [
    "agency_name", "domain", "careers_url", "matched_role",
    "phone", "whatsapp", "linkedin", "instagram", "twitter", "facebook",
]


def load_existing_results() -> set:
    existing = set()
    if not os.path.exists(RESULTS_FILE):
        return existing
    with open(RESULTS_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            existing.add(row.get("domain", "").lower())
    return existing


def log_result(row: dict):
    file_is_new = not os.path.exists(RESULTS_FILE) or os.path.getsize(RESULTS_FILE) == 0
    with open(RESULTS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_FIELDS)
        if file_is_new:
            writer.writeheader()
        writer.writerow(row)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def load_agencies() -> list:
    if not os.path.exists(AGENCIES_FILE):
        log.error("%s not found. Run shopify_outreach.py --collect first.", AGENCIES_FILE)
        return []
    with open(AGENCIES_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def scan_domain(agency_name: str, domain: str):
    is_hiring, careers_url, matched_role, soup = check_hiring_signal(domain)
    if not is_hiring:
        return None

    page_text = soup.get_text(" ", strip=True)
    contacts = extract_contacts(soup, page_text)

    return {
        "agency_name": agency_name,
        "domain": domain,
        "careers_url": careers_url,
        "matched_role": matched_role,
        **contacts,
    }


def run_scan():
    agencies = load_agencies()
    if not agencies:
        return
    already_scanned = load_existing_results()
    total = len(agencies)
    found_count = 0

    log.info("═══ Scanning %d agencies for active developer hiring signals ═══", total)

    for i, agency in enumerate(agencies, 1):
        domain = agency.get("domain", "").lower().strip()
        agency_name = agency.get("agency_name", domain)

        if not domain or domain in already_scanned:
            continue

        log.info("[%d/%d] %s — checking careers page...", i, total, domain)
        result = scan_domain(agency_name, domain)

        if result:
            log.info(
                "  ✓ HIRING: %s | phone=%s | whatsapp=%s | linkedin=%s",
                result["matched_role"],
                bool(result["phone"]), bool(result["whatsapp"]), bool(result["linkedin"]),
            )
            log_result(result)
            found_count += 1
        else:
            log.info("  — No current developer opening found")
            log_result({
                "agency_name": agency_name, "domain": domain, "careers_url": "",
                "matched_role": "", "phone": "", "whatsapp": "",
                "linkedin": "", "instagram": "", "twitter": "", "facebook": "",
            })

        time.sleep(random.uniform(*SCRAPE_DELAY))

    log.info("Scan done: %d agencies actively hiring developers, out of %d checked", found_count, total)
    log.info("Results saved to %s — sort/filter by matched_role to find your call list", RESULTS_FILE)


def main():
    parser = argparse.ArgumentParser(description="Shopify Agency Hiring Signal Scanner")
    parser.add_argument("--domain", help="Scan a single domain for debugging")
    args = parser.parse_args()

    if args.domain:
        result = scan_domain(args.domain, args.domain)
        print(result or f"No hiring signal found for {args.domain}")
        return

    run_scan()
    log.info("Done.")


if __name__ == "__main__":
    main()
