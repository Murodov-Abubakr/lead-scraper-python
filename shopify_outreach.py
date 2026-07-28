#!/usr/bin/env python3
"""
Shopify Agency Email Outreach Tool

Finds Shopify agency contact emails and sends personalized job applications.

Usage:
    python shopify_outreach.py --collect   # Step 1: collect agency websites
    python shopify_outreach.py --scrape    # Step 2: find contact emails via scraping
    python shopify_outreach.py --enrich    # Step 2b: find emails via Hunter.io + Apollo
    python shopify_outreach.py --formfill  # Step 2c: auto-fill contact forms (Selenium) where no email exists
    python shopify_outreach.py --send      # Step 3: send application emails
    python shopify_outreach.py             # Run all steps in sequence
"""

import argparse
import csv
import json
import logging
import os
import random
import re
import smtplib
import time
import urllib.robotparser
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.common.exceptions import WebDriverException, NoSuchElementException

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — edit these or override via .env
# ═══════════════════════════════════════════════════════════════════════════════

YOUR_NAME = os.getenv("YOUR_NAME", "Salman Farisi")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Freelance / Independent")   # used for contact-form "company" fields
YOUR_SKILLS = os.getenv(
    "YOUR_SKILLS",
    "Shopify OS 2.0, Liquid, JSON templates, Shopify Functions, "
    "Theme App Extensions, Storefront & Admin APIs, React, Next.js, Tailwind, Alpine.js",
)
PORTFOLIO_LINKS = [
    "https://github.com/1salmanfarisi7-crypto/Store-2",
    "https://github.com/1salmanfarisi7-crypto/Shopify-Theme-1",
    "https://github.com/1salmanfarisi7-crypto/Store-3",
]

DAILY_SEND_CAP = 100        # max emails sent per calendar day
SEND_DELAY = (30, 45)       # seconds between sends (min, max)
SCRAPE_DELAY = (3, 8)       # seconds between page fetches (min, max)
REQUEST_TIMEOUT = 15        # HTTP request timeout in seconds

AGENCIES_FILE = "agencies.csv"
RESULTS_FILE = "results.csv"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Pages to check on each agency domain when hunting for a contact email
CONTACT_PATHS = [
    "/",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/careers",
    "/jobs",
    "/hire-us",
    "/work-with-us",
    "/team",
    "/get-in-touch",
]

# Paths to check when looking for a fillable contact form (subset, most likely first)
FORM_PATHS = ["/contact", "/contact-us", "/get-in-touch", "/"]

# Keywords used to spot contact-like links when scanning the homepage for real
# contact page URLs (catches non-standard paths like /lets-talk, /start-a-project)
CONTACT_LINK_KEYWORDS = [
    "contact", "get-in-touch", "get in touch", "talk", "touch", "reach",
    "hire", "start", "quote", "connect", "enquire", "inquiry",
    "lets-talk", "let's talk", "lets-chat", "work-with", "say-hello",
    "consultation", "project",
]
MAX_DISCOVERED_LINKS = 6   # cap candidate pages per domain to keep runtime bounded

# Keywords used to identify form fields by name/id/placeholder/aria-label
# Order matters: more specific types (company, phone) are matched before
# generic ones to avoid e.g. "company" mistakenly filling a subject field.
FORM_FIELD_KEYWORDS = {
    "name": ["name", "fullname", "full-name", "full_name", "your-name", "yourname"],
    "email": ["email", "mail", "e-mail"],
    "company": ["company", "organisation", "organization", "business-name", "businessname"],
    "phone": ["phone", "mobile", "tel", "telephone"],
    "subject": ["subject", "topic"],
    "message": ["message", "comment", "msg", "body", "details", "enquiry", "inquiry", "tell-us"],
}

# Field types we know how to fill. A required field whose type isn't in this
# set (e.g. phone, or a dropdown/select we can't reason about) means we can't
# honestly complete the form, so we skip it rather than submit incomplete data.
FILLABLE_FIELD_TYPES = {"name", "email", "company", "subject", "message"}

# If any of these appear in a form's HTML, skip it — can't be solved programmatically
CAPTCHA_INDICATORS = ["recaptcha", "g-recaptcha", "h-captcha", "hcaptcha", "cf-turnstile", "captcha"]

# Phrases checked on the page after submit to confirm the form actually went through
FORM_SUCCESS_INDICATORS = [
    "thank you", "thanks for", "we'll be in touch", "we will be in touch",
    "message sent", "successfully sent", "successfully submitted",
    "we received your", "got your message", "submission received",
    "we'll get back to you", "will get back to you", "shortly",
]
FORM_ERROR_INDICATORS = [
    "required field", "please fill", "please complete", "this field is required",
    "invalid email", "error occurred", "please try again", "something went wrong",
]

# Email local-part keywords ordered lowest → highest preference
# rank_email() returns the index+1 so higher index = higher score
EMAIL_PRIORITY = [
    "team", "studio", "enquiries", "contact",
    "info", "hello", "hi", "hey",
    "careers", "jobs", "hiring", "work", "hr",
]

# ─── API ENRICHMENT ───────────────────────────────────────────────────────────
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")

# ─── SMTP ─────────────────────────────────────────────────────────────────────
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "gmail")   # "gmail" or "zoho"
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")      # App password, not account password

SMTP_CONFIG = {
    "gmail": {"host": "smtp.gmail.com", "port": 587},
    "zoho":  {"host": "smtp.zoho.com",  "port": 587},
}

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# HTTP SESSION
# ═══════════════════════════════════════════════════════════════════════════════

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",   # no "br" — requests can't auto-decode Brotli without an extra package
        "Connection": "keep-alive",
    })
    return s


SESSION = make_session()


def _fetch(url: str, method: str = "GET", data: dict = None) -> requests.Response:
    """Fetches a URL, returns Response or None on any error."""
    try:
        if method == "POST":
            resp = SESSION.post(url, data=data, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        else:
            resp = SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — COLLECT AGENCIES
# ═══════════════════════════════════════════════════════════════════════════════

def _to_domain(url: str) -> str:
    """Extracts bare domain from a URL. 'https://www.foo.com/bar' → 'foo.com'."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        domain = parsed.netloc.lower().replace("www.", "").strip()
        return domain if "." in domain else ""
    except Exception:
        return ""


def _walk_json(obj, results: list, seen: set, depth: int = 0):
    """Recursively searches a JSON structure for {name, website} agency pairs."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        name = (
            obj.get("name") or obj.get("title") or
            obj.get("companyName") or obj.get("company_name") or ""
        )
        url = (
            obj.get("website") or obj.get("websiteUrl") or obj.get("website_url") or
            obj.get("url") or obj.get("externalUrl") or obj.get("external_url") or ""
        )
        if name and url and "http" in url and name not in seen:
            domain = _to_domain(url)
            if domain and "shopify.com" not in domain:
                results.append({
                    "agency_name": name.strip(),
                    "domain": domain,
                    "source": "partner_directory",
                })
                seen.add(name)
        for v in obj.values():
            _walk_json(v, results, seen, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, results, seen, depth + 1)


def collect_from_partner_directory() -> list:
    """
    Scrapes partners.shopify.com/directory/agencies for agency names + website URLs.

    NOTE: This is a JavaScript SPA. A plain HTTP request returns the server-rendered
    shell which rarely contains agency data. The three strategies below attempt to
    extract data anyway. If all yield 0, use collect_from_clutch() or populate
    agencies.csv manually — both are more reliable.
    """
    log.info("Fetching Shopify Partner Directory...")
    agencies = []
    seen_names = set()
    base_url = "https://partners.shopify.com/directory/agencies"

    # Strategy 1: probe for a JSON API endpoint
    for api_url in [
        "https://partners.shopify.com/api/v1/directory/agencies",
        "https://partners.shopify.com/api/directory/agencies",
    ]:
        resp = _fetch(f"{api_url}?page=1")
        if resp and "application/json" in resp.headers.get("content-type", ""):
            log.info("Partner Directory API found at %s", api_url)
            page = 1
            while True:
                r = _fetch(f"{api_url}?page={page}")
                if not r:
                    break
                try:
                    data = r.json()
                except Exception:
                    break
                before = len(agencies)
                _walk_json(data, agencies, seen_names)
                if len(agencies) == before:
                    break
                page += 1
                time.sleep(random.uniform(*SCRAPE_DELAY))
            if agencies:
                log.info("Partner Directory: %d agencies via API", len(agencies))
                return agencies
            break

    # Strategy 2 & 3: parse __NEXT_DATA__ JSON + HTML cards from each page
    for page in range(1, 50):
        resp = _fetch(f"{base_url}?page={page}")
        if not resp:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        before = len(agencies)
        for script in soup.find_all("script"):
            src = script.string or ""
            if "__NEXT_DATA__" in (script.get("id") or "") or (
                len(src) > 200 and ("agencies" in src.lower() or "partners" in src.lower())
            ):
                try:
                    _walk_json(json.loads(src), agencies, seen_names)
                except (json.JSONDecodeError, TypeError):
                    pass
        cards = (
            soup.find_all("article") or
            soup.find_all("li", class_=re.compile(r"agency|partner|card", re.I)) or
            soup.find_all("div", class_=re.compile(r"agency|partner|card|listing", re.I))
        )
        for card in cards:
            heading = card.find(["h2", "h3", "h4"])
            name = heading.get_text(strip=True) if heading else ""
            url = next(
                (a["href"] for a in card.find_all("a", href=True)
                 if a["href"].startswith("http") and "shopify.com" not in a["href"]),
                ""
            )
            if name and url and name not in seen_names:
                domain = _to_domain(url)
                if domain:
                    agencies.append({"agency_name": name, "domain": domain, "source": "partner_directory"})
                    seen_names.add(name)
        if len(agencies) == before:
            break
        time.sleep(random.uniform(*SCRAPE_DELAY))

    log.info("Partner Directory: %d agencies found", len(agencies))
    return agencies


