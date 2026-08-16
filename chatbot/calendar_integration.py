#!/usr/bin/env python3
"""
Google Calendar integration.
Creates appointment events when a patient books via the chatbot.

Setup for clinic:
1. Create a Google Cloud service account
2. Download credentials JSON → save as chatbot/credentials.json
3. Share clinic's Google Calendar with the service account email
4. Add GOOGLE_CALENDAR_ID to .env
"""

import logging, os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")


def _is_configured():
    return bool(os.getenv("GOOGLE_CALENDAR_ID")) and os.path.exists(CREDENTIALS_FILE)


def create_appointment_event(appointment: dict) -> bool:
    """
    Creates a Google Calendar event for a booked appointment.
    Returns True on success, False if not configured or error.
    """
    if not _is_configured():
        return False

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "")
        timezone    = os.getenv("CLINIC_TIMEZONE", "Europe/London")

        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_FILE,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        service = build("calendar", "v3", credentials=creds)

        start = (datetime.now() + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(hours=1)

        event = {
            "summary": f"{appointment['name']} — {appointment['service']}",
            "description": (
                f"Patient: {appointment['name']}\n"
                f"Phone:   {appointment['phone']}\n"
                f"Service: {appointment['service']}\n"
                f"Preferred date: {appointment['date']}\n"
                f"Booked via AI chatbot: {appointment['timestamp']}\n\n"
                f"Call patient to confirm exact time."
            ),
            "start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end":   {"dateTime": end.isoformat(),   "timeZone": timezone},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email",  "minutes": 24 * 60},
                    {"method": "popup",  "minutes": 30},
                ],
            },
            "colorId": "2",  # green — AI-booked appointments
        }

        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        logger.info("Calendar event created: %s", created.get("htmlLink"))
        return True

    except ImportError:
        logger.warning("Google Calendar: install google-api-python-client google-auth")
        return False
    except Exception as e:
        logger.error("Google Calendar error: %s", e)
        return False


def setup_instructions():
    """Print setup instructions for the clinic."""
    print("""
╔══════════════════════════════════════════════════════╗
║        Google Calendar Setup (5 minutes)             ║
╠══════════════════════════════════════════════════════╣
║ 1. Go to console.cloud.google.com                    ║
║ 2. Create project → Enable Google Calendar API       ║
║ 3. Create Service Account → Download JSON key        ║
║ 4. Save key as: chatbot/credentials.json             ║
║ 5. Share clinic Google Calendar with service email   ║
║ 6. Add to .env:                                      ║
║    GOOGLE_CALENDAR_ID=clinic@gmail.com               ║
║    CLINIC_TIMEZONE=Europe/London                     ║
╚══════════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    if _is_configured():
        print("Google Calendar is configured.")
    else:
        setup_instructions()
