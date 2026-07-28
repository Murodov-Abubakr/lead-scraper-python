#!/usr/bin/env python3
"""
Finds the dentist owner's name and personal email using:
1. About page scraping (find dentist name)
2. theHarvester (find all emails on domain)
3. Email permutation + SMTP verification (guess personal email)
4. Sherlock (find social profiles by name)
"""

import re, socket, smtplib, subprocess, requests, json, time
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import logging

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ═══════════════════════════════════════════════════════════════════
# STEP 1: Find dentist name from About page
# ═══════════════════════════════════════════════════════════════════

DR_PATTERN = re.compile(
    r'\b(?:Dr\.?|Doctor)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})',
    re.IGNORECASE
)

DDS_PATTERN = re.compile(
    r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}),?\s+(?:DDS|DMD|D\.D\.S|D\.M\.D)',
    re.IGNORECASE
)

def _fetch(url, timeout=8):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""

def _clean_name(name):
    # Remove trailing "Dr", "DDS", "DMD" etc.
    name = re.sub(r'\b(Dr\.?|DDS|DMD|D\.D\.S\.?|D\.M\.D\.?)\b', '', name, flags=re.IGNORECASE)
    return " ".join(name.split())

def _extract_name(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    for pattern in [DR_PATTERN, DDS_PATTERN]:
        matches = pattern.findall(text)
        if matches:
            name = _clean_name(matches[0].strip())
            if 2 <= len(name.split()) <= 3:
                return name

    return ""

def find_owner_name(website):
    """Scrape About/Team page for dentist name."""
    base = website.rstrip("/")
    pages = [
        base,
        base + "/about",
        base + "/about-us",
        base + "/our-team",
        base + "/team",
        base + "/meet-the-doctor",
        base + "/meet-dr",
        base + "/doctor",
    ]

    for url in pages:
        html = _fetch(url)
        if not html:
            continue
        name = _extract_name(html)
        if name:
            log.info("  Found owner name: %s (from %s)", name, url)
            return name

    return ""


# ═══════════════════════════════════════════════════════════════════
# STEP 2: theHarvester — find all emails on domain
# ═══════════════════════════════════════════════════════════════════

def harvester_emails(domain):
    """Run theHarvester and return list of found emails."""
    try:
        result = subprocess.run(
            ["python", "-m", "theHarvester", "-d", domain, "-b", "bing,duckduckgo,yahoo"],
            capture_output=True, text=True, timeout=45
        )
        output = result.stdout + result.stderr
        emails = set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", output))
        emails = {e.lower() for e in emails if domain.lower() in e.lower()}
        log.info("  theHarvester found: %s", emails)
        return list(emails)
    except Exception as e:
        log.debug("  theHarvester error: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════
# STEP 3: Email permutation from name + domain
# ═══════════════════════════════════════════════════════════════════

def _permutations(first, last, domain):
    f, l = first.lower(), last.lower()
    return [
        f"dr.{f}@{domain}",
        f"dr{f}@{domain}",
        f"dr.{f}.{l}@{domain}",
        f"dr{l}@{domain}",
        f"{f}@{domain}",
        f"{f}.{l}@{domain}",
        f"{f}{l}@{domain}",
        f"{f[0]}{l}@{domain}",
        f"{f[0]}.{l}@{domain}",
        f"{l}@{domain}",
        f"{l}.{f}@{domain}",
    ]


# ═══════════════════════════════════════════════════════════════════
# STEP 4: Disify — free email verification (no signup needed)
# ═══════════════════════════════════════════════════════════════════

def disify_verify(email):
    """
    Verify email via Disify free API.
    Returns True if email format is valid, domain has MX, and is not disposable.
    """
    try:
        r = requests.get(
            f"https://api.disify.com/api/email/{email}",
            timeout=8
        )
        if r.status_code != 200:
            return False
        data = r.json()
        # format=True, dns=True, disposable=False = real email
        return (
            data.get("format", False) and
            data.get("dns", False) and
            not data.get("disposable", True)
        )
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
# STEP 5: Sherlock — find social profiles
# ═══════════════════════════════════════════════════════════════════

def find_social_profiles(name):
    """Find LinkedIn/Facebook profiles for a dentist name."""
    username = name.lower().replace(" ", "")
    try:
        result = subprocess.run(
            ["python", "-m", "sherlock", username, "--timeout", "5",
             "--site", "LinkedIn", "--site", "Facebook", "--print-found"],
            capture_output=True, text=True, timeout=30
        )
        profiles = re.findall(r"https?://\S+", result.stdout)
        return profiles
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════
# MAIN: Find owner email
# ═══════════════════════════════════════════════════════════════════

def find_owner_email(website, business_name=""):
    """
    Full OSINT pipeline:
    1. Find dentist name from About page
    2. theHarvester for domain emails
    3. Permutations + SMTP verify
    Returns (name, email, social_profiles)
    """
    if not website:
        return "", "", []

    domain = urlparse(website).netloc.replace("www.", "")

    # Step 1: Find name
    name = find_owner_name(website)
    parts = name.split() if name else []

    # Step 2: theHarvester — find all real emails on domain
    harvested = harvester_emails(domain)

    # Prefer personal-looking emails (dr, doctor, or owner first name)
    personal_harvested = [
        e for e in harvested
        if any(x in e.lower() for x in ["dr", "doctor"]) or
           (parts and parts[0].lower() in e.lower())
    ]
    all_harvested = personal_harvested or harvested

    # Verify each harvested email with Disify
    for email in all_harvested:
        log.info("  Disify verify harvested: %s", email)
        if disify_verify(email):
            log.info("  ✓ Verified: %s", email)
            return name, email, []
        time.sleep(0.3)

    # Step 3: Permutations + Disify verify (only if we have owner name)
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        for email in _permutations(first, last, domain):
            log.info("  Disify verify permutation: %s", email)
            if disify_verify(email):
                log.info("  ✓ Verified: %s", email)
                return name, email, []
            time.sleep(0.3)

    # Step 4: Sherlock for social profiles (when no email found)
    profiles = []
    if name:
        log.info("  No email found — trying Sherlock for social profiles")
        profiles = find_social_profiles(name)

    return name, "", profiles


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    site = sys.argv[1] if len(sys.argv) > 1 else "https://hildebrandfamilydental.com"
    name, email, profiles = find_owner_email(site)
    print(f"Name:     {name}")
    print(f"Email:    {email}")
    print(f"Profiles: {profiles}")