def collect_from_clutch() -> list:
    """
    Scrapes clutch.co/developers/shopify for Shopify agency listings.
    Clutch is server-rendered HTML — much more reliable than the Partner Directory SPA.
    Paginates through multiple pages until no new results are found.
    """
    log.info("Scraping Clutch.co for Shopify agencies...")
    agencies = []
    seen = set()

    for page in range(1, 20):
        url = f"https://clutch.co/developers/shopify?page={page}"
        resp = _fetch(url)
        if not resp:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        before = len(agencies)

        for card in soup.select("li.provider-list-item, li.directory-list-item, div.provider"):
            name_el = card.select_one("h3.company_info, h3 a, .company-name a, a.company_name")
            link_el = card.select_one("a.website-link, a[data-link_type='website'], .website a")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            url_raw = link_el.get("href", "") if link_el else ""
            # Clutch sometimes stores the real URL in a data attribute
            if not url_raw and link_el:
                url_raw = link_el.get("data-url", "")
            domain = _to_domain(url_raw) if url_raw else ""
            if name and domain and domain not in seen and "clutch.co" not in domain:
                seen.add(domain)
                agencies.append({"agency_name": name, "domain": domain, "source": "clutch"})

        if len(agencies) == before:
            break
        log.info("Clutch page %d: %d agencies so far", page, len(agencies))
        time.sleep(random.uniform(*SCRAPE_DELAY))

    log.info("Clutch: %d agencies found", len(agencies))
    return agencies


def collect_from_search() -> list:
    """
    Best-effort: tries DuckDuckGo Lite then Bing HTML for Shopify agency queries.

    IMPORTANT: Both may block requests. This yields bonus results only — do not
    rely on it as the primary source. Clutch is the reliable fallback.
    """
    queries = [
        "shopify development agency",
        "shopify plus agency ecommerce",
        "shopify theme development company",
        "hire shopify developer agency",
    ]

    EXCLUDE_DOMAINS = {
        "shopify.com", "google.com", "bing.com", "duckduckgo.com",
        "linkedin.com", "clutch.co", "upwork.com", "fiverr.com",
        "reddit.com", "twitter.com", "x.com", "facebook.com",
        "instagram.com", "youtube.com", "yelp.com", "goodfirms.co",
        "sortlist.com", "designrush.com", "bark.com", "expertise.com",
        "hubspot.com", "wordpress.com", "medium.com", "github.com",
        "indeed.com", "glassdoor.com", "trustpilot.com", "capterra.com",
        "clutch.co", "appfutura.com", "topdevelopers.co",
    }

    seen_domains = set()
    results = []

    for query in queries:
        found = 0

        # Try DuckDuckGo Lite (GET, less likely to be blocked than POST)
        ddg_resp = _fetch(
            f"https://lite.duckduckgo.com/lite/?q={query.replace(' ', '+')}&kl=us-en"
        )
        if ddg_resp:
            soup = BeautifulSoup(ddg_resp.text, "html.parser")
            for a in soup.select("a.result-link, span.link-text, td a[href^='http']"):
                url = a.get("href", "")
                name = a.get_text(strip=True)
                domain = _to_domain(url)
                if domain and domain not in seen_domains and domain not in EXCLUDE_DOMAINS:
                    seen_domains.add(domain)
                    results.append({"agency_name": name or domain, "domain": domain, "source": "search"})
                    found += 1

        # Fallback: Bing HTML search
        if found == 0:
            bing_resp = _fetch(
                f"https://www.bing.com/search?q={query.replace(' ', '+')}&count=30",
            )
            if bing_resp:
                soup = BeautifulSoup(bing_resp.text, "html.parser")
                for li in soup.select("li.b_algo"):
                    a = li.select_one("h2 a")
                    if not a:
                        continue
                    url = a.get("href", "")
                    name = a.get_text(strip=True)
                    domain = _to_domain(url)
                    if domain and domain not in seen_domains and domain not in EXCLUDE_DOMAINS:
                        seen_domains.add(domain)
                        results.append({"agency_name": name or domain, "domain": domain, "source": "search"})
                        found += 1

        log.info("Search '%s': +%d agencies", query, found)
        time.sleep(random.uniform(8, 15))

    log.info("Search scraping: %d agencies total", len(results))
    return results


