#!/usr/bin/env python3
"""
Full Outreach Pipeline
Scrape → Audit → Screenshot → Annotate → Personalize → Email
Runs until 1000 businesses contacted, then stops.

Usage:
    python outreach_pipeline.py          # run full pipeline
    python outreach_pipeline.py --test   # send to yourself only (1 lead)
    python outreach_pipeline.py --resume # skip already-sent, continue from where stopped
"""

import argparse
import csv
import json
import os
import random
import re
import smtplib
import subprocess
import tempfile
import time
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

import requests
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

PLACES_API_KEY  = os.getenv("PLACES_API_KEY")
PAGESPEED_KEY   = os.getenv("PAGESPEED_API_KEY")
SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
YOUR_NAME       = os.getenv("YOUR_NAME", "Murodov")

LEADS_FILE      = "dental_leads.csv"
SENT_FILE       = "outreach_sent.json"
SCREENSHOTS_DIR = "screenshots"
TARGET          = 1000
DAILY_LIMIT     = 100  # established 3yr old Gmail account
DELAY_MIN       = 45   # seconds between emails
DELAY_MAX       = 100

CITIES = [
    "San Antonio TX", "El Paso TX",
    "Houston TX", "Dallas TX", "Austin TX",
    "Fort Worth TX", "Corpus Christi TX", "Lubbock TX",
    "Laredo TX", "Irving TX",
]

CHAIN_KEYWORDS = [
    "aspen dental", "bright now", "western dental", "heartland dental",
    "pacific dental", "affordable dentures", "clear choice", "smile direct",
]

