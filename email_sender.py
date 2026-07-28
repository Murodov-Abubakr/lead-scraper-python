#!/usr/bin/env python3
"""
Automated 5-email follow-up sequence for dental clinic outreach.

Usage:
    python email_sender.py --test          # send to yourself only
    python email_sender.py --limit 20      # send to 20 leads today
    python email_sender.py                 # process all pending leads
"""

import argparse
import csv
import json
import os
import random
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
YOUR_NAME       = os.getenv("YOUR_NAME", "Murodov")

LEADS_FILE    = "dental_leads.csv"
SEQUENCE_FILE = "email_sequence.json"   # tracks where each lead is in the sequence
MAX_PER_DAY   = 20                      # Gmail safe limit for cold outreach

# ═══════════════════════════════════════════════════════════════════
# EMAIL TEMPLATES
# ═══════════════════════════════════════════════════════════════════

def build_email(lead, step):
    name     = lead["business_name"]
    city     = lead.get("city", "your area")
    problems = [p.strip() for p in lead.get("problems", "").split("|") if p.strip()]
    phone    = lead.get("phone", "")

    # Pick top 3 problems in readable format
    problem_map = {
        "no_website":         "no website",
        "no_https":           "no HTTPS security (browsers show 'Not Secure')",
        "no_mobile_support":  "not mobile-friendly",
        "no_online_booking":  "no online booking",
        "no_ai_chatbot":      "no live chat or AI assistant",
        "uses_zocdoc":        "paying per patient via Zocdoc",
    }
    readable = []
    for p in problems[:3]:
        for key, val in problem_map.items():
            if p.startswith(key):
                readable.append(val)
                break
        else:
            if p.startswith("slow_site_"):
                readable.append(f"slow website (speed score {p.split('_')[-1]}/100)")
            elif p.startswith("bad_seo_"):
                readable.append(f"poor SEO (score {p.split('_')[-1]}/100)")
            elif p.startswith("old_design_"):
                readable.append(f"outdated website design (last updated {p.split('_')[-1]})")

    bullets = "\n".join(f"  • {p}" for p in readable) if readable else "  • no online booking system"
    worst   = readable[0] if readable else "no online booking"
    count   = len(problems)

    if step == 1:
        subject = f"question about {name}"
        body = f"""Hi,

I was searching for a dentist in {city} and came across your practice.

I ran a quick audit of your website and found {count} issues that may be costing you new patients:

{bullets}

These are small fixes but they make a real difference — patients who can't book online or see a "Not Secure" warning simply go to the next result on Google.

I fix exactly these issues for dental practices. Reply and I'll send you a breakdown of what I'd do specifically for {name}.

{YOUR_NAME}"""

    elif step == 2:
        subject = f"re: {name}"
        body = f"""Hi,

Just making sure my last email didn't get buried.

The main issue I noticed — {worst} — is worth fixing quickly. Patients searching for a dentist in {city} book with whoever makes it easiest. If your competitors have online booking and you don't, those patients are gone.

Reply and I'll send you a short breakdown — no call needed.

{YOUR_NAME}"""

    elif step == 3:
        subject = f"23 more bookings/month — dental practice case"
        body = f"""Hi,

I recently helped a dental practice similar to yours add online booking and an AI assistant to their website.

Within the first month they went from 0 online bookings to 23 per month — patients booking at 10pm, on weekends, without calling the office.

Looking at your site I noticed {worst}. That's exactly what was holding that practice back too.

If you want I can record a 2-minute video showing exactly what I'd fix on your site — just say the word.

{YOUR_NAME}"""

    elif step == 4:
        subject = f"quick question"
        body = f"""Hi,

One question — do you have a way for patients to book an appointment on your website after office hours?

If not, those patients are booking with someone else.

I help dental practices set this up. Takes about a week and costs less than losing one new patient per month.

{YOUR_NAME}"""

    elif step == 5:
        subject = f"closing the loop — {name}"
        body = f"""Hi,

I've reached out a few times and I won't keep following up after this.

If adding online booking, fixing your site speed, or getting found on Google ever becomes a priority — just reply to this email. I'll be here.

{YOUR_NAME}"""

    else:
        return None, None

    return subject, body