# Well-known Shopify agencies — guaranteed seed list so the pipeline always has data
SEED_AGENCIES = [
    # ── Batch 1 (original) ────────────────────────────────────────────────────
    ("We Make Websites",          "wemakewebsites.com"),
    ("Pixel Union",               "pixelunion.net"),
    ("Ethercycle",                "ethercycle.com"),
    ("Pointer Creative",          "pointercreative.com"),
    ("Diff Agency",               "diffagency.com"),
    ("Eastside Co",               "eastsideco.com"),
    ("Velstar",                   "velstar.co.uk"),
    ("Underwaterpistol",          "underwaterpistol.com"),
    ("Swanky",                    "swankyagency.com"),
    ("Object Room",               "objectroom.com"),
    ("Arctic Grey",               "arcticgrey.com"),
    ("Brkfst.io",                 "brkfst.io"),
    ("Trellis Commerce",          "trellis.co"),
    ("Tomorrow Agency",           "tomorrow.agency"),
    ("Fuel Made",                 "fuelmade.com"),
    ("Elkfox",                    "elkfox.com"),
    ("Good as Gold",              "goodasgold.co.nz"),
    ("Blend Commerce",            "blendcommerce.com"),
    ("Charle Agency",             "charleagency.com"),
    ("Pluro",                     "pluro.com"),
    ("Space48",                   "space48.com"),
    ("Disco Labs",                "discolabs.com"),
    ("DigitlHaus",                "digitlhaus.com"),
    ("Rainy City Agency",         "rainycityagency.com"),
    ("Half Helix",                "halfhelix.com"),
    ("Shopify Experts Hub",       "shopifyexpertshub.com"),
    ("Konstructive",              "konstructive.co"),
    ("Thrive",                    "thriveweb.com.au"),
    ("Prismfly",                  "prismfly.com"),
    ("Lounge Lizard",             "loungelizard.com"),
    ("Electric Eye",              "electric.eye"),
    ("Nostra",                    "nostra.ai"),
    ("Logical Position",          "logicalposition.com"),
    ("1Digital Agency",           "1digitalagency.com"),
    ("SmartSites",                "smartsites.com"),
    ("Coalition Technologies",    "coalitiontechnologies.com"),
    ("Absolute Web",              "absoluteweb.com"),
    ("Inflow",                    "goinflow.com"),
    ("Avex Designs",              "avexdesigns.com"),
    ("Guerrilla Commerce",        "guerrillacommerce.com"),
    ("Agency Within",             "agencywithin.com"),
    ("Rock Paper Simple",         "rockpapersimple.com"),
    ("Skup",                      "skup.net"),
    ("Kurt Elster",               "kurtelster.com"),
    ("EcomExperts",               "ecomexperts.io"),
    # ── Batch 2 (US / Canada) ────────────────────────────────────────────────
    ("Blue Acorn iCi",            "blueacorn.com"),
    ("Corra",                     "corra.com"),
    ("Work and Co",               "workandco.com"),
    ("Jamersan",                  "jamersan.com"),
    ("Black Belt Commerce",       "blackbeltcommerce.com"),
    ("Sellry",                    "sellry.com"),
    ("PagePro",                   "pagepro.co"),
    ("The Shop Pad",              "theshoppad.com"),
    ("Bold Commerce",             "boldcommerce.com"),
    ("Elogic Commerce",           "elogic.co"),
    ("Maestrooo",                 "maestrooo.com"),
    ("Out of the Sandbox",        "outofthesandbox.com"),
    ("Archetype Themes",          "archetypethemes.co"),
    ("Webinopoly",                "webinopoly.com"),
    ("Tinloof",                   "tinloof.com"),
    ("Hulk Apps",                 "hulkapps.com"),
    ("Propel Commerce",           "propelcommerce.com"),
    ("Hawke Media",               "hawkemedia.com"),
    ("iamota",                    "iamota.com"),
    ("Commerce Pundit",           "commercepundit.com"),
    ("CartCoders",                "cartcoders.com"),
    ("Envision eCommerce",        "envisionecommerce.com"),
    ("QeRetail",                  "qeretail.com"),
    ("Mindsea",                   "mindsea.com"),
    # ── Batch 3 (UK) ─────────────────────────────────────────────────────────
    ("Statement Agency",          "statement.co.uk"),
    ("Kubix Media",               "kubixmedia.co.uk"),
    ("Sherpas Design",            "sherpas.co.uk"),
    ("Made by Shape",             "madebyshape.co.uk"),
    ("5874 Commerce",             "5874.io"),
    ("Nuanced Media",             "nuanced.co.uk"),
    ("Folio Group",               "folio.group"),
    # ── Batch 4 (Australia / NZ) ─────────────────────────────────────────────
    ("Gorilla 360",               "gorilla360.com.au"),
    ("Orange Digital",            "orangedigital.com.au"),
    ("Digital Darts",             "digitaldarts.com.au"),
    ("Humaan",                    "humaan.com.au"),
    ("Clean Canvas",              "cleancanvas.co.nz"),
    ("Moustache Republic",        "moustacherepublic.com"),
    # ── Batch 5 (Europe) ─────────────────────────────────────────────────────
    ("Nansen",                    "nansen.agency"),
    ("Reload",                    "reloaddk.com"),
    ("Woolman",                   "woolman.io"),
    ("Web and Craft",             "webandcraft.com"),
    ("Softloft",                  "softloft.io"),
    ("Dinarys",                   "dinarys.com"),
    ("Simtech Development",       "simtech.dev"),
    ("Cleveroad",                 "cleveroad.com"),
    ("Netguru",                   "netguru.com"),
    ("Miquido",                   "miquido.com"),
    ("EL Passion",                "elpassion.com"),
    ("Andersen Lab",              "andersenlab.com"),
    # ── Batch 6 (India / Asia) ───────────────────────────────────────────────
    ("Kellton Tech",              "kelltontech.com"),
    ("Konstant Infosolutions",    "konstantinfo.com"),
    ("Sparx IT Solutions",        "sparxitsolutions.com"),
    ("Brainvire",                 "brainvire.com"),
    ("Emizentech",                "emizentech.com"),
    ("Magneto IT Solutions",      "magnetoitsolutions.com"),
    ("Elsner Technologies",       "elsner.com"),
    ("ValueCoders",               "valuecoders.com"),
    ("Iflexion",                  "iflexion.com"),
    ("Ranosys",                   "ranosys.com"),
    ("Cynoinfotech",              "cynoinfotech.com"),
    ("Meetanshi",                 "meetanshi.com"),
    ("Webiators",                 "webiators.com"),
    ("Webkul",                    "webkul.com"),
    ("CriticalRiver",             "criticalriver.com"),
    ("Icoderz Solutions",         "icoderz.com"),
    ("Techtic Solutions",         "techtic.com"),
    ("Growexx",                   "growexx.com"),
    ("Successive Digital",        "successive.tech"),
    ("Rishabh Software",          "rishabhsoft.com"),
    ("Inexture Solutions",        "inexture.com"),
    ("Trigma Solutions",          "trigma.com"),
    ("Hyperlink InfoSystem",      "hyperlinkinfosystem.com"),
    ("Appinventiv",               "appinventiv.com"),
    ("Dev Technosys",             "devtechnosys.com"),
    ("Daffodil Software",         "daffodilsw.com"),
    ("Octal IT Solution",         "octalsoftware.com"),
    ("Magestore",                 "magestore.com"),
    ("Intellectsoft",             "intellectsoft.net"),
    # ── Batch 7 (US / Canada continued) ──────────────────────────────────────
    ("Common Thread Collective",  "commonthreadco.com"),
    ("Digital Silk",              "digitalsilk.com"),
    ("Pixel Cut Labs",            "pixelcutlabs.com"),
    ("Clean Commit",              "cleancommit.io"),
    ("Ignite Digital",            "ignitedigital.com"),
    ("Bemeir",                    "bemeir.com"),
    ("WebDew",                    "webdew.com"),
    ("Codup",                     "codup.co"),
    ("Rolustech",                 "rolustech.com"),
    ("Codeinspire",               "codeinspire.eu"),
    ("Huemor",                    "huemor.rocks"),
    ("Lounge Lizard NY",          "loungelizardny.com"),
    ("Forix Commerce",            "forixcommerce.com"),
    ("Inflow Commerce",           "inflowcommerce.com"),
    ("Growthsayers",              "growthsayers.com"),
    ("Perch Commerce",            "perchcommerce.com"),
    # ── Batch 8 (UK / Europe continued) ──────────────────────────────────────
    ("Reach Digital",             "reach-digital.nl"),
    ("iO Digital",                "iodigital.com"),
    ("Youwe",                     "youwe.nl"),
    ("Magebit",                   "magebit.com"),
    ("WDEVS",                     "wdevs.com"),
    ("Basecom",                   "basecom.de"),
    ("Youwe Agency",              "youwe.com"),
    ("Onepager",                  "onepager.com"),
    ("Rocketcode",                "rocketcode.com"),
    ("Cronit",                    "cronit.io"),
    # ── Batch 9 (India continued) ─────────────────────────────────────────────
    ("Bacancy Technology",        "bacancy.com"),
    ("Peerbits",                  "peerbits.com"),
    ("Matellio",                  "matellio.com"),
    ("AlakmalaK Technologies",    "alakmalak.com"),
    ("Pixlogix Infotech",         "pixlogix.com"),
    ("Vrinsoft",                  "vrinsoft.com"),
    ("WebGarh",                   "webgarh.com"),
    ("Fingent",                   "fingent.com"),
    ("ColorWhistle",              "colorwhistle.com"),
    ("iDesign iBuy",              "idesignibuy.com"),
    ("Binary Folks",              "binaryfolks.com"),
    ("Techuz InfoWeb",            "techuz.com"),
    ("Chetu",                     "chetu.com"),
    ("Sphinx Solutions",          "sphinx-solution.com"),
    ("Seasia Infotech",           "seasiainfotech.com"),
    ("IndyLogix Solutions",       "indylogix.com"),
    ("Mobisoft Infotech",         "mobisoftinfotech.com"),
    ("Arka Softwares",            "arkasoftwares.com"),
    ("Xicom Technologies",        "xicom.biz"),
    ("Softprodigy",               "softprodigy.com"),
    ("Volansys Technologies",     "volansys.com"),
    ("Andile Solutions",          "andilesolutions.com"),
    ("Evince Development",        "evincedev.com"),
    ("Echoinnovateit",            "echoinnovateit.com"),
    ("Solulab",                   "solulab.com"),
    ("WPWeb Infotech",            "wpwebinfotech.com"),
    ("Korona Studios",            "koronastudios.com"),
    # ── Batch 10 (Mixed / Global) ─────────────────────────────────────────────
    ("Sunrise Integration",       "sunriseintegration.com"),
    ("IntuitSolutions",           "intuitsolutions.net"),
    ("Accent Systems",            "accent-systems.com"),
    ("Appnovation",               "appnovation.com"),
    ("Croud",                     "croud.com"),
    ("Vaan Group",                "vaangroup.com"),
    ("Diff Agency CA",            "diff.agency"),
    ("Pixie Dust Web Design",     "pixiedustwebdesign.com"),
    ("Toptal Commerce",           "toptal.com"),
    ("Shopify Designers",         "shopifydesigners.co"),
    ("Agency Boon",               "agencyboon.com"),
    ("MindSea Development",       "mindsea.com"),
    ("Bramer",                    "bramer.co"),
    ("Accentuate",                "accentuate.io"),
    ("Wyde",                      "wyde.com"),
    ("Mango",                     "mango.am"),
    ("P3 Media",                  "p3media.com"),
    ("Fuel",                      "fuel.agency"),
    ("1903 Creative",             "1903creative.com"),
    ("Pixel Theory",              "pixeltheory.co"),
    # ── Batch 11 (final push) ─────────────────────────────────────────────────
    ("Webential",                 "webential.com"),
    ("Softcoded",                 "softcoded.com"),
    ("Innoraft",                  "innoraft.com"),
    ("Amigoways",                 "amigoways.com"),
    ("Ixora Solution",            "ixorasolution.com"),
    ("Alakmalak",                 "alakmalak.in"),
    ("Vxplore Technologies",      "vxplore.com"),
    ("Metizsoft Solutions",       "metizsoft.com"),
    ("Solwin Infotech",           "solwininfotech.com"),
    ("Zepto Systems",             "zeptosystems.com"),
    ("Infowind Technologies",     "infowindtech.com"),
    ("Samyak Infotech",           "samyakinfotech.com"),
    ("Sapphire Software",         "sapphiresolutions.net"),
    ("Nettechnocrats",            "nettechnocrats.com"),
    ("Techpanda",                 "techpanda.in"),
    ("Evrig Solutions",           "evrigsolutions.com"),
    ("Isyncsolutions",            "isyncsolutions.com"),
    ("Krify Software",            "krify.co"),
    ("Positiwise",                "positiwise.com"),
    ("Livetech Services",         "livetechservices.com"),
    ("Sagarinfotech",             "sagarinfotech.com"),
    ("Desss",                     "desss.com"),
    ("Dolphin Web Solution",      "dolphinwebsolution.com"),
    ("Finoit Technologies",       "finoit.com"),
    ("PixelCrayons",              "pixelcrayons.com"),
    ("Algoworks",                 "algoworks.com"),
    ("Zfort Group",               "zfort.com"),
    ("Diceus",                    "diceus.com"),
    ("Devox Software",            "devoxsoftware.com"),
    ("MLSDev",                    "mlsdev.com"),
    ("Requestum",                 "requestum.com"),
    ("Brocoders",                 "brocoders.com"),
    ("Akveo",                     "akveo.com"),
    ("Eleken",                    "eleken.co"),
    ("Agilie",                    "agilie.com"),
    ("Orangesoft",                "orangesoft.co"),
    ("Yalantis",                  "yalantis.com"),
    ("Boldare",                   "boldare.com"),
    ("Apptension",                "apptension.com"),
    ("STX Next",                  "stxnext.com"),
    # ── Batch 13 (Shopify/ecommerce-specific only — no generic dev shops) ────
    ("Carve Digital",             "carve.co.uk"),
    ("Vervaunt",                  "vervaunt.com"),
    ("Demac Media",               "demacmedia.com"),
    ("BVAccel",                   "bvaccel.com"),
    ("Speed Boostr",              "speedboostr.com"),
    ("Pilothouse",                "pilothouse.co"),
    ("NoGood",                    "nogood.io"),
    ("MuteSix",                   "mutesix.com"),
    ("Tier 11",                   "tier11.com"),
    ("Foxwell Digital",           "foxwelldigital.com"),
    ("Disruptive Advertising",    "disruptiveadvertising.com"),
    ("Geometric Box",             "geometricbox.com"),
    ("Bird Marketing",            "birdmarketing.co.uk"),
    ("Acart Communications",      "acartcommunications.com"),
    ("Salted Stone",              "saltedstone.com"),
    ("New Engen",                 "newengen.com"),
    ("Vital Design",              "vitaldesign.com"),
    ("Power Digital Marketing",   "powerdigitalmarketing.com"),
    ("Praella",                   "praella.com"),
    ("Visiture",                  "visiture.com"),
    ("Visual Soldiers",           "visualsoldiers.com"),
    ("KlientBoost",               "klientboost.com"),
    ("HawkSEM",                   "hawksem.com"),
    ("SmartBug Media",            "smartbugmedia.com"),
    # ── Batch 14 (more verified Shopify/ecommerce-specific) ───────────────────
    ("Vaimo",                     "vaimo.com"),
    ("Absolunet",                 "absolunet.com"),
    ("Groove Commerce",           "groovecommerce.com"),
    ("Blue Stout",                "bluestout.com"),
    ("Voy Media",                 "voymedia.com"),
    ("Single Grain",              "singlegrain.com"),
    ("Metric Digital",            "metricdigital.com"),
    ("Ladder",                    "ladder.io"),
    ("Thrive Internet Marketing", "thriveagency.com"),
    ("Helium SEO",                "heliumseo.com"),
    ("Cart.com",                  "cart.com"),
    ("Inviqa",                    "inviqa.com"),
    # ── Batch 15 (push to 100 — ecommerce-specific + global commerce practices) ──
    ("DEPT",                      "dept.com"),
    ("Born Group",                "borngroup.com"),
    ("Quirk",                     "quirk.biz"),
    ("Rogerwilco",                "rogerwilco.co.za"),
    ("Hoorah Digital",            "hoorahdigital.com"),
    ("Spiralytics",               "spiralytics.com"),
    ("Increnet",                  "increnet.com"),
    ("UENO",                      "ueno.co"),
    ("Studio Science",            "studioscience.com"),
    ("Big Spaceship",             "bigspaceship.com"),
    ("Instrument",                "instrument.com"),
    ("Critical Mass",             "criticalmass.com"),
    ("AKQA",                      "akqa.com"),
    ("Huge",                      "hugeinc.com"),
    ("Isobar",                    "isobar.com"),
    ("Mirum Agency",              "mirumagency.com"),
    ("Razorfish",                 "razorfish.com"),
    ("POSSIBLE",                  "possible.com"),
    ("iCrossing",                 "icrossing.com"),
    ("R/GA",                      "rga.com"),
    ("Publicis Sapient",          "publicissapient.com"),
    ("Code and Theory",           "codeandtheory.com"),
    ("Fantasy Interactive",       "fantasy.co"),
    ("Wunderman Thompson Commerce","wundermanthompson.com"),
    # More Shopify/ecommerce boutiques
    ("Switch",                    "switchagency.co"),
    ("Atomic Design",             "atomicdesign.co"),
    ("Carve",                     "carve.studio"),
    ("Volcanic",                  "volcanic.co.uk"),
    ("Add People",                "addpeople.co.uk"),
    ("Receptional",               "receptional.com"),
    ("93digital",                 "93digital.co.uk"),
    ("Hexagon",                   "hexagon-agency.com"),
    ("Storm Internet",            "storminternet.co.uk"),
    ("Adido",                     "adido-digital.co.uk"),
    ("Bopgun",                    "bopgun.com"),
    ("Boxed Pixels",              "boxedpixels.co.uk"),
    ("Bristol Web Agency",        "bristolwebagency.co.uk"),
    ("Box UK",                    "boxuk.com"),
    ("Code Computerlove",         "codecomputerlove.com"),
    ("Substrakt",                 "substrakt.com"),
    ("Appnova",                   "appnova.com"),
    ("Bigfork",                   "bigfork.co.uk"),
    ("Yard Digital",              "yard.agency"),
    ("Cyber-Duck",                "cyber-duck.co.uk"),
    ("Reflex Studios",            "reflexstudios.co.uk"),
    ("Visualsoft",                "visualsoft.co.uk"),
    ("Krystal Digital",           "krystal.io"),
    ("Indigoextra",               "indigoextra.com"),
    ("Cobwebsites",               "cobwebsites.com"),
    ("Webcetera",                 "webcetera.co.uk"),
    ("Vudu Digital",              "vududigital.co.uk"),
    ("Liverpool Web Designers",   "liverpoolwebdesigners.com"),
    ("Wired Engine",              "wiredengine.co.uk"),
    ("Yoma Brand",                "yomabrand.com"),
    ("Cocoonfx",                  "cocoonfx.com"),
    ("Whitespace",                "whitespace.co.uk"),
    ("Pixel Kicks",               "pixelkicks.co.uk"),
    ("Catalyst Studios",          "catalyststudios.co.uk"),
    ("Browser Media",             "browsermedia.co.uk"),
    ("Tug Agency",                "tugagency.com"),
    ("Bloom Agency",              "bloomagency.co.uk"),
    ("Conjura",                   "conjura.com"),
    ("Periscopix",                "periscopix.co.uk"),
    ("Forward3D",                 "forward3d.com"),
    ("Greenlight Digital",        "greenlightdigital.com"),
    ("Found",                     "found.co.uk"),
    ("Embryo",                    "embryo.com"),
    ("Rise at Seven",             "riseatseven.com"),
    ("Hallam",                    "hallaminternet.com"),
    ("Reflective Digital",        "reflectivedigital.co.uk"),
    ("Edit Agency",               "editagency.co.uk"),
    ("Impression",                "impression.co.uk"),
    ("Distinctly",                "distinctly.co.uk"),
    ("Crowd",                     "thisiscrowd.com"),
    ("Roast",                     "roast.studio"),
    # ── Batch 16 (push to 100 — fresh regions) ────────────────────────────────
    # Germany / Austria
    ("Dotsource",                 "dotsource.de"),
    ("Netz98",                    "netz98.de"),
    ("Brandung",                  "brandung.de"),
    ("Limesoda",                  "limesoda.com"),
    # Netherlands
    ("Valtech",                   "valtech.com"),
    ("Mirabeau",                  "mirabeau.nl"),
    ("ISM eCompany",              "ism-ecompany.nl"),
    ("Mobiquity",                 "mobiquity.com"),
    # Nordics
    ("Knowit",                    "knowit.se"),
    ("Geta",                      "geta.no"),
    # Ireland
    ("Granite Digital",           "granitedigital.ie"),
    # Italy
    ("BitBang",                   "bitbang.it"),
    # Singapore
    ("Hashmeta",                  "hashmeta.com"),
    # Poland / Czech
    ("10Clouds",                  "10clouds.com"),
    ("Selleo",                    "selleo.com"),
    ("The Software House",       "tsh.io"),
    ("Zaven",                     "zaven.co"),
    # US boutiques
    ("Fresh Consulting",          "freshconsulting.com"),
    ("Method",                    "method.com"),
    ("Rally Interactive",         "rallyinteractive.com"),
    # More UK/AU digital agencies with commerce practices
    ("Numiko",                    "numiko.com"),
    ("Bozboz",                    "bozboz.co.uk"),
    ("Bigdrop",                   "bigdropinc.com"),
    ("Codal",                     "codal.com"),
    ("Blue Fountain Media",       "bluefountainmedia.com"),
    ("MuteSix Studio",            "mutesixstudio.com"),
    ("Newcraft",                  "newcraft.com"),
    ("Spire Digital",             "spiredigital.com"),
    ("Pixc Studio",               "pixc.com"),
    ("Sleeve",                    "sleeve.com.au"),
    ("Reload Digital AU",         "reloaddigital.com.au"),
    ("Atomic 212",                "atomic212.com"),
    ("Resolution Digital",        "resolutiondigital.com.au"),
    ("Liquid Agency",             "liquidagency.com"),
    ("Multiverse Media",          "multiversemedia.com"),
    ("Vine Search Engine Marketing","vinesearch.com.au"),
    ("Datapult",                  "datapult.io"),
    ("8 Million Stories",         "8millionstories.com"),
    # ── Batch 17 (Canadian agencies + ecommerce consultancies + Shopify apps) ──
    ("Sundog Interactive",        "sundoginteractive.com"),
    ("Practicology",              "practicology.com"),
    ("Tryzens",                   "tryzens.com"),
    ("Jam3",                      "jam3.com"),
    ("Lg2",                       "lg2.com"),
    ("Sid Lee",                   "sidlee.com"),
    ("Cossette",                  "cossette.com"),
    ("Underground Industries",    "undergroundindustries.com"),
    ("Salmon",                    "salmon.com"),
    ("eCommera",                  "ecommera.com"),
    # Shopify app/platform companies (real support infrastructure)
    ("Shogun",                    "getshogun.com"),
    ("Vitals",                    "vitals.co"),
    ("PageFly",                   "pagefly.io"),
    ("GemPages",                  "gempages.net"),
    ("Tapcart",                   "tapcart.com"),
    ("Recharge",                  "rechargepayments.com"),
    ("Smile.io",                  "smile.io"),
    ("Gorgias",                   "gorgias.com"),
    ("Okendo",                    "okendo.io"),
    ("Loop Returns",              "loopreturns.com"),
    ("Postscript",                "postscript.io"),
    ("Zipify",                    "zipify.com"),
    ("Privy",                     "privy.com"),
    ("Justuno",                   "justuno.com"),
    ("AfterShip",                 "aftership.com"),
    ("ReConvert",                 "reconvert.io"),
    ("Foursixty",                 "foursixty.com"),
    ("LoyaltyLion",               "loyaltylion.com"),
    ("Stamped.io",                "stamped.io"),
    # ── Batch 18 (final agency push) ──────────────────────────────────────────
    ("Bear Group",                "beargroup.com"),
    ("Aeolidia",                  "aeolidia.com"),
    ("Outerbox",                  "outerboxdesign.com"),
    ("WebLinc",                   "weblinc.com"),
    ("9Sail",                     "9sail.com"),
]


