#!/usr/bin/env python3
"""
Focused send script: San Antonio TX + El Paso TX leads only.
Retries leads previously skipped with no_email using contact-page scraping + Apollo.
Skips only leads already successfully SENT.
"""

import csv, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))

from outreach_pipeline import (
    find_email, generate_email, send_email,
    take_annotated_screenshot, log,
    SENT_FILE, LEADS_FILE, SCREENSHOTS_DIR, TARGET,
    DAILY_LIMIT, DELAY_MIN, DELAY_MAX,
)
import re, random, time

STOP_AT_TOTAL = 200   # stop once this many total emails sent

def load_sent():
    if not os.path.exists(SENT_FILE):
        return {}
    with open(SENT_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_sent(data):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def main():
    # Use existing leads; scrape more if needed to hit the target
    from outreach_pipeline import scrape_leads as _scrape
    all_leads = _scrape(STOP_AT_TOTAL * 3)   # keep enough headroom

    leads = [l for l in all_leads if l.get("problems", "").strip()]
    log.info("Total leads with problems: %d", len(leads))

    sent       = load_sent()
    total_sent = sum(1 for v in sent.values() if v.get("status") == "sent")
    sent_today = 0

    log.info("Total sent so far: %d | Target: %d", total_sent, STOP_AT_TOTAL)

    if total_sent >= STOP_AT_TOTAL:
        log.info("Already at target (%d). Nothing to do.", STOP_AT_TOTAL)
        return

    for lead in leads:
        if total_sent >= STOP_AT_TOTAL:
            log.info("Reached %d total — stopping.", STOP_AT_TOTAL)
            break
        if sent_today >= DAILY_LIMIT:
            log.info("Daily limit reached (%d). Run again tomorrow.", DAILY_LIMIT)
            break

        name     = lead.get("business_name", "").strip()
        website  = lead.get("website", "").strip()
        problems = [p.strip() for p in lead.get("problems", "").split("|") if p.strip()]
        key      = name.lower()

        # Skip only if already SENT — retry no_email and failed
        if sent.get(key, {}).get("status") == "sent":
            continue

        # Find email: Hunter → contact page scrape → Apollo
        email = find_email(website, name)

        if not email:
            log.info("  SKIP %s — still no email found", name)
            sent[key] = {"status": "no_email", "timestamp": __import__("datetime").datetime.now().isoformat()}
            save_sent(sent)
            continue

        log.info("Processing: %s <%s> | %d problems", name, email, len(problems))

        # Screenshot with CSS overlay
        safe_name   = re.sub(r"[^\w]", "_", name)[:40]
        ann_path    = os.path.join(SCREENSHOTS_DIR, f"{safe_name}_ann.png")
        has_screenshot = False
        if website and "no_website" not in problems:
            has_screenshot = take_annotated_screenshot(website, problems, ann_path)
            if has_screenshot:
                log.info("  Screenshot annotated ✓")

        subject, body = generate_email(lead)
        img_to_send   = ann_path if has_screenshot and os.path.exists(ann_path) else None
        success       = send_email(email, subject, body, img_to_send)

        if success:
            sent[key] = {
                "email":     email,
                "problems":  lead.get("problems", ""),
                "status":    "sent",
                "timestamp": __import__("datetime").datetime.now().isoformat(),
            }
            save_sent(sent)
            total_sent += 1
            sent_today += 1
            log.info("  ✓ SENT (%d today | %d/%d total)", sent_today, total_sent, TARGET)

            delay = random.randint(DELAY_MIN, DELAY_MAX)
            log.info("  Waiting %ds...", delay)
            time.sleep(delay)
        else:
            sent[key] = {"status": "failed", "timestamp": __import__("datetime").datetime.now().isoformat()}
            save_sent(sent)

    log.info("=" * 60)
    log.info("Done. Sent today: %d | Total: %d/%d", sent_today, total_sent, STOP_AT_TOTAL)
    log.info("=" * 60)

if __name__ == "__main__":
    main()