# ═══════════════════════════════════════════════════════════════════
# SENDING
# ═══════════════════════════════════════════════════════════════════

def send_email(to_email, subject, body):
    try:
        msg = MIMEMultipart()
        msg["From"]    = SENDER_EMAIL
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"  FAILED to send to {to_email}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
# SEQUENCE TRACKER
# ═══════════════════════════════════════════════════════════════════

def load_sequence():
    if not os.path.exists(SEQUENCE_FILE):
        return {}
    with open(SEQUENCE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_sequence(data):
    with open(SEQUENCE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def is_due(record, force=False):
    """Check if this lead is due for the next email today."""
    if record.get("stopped"):
        return False
    last_sent = record.get("last_sent")
    step      = record.get("step", 0)

    if step == 0:
        return True  # never contacted

    if not last_sent:
        return True

    if force:
        return True  # bypass day threshold

    last_date = datetime.fromisoformat(last_sent)
    now       = datetime.now()

    # Days between each step
    delays = {1: 3, 2: 4, 3: 7, 4: 7}  # days after each step before next
    delay  = delays.get(step, 999)

    return (now - last_date).days >= delay


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def process(leads, limit=None, test_mode=False, force=False):
    sequence = load_sequence()

    # Build a name→lead lookup so we can supplement CSV leads with sequence data
    lead_by_key = {l.get("business_name", "").strip().lower(): l for l in leads}

    # Also process leads that are ONLY in the sequence (email found by pipeline,
    # not in the CSV's email column)
    all_keys = set(lead_by_key.keys()) | set(sequence.keys())

    sent_today = 0

    for key in all_keys:
        if limit and sent_today >= limit:
            break

        lead   = lead_by_key.get(key, {})
        record = sequence.get(key, {"step": 0, "last_sent": None, "stopped": False})

        name  = lead.get("business_name", "").strip() or key.title()
        # Email: prefer sequence record (found by Hunter/Apollo), fall back to CSV
        email = record.get("email", "").strip() or lead.get("email", "").strip()

        if not name or not email:
            continue

        if test_mode:
            email = SENDER_EMAIL  # send to yourself in test mode

        if not is_due(record, force=force):
            continue

        next_step = record["step"] + 1
        if next_step > 5:
            record["stopped"] = True
            sequence[key] = record
            continue

        subject, body = build_email(lead, next_step)
        if not subject:
            continue

        print(f"Sending email {next_step}/5 to {name} <{email}>")
        success = send_email(email, subject, body)

        if success:
            record["step"]      = next_step
            record["last_sent"] = datetime.now().isoformat()
            record["email"]     = email
            if next_step == 5:
                record["stopped"] = True
            sequence[key] = record
            sent_today += 1
            print(f"  ✓ Sent (step {next_step}/5)")

            # Random delay between emails — looks human, avoids spam triggers
            if not test_mode:
                delay = random.randint(45, 120)
                print(f"  Waiting {delay}s before next...")
                time.sleep(delay)
        else:
            print(f"  ✗ Failed")

        save_sequence(sequence)

    print(f"\nDone. Sent {sent_today} emails today.")
    print(f"Run again tomorrow to send next batch of follow-ups.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test",  action="store_true", help="Send all emails to yourself")
    p.add_argument("--limit", type=int, default=MAX_PER_DAY)
    p.add_argument("--force", action="store_true", help="Send regardless of day threshold")
    args = p.parse_args()

    if not os.path.exists(LEADS_FILE):
        print(f"ERROR: {LEADS_FILE} not found. Run lead_scraper.py first.")
        return

    leads = list(csv.DictReader(open(LEADS_FILE, encoding="utf-8")))
    leads = [l for l in leads if l.get("problems")]  # only leads with problems
    print(f"Loaded {len(leads)} leads with problems")

    process(leads, limit=args.limit, test_mode=args.test, force=args.force)


if __name__ == "__main__":
    main()
