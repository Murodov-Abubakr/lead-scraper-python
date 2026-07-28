"""
AI Dental Chatbot — Conversation Logic

Handles appointment booking, FAQ, and lead capture.
No external AI API needed — smart intent detection + state machine.
"""

import json
import re
import os
from datetime import datetime

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "clinic_config.json")

with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)

# ═══════════════════════════════════════════════════════════════════
# INTENT DETECTION
# ═══════════════════════════════════════════════════════════════════

INTENTS = {
    "book_appointment": [
        "book", "appointment", "schedule", "reserve", "visit",
        "come in", "see a dentist", "make an appointment", "availability",
        "available", "slot", "opening", "when can i", "i need to",
    ],
    "hours": [
        "hours", "open", "close", "when", "time", "schedule",
        "what time", "are you open", "working hours", "business hours",
    ],
    "location": [
        "where", "address", "location", "directions", "located",
        "find you", "map", "street", "city", "zip",
    ],
    "services": [
        "service", "offer", "treatment", "procedure", "do you do",
        "can you", "whitening", "cleaning", "implant", "invisalign",
        "root canal", "crown", "filling", "extraction", "emergency",
        "pediatric", "children", "kids",
    ],
    "insurance": [
        "insurance", "accept", "coverage", "plan", "delta", "cigna",
        "aetna", "metlife", "united", "bluecross", "humana", "covered",
    ],
    "price": [
        "price", "cost", "how much", "fee", "charge", "payment",
        "pay", "affordable", "cheap", "expensive", "financing",
    ],
    "emergency": [
        "emergency", "urgent", "pain", "hurts", "broke", "broken",
        "knocked out", "severe", "asap", "now", "today", "bleeding",
    ],
    "greeting": [
        "hi", "hello", "hey", "good morning", "good afternoon",
        "good evening", "howdy", "what's up", "greetings",
    ],
    "thanks": [
        "thank", "thanks", "appreciate", "helpful", "great", "awesome",
        "perfect", "wonderful", "excellent",
    ],
    "human": [
        "human", "person", "real", "agent", "staff", "someone",
        "talk to", "speak to", "call me", "phone",
    ],
}


def detect_intent(text: str) -> str:
    text_lower = text.lower()
    scores = {intent: 0 for intent in INTENTS}
    for intent, keywords in INTENTS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[intent] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "unknown"


def is_after_hours() -> bool:
    now  = datetime.now()
    day  = now.strftime("%A").lower()
    hour = now.hour
    hours_str = CONFIG["hours"].get(day, "Closed")
    if hours_str == "Closed":
        return True
    try:
        open_t  = datetime.strptime(hours_str.split(" - ")[0], "%I:%M %p")
        close_t = datetime.strptime(hours_str.split(" - ")[1], "%I:%M %p")
        return not (open_t.hour <= hour < close_t.hour)
    except Exception:
        return False

# ═══════════════════════════════════════════════════════════════════
# RESPONSES
# ═══════════════════════════════════════════════════════════════════

def fmt_hours() -> str:
    lines = []
    for day, hrs in CONFIG["hours"].items():
        lines.append(f"  {day.capitalize()}: {hrs}")
    return "\n".join(lines)


def fmt_services() -> str:
    return "\n".join(f"  • {s}" for s in CONFIG["services"])


def fmt_insurance() -> str:
    return ", ".join(CONFIG["insurance"])