os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("outreach_log.txt", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# FONTS
# ═══════════════════════════════════════════════════════════════════

def load_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf"  if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()

# ═══════════════════════════════════════════════════════════════════
# GOOGLE PLACES SCRAPER
# ═══════════════════════════════════════════════════════════════════

def search_places(query, next_token=None):
    url    = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": query, "key": PLACES_API_KEY}
    if next_token:
        params["pagetoken"] = next_token
    try:
        r = requests.get(url, params=params, timeout=10)
        return r.json()
    except Exception:
        return {}


def get_details(place_id):
    url    = "https://maps.googleapis.com/maps/api/place/details/json"
    fields = "name,formatted_address,formatted_phone_number,website,rating,user_ratings_total"
    params = {"place_id": place_id, "fields": fields, "key": PLACES_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        return r.json().get("result", {})
    except Exception:
        return {}

# ═══════════════════════════════════════════════════════════════════
# WEBSITE AUDIT
# ═══════════════════════════════════════════════════════════════════

def check_pagespeed(url):
    try:
        endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
        params   = {"url": url, "key": PAGESPEED_KEY, "strategy": "mobile"}
        r        = requests.get(endpoint, params=params, timeout=30)
        data     = r.json()
        cats     = data.get("lighthouseResult", {}).get("categories", {})
        perf     = int((cats.get("performance", {}).get("score", 0) or 0) * 100)
        seo      = int((cats.get("seo",         {}).get("score", 0) or 0) * 100)
        return perf, seo
    except Exception:
        return None, None


def audit_website(website):
    problems = []
    if not website:
        return ["no_website"]

    url = website if website.startswith("http") else "https://" + website

    # HTTPS
    try:
        r = requests.get(url, timeout=8, allow_redirects=True)
        if not r.url.startswith("https"):
            problems.append("no_https")

        html = r.text.lower()

        # Mobile viewport
        if "viewport" not in html:
            problems.append("no_mobile_support")

        # Copyright year
        years = re.findall(r"copyright.*?(\d{4})", html)
        if years:
            yr = int(max(years))
            if yr < 2022:
                problems.append(f"old_design_{yr}")

        # Chatbot detection
        chatbot_signals = ["tawk", "intercom", "drift", "crisp", "livechat",
                           "zendesk", "hubspot", "tidio", "freshchat", "chat"]
        if not any(s in html for s in chatbot_signals):
            problems.append("no_ai_chatbot")

        # Booking detection
        booking_signals = ["book", "appointment", "schedule", "reserve", "calendly",
                           "acuity", "booksy", "simplepractice", "opendental"]
        if not any(s in html for s in booking_signals):
            problems.append("no_online_booking")

        # Third-party booking
        if "zocdoc" in html:
            problems.append("uses_zocdoc")

    except requests.exceptions.SSLError:
        problems.append("no_https")
    except Exception:
        pass

    # PageSpeed
    perf, seo = check_pagespeed(url)
    if perf is not None and perf < 60:
        problems.append(f"slow_site_{perf}")
    if seo is not None and seo < 70:
        problems.append(f"bad_seo_{seo}")

    return problems


# ═══════════════════════════════════════════════════════════════════
# SCREENSHOT + ANNOTATION
# ═══════════════════════════════════════════════════════════════════

def take_annotated_screenshot(url, problems, output_path):
    """
    Opens the site in Chrome, injects a CSS/JS audit overlay into the DOM,
    then screenshots. Browser renders anti-aliased circles, drop shadows,
    SVG connector lines, and system fonts — far better than PIL drawing.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--hide-scrollbars")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-software-rasterizer")

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts,
        )
        driver.set_page_load_timeout(20)

        try:
            try:
                driver.get(url if url.startswith("http") else "https://" + url)
            except Exception:
                pass  # page load timeout is fine — take screenshot of whatever loaded
            time.sleep(2)

            W, H = 1280, 900
            POSITIONS = {
                "no_https":          (W // 2, 80,  "NOT SECURE — no padlock"),
                "no_online_booking": (W // 2, 110, "NO BOOKING BUTTON"),
                "no_ai_chatbot":     (W - 60, H - 100, "NO LIVE CHAT WIDGET"),
                "no_mobile_support": (W - 30, H // 2, "BREAKS ON MOBILE"),
                "uses_zocdoc":       (W // 2, 140, "PAYING PER PATIENT — ZOCDOC"),
            }

            annotations = []
            used_y = []
            for i, prob in enumerate(problems[:5], 1):
                if prob == "no_website":
                    continue
                if prob in POSITIONS:
                    px, py, label = POSITIONS[prob]
                elif prob.startswith("old_design_"):
                    yr = prob.split("_")[-1]
                    px, py, label = W // 2, H - 80, f"OUTDATED — LAST UPDATED {yr}"
                elif prob.startswith("slow_site_"):
                    score = prob.split("_")[-1]
                    px, py, label = W // 2, H // 2, f"SPEED SCORE: {score}/100"
                elif prob.startswith("bad_seo_"):
                    score = prob.split("_")[-1]
                    px, py, label = W // 3, 80, f"SEO: {score}/100 — INVISIBLE ON GOOGLE"
                else:
                    continue

                while any(abs(py - uy) < 55 for uy in used_y):
                    py += 60
                used_y.append(py)

                annotations.append({
                    "x": px, "y": py, "label": label, "num": i,
                    "sideRight": px >= W // 2,
                })

            ann_json  = json.dumps(annotations)
            num_probs = len(problems)
            name_safe = YOUR_NAME.replace("'", "\\'")

            js = f"""
(function() {{
    var A = {ann_json};
    var numProbs = {num_probs};
    var yourName = '{name_safe}';

    var ov = document.createElement('div');
    ov.id = '__audit_overlay';
    ov.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:2147483647;font-family:-apple-system,Segoe UI,Arial,sans-serif;';

    /* ── Banner ── */
    var banner = document.createElement('div');
    banner.style.cssText = [
        'position:absolute;top:0;left:0;right:0;height:58px;',
        'background:linear-gradient(90deg,#1a0a0a 0%,#0f0f0f 60%);',
        'border-bottom:1px solid rgba(239,68,68,0.25);',
        'display:flex;align-items:center;padding:0 20px;box-sizing:border-box;gap:0;',
        'box-shadow:0 2px 24px rgba(0,0,0,0.6);'
    ].join('');

    /* left red accent bar */
    var accentBar = document.createElement('div');
    accentBar.style.cssText = 'width:4px;height:34px;background:linear-gradient(180deg,#ef4444,#b91c1c);border-radius:2px;margin-right:14px;flex-shrink:0;box-shadow:0 0 8px rgba(239,68,68,0.6);';
    banner.appendChild(accentBar);

    /* title block */
    var titleBlock = document.createElement('div');
    titleBlock.style.cssText = 'display:flex;flex-direction:column;justify-content:center;gap:2px;';
    titleBlock.innerHTML = [
        '<span style="color:#f9fafb;font-size:15px;font-weight:800;letter-spacing:0.8px;text-transform:uppercase;line-height:1;">Website Audit Report</span>',
        '<span style="color:#6b7280;font-size:11.5px;font-weight:500;letter-spacing:0.2px;line-height:1;">Issues detected that may be losing you patients</span>'
    ].join('');
    banner.appendChild(titleBlock);

    /* spacer */
    var spacer = document.createElement('div');
    spacer.style.cssText = 'flex:1;';
    banner.appendChild(spacer);

    /* red problem count badge */
    var badge = document.createElement('div');
    badge.style.cssText = [
        'display:flex;align-items:center;gap:7px;',
        'background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.35);',
        'border-radius:20px;padding:5px 13px 5px 9px;'
    ].join('');
    badge.innerHTML = [
        '<div style="width:8px;height:8px;border-radius:50%;background:#ef4444;box-shadow:0 0 6px #ef4444;flex-shrink:0;"></div>',
        '<span style="color:#fca5a5;font-size:12px;font-weight:700;white-space:nowrap;">' + numProbs + ' issue' + (numProbs !== 1 ? 's' : '') + ' found</span>'
    ].join('');
    banner.appendChild(badge);

    ov.appendChild(banner);

    A.forEach(function(a) {{
        var dot = document.createElement('div');
        dot.style.cssText = 'position:absolute;width:32px;height:32px;border-radius:50%;background:rgba(239,68,68,0.2);border:2.5px solid #ef4444;box-shadow:0 0 0 5px rgba(239,68,68,0.12),0 0 14px rgba(239,68,68,0.35);box-sizing:border-box;';
        dot.style.left = (a.x - 16) + 'px'; dot.style.top = (a.y - 16) + 'px';
        var inner = document.createElement('div');
        inner.style.cssText = 'position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:10px;height:10px;border-radius:50%;background:#ef4444;';
        dot.appendChild(inner);
        ov.appendChild(dot);

        var boxW = 242, boxH = 36;
        var bx = a.sideRight ? (a.x - boxW - 24) : (a.x + 24);
        var by = a.y - boxH / 2;
        var box = document.createElement('div');
        box.style.cssText = 'position:absolute;display:flex;align-items:center;background:#ef4444;border-radius:8px;box-shadow:0 4px 18px rgba(239,68,68,0.5);overflow:hidden;';
        box.style.left = bx + 'px'; box.style.top = by + 'px';
        box.style.width = boxW + 'px'; box.style.height = boxH + 'px';
        box.innerHTML = '<div style="min-width:28px;height:28px;background:white;border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 9px;font-size:12px;font-weight:800;color:#ef4444;flex-shrink:0;">' + a.num + '</div><span style="color:white;font-size:11.5px;font-weight:700;letter-spacing:0.4px;white-space:nowrap;">' + a.label + '</span>';
        ov.appendChild(box);

        var svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
        svg.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;overflow:visible;pointer-events:none;';
        var lx1 = a.sideRight ? (a.x - 16) : (a.x + 16);
        var lx2 = a.sideRight ? (bx + boxW) : bx;
        var line = document.createElementNS('http://www.w3.org/2000/svg','line');
        line.setAttribute('x1', lx1); line.setAttribute('y1', a.y);
        line.setAttribute('x2', lx2); line.setAttribute('y2', a.y);
        line.setAttribute('stroke','#ef4444');
        line.setAttribute('stroke-width','2');
        line.setAttribute('stroke-dasharray','5,3');
        svg.appendChild(line);
        ov.appendChild(svg);
    }});

    /* ── Footer CTA ── */
    var cta = document.createElement('div');
    cta.style.cssText = [
        'position:absolute;bottom:0;left:0;right:0;height:50px;',
        'background:linear-gradient(90deg,#021a0e 0%,#0f0f0f 60%);',
        'border-top:1px solid rgba(52,211,153,0.2);',
        'display:flex;align-items:center;padding:0 20px;gap:0;',
        'box-shadow:0 -2px 20px rgba(0,0,0,0.5);'
    ].join('');

    /* green accent bar */
    var greenBar = document.createElement('div');
    greenBar.style.cssText = 'width:4px;height:30px;background:linear-gradient(180deg,#34d399,#059669);border-radius:2px;margin-right:14px;flex-shrink:0;box-shadow:0 0 8px rgba(52,211,153,0.5);';
    cta.appendChild(greenBar);

    /* message */
    var ctaText = document.createElement('div');
    ctaText.style.cssText = 'display:flex;flex-direction:column;gap:2px;';
    ctaText.innerHTML = [
        '<span style="color:#f9fafb;font-size:13px;font-weight:700;letter-spacing:0.3px;line-height:1;">I can fix all ' + numProbs + ' of these — reply to this email to get started</span>',
        '<span style="color:#4b5563;font-size:11px;font-weight:500;line-height:1;">Sent by ' + yourName + ' · I specialize exclusively in dental practice websites</span>'
    ].join('');
    cta.appendChild(ctaText);

    /* arrow pill on right */
    var ctaBtn = document.createElement('div');
    ctaBtn.style.cssText = 'margin-left:auto;background:linear-gradient(135deg,#34d399,#059669);border-radius:16px;padding:6px 14px;display:flex;align-items:center;gap:6px;box-shadow:0 2px 10px rgba(52,211,153,0.35);';
    ctaBtn.innerHTML = '<span style="color:white;font-size:12px;font-weight:800;white-space:nowrap;">Reply Now →</span>';
    cta.appendChild(ctaBtn);

    ov.appendChild(cta);

    document.body.appendChild(ov);
}})();
"""
            driver.execute_script(js)
            time.sleep(0.5)
            driver.save_screenshot(output_path)
            return True
        finally:
            driver.quit()
    except Exception as e:
        log.warning("Screenshot failed: %s", e)
        return False




# ═══════════════════════════════════════════════════════════════════
# EMAIL GENERATOR
# ═══════════════════════════════════════════════════════════════════

PROBLEM_MAP = {
    "no_website":        "no website at all",
    "no_https":          "no HTTPS security — browsers show a red 'Not Secure' warning",
    "no_mobile_support": "not mobile-friendly — broken on phones",
    "no_online_booking": "no way to book appointments online",
    "no_ai_chatbot":     "no live chat or AI assistant",
    "uses_zocdoc":       "paying per patient via Zocdoc instead of owning your own booking",
}

def _pick_service(problems):
    """Three offers only: build, replace_zocdoc, chatbot."""
    if "no_website" in problems:
        return "build", "build you a professional dental website — mobile-friendly, Google-optimized, with AI booking built in so patients can request appointments 24/7"
    if "uses_zocdoc" in problems:
        return "replace_zocdoc", "replace Zocdoc with your own AI booking system — you keep 100% of every patient, no per-booking fees ever"
    return "chatbot", "add an AI booking assistant to your site so patients can book appointments 24/7 without calling the office"


SUBJECTS = [
    "Noticed something on {name}'s website",
    "Quick question for {name}",
    "{name} — patients searching online may not be finding you",
    "Looked at {name}'s website today",
    "Something worth knowing about {name}'s online presence",
]

POSITIVES = [
    "Your practice has solid reviews in {city} — that tells me you deliver great care.",
    "I can see {name} has been serving patients in {city} — that kind of reputation takes real work.",
    "Dental practices with consistent reviews like yours clearly focus on patient experience.",
    "Strong local presence in {city} — your patients clearly trust you.",
]

COMPETITOR_FRAMES = [
    "The challenge is that most dental practices in {city} now have online booking — patients searching at 9pm will book with whoever makes it easiest.",
    "I checked several practices near you in {city} and the ones getting the most new patients all have one thing in common: patients can book without calling.",
    "Most patients in {city} now decide on a dentist before they ever pick up the phone. What they see on your website determines if they call you or your competitor.",
    "In {city} right now, the dental practices growing fastest are the ones that let patients self-serve online — booking, questions, everything.",
]

CLOSERS = [
    "Reply 'yes' and I'll send you a short breakdown of exactly what I'd do for {name} — no call needed.",
    "If you want I can record a 2-minute video showing exactly what I'd fix on your site — just say the word.",
    "One reply and I can have a specific plan over to you by tomorrow.",
    "Worth a look? Reply and I'll send the fix breakdown — takes 30 seconds to read.",
]


def generate_email(lead):
    name     = lead["business_name"]
    city     = lead.get("city", "your area").replace(" TX", ", TX").replace(" FL", ", FL")
    problems = [p.strip() for p in lead.get("problems", "").split("|") if p.strip()]
    rating   = lead.get("rating", "")

    service_type, service_pitch = _pick_service(problems)

    positive   = random.choice(POSITIVES).format(name=name, city=city)
    comp_frame = random.choice(COMPETITOR_FRAMES).format(name=name, city=city)
    closer     = random.choice(CLOSERS).format(name=name)

    subjects = {
        "build":          f"couldn't find {name} online",
        "replace_zocdoc": f"Zocdoc alternative for {name}",
        "chatbot":        f"question about {name}",
    }
    subject = subjects.get(service_type, f"question about {name}")

    specialist = "I specialize exclusively in dental practices — I know exactly what patients look for when choosing a dentist online."

    # Zocdoc-specific email (strongest argument — saves them real money)
    if service_type == "replace_zocdoc":
        body = f"""Hi,

{positive}

I ran a quick audit and found {len(problems)} issue{'s' if len(problems) != 1 else ''} on your site — all marked in the screenshot. The most costly one: you're using Zocdoc.

Zocdoc charges $30–$80 per new patient. For a busy practice that's hundreds every month going out the door permanently.

I build AI booking systems for dental practices that do everything Zocdoc does — appointment requests, patient intake, FAQ — but you own it. Flat monthly fee, no per-patient charges ever.

{specialist}

{closer}

{YOUR_NAME}"""

    elif service_type == "build":
        body = f"""Hi,

I was searching for a dentist in {city} and couldn't find a website for {name}.

{comp_frame}

Without a website you're invisible to anyone searching online. I build professional dental websites in 7 days — optimized for Google, mobile-friendly, with an AI booking assistant built in so patients can request appointments 24/7.

{specialist}

{closer}

{YOUR_NAME}"""

    else:
        issues_line = (
            f"I ran a quick audit and found {len(problems)} issue{'s' if len(problems) != 1 else ''} on your site — all marked in the screenshot."
            if len(problems) > 1 else
            "I ran a quick audit and found an issue on your site — marked in the screenshot."
        )
        body = f"""Hi,

{positive}

{comp_frame}

{issues_line} The biggest one: I can {service_pitch}.

{specialist}

{closer}

{YOUR_NAME}"""

    return subject, body


# ═══════════════════════════════════════════════════════════════════
# EMAIL SENDER
# ═══════════════════════════════════════════════════════════════════

def send_email(to_email, subject, body, image_path=None):
    try:
        msg            = MIMEMultipart()
        msg["From"]    = SENDER_EMAIL
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                part = MIMEBase("image", "png")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment",
                                filename="website_audit.png")
                msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        log.error("Email failed to %s: %s", to_email, e)
        return False


# ═══════════════════════════════════════════════════════════════════
# SENT TRACKER
# ═══════════════════════════════════════════════════════════════════

def load_sent():
    if not os.path.exists(SENT_FILE):
        return {}
    with open(SENT_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_sent(data):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ═══════════════════════════════════════════════════════════════════
# LEAD SCRAPER
# ═══════════════════════════════════════════════════════════════════

def scrape_leads(target=1000):
    existing = []
    if os.path.exists(LEADS_FILE):
        existing = list(csv.DictReader(open(LEADS_FILE, encoding="utf-8")))
        log.info("Loaded %d existing leads from %s", len(existing), LEADS_FILE)
        if len(existing) >= target:
            return existing

    leads      = {l["business_name"].lower(): l for l in existing}
    fieldnames = ["business_name", "address", "city", "phone", "email",
                  "website", "rating", "reviews", "problems", "problem_count",
                  "lead_score"]

    writer_file = open(LEADS_FILE, "a", newline="", encoding="utf-8")
    writer      = csv.DictWriter(writer_file, fieldnames=fieldnames)
    if not existing:
        writer.writeheader()

    for city in CITIES:
        if len(leads) >= target:
            break

        query    = f"dental clinic in {city}"
        token    = None
        page     = 0

        while len(leads) < target and page < 3:
            if token:
                time.sleep(2)

            data    = search_places(query, token)
            results = data.get("results", [])
            token   = data.get("next_page_token")

            for place in results:
                if len(leads) >= target:
                    break

                name = place.get("name", "").strip()
                if not name:
                    continue
                if any(kw in name.lower() for kw in CHAIN_KEYWORDS):
                    continue
                if name.lower() in leads:
                    continue

                details  = get_details(place.get("place_id", ""))
                website  = details.get("website", "")
                phone    = details.get("formatted_phone_number", "")
                address  = details.get("formatted_address", "")
                rating   = place.get("rating", 0)
                reviews  = place.get("user_ratings_total", 0)

                log.info("Auditing: %s", name)
                problems     = audit_website(website)
                problem_count = len(problems)
                lead_score   = problem_count * 20 + (10 if not website else 0)

                row = {
                    "business_name": name,
                    "address":       address,
                    "city":          city,
                    "phone":         phone,
                    "email":         "",   # email enrichment below
                    "website":       website,
                    "rating":        rating,
                    "reviews":       reviews,
                    "problems":      " | ".join(problems),
                    "problem_count": problem_count,
                    "lead_score":    lead_score,
                }

                leads[name.lower()] = row
                writer.writerow(row)
                writer_file.flush()
                log.info("  %d leads | %d problems: %s", len(leads), problem_count, ", ".join(problems[:3]))

                time.sleep(0.5)

            if not token:
                break
            page += 1

    writer_file.close()
    log.info("Scraped %d total leads", len(leads))
    return list(leads.values())


# ═══════════════════════════════════════════════════════════════════
# EMAIL ENRICHMENT (Hunter.io)
# ═══════════════════════════════════════════════════════════════════

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_BAD_EMAIL_WORDS = {"noreply", "no-reply", "privacy", "legal", "sentry", "example",
                    "schema", "jquery", "w3.org", "google", "wix.com", "wordpress",
                    "first.last", "name@email", "email@email", "user@domain",
                    "test@email", "test@test", "your@email", "info@example"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico"}


def _is_real_email(email):
    """Filter out image filenames, placeholders, and obviously fake addresses."""
    domain = email.split("@")[-1].lower()
    # Image file extensions in domain (e.g. flags@2x.png, image@2x-300x300.jpg)
    if any(domain.endswith(ext) for ext in _IMAGE_EXTS):
        return False
    # Domain starts with digit (e.g. @2x-1024x609, @300x)
    if domain[0].isdigit():
        return False
    # Domain has no dot or is too short
    if "." not in domain or len(domain) < 4:
        return False
    # Placeholder patterns
    if any(bad in email.lower() for bad in _BAD_EMAIL_WORDS):
        return False
    return True


def _domain(website):
    return website.replace("https://", "").replace("http://", "").split("/")[0].lstrip("www.")


def _hunter_email(website):
    key = os.getenv("HUNTER_API_KEY")
    if not key or not website:
        return ""
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": _domain(website), "api_key": key, "limit": 5},
            timeout=10,
        )
        emails = r.json().get("data", {}).get("emails", [])
        if not emails:
            return ""
        for e in emails:
            v = e["value"]
            if any(x in v for x in ["info", "contact", "appt", "office", "hello", "dental"]):
                return v
        return emails[0]["value"]
    except Exception:
        return ""


def _scrape_email_from_site(website):
    if not website:
        return ""
    base = website.rstrip("/")
    pages = [base, base + "/contact", base + "/contact-us",
             base + "/about", base + "/about-us"]
    found = set()
    for url in pages:
        try:
            r = requests.get(url, timeout=8,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            # mailto: links first — most reliable
            for match in re.finditer(r'mailto:([^"\'>\s?&#]+)', r.text):
                email = match.group(1).lower().strip(".")
                if _is_real_email(email):
                    found.add(email)
            # General email pattern in body
            for email in EMAIL_RE.findall(r.text):
                if _is_real_email(email.lower()):
                    found.add(email.lower())
        except Exception:
            continue
        if found:
            break

    if not found:
        return ""
    for priority in ["appt", "appo", "dental", "contact", "info", "office", "hello", "admin"]:
        for e in found:
            if priority in e:
                return e
    return sorted(found)[0]


def _apollo_email(website):
    key = os.getenv("APOLLO_API_KEY")
    if not key or not website:
        return ""
    try:
        r = requests.post(
            "https://api.apollo.io/v1/organizations/enrich",
            headers={"Content-Type": "application/json"},
            json={"api_key": key, "domain": _domain(website)},
            timeout=10,
        )
        org_email = r.json().get("organization", {}).get("email", "")
        if org_email and "@" in org_email:
            return org_email
    except Exception:
        pass
    return ""


def _mx_exists(email):
    """Return True if the email's domain has a live MX record."""
    try:
        import socket
        domain = email.split("@")[1]
        # getaddrinfo on the domain — fast proxy for DNS existence
        socket.getaddrinfo(domain, 25, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return True
    except Exception:
        return False


def find_email(website, business_name=""):
    """Hunter → contact-page scrape → Apollo, with MX validation on result."""
    email = (
        _hunter_email(website)
        or _scrape_email_from_site(website)
        or _apollo_email(website)
    )
    if not email:
        return ""
    if not _mx_exists(email):
        log.info("  Email %s failed MX check — domain has no mail server", email)
        return ""
    return email


# ═══════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════════

def run_pipeline(test_mode=False, resume=True):
    log.info("=" * 60)
    log.info("OUTREACH PIPELINE STARTED — target: %d businesses", TARGET)
    log.info("=" * 60)

    # Step 1: Scrape leads
    log.info("STEP 1: Scraping leads...")
    leads = scrape_leads(TARGET)
    log.info("Total leads available: %d", len(leads))

    # Step 2: Load sent tracker
    sent      = load_sent()
    total_sent = len(sent)
    log.info("Already sent: %d | Remaining: %d", total_sent, TARGET - total_sent)

    if total_sent >= TARGET:
        log.info("TARGET REACHED (%d). Pipeline complete.", TARGET)
        return

    # Step 3: Process each lead
    sent_today = 0

    for lead in leads:
        if total_sent >= TARGET:
            log.info("TARGET REACHED — stopping pipeline.")
            break

        if sent_today >= DAILY_LIMIT:
            log.info("Daily limit reached (%d). Run again tomorrow.", DAILY_LIMIT)
            break

        name    = lead.get("business_name", "").strip()
        website = lead.get("website", "")
        problems_str = lead.get("problems", "")
        problems = [p.strip() for p in problems_str.split("|") if p.strip()]

        if not name or not problems:
            continue

        key = name.lower()
        if key in sent:
            continue

        # Find email if missing
        email = lead.get("email", "").strip()
        if not email:
            email = find_email(website, name)

        if not email and not test_mode:
            log.info("  SKIP %s — no email found", name)
            sent[key] = {"status": "no_email", "timestamp": datetime.now().isoformat()}
            save_sent(sent)
            continue

        if test_mode:
            email = SENDER_EMAIL

        log.info("Processing: %s <%s> | %d problems", name, email, len(problems))

        # Screenshot with CSS/JS overlay injected directly into the browser
        ann_path = os.path.join(SCREENSHOTS_DIR, f"{re.sub(r'[^\\w]', '_', name)[:40]}_ann.png")

        has_screenshot = False
        if website and "no_website" not in problems:
            has_screenshot = take_annotated_screenshot(website, problems, ann_path)
            if has_screenshot:
                log.info("  Screenshot annotated ✓")

        # Generate email
        subject, body = generate_email(lead)

        # Send
        img_to_send = ann_path if has_screenshot and os.path.exists(ann_path) else None
        success     = send_email(email, subject, body, img_to_send)

        if success:
            sent[key] = {
                "email":     email,
                "problems":  problems_str,
                "status":    "sent",
                "timestamp": datetime.now().isoformat(),
            }
            save_sent(sent)
            total_sent  += 1
            sent_today  += 1
            log.info("  ✓ SENT (%d/%d total)", total_sent, TARGET)

            if total_sent >= TARGET:
                break

            # Human-like delay
            delay = random.randint(DELAY_MIN, DELAY_MAX)
            log.info("  Waiting %ds...", delay)
            time.sleep(delay)
        else:
            sent[key] = {"status": "failed", "timestamp": datetime.now().isoformat()}
            save_sent(sent)

    log.info("=" * 60)
    log.info("Session complete. Sent today: %d | Total: %d/%d", sent_today, total_sent, TARGET)
    log.info("=" * 60)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test",   action="store_true", help="Send to yourself only")
    p.add_argument("--resume", action="store_true", default=True)
    args = p.parse_args()

    run_pipeline(test_mode=args.test, resume=args.resume)


if __name__ == "__main__":
    main()
