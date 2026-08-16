#!/usr/bin/env python3
"""
Finds the dentist owner's name and personal email using:
1. About page scraping (find dentist name)
2. theHarvester (find all emails on domain)
3. Holehe (verify email by checking site registrations)
4. Social-Analyzer (find Facebook/Instagram/LinkedIn by name)
5. Sherlock (find social accounts by username)
"""

import re, socket, subprocess, requests, json, time, asyncio
from urllib.parse import urlparse
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
# STEP 4: Holehe — verify email by checking site registrations
# ═══════════════════════════════════════════════════════════════════

async def _holehe_check(email):
    """Run holehe async check — returns list of sites where email is registered."""
    try:
        import httpx
        from holehe.core import import_submodules, get_functions, default_checker
        modules   = import_submodules("holehe.modules")
        functions = get_functions(modules)
        client    = httpx.AsyncClient()
        out       = []
        await default_checker(email, functions, client, out)
        await client.aclose()
        return [r["name"] for r in out if r.get("exists")]
    except Exception as e:
        log.debug("  Holehe error: %s", e)
        return []

def holehe_verify(email):
    """Returns True if email is registered on at least one site."""
    try:
        sites = asyncio.run(_holehe_check(email))
        if sites:
            log.info("  Holehe found %s registered on: %s", email, sites[:3])
            return True
        return False
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
# STEP 5b: Social-Analyzer — find profiles by full name
# ═══════════════════════════════════════════════════════════════════

def social_analyzer_profiles(name):
    """Find social profiles using Social-Analyzer (better for full names)."""
    try:
        result = subprocess.run(
            ["python", "-m", "social_analyzer", "--query", name,
             "--platforms", "facebook,linkedin,instagram",
             "--output", "json", "--silent"],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        profiles = []
        for platform, info in data.items():
            if isinstance(info, dict) and info.get("url"):
                profiles.append(info["url"])
        return profiles
    except Exception:
        return []


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

    # Return harvested personal email directly (real, found by theHarvester)
    if all_harvested:
        log.info("  Using harvested email: %s", all_harvested[0])
        return name, all_harvested[0], []

    # Step 3: Permutations + Holehe verify
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        perms = _permutations(first, last, domain)

        for email in perms:
            log.info("  Holehe checking: %s", email)
            if holehe_verify(email):
                log.info("  ✓ Holehe confirmed: %s", email)
                return name, email, []

        # Holehe found nothing — return all permutations for bounce tracking
        log.info("  Holehe found nothing — returning permutations for bounce tracking")
        return name, perms[0], perms

    # Step 4: Social-Analyzer + Sherlock for social profiles
    profiles = []
    if name:
        log.info("  No email found — trying Social-Analyzer + Sherlock")
        profiles = social_analyzer_profiles(name)
        if not profiles:
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