RESPONSES = {
    "greeting": lambda: (
        f"Hi there! 👋 Welcome to {CONFIG['clinic_name']}. "
        f"I can help you book an appointment or answer any questions.\n\n"
        f"What can I help you with today?\n"
        f"  1️⃣  Book an appointment\n"
        f"  2️⃣  Our hours & location\n"
        f"  3️⃣  Services we offer\n"
        f"  4️⃣  Insurance we accept"
    ),
    "hours": lambda: (
        f"🕐 Our office hours are:\n\n{fmt_hours()}\n\n"
        f"📍 We're located at: {CONFIG['address']}\n"
        f"📞 Phone: {CONFIG['phone']}\n\n"
        f"Would you like to book an appointment?"
    ),
    "location": lambda: (
        f"📍 We're located at:\n{CONFIG['address']}\n\n"
        f"📞 Phone: {CONFIG['phone']}\n\n"
        f"Our hours are:\n{fmt_hours()}\n\n"
        f"Would you like to book an appointment?"
    ),
    "services": lambda: (
        f"We offer a full range of dental services:\n\n{fmt_services()}\n\n"
        f"Would you like to book an appointment for any of these?"
    ),
    "insurance": lambda: (
        f"✅ We accept the following insurance plans:\n\n{fmt_insurance()}\n\n"
        f"Don't see yours? Give us a call at {CONFIG['phone']} "
        f"and we'll check your coverage. Would you like to book an appointment?"
    ),
    "price": lambda: (
        f"💰 We offer competitive pricing and flexible payment options.\n\n"
        f"For exact pricing, it depends on the specific treatment needed. "
        f"We'd be happy to give you a full cost estimate after your exam.\n\n"
        f"We also offer:\n"
        f"  • Flexible payment plans\n"
        f"  • 0% financing options\n"
        f"  • Insurance billing\n\n"
        f"Would you like to book a free consultation?"
    ),
    "emergency": lambda: (
        f"⚠️ Dental Emergency? We're here to help!\n\n"
        f"📞 Call us immediately: {CONFIG['phone']}\n\n"
        f"We offer same-day emergency appointments for:\n"
        f"  • Severe tooth pain\n"
        f"  • Broken or knocked-out teeth\n"
        f"  • Dental infections\n"
        f"  • Bleeding gums\n\n"
        f"If it's after hours, I can take your details and have someone "
        f"call you first thing in the morning. Would that help?"
    ),
    "thanks": lambda: (
        f"You're welcome! 😊 Is there anything else I can help you with?\n\n"
        f"Feel free to book an appointment anytime!"
    ),
    "human": lambda: (
        f"Of course! You can reach our team directly:\n\n"
        f"📞 Phone: {CONFIG['phone']}\n"
        f"📧 Email: {CONFIG['email']}\n"
        f"📍 Address: {CONFIG['address']}\n\n"
        f"Or I can take your details and have someone call you back. "
        f"Would you like that?"
    ),
    "unknown": lambda: (
        f"I want to make sure I help you correctly! Here's what I can do:\n\n"
        f"  📅  Book an appointment\n"
        f"  🕐  Share our hours & location\n"
        f"  🦷  Tell you about our services\n"
        f"  💳  Check insurance coverage\n\n"
        f"What would you like to know?"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# BOOKING STATE MACHINE
# ═══════════════════════════════════════════════════════════════════

BOOKING_STEPS = [
    "ask_name",
    "ask_phone",
    "ask_service",
    "ask_date",
    "ask_new_patient",
    "confirm",
]

BOOKING_QUESTIONS = {
    "ask_name":        "Great! Let's get you booked. 😊\n\nFirst, what's your full name?",
    "ask_phone":       "Thanks {name}! What's the best phone number to reach you?",
    "ask_service":     (
        "Perfect. What type of appointment are you looking for?\n\n"
        + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(CONFIG["services"][:6]))
        + "\n\n(Or just type what you need)"
    ),
    "ask_date":        "What date works best for you? (e.g. Monday, July 28 or 'this week')",
    "ask_new_patient": "Are you a new patient with us? (yes / no)",
    "confirm":         (
        "✅ Perfect! Here's your appointment summary:\n\n"
        "  👤 Name: {name}\n"
        "  📞 Phone: {phone}\n"
        "  🦷 Service: {service}\n"
        "  📅 Preferred date: {date}\n"
        "  🆕 New patient: {new_patient}\n\n"
        "Shall I confirm this booking? (yes / no)"
    ),
}


def get_booking_question(step: str, data: dict) -> str:
    q = BOOKING_QUESTIONS.get(step, "")
    return q.format(**{k: v or "—" for k, v in data.items()})


def booking_confirmed_message(data: dict) -> str:
    after = is_after_hours()
    if after:
        return (
            f"🎉 Your appointment request has been received!\n\n"
            f"  👤 {data.get('name')}\n"
            f"  📞 {data.get('phone')}\n"
            f"  🦷 {data.get('service')}\n"
            f"  📅 {data.get('date')}\n\n"
            f"Our office is currently closed but {CONFIG['doctor_name']}'s team "
            f"will call you first thing tomorrow morning to confirm your exact time.\n\n"
            f"Thank you for choosing {CONFIG['clinic_name']}! 😊"
        )
    else:
        return (
            f"🎉 Appointment booked!\n\n"
            f"  👤 {data.get('name')}\n"
            f"  📞 {data.get('phone')}\n"
            f"  🦷 {data.get('service')}\n"
            f"  📅 {data.get('date')}\n\n"
            f"Our team will call you shortly at {data.get('phone')} "
            f"to confirm your exact appointment time.\n\n"
            f"See you soon at {CONFIG['clinic_name']}! 🦷"
        )

# ═══════════════════════════════════════════════════════════════════
# MAIN CHAT FUNCTION
# ═══════════════════════════════════════════════════════════════════

def chat(message: str, session: dict) -> tuple:
    """
    Main entry point.
    session = dict kept per user (in-memory or Redis)
    Returns (reply_text, updated_session, appointment_data_if_completed)
    """
    message    = message.strip()
    booking    = session.get("booking", False)
    step       = session.get("step", None)
    book_data  = session.get("book_data", {
        "name": "", "phone": "", "service": "",
        "date": "", "new_patient": ""
    })
    appointment = None

    # ── In booking flow ──
    if booking and step:
        if step == "ask_name":
            book_data["name"] = message
            session["step"]   = "ask_phone"
            reply = get_booking_question("ask_phone", book_data)

        elif step == "ask_phone":
            book_data["phone"] = message
            session["step"]    = "ask_service"
            reply = get_booking_question("ask_service", book_data)

        elif step == "ask_service":
            # Accept number or text
            if message.isdigit():
                idx = int(message) - 1
                book_data["service"] = CONFIG["services"][idx] if 0 <= idx < len(CONFIG["services"]) else message
            else:
                book_data["service"] = message
            session["step"] = "ask_date"
            reply = get_booking_question("ask_date", book_data)

        elif step == "ask_date":
            book_data["date"] = message
            session["step"]   = "ask_new_patient"
            reply = get_booking_question("ask_new_patient", book_data)

        elif step == "ask_new_patient":
            book_data["new_patient"] = "Yes" if message.lower().startswith("y") else "No"
            session["step"]         = "confirm"
            reply = get_booking_question("confirm", book_data)

        elif step == "confirm":
            if message.lower().startswith("y"):
                reply       = booking_confirmed_message(book_data)
                appointment = dict(book_data)
                appointment["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                session["booking"]  = False
                session["step"]     = None
                session["book_data"] = {}
            else:
                reply = (
                    "No problem! Your details have been cleared.\n\n"
                    "Is there anything else I can help you with?"
                )
                session["booking"]  = False
                session["step"]     = None
                session["book_data"] = {}
        else:
            session["booking"] = False
            reply = RESPONSES["unknown"]()

        session["book_data"] = book_data
        return reply, session, appointment

    # ── Intent detection ──
    intent = detect_intent(message)

    if intent == "book_appointment" or message.strip() in ["1", "book"]:
        session["booking"]   = True
        session["step"]      = "ask_name"
        session["book_data"] = {"name": "", "phone": "", "service": "", "date": "", "new_patient": ""}
        after = is_after_hours()
        prefix = f"⏰ {CONFIG['after_hours_message']}\n\n" if after else ""
        reply = prefix + get_booking_question("ask_name", session["book_data"])

    elif intent in RESPONSES:
        reply = RESPONSES[intent]()

    elif message.strip() in ["2"]:
        reply = RESPONSES["hours"]()
    elif message.strip() in ["3"]:
        reply = RESPONSES["services"]()
    elif message.strip() in ["4"]:
        reply = RESPONSES["insurance"]()

    else:
        reply = RESPONSES["unknown"]()

    return reply, session, appointment
