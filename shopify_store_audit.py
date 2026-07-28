#!/usr/bin/env python3
"""
Shopify Store Audit Outreach Tool

Finds live Shopify stores, runs a free Google PageSpeed Insights (Lighthouse)
audit on each, and sends a personalized cold email pointing out a real,
verifiable performance/SEO issue with an offer to fix it.

Fully separate from shopify_outreach.py (agency outreach) — own data files,
own HTTP session, no shared state. Pure requests-based for discovery/audit/
scraping; Selenium is used only for the optional --formfill step.

Usage:
    python shopify_store_audit.py --collect   # Step 1: discover Shopify store URLs
    python shopify_store_audit.py --analyze   # Step 2: run PageSpeed audit on each
    python shopify_store_audit.py --scrape    # Step 3: find contact emails
    python shopify_store_audit.py --formfill  # Step 3b: auto-fill contact forms where no email exists
    python shopify_store_audit.py --send      # Step 4: send personalized audit emails
    python shopify_store_audit.py             # Run all steps in sequence
"""

import argparse
import csv
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
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — shares SENDER_EMAIL/PASSWORD/PROVIDER with shopify_outreach.py's .env
# ═══════════════════════════════════════════════════════════════════════════════

YOUR_NAME = os.getenv("YOUR_NAME", "Salman Farisi")
PORTFOLIO_LINKS = [
    "https://github.com/1salmanfarisi7-crypto/Store-2",
    "https://github.com/1salmanfarisi7-crypto/Shopify-Theme-1",
    "https://github.com/1salmanfarisi7-crypto/Store-3",
]

PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY", "")   # optional — works without one, just slower/rate-limited

DAILY_SEND_CAP = 100
SEND_DELAY = (30, 45)
SCRAPE_DELAY = (3, 8)
REQUEST_TIMEOUT = 20   # PageSpeed audits take longer than a normal page fetch

STORES_FILE = "stores.csv"
RESULTS_FILE = "results_stores.csv"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CONTACT_PATHS = [
    "/", "/contact", "/contact-us", "/pages/contact", "/pages/contact-us",
    "/about", "/pages/about-us",
]

EMAIL_PRIORITY = [
    "team", "studio", "enquiries", "contact", "info", "hello", "hi", "support",
]

# Performance thresholds used to decide whether a store is worth emailing at all
MAX_PERFORMANCE_SCORE_TO_CONTACT = 70   # only email stores with real, provable problems
MIN_LOAD_TIME_TO_CONTACT = 3.0          # seconds — Google's "good" threshold is ~2.5s

COMPANY_NAME = os.getenv("COMPANY_NAME", "Freelance / Independent")   # used for contact-form "company" fields

# Paths to check when looking for a fillable contact form (subset, most likely first)
FORM_PATHS = ["/pages/contact", "/contact", "/contact-us", "/"]

# Keywords used to spot contact-like links when scanning the homepage for real
# contact page URLs (catches non-standard paths like /pages/get-in-touch)
CONTACT_LINK_KEYWORDS = [
    "contact", "get-in-touch", "get in touch", "talk", "touch", "reach",
    "support", "help", "lets-talk", "let's talk", "say-hello", "customer-service",
]
MAX_DISCOVERED_LINKS = 6

FORM_FIELD_KEYWORDS = {
    "name": ["name", "fullname", "full-name", "full_name", "your-name", "yourname"],
    "email": ["email", "mail", "e-mail"],
    "company": ["company", "organisation", "organization", "business-name", "businessname"],
    "phone": ["phone", "mobile", "tel", "telephone"],
    "subject": ["subject", "topic"],
    "message": ["message", "comment", "msg", "body", "details", "enquiry", "inquiry", "tell-us"],
}
FILLABLE_FIELD_TYPES = {"name", "email", "company", "subject", "message"}

CAPTCHA_INDICATORS = ["recaptcha", "g-recaptcha", "h-captcha", "hcaptcha", "cf-turnstile", "captcha"]

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

# ─── SMTP (reused from the agency tool's .env) ───────────────────────────────
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "gmail")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")

SMTP_CONFIG = {
    "gmail": {"host": "smtp.gmail.com", "port": 587},
    "zoho":  {"host": "smtp.zoho.com",  "port": 587},
}

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# HTTP SESSION — separate from shopify_outreach.py's SESSION, no shared state
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


