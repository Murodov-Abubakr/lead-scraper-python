#!/usr/bin/env python3
"""
Resend email 1 WITH screenshot to leads whose original email had no screenshot attached.
Only targets leads still at step 1 (haven't received follow-up yet).
Updates their timestamp in email_sequence.json so the Day 3 follow-up is rescheduled from today.
"""
import csv, json, os, re, sys, time, random
sys.path.insert(0, os.path.dirname(__file__))

from outreach_pipeline import (
    find_email, generate_email, send_email,
    take_annotated_screenshot, log,
    SCREENSHOTS_DIR, DELAY_MIN, DELAY_MAX,
)
from datetime import datetime

SEQUENCE_FILE = "email_sequence.json"
SENT_FILE     = "outreach_sent.json"
LEADS_FILE    = "dental_leads.csv"

def main():
    with open(SENT_FILE) as f:
        sent = json.load(f)

    with open(SEQUENCE_FILE) as f:
        seq = json.load(f)

    leads_by_key = {}
    with open(LEADS_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k = row.get("business_name", "").strip().lower()
            leads_by_key[k] = row

    # Find leads: no screenshot file + still at step 1
    targets = []
    for key, v in sent.items():
        if v.get("status") != "sent":
            continue
        safe_name = re.sub(r"[^\w]", "_", key)[:40]
        ann_path  = os.path.join(SCREENSHOTS_DIR, safe_name + "_ann.png")
        if not os.path.exists(ann_path) and seq.get(key, {}).get("step", 1) == 1:
            targets.append((key, v))

    log.info("Leads to resend with screenshot: %d", len(targets))

    resent = 0
    for key, v in targets:
        email    = v.get("email", "")
        lead     = leads_by_key.get(key, {})
        problems = [p.strip() for p in v.get("problems", "").split("|") if p.strip()]

        if not email or not lead:
            log.info("  SKIP %s — no lead data", key)
            continue

        website = lead.get("website", "").strip()

        safe_name = re.sub(r"[^\w]", "_", key)[:40]
        ann_path  = os.path.join(SCREENSHOTS_DIR, safe_name + "_ann.png")

        log.info("Processing: %s <%s>", lead.get("business_name", key), email)

        has_screenshot = False
        if website and "no_website" not in problems:
            has_screenshot = take_annotated_screenshot(website, problems, ann_path)
            if has_screenshot:
                log.info("  Screenshot annotated ✓")
            else:
                log.info("  Screenshot failed — sending without")

        subject, body = generate_email(lead)
        img_to_send   = ann_path if has_screenshot and os.path.exists(ann_path) else None
        success       = send_email(email, subject, body, img_to_send)

        if success:
            # Reset timestamp so Day 3 follow-up is scheduled from today
            seq[key]["last_sent"] = datetime.now().isoformat()
            with open(SEQUENCE_FILE, "w") as f:
                json.dump(seq, f, indent=2)
            resent += 1
            log.info("  ✓ Resent (%d/%d)", resent, len(targets))
            delay = random.randint(DELAY_MIN, DELAY_MAX)
            log.info("  Waiting %ds...", delay)
            time.sleep(delay)
        else:
            log.info("  ✗ Failed")

    log.info("Done. Resent %d emails with screenshot.", resent)

if __name__ == "__main__":
    main()
