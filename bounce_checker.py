#!/usr/bin/env python3
"""
Gmail bounce checker.
Connects via IMAP, finds bounce notifications, returns list of bounced emails.
Used to verify if a guessed owner email was wrong.
"""

import imaplib
import email
import re
import os
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")

BOUNCE_SUBJECTS = [
    "delivery status notification",
    "mail delivery failed",
    "undeliverable",
    "delivery failure",
    "returned mail",
    "failure notice",
    "mailer-daemon",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def connect_gmail():
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    imap.login(SENDER_EMAIL, SENDER_PASSWORD)
    imap.select("INBOX")
    return imap


def get_bounced_emails(since_minutes=15):
    """
    Check Gmail inbox for bounce notifications in the last N minutes.
    Returns set of email addresses that bounced.
    """
    bounced = set()
    try:
        imap = connect_gmail()
        since = (datetime.now() - timedelta(minutes=since_minutes)).strftime("%d-%b-%Y")
        _, msgs = imap.search(None, f'(FROM "mailer-daemon" SINCE "{since}")')

        for num in msgs[0].split():
            _, data = imap.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            subject = msg.get("Subject", "").lower()

            if any(s in subject for s in BOUNCE_SUBJECTS):
                # Extract bounced address from body
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                found = EMAIL_RE.findall(body)
                for addr in found:
                    if addr.lower() != SENDER_EMAIL.lower():
                        bounced.add(addr.lower())

        imap.logout()
    except Exception as e:
        print(f"  Bounce check error: {e}")

    return bounced


def wait_and_check_bounce(email_address, wait_minutes=10):
    """
    Wait N minutes then check if a specific email bounced.
    Returns True if bounced, False if delivered.
    """
    print(f"  Waiting {wait_minutes} mins to check if {email_address} bounced...")
    time.sleep(wait_minutes * 60)
    bounced = get_bounced_emails(since_minutes=wait_minutes + 2)
    return email_address.lower() in bounced


if __name__ == "__main__":
    import sys
    mins = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    print(f"Checking for bounces in last {mins} minutes...")
    bounced = get_bounced_emails(since_minutes=mins)
    if bounced:
        print(f"Bounced emails ({len(bounced)}):")
        for e in bounced:
            print(f"  {e}")
    else:
        print("No bounces found.")