def _fetch(url: str, method: str = "GET", data: dict = None, timeout: int = REQUEST_TIMEOUT):
    try:
        if method == "POST":
            resp = SESSION.post(url, data=data, timeout=timeout, allow_redirects=True)
        else:
            resp = SESSION.get(url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SHOPIFY DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

SHOPIFY_MARKERS = [
    "cdn.shopify.com",
    "Shopify.theme",
    "shopify-checkout-api-token",
    "/cart.js",
    "x-shopid",
    "myshopify.com",
]


def is_shopify_store(url: str) -> bool:
    """Checks a URL's HTML + headers for Shopify platform fingerprints."""
    resp = _fetch(url)
    if not resp:
        return False
    haystack = (resp.text + " " + " ".join(resp.headers.values())).lower()
    return any(marker.lower() in haystack for marker in SHOPIFY_MARKERS)


def _to_domain(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        domain = parsed.netloc.lower().replace("www.", "").strip()
        return domain if "." in domain else ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — DISCOVER SHOPIFY STORES
# ═══════════════════════════════════════════════════════════════════════════════

# Broad commerce/niche search terms — deliberately NOT "shopify" so results are
# real merchant stores, not agencies or Shopify's own marketing pages.
NICHE_QUERIES = [
    "handmade candles shop online",
    "organic skincare store online",
    "boutique jewelry shop online",
    "custom pet accessories store",
    "sustainable clothing brand store",
    "artisan coffee shop online store",
    "yoga apparel online store",
    "home decor boutique online",
]

EXCLUDE_DOMAINS = {
    "shopify.com", "google.com", "bing.com", "duckduckgo.com", "amazon.com",
    "etsy.com", "ebay.com", "facebook.com", "instagram.com", "pinterest.com",
    "youtube.com", "walmart.com", "target.com", "wikipedia.org",
}


def collect_from_search() -> list:
    """
    Searches DuckDuckGo Lite + Bing for niche commerce terms, then verifies
    each result is an actual Shopify store before keeping it. Best-effort —
    both engines may rate-limit; failures are skipped gracefully.
    """
    seen_domains = set()
    results = []

    for query in NICHE_QUERIES:
        candidates = []

        # DuckDuckGo Lite
        ddg_resp = _fetch(f"https://lite.duckduckgo.com/lite/?q={query.replace(' ', '+')}")
        if ddg_resp:
            soup = BeautifulSoup(ddg_resp.text, "html.parser")
            for a in soup.select("a.result-link, span.link-text, td a[href^='http']"):
                url = a.get("href", "")
                if url:
                    candidates.append(url)

        # Bing (fallback / supplement)
        bing_resp = _fetch(f"https://www.bing.com/search?q={query.replace(' ', '+')}&count=20")
        if bing_resp:
            soup = BeautifulSoup(bing_resp.text, "html.parser")
            for li in soup.select("li.b_algo"):
                a = li.select_one("h2 a")
                if a and a.get("href"):
                    candidates.append(a["href"])

        verified_this_query = 0
        for url in candidates:
            domain = _to_domain(url)
            if not domain or domain in seen_domains or domain in EXCLUDE_DOMAINS:
                continue
            seen_domains.add(domain)

            if is_shopify_store(f"https://{domain}"):
                results.append({"domain": domain, "source": "search"})
                verified_this_query += 1

            time.sleep(random.uniform(1.5, 3))

        log.info("Query '%s': +%d verified Shopify stores", query, verified_this_query)
        time.sleep(random.uniform(8, 15))

    log.info("Search discovery: %d Shopify stores total", len(results))
    return results


# Known real Shopify stores — guaranteed baseline so the pipeline always has data
# even if search discovery is blocked. Mix of sizes; smaller/mid stores are more
# likely to have a fixable performance problem worth emailing about.
SEED_STORES = [
    "gymshark.com", "allbirds.com", "kyliecosmetics.com", "fashionnova.com",
    "colourpop.com", "mvmtwatches.com", "bombas.com", "brooklinen.com",
    "chubbiesshorts.com", "deathwishcoffee.com", "thirdlove.com", "knixteen.com",
    "tentree.com", "untuckit.com", "rumpl.com", "manscaped.com",
    "blendjet.com", "skinnydiplondon.com", "hauteandfree.com", "wearelovingyou.com",
    "obvi.com", "jacindasshop.com", "puravidabracelets.com", "spongelle.com",
    "beardbrand.com", "taylorstitch.com", "meundies.com", "bulletproof.com",
    "snowehome.com", "soylent.com", "vellabox.com", "graza.co",
    "liquiddeath.com", "magicspoon.com", "olipop.com", "feastables.com",
    "ridge.com", "wandererprints.com", "outdoorvoices.com", "verishop.com",
    # ── Batch 2 (push to 150) ───────────────────────────────────────────────
    # Beauty / skincare
    "glossier.com", "thrivecausemetics.com", "drunkelephant.com", "summerfridays.com",
    "biossance.com", "glowrecipe.com", "herbivorebotanicals.com", "meetmaude.com",
    "briogeohair.com", "supergoop.com",
    # Fashion / apparel
    "rowingblazers.com", "ministryofsupply.com", "cuyana.com", "vuoriclothing.com",
    "girlfriend.com", "wearpact.com", "american-giant.com", "unitedbyblue.com",
    "rhone.com", "westernrise.com",
    # Food / beverage
    "omsom.com", "flybyjing.com", "4505meats.com", "vitalproteins.com",
    "drinkpoppi.com", "takearecess.com",
    # Home goods
    "parachutehome.com", "buffy.co", "coyuchi.com", "bollandbranch.com",
    "fromourplace.com", "carawayhome.com", "greatjonesgoods.com",
    # Pet products
    "wildone.com", "barkbox.com", "meetbatch.com",
    # Fitness / wellness
    "thebalabar.com", "tenthousand.cc", "livemomentous.com",
    # Jewelry / accessories
    "mejuri.com", "catbirdnyc.com", "auratenewyork.com",
    # Kids / baby
    "meetlalo.com", "kytebaby.com",
    # Outdoor / travel
    "cotopaxi.com", "bellroy.com",
    # Coffee / tea
    "beanbox.com", "atlascoffeeclub.com",
    # Tech / gadgets
    "shopmoment.com", "peakdesign.com",
    # Supplements / wellness
    "seed.com", "careof.com",
    # ── Batch 3 (push to 150) ───────────────────────────────────────────────
    # Sleepwear / loungewear
    "lunya.co", "petiteplume.com",
    # Athletic / sportswear
    "tracksmith.com", "janji.com", "districtvision.com", "beyondyoga.com",
    # Sunglasses
    "diffeyewear.com",
    # Bags / luggage
    "dagnedover.com", "statebags.com", "baboontothemoon.com",
    # Watches
    "vincerocollective.com",
    # Candles / home fragrance
    "boysmellscandles.com", "otherland.com",
    # Plants
    "thesill.com", "bloomscape.com",
    # Kitchenware
    "materialkitchen.com",
    # Sustainable / eco
    "packagefreeshop.com", "blueland.com",
    # Kids
    "primary.com", "monicaandandy.com",
    # Wine / spirits
    "brightcellars.com",
    # Activewear
    "alalastyle.com",
    # Furniture
    "floydhome.com", "burrow.com",
    # Mattress / sleep
    "helixsleep.com",
    # Beauty (more)
    "functionofbeauty.com", "necessaire.com", "youthtothepeople.com",
    "iliabeauty.com", "kosas.com", "saiebeauty.com", "milkmakeup.com",
    # Fashion (more)
    "universalstandard.com", "quince.com",
    # Food (more)
    "partakefoods.com", "hukitchen.com", "caulipower.com", "sietefoods.com",
    # ── Batch 4 (push to 200) ───────────────────────────────────────────────
    # Men's fashion
    "buckmason.com", "huckberry.com", "mizzenandmain.com",
    # Footwear
    "vessi.com", "atoms.com", "koio.co", "greats.com",
    # Swimwear
    "andieswim.com", "summersalt.com",
    # Eyewear
    "paireyewear.com",
    # Cookware
    "madeincookware.com",
    # Outdoor / camping
    "nemoequipment.com",
    # Hair / personal care
    "prose.com",
    # Fitness
    "hyperice.com",
    # Baby / maternity
    "babylist.com",
    # Spirits
    "drinkhaus.com",
    # Tech accessories
    "casetify.com", "nativeunion.com",
    # Stationery
    "poketo.com", "baronfig.com",
    # Beauty (more)
    "glamnetic.com", "patrickta.com", "westmanatelier.com", "tower28beauty.com",
    # Fashion (more)
    "princesspolly.com", "edikted.com", "oakandfort.com", "bandier.com",
    # Food (more)
    "jenis.com", "milkbarstore.com", "brightland.co",
    # Activewear
    "cutsclothing.com",
    # Pet
    "fablepets.com",
    # Wellness
    "moonjuice.com",
    # Accessories
    "parkerclay.com", "senreve.com",
    # Kids
    "lovevery.com",
    # Jewelry
    "brilliantearth.com", "withclarity.com",
    # Sleepwear
    "eberjey.com",
    # ── Batch 5 (push to 200) ───────────────────────────────────────────────
    # Travel / luggage
    "monos.com", "july.com",
    # Bedding
    "coopsleepgoods.com",
    # Activewear
    "representclo.com",
    # Vitamins / supplements
    "ritual.com", "humnutrition.com",
    # Cleaning
    "grove.co", "branchbasics.com",
    # Denim
    "boyish.com", "dl1961.com", "imogeneandwillie.com",
    # Tea
    "artoftea.com", "piquelife.com",
    # Chocolate
    "compartes.com", "mastmarket.com",
    # Snacks
    "hippeas.com",
    # Candles
    "brooklyncandlestudio.com",
    # Fashion (more)
    "naadam.com", "richer-poorer.com",
    # Accessories (more)
    "clarev.com", "mansurgavriel.com",
    # Beauty (more)
    "versedskin.com", "roseinc.com", "meritbeauty.com",
    # Home (more)
    "cozyearth.com", "pigletinbed.com",
    # Beverages (more)
    "delacalle.co", "dirtylemon.com",
    # ── Batch 6 (push to 200) ───────────────────────────────────────────────
    # Watches
    "shinola.com",
    # Bags
    "wantlesessentiels.com", "sherpani.com",
    # Skincare tools
    "solawave.co",
    # Hair tools
    "t3micro.com",
    # Men's skincare
    "everymanjack.com", "geologie.com",
    # Plus size
    "eloquii.com",
    # Maternity
    "hatchcollection.com", "storq.com",
    # Kids
    "littlesleepies.com",
    # Yoga / meditation
    "manduka.com", "aloyoga.com",
    # Camping
    "gossamergear.com",
    # Running
    "banditrunning.com",
    # Wine accessories
    "govino.com",
    # Beauty (more)
    "truebotanicals.com", "osmia.com",
    # Food (more)
    "eatfishwife.com",
    # Fashion (more)
    "fahertybrand.com", "outerknown.com",
    # Mattress / sleep
    "saatva.com", "avocadogreenmattress.com",
    # ── Batch 7 (push to 200) ───────────────────────────────────────────────
    # Backpacks / bags
    "topodesigns.com",
    # Phone accessories
    "mous.co", "pelacase.com",
    # Cycling
    "rapha.cc",
    # Golf
    "malbongolf.com",
    # Sun care
    "vacation.inc", "coola.com",
    # Men's fashion
    "toddsnyder.com",
    # Socks / underwear
    "stance.com", "tommyjohn.com",
    # Activewear (more)
    "yearofours.com", "setactive.co",
    # Subscription
    "causebox.com",
    # Beverages (more)
    "drinkugly.com", "health-ade.com",
    # Beauty (more)
    "dieuxskin.com", "axiology.com",
    # Coffee gear
    "fellowproducts.com",
    # Kids (more)
    "tubbytodd.com",
    # Tech accessories (more)
    "twelvesouth.com",
    # Denim (more)
    "motherdenim.com",
    # Jewelry (more)
    "vrai.com",
    # ── Batch 8 (push to 200) ───────────────────────────────────────────────
    # Spices
    "diasporaco.com",
    # Sports nutrition
    "kachava.com", "legionathletics.com",
    # Essential oils
    "vitruvi.com",
    # Sunglasses (more)
    "priverevaux.com",
    # Perfume
    "dedcool.com", "phlur.com",
    # Men's grooming
    "dollarshaveclub.com",
    # Games
    "explodingkittens.com",
    # Food (more)
    "truff.com",
    # Skincare (more)
    "blume.com", "starface.world",
    # Haircare
    "theouai.com",
    # Bags (more)
    "baggu.com",
    # Shoes
    "rothys.com",
    # Fitness (more)
    "whoop.com",
    # Pet (more)
    "westpaw.com",
    # ── Batch 9 (push to 200) ───────────────────────────────────────────────
    # Water bottles
    "livelarq.com",
    # Home / rugs
    "ruggable.com",
    # Art prints
    "minted.com",
    # Vegan leather goods
    "mattandnat.com",
    # Food (more)
    "dailyharvest.com",
    # Beauty (more)
    "typology.com",
    # Denim (more)
    "agolde.com",
    # Coffee (more)
    "driftaway.coffee", "partnerscoffee.com",
    # Chocolate (more)
    "dandelionchocolate.com",
    # ── Batch 10 (push to 200) ──────────────────────────────────────────────
    # Hot sauce
    "yellowbirdfoods.com",
    # Meal kits
    "sakara.com",
    # Planners
    "pandaplanner.com",
    # Protein bars
    "perfectbar.com",
    # Travel
    "paravel.com", "ritualbeverageco.com",
    # Candles (more)
    "homesick.com",
    # Skincare (more)
    "herocosmetics.us",
    # ── Batch 11 (push to 200) ──────────────────────────────────────────────
    # Outdoor (more)
    "bigagnes.com",
    # Crafts
    "weareknitters.com",
    # Automotive care
    "chemicalguys.com",
    # Snack box
    "bokksu.com",
    # Candles (more)
    "paddywax.com",
    # Furniture (more)
    "article.com", "interiordefine.com",
]


def load_existing_stores() -> set:
    existing = set()
    if not os.path.exists(STORES_FILE):
        return existing
    with open(STORES_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row.get("domain", "").lower().strip()
            if d:
                existing.add(d)
    return existing


def collect_stores():
    """Collects store domains from the seed list + search discovery, deduplicates, saves."""
    existing = load_existing_stores()
    log.info("Already collected: %d domains in %s", len(existing), STORES_FILE)

    all_stores = [{"domain": d, "source": "seed"} for d in SEED_STORES]
    all_stores += collect_from_search()

    new_stores = []
    seen = set(existing)
    for store in all_stores:
        d = store["domain"].lower().strip()
        if d and d not in seen:
            seen.add(d)
            store["domain"] = d
            new_stores.append(store)

    if not new_stores:
        log.info("No new stores found.")
        return

    file_is_new = not os.path.exists(STORES_FILE) or os.path.getsize(STORES_FILE) == 0
    with open(STORES_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["domain", "source"])
        if file_is_new:
            writer.writeheader()
        writer.writerows(new_stores)

    log.info("Saved %d new stores to %s", len(new_stores), STORES_FILE)


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — PAGESPEED AUDIT
# ═══════════════════════════════════════════════════════════════════════════════

def get_pagespeed_data(url: str) -> dict:
    """
    Calls Google's free PageSpeed Insights API (Lighthouse-as-a-service).
    Returns dict with performance_score, seo_score, load_time_s, or None on failure.
    Works without an API key (rate-limited); pass PAGESPEED_API_KEY in .env for
    higher quota (~25k requests/day free).
    """
    params = {
        "url": url,
        "strategy": "mobile",
        "category": ["performance", "seo"],
    }
    if PAGESPEED_API_KEY:
        params["key"] = PAGESPEED_API_KEY

    try:
        resp = SESSION.get(
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
            params=params,
            timeout=60,   # Lighthouse audits genuinely take 20-40s server-side
        )
        if resp.status_code != 200:
            log.debug("PageSpeed API error %d for %s", resp.status_code, url)
            return None
        data = resp.json()
        lighthouse = data.get("lighthouseResult", {})
        categories = lighthouse.get("categories", {})
        audits = lighthouse.get("audits", {})

        perf_score = categories.get("performance", {}).get("score")
        seo_score = categories.get("seo", {}).get("score")
        load_time_ms = audits.get("speed-index", {}).get("numericValue") or \
            audits.get("largest-contentful-paint", {}).get("numericValue")

        if perf_score is None:
            return None

        return {
            "performance_score": round(perf_score * 100),
            "seo_score": round(seo_score * 100) if seo_score is not None else None,
            "load_time_s": round(load_time_ms / 1000, 1) if load_time_ms else None,
        }
    except Exception as e:
        log.debug("PageSpeed request failed for %s: %s", url, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — FIND CONTACT EMAIL (same approach as the agency tool)
# ═══════════════════════════════════════════════════════════════════════════════

_COMMON_TLDS = (
    "com|net|org|io|co|info|biz|us|uk|ca|au|de|fr|nl|in|me|app|shop|store|"
    "dev|ai|xyz|email|agency|studio|tech|online|group|company|design"
)
_EMAIL_RE = re.compile(
    rf"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.(?:{_COMMON_TLDS})(?![a-zA-Z])",
    re.IGNORECASE,
)
_OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*at\s*[\]\)]?\s*"
    r"([a-zA-Z0-9.\-]+)\s*[\[\(]?\s*dot\s*[\]\)]?\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)

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


def extract_emails(text: str) -> list:
    found = list(_EMAIL_RE.findall(text))
    for m in _OBFUSCATED_RE.finditer(text):
        found.append(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
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
    email_domain = email.split("@")[-1] if "@" in email else ""
    if domain not in email_domain and email_domain not in domain:
        return -1
    local = email.split("@")[0].lower()
    for i, keyword in enumerate(EMAIL_PRIORITY):
        if keyword in local:
            return i + 1
    return 0


def find_email(domain: str):
    all_emails = []
    for path in CONTACT_PATHS:
        if not robots_allowed(domain, path):
            continue
        resp = _fetch(f"https://{domain}{path}") or _fetch(f"http://{domain}{path}")
        if not resp:
            time.sleep(random.uniform(*SCRAPE_DELAY))
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if a["href"].lower().startswith("mailto:"):
                email = a["href"][7:].split("?")[0].split(",")[0].strip(" \t\r\n.,;\"'").lower()
                if "@" in email:
                    all_emails.append(email)
        # Strip <script>/<style> — their raw JS/JSON content has no whitespace
        # boundaries and produces garbage matches (e.g. JSON > escapes or
        # minified code bleeding into what looks like a local-part/TLD).
        for tag in soup(["script", "style"]):
            tag.decompose()
        all_emails += extract_emails(soup.get_text(" ", strip=True))
        if all_emails:
            break
        time.sleep(random.uniform(*SCRAPE_DELAY))

    if not all_emails:
        return None

    # Defense in depth: reject residual extraction artifacts.
    all_emails = [e for e in all_emails if 3 <= len(e.split("@")[0]) <= 64 and "u00" not in e.split("@")[0][:5]]
    if not all_emails:
        return None

    ranked = sorted(set(all_emails), key=lambda e: rank_email(e, domain), reverse=True)
    best = ranked[0]
    return best if rank_email(best, domain) >= 0 else None


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3b — AUTO-FILL CONTACT FORMS (Selenium) — same proven approach as the
# agency tool: driver health-check, required-field validation, CAPTCHA
# detection, success confirmation.
# ═══════════════════════════════════════════════════════════════════════════════

def make_chrome_driver():
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,1024")
    options.add_argument("--log-level=3")
    driver = webdriver.Chrome(options=options)
    # Without this, a single page with a hanging resource or heavy script
    # (live chat widgets, video backgrounds, etc.) can block driver.get()
    # for minutes — cap it so one bad site can't stall the whole run.
    driver.set_page_load_timeout(20)
    return driver


def _driver_is_alive(driver) -> bool:
    try:
        driver.execute_script("return 1")
        return True
    except Exception:
        return False


def discover_contact_links(driver, domain: str) -> list:
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
                continue
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


def _has_unfillable_required_field(form_element) -> bool:
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
                return True
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
                return True
        except Exception:
            continue
    return False


def _check_submission_success(driver, url_before: str) -> bool:
    time.sleep(2)
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    except Exception:
        return False
    if any(err in page_text for err in FORM_ERROR_INDICATORS):
        return False
    if driver.current_url != url_before:
        return True
    return any(success in page_text for success in FORM_SUCCESS_INDICATORS)


def fill_contact_form(driver, domain: str, subject: str, body: str) -> str:
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
                continue

            email_field = _find_form_field(form, "email")
            message_field = _find_form_field(form, "message")
            if not email_field or not message_field:
                continue
            if _has_unfillable_required_field(form):
                continue

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
    log.info("═══ STEP 3b: Auto-filling contact forms ═══")
    results = load_results()
    targets = [(d, r) for d, r in results.items() if r.get("status") == "no_email_found"]
    total = len(targets)

    if total == 0:
        log.info("Nothing to do — no domains with no_email_found status.")
        return

    log.info("Trying contact forms for %d stores...", total)
    driver = make_chrome_driver()
    submitted_count = 0
    unconfirmed_count = 0
    captcha_count = 0

    try:
        for i, (domain, row) in enumerate(targets, 1):
            store_name = domain.split(".")[0].replace("-", " ").title()
            pagespeed = {
                "performance_score": int(row["performance_score"]) if row.get("performance_score") else None,
                "seo_score": int(row["seo_score"]) if row.get("seo_score") else None,
                "load_time_s": float(row["load_time_s"]) if row.get("load_time_s") else None,
            }
            subject, body = build_audit_email(store_name, pagespeed)

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
                log.warning("  Unexpected error on %s: %s — recreating browser", domain, e)
                log_result(domain, row.get("performance_score"), row.get("seo_score"), row.get("load_time_s"), "", "form_fill_failed")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = make_chrome_driver()
                time.sleep(random.uniform(*SCRAPE_DELAY))
                continue

            perf, seo, load_time = row.get("performance_score"), row.get("seo_score"), row.get("load_time_s")
            if status == "submitted":
                log.info("  ✓ Form submitted (confirmed)")
                log_result(domain, perf, seo, load_time, "", "form_submitted")
                submitted_count += 1
            elif status == "submitted_unconfirmed":
                log.info("  ~ Form submitted (no confirmation detected)")
                log_result(domain, perf, seo, load_time, "", "form_submitted_unconfirmed")
                unconfirmed_count += 1
            elif status == "captcha_blocked":
                log.info("  — Blocked by CAPTCHA")
                log_result(domain, perf, seo, load_time, "", "form_fill_captcha")
                captcha_count += 1
            else:
                log.info("  — No usable form found")
                log_result(domain, perf, seo, load_time, "", "form_fill_no_form")

            time.sleep(random.uniform(*SCRAPE_DELAY))
    finally:
        driver.quit()

    log.info(
        "Form-fill done: %d confirmed, %d unconfirmed, %d captcha-blocked, out of %d domains",
        submitted_count, unconfirmed_count, captcha_count, total,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — BUILD + SEND PERSONALIZED AUDIT EMAIL
# ═══════════════════════════════════════════════════════════════════════════════

AUDIT_TEMPLATES = [
    {
        "subject": "Quick technical note on {store_name}'s site speed",
        "body": (
            "Hi {store_name} team,\n\n"
            "I ran a quick technical audit on your store using Google's PageSpeed Insights "
            "tool and noticed a couple of things that are likely costing you sales:\n\n"
            "{findings}\n\n"
            "Slow load times directly hurt mobile conversion rates and search rankings, so "
            "these are usually fixable without a full redesign.\n\n"
            "I'm a Shopify developer (OS 2.0, Liquid, Storefront/Admin APIs, performance "
            "optimization) who also runs my own Shopify store, so I understand both the "
            "technical and the business side. Happy to put together a short breakdown of "
            "exactly what's slowing things down — no obligation either way.\n\n"
            "A few recent projects:\n"
            "- {portfolio_1}\n"
            "- {portfolio_2}\n"
            "- {portfolio_3}\n\n"
            "Worth a quick look?\n\n"
            "Best,\n{your_name}\n\n"
            "(If you'd rather not hear from me again, just reply and let me know.)"
        ),
    },
    {
        "subject": "Found a fixable performance issue on {store_name}",
        "body": (
            "Hi {store_name} team,\n\n"
            "I came across your store and ran it through Google's PageSpeed Insights "
            "(Lighthouse) audit out of curiosity. Here's what came back:\n\n"
            "{findings}\n\n"
            "Both of those numbers directly affect conversion rate and SEO ranking, and "
            "they're usually quick wins — image optimization, app/script cleanup, theme "
            "tweaks — rather than a full rebuild.\n\n"
            "I build and optimize Shopify stores for a living (OS 2.0, Liquid, Storefront/"
            "Admin APIs, Core Web Vitals) and run my own store, so I've dealt with this "
            "exact problem before. Happy to send a short, free breakdown of what's slowing "
            "you down if useful.\n\n"
            "Recent work:\n"
            "- {portfolio_1}\n"
            "- {portfolio_2}\n"
            "- {portfolio_3}\n\n"
            "Let me know if you'd like the details.\n\n"
            "Best,\n{your_name}\n\n"
            "(Reply \"unsubscribe\" and I won't reach out again.)"
        ),
    },
]


def build_findings_text(pagespeed: dict) -> str:
    lines = []
    if pagespeed.get("load_time_s") is not None:
        lines.append(
            f"- Your homepage takes about {pagespeed['load_time_s']}s to become usable on "
            f"mobile (Google recommends under 2.5s for good conversion rates)"
        )
    if pagespeed.get("performance_score") is not None:
        lines.append(f"- Lighthouse performance score: {pagespeed['performance_score']}/100")
    if pagespeed.get("seo_score") is not None:
        lines.append(f"- Lighthouse SEO score: {pagespeed['seo_score']}/100")
    return "\n".join(lines)


def build_audit_email(store_name: str, pagespeed: dict) -> tuple:
    template = random.choice(AUDIT_TEMPLATES)
    findings = build_findings_text(pagespeed)
    subject = template["subject"].format(store_name=store_name)
    body = template["body"].format(
        store_name=store_name,
        findings=findings,
        your_name=YOUR_NAME,
        portfolio_1=PORTFOLIO_LINKS[0],
        portfolio_2=PORTFOLIO_LINKS[1],
        portfolio_3=PORTFOLIO_LINKS[2],
    )
    return subject, body


def send_email(to_address: str, subject: str, body: str) -> bool:
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        raise ValueError("SENDER_EMAIL and SENDER_PASSWORD must be set in .env before sending.")

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
        log.error("SMTP authentication failed — check SENDER_EMAIL/SENDER_PASSWORD in .env")
        return False
    except smtplib.SMTPException as e:
        log.error("SMTP error sending to %s: %s", to_address, e)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# RESULTS TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

RESULTS_FIELDS = [
    "domain", "performance_score", "seo_score", "load_time_s",
    "email", "status", "timestamp",
]
# status: analyzed | skipped_good_score | email_found | no_email_found | sent | failed


def load_results() -> dict:
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
    today = date.today().isoformat()
    return sum(
        1 for r in results.values()
        if r.get("status") == "sent" and r.get("timestamp", "").startswith(today)
    )


def log_result(domain, performance_score, seo_score, load_time_s, email, status):
    file_is_new = not os.path.exists(RESULTS_FILE) or os.path.getsize(RESULTS_FILE) == 0
    with open(RESULTS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_FIELDS)
        if file_is_new:
            writer.writeheader()
        writer.writerow({
            "domain": domain,
            "performance_score": performance_score if performance_score is not None else "",
            "seo_score": seo_score if seo_score is not None else "",
            "load_time_s": load_time_s if load_time_s is not None else "",
            "email": email,
            "status": status,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def load_stores() -> list:
    if not os.path.exists(STORES_FILE):
        log.error("%s not found. Run --collect first.", STORES_FILE)
        return []
    with open(STORES_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_collect():
    log.info("═══ STEP 1: Discovering Shopify stores ═══")
    collect_stores()


def run_analyze():
    log.info("═══ STEP 2: Running PageSpeed audits ═══")
    stores = load_stores()
    if not stores:
        return
    results = load_results()
    total = len(stores)

    for i, store in enumerate(stores, 1):
        domain = store.get("domain", "").lower().strip()
        if domain in results:
            log.info("[%d/%d] %s — already analyzed, skipping", i, total, domain)
            continue

        log.info("[%d/%d] %s — running PageSpeed audit...", i, total, domain)
        pagespeed = get_pagespeed_data(f"https://{domain}")

        if not pagespeed:
            log.info("  — Audit failed (site unreachable or not real Shopify store)")
            log_result(domain, None, None, None, "", "audit_failed")
            time.sleep(random.uniform(2, 4))
            continue

        perf = pagespeed["performance_score"]
        load_time = pagespeed.get("load_time_s") or 0
        is_bad_enough = perf <= MAX_PERFORMANCE_SCORE_TO_CONTACT or load_time >= MIN_LOAD_TIME_TO_CONTACT

        if is_bad_enough:
            log.info("  ✓ Real issue found: perf=%s load=%ss — worth contacting", perf, load_time)
            log_result(domain, perf, pagespeed.get("seo_score"), pagespeed.get("load_time_s"), "", "analyzed")
        else:
            log.info("  — Site performs well (perf=%s) — skipping, nothing honest to say", perf)
            log_result(domain, perf, pagespeed.get("seo_score"), pagespeed.get("load_time_s"), "", "skipped_good_score")

        time.sleep(random.uniform(2, 4))


def run_scrape():
    log.info("═══ STEP 3: Finding contact emails ═══")
    results = load_results()
    targets = [(d, r) for d, r in results.items() if r.get("status") == "analyzed"]
    total = len(targets)

    for i, (domain, row) in enumerate(targets, 1):
        log.info("[%d/%d] %s — searching for email...", i, total, domain)
        email = find_email(domain)

        perf, seo, load_time = row.get("performance_score"), row.get("seo_score"), row.get("load_time_s")
        if email:
            log.info("  ✓ Found: %s", email)
            log_result(domain, perf, seo, load_time, email, "email_found")
        else:
            log.info("  — No email found")
            log_result(domain, perf, seo, load_time, "", "no_email_found")

        time.sleep(random.uniform(*SCRAPE_DELAY))


def run_send():
    log.info("═══ STEP 4: Sending personalized audit emails ═══")
    results = load_results()
    sent_today = count_sent_today(results)
    to_send = [r for r in results.values() if r.get("status") == "email_found" and r.get("email")]
    total = len(to_send)

    log.info("Ready to send: %d | Sent today: %d | Daily cap: %d", total, sent_today, DAILY_SEND_CAP)
    if total == 0:
        log.info("Nothing to send. Run --scrape first.")
        return

    for i, row in enumerate(to_send, 1):
        if sent_today >= DAILY_SEND_CAP:
            log.info("Daily cap reached. Re-run tomorrow.")
            break

        domain = row["domain"]
        email = row["email"]
        store_name = domain.split(".")[0].replace("-", " ").title()
        pagespeed = {
            "performance_score": int(row["performance_score"]) if row.get("performance_score") else None,
            "seo_score": int(row["seo_score"]) if row.get("seo_score") else None,
            "load_time_s": float(row["load_time_s"]) if row.get("load_time_s") else None,
        }

        log.info("[%d/%d] %s — sending to %s...", i, total, domain, email)
        subject, body = build_audit_email(store_name, pagespeed)
        success = send_email(email, subject, body)
        status = "sent" if success else "failed"

        log_result(domain, row.get("performance_score"), row.get("seo_score"), row.get("load_time_s"), email, status)
        if success:
            sent_today += 1
            log.info("  [%d/%d] %s — SENT", i, total, domain)
        else:
            log.warning("  [%d/%d] %s — FAILED", i, total, domain)

        delay = random.uniform(*SEND_DELAY)
        time.sleep(delay)

    log.info("Done sending.")


def main():
    parser = argparse.ArgumentParser(description="Shopify Store Audit Outreach")
    parser.add_argument("--collect",  action="store_true", help="Discover Shopify store URLs")
    parser.add_argument("--analyze",  action="store_true", help="Run PageSpeed audits")
    parser.add_argument("--scrape",   action="store_true", help="Find contact emails")
    parser.add_argument("--formfill", action="store_true", help="Auto-fill contact forms where no email exists")
    parser.add_argument("--send",     action="store_true", help="Send personalized audit emails")
    args = parser.parse_args()

    run_all = not (args.collect or args.analyze or args.scrape or args.formfill or args.send)

    if args.collect or run_all:
        run_collect()
    if args.analyze or run_all:
        run_analyze()
    if args.scrape or run_all:
        run_scrape()
    if args.formfill or run_all:
        run_formfill()
    if args.send or run_all:
        run_send()

    log.info("Done.")


if __name__ == "__main__":
    main()