def load_existing_agencies() -> set:
    """Returns the set of domains already saved in agencies.csv."""
    existing = set()
    if not os.path.exists(AGENCIES_FILE):
        return existing
    with open(AGENCIES_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row.get("domain", "").lower().strip()
            if d:
                existing.add(d)
    return existing


def collect_agencies():
    """
    Collects agencies from the seed list, Clutch, Partner Directory, and search.
    Deduplicates by domain and appends new entries to agencies.csv.
    Already-collected domains are skipped so the script is resumable.
    """
    existing = load_existing_agencies()
    log.info("Already collected: %d domains in %s", len(existing), AGENCIES_FILE)

    # Always start with the hardcoded seed list — guaranteed to have data
    all_agencies = [
        {"agency_name": name, "domain": domain, "source": "seed"}
        for name, domain in SEED_AGENCIES
    ]

    # Clutch is the most reliable scrape source
    all_agencies += collect_from_clutch()

    # Partner Directory (may yield 0 — that's OK)
    all_agencies += collect_from_partner_directory()

    # Search engines (best-effort bonus)
    all_agencies += collect_from_search()

    # Deduplicate against existing file and within this batch
    new_agencies = []
    seen = set(existing)
    for agency in all_agencies:
        d = agency["domain"].lower().strip()
        if d and d not in seen:
            seen.add(d)
            agency["domain"] = d
            new_agencies.append(agency)

    if not new_agencies:
        log.info("No new agencies found.")
        return

    file_is_new = not os.path.exists(AGENCIES_FILE) or os.path.getsize(AGENCIES_FILE) == 0
    with open(AGENCIES_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["agency_name", "domain", "source"])
        if file_is_new:
            writer.writeheader()
        writer.writerows(new_agencies)

    log.info("Saved %d new agencies to %s", len(new_agencies), AGENCIES_FILE)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — FIND EMAILS
# ═══════════════════════════════════════════════════════════════════════════════

# TLD must be from this known list with a non-letter boundary after it — an
# open-ended {2,} class would greedily swallow trailing text with no space
# separator (e.g. raw HTML/JS concatenation), producing garbage like
# "support@shop.comwe" or JSON > escapes bleeding into the local-part.
_COMMON_TLDS = (
    "com|net|org|io|co|info|biz|us|uk|ca|au|de|fr|nl|in|me|app|shop|store|"
    "dev|ai|xyz|email|agency|studio|tech|online|group|company|design"
)
_EMAIL_RE = re.compile(
    rf"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.(?:{_COMMON_TLDS})(?![a-zA-Z])",
    re.IGNORECASE,
)

# Matches obfuscated patterns: "hello [at] agency [dot] com"
_OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*at\s*[\]\)]?\s*"
    r"([a-zA-Z0-9.\-]+)\s*[\[\(]?\s*dot\s*[\]\)]?\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)

