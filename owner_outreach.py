#!/usr/bin/env python3
"""
Pipeline B — Personal owner email outreach.
1. Find dentist name from About page
2. theHarvester for domain emails
3. Send to best-guess email (dr.firstname@domain.com)
4. Wait 10 mins, check Gmail for bounce
5. If bounced → try next permutation
6. Sends personalized "Hi Dr. Smith," email
"""

import csv, json, os, re, sys, time, random
sys.path.insert(0, os.path.dirname(__file__))

from outreach_pipeline import send_email, log, SCREENSHOTS_DIR, DELAY_MIN, DELAY_MAX
from owner_finder import find_owner_email, _permutations
from bounce_checker import wait_and_check_bounce, get_bounced_emails
from urllib.parse import urlparse
from datetime import datetime
from dotenv import load_dotenv
import os

load_dotenv()

YOUR_NAME   = os.getenv("YOUR_NAME", "Murodov")
LEADS_FILE  = "dental_leads.csv"
OWNER_FILE  = "owner_outreach.json"  # tracks owner outreach separately
DAILY_LIMIT = 30
BOUNCE_WAIT = 10  # minutes to wait for bounce


def load_owner_sent():
    if not os.path.exists(OWNER_FILE):
        return {}
    with open(OWNER_FILE) as f:
        return json.load(f)

def save_owner_sent(data):
    with open(OWNER_FILE, "w") as f:
        json.dump(data, f, indent=2)


def build_personal_email(name, clinic_name, city, problems):
    """Short, personal email addressed to Dr. Name."""
    first = name.split()[0] if name else ""
    last  = name.split()[-1] if name and len(name.split()) > 1 else ""

    problem_map = {
        "no_online_booking":  "no way for patients to book appointments online",
        "no_ai_chatbot":      "no live chat for patients after hours",
        "no_https":           "no HTTPS security on your site",
        "no_mobile_support":  "your site isn't mobile-friendly",
        "uses_zocdoc":        "paying Zocdoc per patient",
    }
    top_problem = ""
    for p in problems:
        for key, val in problem_map.items():
            if p.startswith(key):
                top_problem = val
                break
        if top_problem:
            break
    if not top_problem:
        top_problem = "no way for patients to book after hours"

    subject = f"Dr. {last} — quick question" if last else f"question about {clinic_name}"

    body = f"""Hi Dr. {first if first else last},

I checked {clinic_name}'s website and noticed {top_problem}.

Patients who can't book online go to the next dentist on Google.

I fix this for dental practices in about a week — adds 24/7 booking and an AI assistant that answers patient questions even at 10pm.

Worth a quick look? Reply and I'll send you a 2-minute demo.

{YOUR_NAME}"""

    return subject, body


def try_permutations(lead, name, perms, sent_today):
    """Try each email permutation, check bounce, return successful email or ''."""
    clinic = lead.get("business_name", "")
    city   = lead.get("city", "")
    probs  = [p.strip() for p in lead.get("problems", "").split("|") if p.strip()]

    for email in perms:
        subject, body = build_personal_email(name, clinic, city, probs)
        log.info("  Trying owner email: %s", email)
        success = send_email(email, subject, body, None)

        if not success:
            continue

        # Wait and check for bounce
        bounced = wait_and_check_bounce(email, wait_minutes=BOUNCE_WAIT)

        if bounced:
            log.info("  ✗ Bounced: %s — trying next", email)
            continue
        else:
            log.info("  ✓ Delivered: %s", email)
            return email

    return ""


def main():
    leads = list(csv.DictReader(open(LEADS_FILE, encoding="utf-8")))
    leads = [l for l in leads if l.get("problems") and l.get("website")]
    log.info("Loaded %d leads with website + problems", len(leads))

    owner_sent = load_owner_sent()
    sent_today = 0

    for lead in leads:
        if sent_today >= DAILY_LIMIT:
            log.info("Daily limit reached (%d). Run again tomorrow.", DAILY_LIMIT)
            break

        name    = lead.get("business_name", "").strip()
        website = lead.get("website", "").strip()
        key     = name.lower()

        # Skip already processed
        if owner_sent.get(key, {}).get("status") in ("sent", "no_owner_email"):
            continue

        log.info("Finding owner for: %s", name)
        owner_name, best_guess, perms = find_owner_email(website, name)

        if not owner_name:
            log.info("  No owner name found — skipping")
            owner_sent[key] = {"status": "no_owner_email", "timestamp": datetime.now().isoformat()}
            save_owner_sent(owner_sent)
            continue

        log.info("  Owner: %s", owner_name)

        # Use all permutations if available, otherwise just best guess
        emails_to_try = perms if isinstance(perms, list) and len(perms) > 1 else [best_guess]

        delivered_email = try_permutations(lead, owner_name, emails_to_try, sent_today)

        if delivered_email:
            owner_sent[key] = {
                "owner_name":  owner_name,
                "email":       delivered_email,
                "status":      "sent",
                "timestamp":   datetime.now().isoformat(),
            }
            sent_today += 1
            log.info("  ✓ SENT to %s (%d today)", delivered_email, sent_today)
        else:
            owner_sent[key] = {
                "owner_name": owner_name,
                "status":     "no_owner_email",
                "timestamp":  datetime.now().isoformat(),
            }
            log.info("  All permutations bounced — no valid owner email found")

        save_owner_sent(owner_sent)

        delay = random.randint(DELAY_MIN, DELAY_MAX)
        log.info("  Waiting %ds before next lead...", delay)
        time.sleep(delay)

    log.info("Done. Sent %d owner emails today.", sent_today)


if __name__ == "__main__":
    main()