_robots_cache: dict = {}


def robots_allowed(domain: str, path: str) -> bool:
    """Returns True if robots.txt permits this user-agent to fetch the path."""
    if domain not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"https://{domain}/robots.txt")
        try:
            rp.read()
            _robots_cache[domain] = rp
        except Exception:
            _robots_cache[domain] = None   # assume allowed when robots.txt unreachable
    rp = _robots_cache[domain]
    return True if rp is None else rp.can_fetch(USER_AGENT, path)


def extract_emails(text: str) -> list:
    """
    Extracts email addresses from raw text.
    Handles standard format, common HTML entity encoding, and at/dot obfuscation.
    """
    found = list(_EMAIL_RE.findall(text))

    # Obfuscated: "hello [at] agency [dot] com"
    for m in _OBFUSCATED_RE.finditer(text):
        found.append(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")

    # HTML-entity encoded @ sign
    decoded = text.replace("&#64;", "@").replace("%40", "@")
    if decoded != text:
        found.extend(_EMAIL_RE.findall(decoded))

    seen, unique = set(), []
    for e in found:
        e = e.lower().strip(".,;\"'")
        if e not in seen and "@" in e:
            seen.add(e)
            unique.append(e)
    return unique


def rank_email(email: str, domain: str) -> int:
    """
    Scores an email for preference. Higher = better.
    Returns -1 for emails that don't belong to the target domain.
    """
    email_domain = email.split("@")[-1] if "@" in email else ""
    # Accept exact match or subdomain match
    if domain not in email_domain and email_domain not in domain:
        return -1
    local = email.split("@")[0].lower()
    for i, keyword in enumerate(EMAIL_PRIORITY):
        if keyword in local:
            return i + 1
    return 0   # valid but no keyword match


def find_email(domain: str) -> str:
    """
    Visits CONTACT_PATHS on the agency domain looking for a contact email.
    Returns the highest-ranked email found, or None.
    """
    all_emails = []

    for path in CONTACT_PATHS:
        if not robots_allowed(domain, path):
            log.debug("robots.txt blocks %s%s", domain, path)
            continue

        resp = _fetch(f"https://{domain}{path}") or _fetch(f"http://{domain}{path}")
        if not resp:
            time.sleep(random.uniform(*SCRAPE_DELAY))
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # mailto: links are the most reliable source
        for a in soup.find_all("a", href=True):
            if a["href"].lower().startswith("mailto:"):
                # Some sites list multiple comma-separated addresses in one
                # mailto: link — take the first and strip stray punctuation
                # that plain whitespace-only .strip() wouldn't catch.
                email = a["href"][7:].split("?")[0].split(",")[0].strip(" \t\r\n.,;\"'").lower()
                if "@" in email:
                    all_emails.append(email)

        # Strip <script>/<style> before extracting visible text — their content
        # (raw JS/JSON) has no whitespace boundaries and produces garbage
        # matches when scanned with the email regex (e.g. JSON > escapes
        # or minified code bleeding into what looks like a local-part/TLD).
        for tag in soup(["script", "style"]):
            tag.decompose()
        all_emails += extract_emails(soup.get_text(" ", strip=True))

        if all_emails:
            break   # found candidates — stop scanning further paths

        time.sleep(random.uniform(*SCRAPE_DELAY))

    if not all_emails:
        return None

    # Defense in depth: reject anything that still doesn't look like a real
    # email (sanity length checks catch any residual extraction artifacts).
    all_emails = [e for e in all_emails if 3 <= len(e.split("@")[0]) <= 64 and "u00" not in e.split("@")[0][:5]]
    if not all_emails:
        return None

    ranked = sorted(set(all_emails), key=lambda e: rank_email(e, domain), reverse=True)
    best = ranked[0]
    return best if rank_email(best, domain) >= 0 else None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — BUILD EMAIL
# ═══════════════════════════════════════════════════════════════════════════════

TEMPLATES = [
    # Variation A — original structure from the job-seeker's brief
    {
        "subject": "Shopify developer with 1 year hands-on experience",
        "body": (
            "Hi {agency_name} team,\n\n"
            "I'm a Shopify developer with a year of hands-on experience, including running my own "
            "Shopify store with real sales. I'm reaching out to see if you're looking for freelance "
            "or full-time developer support.\n\n"
            "My work covers Shopify OS 2.0, Liquid, JSON templates, sections and blocks, metafields, "
            "Shopify Functions, and theme app extensions, along with the Storefront and Admin APIs. "
            "On the frontend I work with React, Next.js, Tailwind, and Alpine.js, and I pay close "
            "attention to Core Web Vitals, Lighthouse scores, and accessibility when building or "
            "customising themes.\n\n"
            "A few recent projects:\n"
            "- {portfolio_1}\n"
            "- {portfolio_2}\n"
            "- {portfolio_3}\n\n"
            "Would you be open to a quick chat about any current or upcoming Shopify projects "
            "where I could help?\n\n"
            "Best,\n{your_name}"
        ),
    },
    # Variation B — agency name in the opening sentence
    {
        "subject": "Available Shopify developer — 1 year experience, real store background",
        "body": (
            "Hi {agency_name} team,\n\n"
            "I came across {agency_name} and wanted to reach out directly about potential "
            "freelance or full-time developer opportunities.\n\n"
            "I'm a Shopify developer with a year of hands-on experience — including building and "
            "running my own Shopify store with real sales. I work across the full Shopify stack: "
            "OS 2.0, Liquid, JSON templates, sections and blocks, metafields, Shopify Functions, "
            "theme app extensions, and the Storefront and Admin APIs. On the frontend I use React, "
            "Next.js, Tailwind, and Alpine.js, with strong attention to Core Web Vitals and "
            "accessibility.\n\n"
            "Recent projects:\n"
            "- {portfolio_1}\n"
            "- {portfolio_2}\n"
            "- {portfolio_3}\n\n"
            "Happy to share more — would a quick conversation make sense?\n\n"
            "Best,\n{your_name}"
        ),
    },
    # Variation C — lead with technical skills
    {
        "subject": "Shopify dev available — OS 2.0, Liquid, APIs, React",
        "body": (
            "Hi {agency_name} team,\n\n"
            "I'm a Shopify developer with hands-on experience across OS 2.0, Liquid, JSON "
            "templates, Shopify Functions, theme app extensions, and the Storefront and Admin APIs, "
            "plus React, Next.js, Tailwind, and Alpine.js on the frontend. I also run my own "
            "Shopify store, so I understand the merchant side as well as the technical side.\n\n"
            "I'm looking for freelance or full-time developer opportunities and thought {agency_name} "
            "might be a great fit. A few recent projects:\n"
            "- {portfolio_1}\n"
            "- {portfolio_2}\n"
            "- {portfolio_3}\n\n"
            "Would you be open to a brief chat about how I could support your team?\n\n"
            "Best,\n{your_name}"
        ),
    },
]


def build_email(agency_name: str) -> tuple:
    """
    Returns (subject, body) for a personalized application email.
    Randomly picks one of the three template variations.
    """
    template = random.choice(TEMPLATES)
    subject = template["subject"]
    body = template["body"].format(
        agency_name=agency_name,
        your_name=YOUR_NAME,
        portfolio_1=PORTFOLIO_LINKS[0],
        portfolio_2=PORTFOLIO_LINKS[1],
        portfolio_3=PORTFOLIO_LINKS[2],
    )
    return subject, body


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — SEND EMAIL
# ═══════════════════════════════════════════════════════════════════════════════

def send_email(to_address: str, subject: str, body: str) -> bool:
    """
    Sends a plain-text email via SMTP (Gmail or Zoho).
    Returns True on success, False on any SMTP error.

    Set EMAIL_PROVIDER, SENDER_EMAIL, and SENDER_PASSWORD in .env.
    Use an App Password, not your account password (see README).
    """
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        raise ValueError(
            "SENDER_EMAIL and SENDER_PASSWORD must be set in .env before sending."
        )

    cfg = SMTP_CONFIG.get(EMAIL_PROVIDER.lower(), SMTP_CONFIG["gmail"])

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{YOUR_NAME} <{SENDER_EMAIL}>"
    msg["To"] = to_address
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, to_address, msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP authentication failed — check SENDER_EMAIL and SENDER_PASSWORD in .env")
        return False
    except smtplib.SMTPException as e:
        log.error("SMTP error sending to %s: %s", to_address, e)
        return False
    except OSError as e:
        log.error("Network error sending to %s: %s — will retry on next run", to_address, e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# RESULTS TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

RESULTS_FIELDS = ["agency_name", "domain", "email", "status", "timestamp"]

# Possible status values written to results.csv:
#   email_found      — email scraped, not yet sent
#   no_email_found   — scraping complete, no email on site
#   sent             — application email sent successfully
#   failed           — SMTP send failed (can retry by editing status back to email_found)


def load_results() -> dict:
    """
    Returns {domain: latest_row} from results.csv.
    When a domain appears multiple times, the last row wins (latest status).
    """
    rows = {}
    if not os.path.exists(RESULTS_FILE):
        return rows
    with open(RESULTS_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row.get("domain", "").lower().strip()
            if d:
                rows[d] = row
    return rows


def count_sent_today(results: dict) -> int:
    """Counts rows with status='sent' and today's date in the timestamp."""
    today = date.today().isoformat()
    return sum(
        1 for r in results.values()
        if r.get("status") == "sent" and r.get("timestamp", "").startswith(today)
    )


def log_result(agency_name: str, domain: str, email: str, status: str):
    """Appends one row to results.csv. Creates the file with header if needed."""
    file_is_new = not os.path.exists(RESULTS_FILE) or os.path.getsize(RESULTS_FILE) == 0
    with open(RESULTS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_FIELDS)
        if file_is_new:
            writer.writeheader()
        writer.writerow({
            "agency_name": agency_name,
            "domain": domain,
            "email": email,
            "status": status,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2b — ENRICH WITH HUNTER.IO + APOLLO
# ═══════════════════════════════════════════════════════════════════════════════

def hunter_find_email(domain: str) -> str:
    """
    Queries Hunter.io Domain Search API for the most common email at a domain.
    Free plan: 25 searches/month.
    Returns the best email found or None.
    """
    if not HUNTER_API_KEY:
        return None
    try:
        resp = SESSION.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 5},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        emails = data.get("data", {}).get("emails", [])
        if not emails:
            return None
        # Prefer emails with type "generic" (contact@, info@, etc.) first
        generics = [e for e in emails if e.get("type") == "generic"]
        candidates = generics or emails
        # Sort by confidence score descending
        candidates.sort(key=lambda e: e.get("confidence", 0), reverse=True)
        return candidates[0].get("value")
    except Exception as e:
        log.debug("Hunter error for %s: %s", domain, e)
        return None


def apollo_find_email(domain: str, agency_name: str) -> str:
    """
    Queries Apollo.io People Search API for a decision-maker contact at the domain.
    Free plan: 75 credits/month. Uses API key in header (not URL param).
    Returns the first email found or None.
    """
    if not APOLLO_API_KEY:
        return None
    try:
        resp = SESSION.post(
            "https://api.apollo.io/api/v1/mixed_people/search",
            headers={
                "Content-Type": "application/json",
                "x-api-key": APOLLO_API_KEY,
            },
            json={
                "organization_domains": [domain],
                "person_titles": [
                    "CEO", "Founder", "Co-Founder", "Director",
                    "Head of", "Manager", "Owner", "Partner",
                ],
                "per_page": 5,
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        people = data.get("people", [])
        for person in people:
            email = person.get("email") or ""
            if email and "@" in email and "apollo.io" not in email:
                return email.lower()
        return None
    except Exception as e:
        log.debug("Apollo error for %s: %s", domain, e)
        return None


def run_enrich():
    """
    Runs Hunter.io then Apollo against every domain that previously returned
    no_email_found. Updates results.csv with any newly found emails.
    """
    if not HUNTER_API_KEY and not APOLLO_API_KEY:
        log.error("Set HUNTER_API_KEY and/or APOLLO_API_KEY in .env before running --enrich")
        return

    results = load_results()
    no_email_domains = [
        (d, r) for d, r in results.items()
        if r.get("status") == "no_email_found"
    ]
    total = len(no_email_domains)
    log.info("═══ STEP 2b: API enrichment for %d domains ═══", total)
    log.info("Hunter key: %s | Apollo key: %s",
             "set" if HUNTER_API_KEY else "missing",
             "set" if APOLLO_API_KEY else "missing")

    found_count = 0
    for i, (domain, row) in enumerate(no_email_domains, 1):
        agency_name = row.get("agency_name", domain)
        log.info("[%d/%d] %s — trying Hunter...", i, total, domain)

        email = hunter_find_email(domain)
        source = "hunter"

        if not email:
            log.info("  Hunter: nothing. Trying Apollo...")
            email = apollo_find_email(domain, agency_name)
            source = "apollo"

        if email:
            log.info("  ✓ Found via %s: %s", source, email)
            log_result(agency_name, domain, email, "email_found")
            found_count += 1
        else:
            log.info("  — Not found in either API")

        # Polite delay between API calls
        time.sleep(random.uniform(1.5, 3.0))

    log.info("Enrichment done: %d new emails found out of %d domains", found_count, total)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2c — AUTO-FILL CONTACT FORMS (Selenium)
# ═══════════════════════════════════════════════════════════════════════════════

def make_chrome_driver():
    """Creates a headless Chrome driver for form filling."""
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,1024")
    options.add_argument("--log-level=3")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(30)   # cap slow/hanging pages instead of stalling the whole run
    return driver


def _driver_is_alive(driver) -> bool:
    """Lightweight health check — returns False if the browser session is dead."""
    try:
        driver.execute_script("return 1")
        return True
    except Exception:
        return False


def discover_contact_links(driver, domain: str) -> list:
    """
    Visits the homepage and extracts internal links whose URL or link text
    suggests a contact page (catches non-standard paths like /lets-talk or
    /start-a-project that FORM_PATHS would miss). Falls back to FORM_PATHS
    if the homepage fails to load or no matching links are found.

    """
    try:
        driver.get(f"https://{domain}/")
    except Exception:
        return FORM_PATHS

    time.sleep(2)

    discovered = []
    try:
        links = driver.find_elements(By.TAG_NAME, "a")
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                text = (link.text or "").lower()
            except Exception:
                continue
            if not href or domain not in href:
                continue   # external or empty link
            combined = (href + " " + text).lower()
            if any(kw in combined for kw in CONTACT_LINK_KEYWORDS):
                path = urlparse(href).path or "/"
                if path not in discovered:
                    discovered.append(path)
            if len(discovered) >= MAX_DISCOVERED_LINKS:
                break
    except Exception:
        pass

    if "/" not in discovered:
        discovered.append("/")

    return discovered or FORM_PATHS


def _find_form_field(form_element, field_type: str):
    """Finds an input/textarea inside a form matching keywords for field_type."""
    keywords = FORM_FIELD_KEYWORDS[field_type]
    candidates = form_element.find_elements(By.TAG_NAME, "input") + \
        form_element.find_elements(By.TAG_NAME, "textarea")
    for el in candidates:
        try:
            input_type = (el.get_attribute("type") or "text").lower()
            if input_type in ("hidden", "checkbox", "radio", "submit", "button", "file"):
                continue
            attrs = " ".join([
                el.get_attribute("name") or "",
                el.get_attribute("id") or "",
                el.get_attribute("placeholder") or "",
                el.get_attribute("aria-label") or "",
            ]).lower()
            if any(kw in attrs for kw in keywords):
                return el
        except Exception:
            continue
    return None


def _check_submission_success(driver, url_before: str) -> bool:
    """
    After clicking submit, checks for evidence the form actually went through:
    a URL change (redirect to a thank-you page) or a success phrase appearing
    on the page. Returns False if an error phrase is found or no signal exists.
    """
    time.sleep(2)   # allow redirect / AJAX response to land

    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        return False

    if any(err in page_text for err in FORM_ERROR_INDICATORS):
        return False

    if driver.current_url != url_before:
        return True   # redirected to a different page, likely a thank-you page

    return any(success in page_text for success in FORM_SUCCESS_INDICATORS)


def _has_unfillable_required_field(form_element) -> bool:
    """
    Returns True if the form has a required field we can't honestly fill —
    a phone number, a dropdown/select, or anything outside FILLABLE_FIELD_TYPES.
    Submitting without it would either fail validation or force fabricating
    data, so such forms are skipped entirely rather than submitted incomplete.
    """
    try:
        elements = (
            form_element.find_elements(By.TAG_NAME, "input") +
            form_element.find_elements(By.TAG_NAME, "textarea") +
            form_element.find_elements(By.TAG_NAME, "select")
        )
    except Exception:
        return False

    for el in elements:
        try:
            input_type = (el.get_attribute("type") or "text").lower()
            if input_type in ("hidden", "checkbox", "radio", "submit", "button", "file"):
                continue
            is_required = (
                el.get_attribute("required") is not None or
                (el.get_attribute("aria-required") or "").lower() == "true"
            )
            if not is_required:
                continue

            if el.tag_name.lower() == "select":
                return True   # can't honestly choose a dropdown option

            attrs = " ".join([
                el.get_attribute("name") or "",
                el.get_attribute("id") or "",
                el.get_attribute("placeholder") or "",
                el.get_attribute("aria-label") or "",
            ]).lower()

            matched_type = next(
                (ft for ft, kws in FORM_FIELD_KEYWORDS.items() if any(kw in attrs for kw in kws)),
                None,
            )
            if matched_type not in FILLABLE_FIELD_TYPES:
                return True   # required field we can't fill (phone, unrecognized, etc.)
        except Exception:
            continue

    return False


def fill_contact_form(driver, domain: str, subject: str, body: str) -> str:
    """
    Discovers likely contact page URLs from the homepage's links (falling back to
    FORM_PATHS), then tries to fill + submit a contact form on each.
    Returns one of: "submitted", "submitted_unconfirmed", "captcha_blocked", "no_form_found".
    Skips forms protected by CAPTCHA — those cannot be solved programmatically.
    "submitted" means a success phrase or redirect was detected after submit;
    "submitted_unconfirmed" means the click succeeded but no confirmation signal
    was found, so the submission may or may not have actually gone through.
    """
    captcha_seen = False
    paths_to_try = discover_contact_links(driver, domain)

    for path in paths_to_try:
        url = f"https://{domain}{path}"
        try:
            driver.get(url)
        except Exception as e:
            log.debug("Page load failed for %s%s: %s", domain, path, e)
            continue

        time.sleep(3)

        # Page-wide CAPTCHA check (reCAPTCHA/hCaptcha often render as an iframe
        # that sits outside the <form> element itself)
        try:
            iframe_srcs = " ".join(
                (f.get_attribute("src") or "") for f in driver.find_elements(By.TAG_NAME, "iframe")
            ).lower()
            if any(ind in iframe_srcs for ind in CAPTCHA_INDICATORS):
                captcha_seen = True
        except Exception:
            pass

        try:
            forms = driver.find_elements(By.TAG_NAME, "form")
        except Exception:
            continue

        for form in forms:
            try:
                form_html = (form.get_attribute("outerHTML") or "").lower()
            except Exception:
                continue

            if any(ind in form_html for ind in CAPTCHA_INDICATORS):
                captcha_seen = True
                continue   # cannot solve CAPTCHA — skip this form

            email_field = _find_form_field(form, "email")
            message_field = _find_form_field(form, "message")
            if not email_field or not message_field:
                continue   # not a real contact form

            if _has_unfillable_required_field(form):
                continue   # required phone/dropdown/unknown field — can't honestly complete this form

            name_field = _find_form_field(form, "name")
            subject_field = _find_form_field(form, "subject")
            company_field = _find_form_field(form, "company")

            try:
                if name_field:
                    name_field.clear()
                    name_field.send_keys(YOUR_NAME)
                email_field.clear()
                email_field.send_keys(SENDER_EMAIL)
                if company_field:
                    company_field.clear()
                    company_field.send_keys(COMPANY_NAME)
                if subject_field:
                    subject_field.clear()
                    subject_field.send_keys(subject)
                message_field.clear()
                message_field.send_keys(body)

                url_before = driver.current_url
                submit_btn = form.find_element(
                    By.CSS_SELECTOR, "button[type='submit'], input[type='submit'], button"
                )
                submit_btn.click()
                return "submitted" if _check_submission_success(driver, url_before) else "submitted_unconfirmed"
            except Exception as e:
                log.debug("Form fill failed on %s%s: %s", domain, path, e)
                continue

    return "captcha_blocked" if captcha_seen else "no_form_found"


def run_formfill():
    """
    Runs Selenium against every domain with status no_email_found (or still
    no_email_found after --enrich) and tries to submit the application via
    their contact form instead. Updates results.csv with status form_submitted,
    captcha_blocked, no_form_found, or formfill_failed.
    """
    results = load_results()
    targets = [
        (d, r) for d, r in results.items()
        if r.get("status") == "no_email_found"
    ]
    total = len(targets)
    log.info("═══ STEP 2c: Auto-filling contact forms for %d domains ═══", total)

    if total == 0:
        log.info("Nothing to do — no domains with no_email_found status.")
        return

    driver = make_chrome_driver()
    submitted_count = 0
    unconfirmed_count = 0
    captcha_count = 0
    try:
        for i, (domain, row) in enumerate(targets, 1):
            agency_name = row.get("agency_name", domain)
            subject, body = build_email(agency_name)

            # Proactive health check — a long-running headless Chrome session can
            # crash silently; without this, a dead browser causes every subsequent
            # domain to be misreported as "no usable form found" instead of retried.
            if not _driver_is_alive(driver):
                log.warning("  Browser session died — recreating before continuing")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = make_chrome_driver()

            log.info("[%d/%d] %s — trying contact form...", i, total, domain)
            try:
                status = fill_contact_form(driver, domain, subject, body)
            except Exception as e:
                log.warning("  Unexpected error on %s: %s — recreating browser and continuing", domain, e)
                log_result(agency_name, domain, "", "form_fill_failed")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = make_chrome_driver()
                time.sleep(random.uniform(*SCRAPE_DELAY))
                continue

            if status == "submitted":
                log.info("  ✓ Form submitted (confirmed)")
                log_result(agency_name, domain, "", "form_submitted")
                submitted_count += 1
            elif status == "submitted_unconfirmed":
                log.info("  ~ Form submitted (no confirmation detected)")
                log_result(agency_name, domain, "", "form_submitted_unconfirmed")
                unconfirmed_count += 1
            elif status == "captcha_blocked":
                log.info("  — Blocked by CAPTCHA")
                log_result(agency_name, domain, "", "form_fill_captcha")
                captcha_count += 1
            elif status == "no_form_found":
                log.info("  — No usable form found")
                log_result(agency_name, domain, "", "form_fill_no_form")
            else:
                log.info("  — %s", status)
                log_result(agency_name, domain, "", "form_fill_failed")

            time.sleep(random.uniform(*SCRAPE_DELAY))
    finally:
        driver.quit()

    log.info(
        "Form-fill done: %d confirmed, %d unconfirmed, %d captcha-blocked, out of %d domains",
        submitted_count, unconfirmed_count, captcha_count, total,
    )


def load_agencies() -> list:
    if not os.path.exists(AGENCIES_FILE):
        log.error(
            "%s not found. Run 'python shopify_outreach.py --collect' first, "
            "or manually create the file with columns: agency_name, domain, source",
            AGENCIES_FILE,
        )
        return []
    with open(AGENCIES_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_collect():
    log.info("═══ STEP 1: Collecting agency websites ═══")
    collect_agencies()


def run_scrape():
    log.info("═══ STEP 2: Finding contact emails ═══")
    agencies = load_agencies()
    if not agencies:
        return

    results = load_results()
    total = len(agencies)

    for i, agency in enumerate(agencies, 1):
        domain = agency.get("domain", "").lower().strip()
        agency_name = agency.get("agency_name", domain)

        if domain in results:
            log.info("[%d/%d] %s — already processed, skipping", i, total, domain)
            continue

        log.info("[%d/%d] %s — searching for email...", i, total, domain)
        email = find_email(domain)

        if email:
            log.info("  ✓ Found: %s", email)
            log_result(agency_name, domain, email, "email_found")
        else:
            log.info("  — No email found")
            log_result(agency_name, domain, "", "no_email_found")

        time.sleep(random.uniform(*SCRAPE_DELAY))


def run_send():
    log.info("═══ STEP 3: Sending application emails ═══")
    results = load_results()
    sent_today = count_sent_today(results)

    to_send = [
        r for r in results.values()
        if r.get("status") == "email_found" and r.get("email")
    ]
    total = len(to_send)

    log.info(
        "Ready to send: %d | Sent today: %d | Daily cap: %d",
        total, sent_today, DAILY_SEND_CAP,
    )

    if total == 0:
        log.info("Nothing to send. Run --scrape first to find emails.")
        return

    sent_count = 0
    for i, row in enumerate(to_send, 1):
        if sent_today >= DAILY_SEND_CAP:
            log.info(
                "Daily cap of %d reached. Re-run tomorrow to continue.",
                DAILY_SEND_CAP,
            )
            break

        agency_name = row["agency_name"]
        domain = row["domain"]
        email = row["email"]

        log.info("[%d/%d] %s — sending to %s...", i, total, domain, email)

        subject, body = build_email(agency_name)
        success = send_email(email, subject, body)
        status = "sent" if success else "failed"

        log_result(agency_name, domain, email, status)

        # Update in-memory dict so we don't double-send within the same run
        results[domain] = {**row, "status": status, "timestamp": datetime.now().isoformat()}

        if success:
            sent_today += 1
            sent_count += 1
            log.info("  [%d/%d] %s — SENT", i, total, domain)
        else:
            log.warning("  [%d/%d] %s — FAILED", i, total, domain)

        delay = random.uniform(*SEND_DELAY)
        log.info("  Waiting %.0fs...", delay)
        time.sleep(delay)

    log.info("Sent %d emails this run (%d today total).", sent_count, sent_today)


def main():
    parser = argparse.ArgumentParser(
        description="Shopify Agency Email Outreach — find agencies, scrape emails, send applications."
    )
    parser.add_argument("--collect",  action="store_true", help="Collect agency websites")
    parser.add_argument("--scrape",   action="store_true", help="Find contact emails via scraping")
    parser.add_argument("--enrich",   action="store_true", help="Find emails via Hunter.io + Apollo")
    parser.add_argument("--formfill", action="store_true", help="Auto-fill contact forms (Selenium) where no email exists")
    parser.add_argument("--send",     action="store_true", help="Send application emails")
    args = parser.parse_args()

    run_all = not (args.collect or args.scrape or args.enrich or args.formfill or args.send)

    if args.collect or run_all:
        run_collect()
    if args.scrape or run_all:
        run_scrape()
    if args.enrich:
        run_enrich()
    if args.formfill or run_all:
        run_formfill()
    if args.send or run_all:
        run_send()

    if run_all:
        results = load_results()
        emails_sent = sum(1 for r in results.values() if r.get("status") == "sent")
        forms_submitted = sum(
            1 for r in results.values()
            if r.get("status") in ("form_submitted", "form_submitted_unconfirmed")
        )
        log.info(
            "═══ Combined outreach total: %d emails sent + %d forms submitted = %d applications ═══",
            emails_sent, forms_submitted, emails_sent + forms_submitted,
        )

    log.info("Done.")


if __name__ == "__main__":
    main()
