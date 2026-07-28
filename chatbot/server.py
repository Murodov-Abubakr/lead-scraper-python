"""
Dental AI Chatbot — Flask Server

Run: python server.py
Then open: http://localhost:5000/demo
"""

import csv
import json
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from bot import chat, CONFIG

app = Flask(__name__)

APPOINTMENTS_FILE = os.path.join(os.path.dirname(__file__), "appointments.csv")
sessions = {}   # In production use Redis; for demo in-memory is fine

# ═══════════════════════════════════════════════════════════════════
# APPOINTMENT STORAGE + EMAIL NOTIFICATION
# ═══════════════════════════════════════════════════════════════════

def save_appointment(data: dict):
    is_new = not os.path.exists(APPOINTMENTS_FILE)
    with open(APPOINTMENTS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "phone", "service", "date", "new_patient", "timestamp"])
        if is_new:
            w.writeheader()
        w.writerow(data)


def notify_clinic(appointment: dict):
    sender   = os.getenv("SENDER_EMAIL")
    password = os.getenv("SENDER_PASSWORD")
    owner    = CONFIG.get("owner_email", sender)

    if not sender or not password:
        return

    body = f"""New appointment booked via AI chatbot!

Patient: {appointment['name']}
Phone:   {appointment['phone']}
Service: {appointment['service']}
Date:    {appointment['date']}
New patient: {appointment['new_patient']}
Booked at: {appointment['timestamp']}

Please call the patient to confirm their exact appointment time.
"""
    msg = MIMEText(body)
    msg["Subject"] = f"New Appointment — {appointment['name']} — {appointment['service']}"
    msg["From"]    = sender
    msg["To"]      = owner

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(sender, password)
            s.send_message(msg)
    except Exception as e:
        app.logger.error("Email notification failed: %s", e)


# ═══════════════════════════════════════════════════════════════════
# CHAT API
# ═══════════════════════════════════════════════════════════════════

@app.route("/api/chat", methods=["POST"])
def chat_endpoint():
    data       = request.get_json()
    message    = data.get("message", "").strip()
    session_id = data.get("session_id", "default")

    if not message:
        return jsonify({"reply": "Please type a message."})

    session = sessions.get(session_id, {})
    reply, session, appointment = chat(message, session)
    sessions[session_id] = session

    if appointment:
        save_appointment(appointment)
        notify_clinic(appointment)

    return jsonify({"reply": reply, "appointment_booked": bool(appointment)})


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "clinic_name":     CONFIG["clinic_name"],
        "welcome_message": CONFIG["welcome_message"],
        "widget_color":    CONFIG.get("widget_color", "#2563eb"),
    })


# ═══════════════════════════════════════════════════════════════════
# DEMO PAGE
# ═══════════════════════════════════════════════════════════════════

DEMO_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ clinic_name }} — AI Chatbot Demo</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f1f5f9; min-height: 100vh; display: flex;
         align-items: center; justify-content: center; padding: 20px; }

  .demo-wrapper { max-width: 480px; width: 100%; }
  .demo-label { text-align: center; margin-bottom: 16px; color: #64748b; font-size: 14px; }
  .demo-label strong { color: #1e293b; font-size: 18px; display: block; margin-bottom: 4px; }

  /* Chat widget */
  .chat-widget { background: white; border-radius: 20px;
                 box-shadow: 0 20px 60px rgba(0,0,0,0.15); overflow: hidden; }

  .chat-header { background: {{ color }}; padding: 18px 20px;
                 display: flex; align-items: center; gap: 12px; }
  .chat-avatar { width: 42px; height: 42px; background: rgba(255,255,255,0.25);
                 border-radius: 50%; display: flex; align-items: center;
                 justify-content: center; font-size: 20px; }
  .chat-header-info { color: white; }
  .chat-header-info .name { font-weight: 700; font-size: 16px; }
  .chat-header-info .status { font-size: 12px; opacity: 0.85; }
  .online-dot { width: 8px; height: 8px; background: #4ade80;
                border-radius: 50%; display: inline-block; margin-right: 5px; }

  .chat-messages { height: 420px; overflow-y: auto; padding: 20px;
                   display: flex; flex-direction: column; gap: 14px; }

  .msg { max-width: 82%; display: flex; flex-direction: column; gap: 4px; }
  .msg.bot  { align-self: flex-start; }
  .msg.user { align-self: flex-end; }

  .msg .bubble { padding: 12px 16px; border-radius: 18px;
                 font-size: 14px; line-height: 1.55; white-space: pre-wrap; }
  .msg.bot  .bubble { background: #f1f5f9; color: #1e293b;
                       border-bottom-left-radius: 4px; }
  .msg.user .bubble { background: {{ color }}; color: white;
                       border-bottom-right-radius: 4px; }

  .msg .time { font-size: 11px; color: #94a3b8; padding: 0 4px; }
  .msg.user .time { text-align: right; }

  .typing { display: flex; gap: 4px; padding: 12px 16px;
            background: #f1f5f9; border-radius: 18px; border-bottom-left-radius: 4px;
            width: fit-content; }
  .typing span { width: 8px; height: 8px; background: #94a3b8;
                 border-radius: 50%; animation: bounce 1.2s infinite; }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%,80%,100% { transform: translateY(0); }
                      40% { transform: translateY(-6px); } }

  .chat-input { display: flex; gap: 10px; padding: 16px;
                border-top: 1px solid #e2e8f0; }
  .chat-input input { flex: 1; border: 1px solid #e2e8f0; border-radius: 25px;
                       padding: 10px 18px; font-size: 14px; outline: none;
                       transition: border-color 0.2s; }
  .chat-input input:focus { border-color: {{ color }}; }
  .chat-input button { background: {{ color }}; color: white; border: none;
                        border-radius: 50%; width: 42px; height: 42px;
                        cursor: pointer; font-size: 18px; transition: opacity 0.2s; }
  .chat-input button:hover { opacity: 0.85; }

  .powered { text-align: center; padding: 10px; font-size: 11px; color: #94a3b8; }
</style>
</head>
<body>
<div class="demo-wrapper">
  <div class="demo-label">
    <strong>AI Chatbot Demo</strong>
    This is how it looks on your website
  </div>

  <div class="chat-widget">
    <div class="chat-header">
      <div class="chat-avatar">🦷</div>
      <div class="chat-header-info">
        <div class="name">{{ clinic_name }}</div>
        <div class="status"><span class="online-dot"></span>AI Assistant · Online 24/7</div>
      </div>
    </div>

    <div class="chat-messages" id="messages"></div>

    <div class="chat-input">
      <input type="text" id="userInput" placeholder="Type a message..."
             onkeypress="if(event.key==='Enter') sendMessage()">
      <button onclick="sendMessage()">➤</button>
    </div>
  </div>
  <div class="powered">Powered by AI · Built by {{ your_name }}</div>
</div>

<script>
const SESSION_ID = Math.random().toString(36).substr(2, 9);

function getTime() {
  return new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
}

function addMessage(text, sender) {
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = `msg ${sender}`;
  div.innerHTML = `
    <div class="bubble">${text.replace(/\n/g, '<br>')}</div>
    <div class="time">${getTime()}</div>
  `;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function showTyping() {
  const msgs = document.getElementById('messages');
  const div = document.createElement('div');
  div.id = 'typing';
  div.className = 'msg bot';
  div.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function removeTyping() {
  const t = document.getElementById('typing');
  if (t) t.remove();
}

async function sendMessage() {
  const input = document.getElementById('userInput');
  const text  = input.value.trim();
  if (!text) return;
  input.value = '';
  addMessage(text, 'user');
  showTyping();

  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: text, session_id: SESSION_ID})
  });
  const data = await resp.json();
  removeTyping();
  addMessage(data.reply, 'bot');

  if (data.appointment_booked) {
    setTimeout(() => addMessage('Is there anything else I can help you with? 😊', 'bot'), 1500);
  }
}

// Auto-send welcome message
window.onload = async () => {
  showTyping();
  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: 'hi', session_id: SESSION_ID})
  });
  const data = await resp.json();
  removeTyping();
  addMessage(data.reply, 'bot');
};
</script>
</body>
</html>"""


@app.route("/demo")
def demo():
    return render_template_string(
        DEMO_HTML,
        clinic_name=CONFIG["clinic_name"],
        color=CONFIG.get("widget_color", "#2563eb"),
        your_name=os.getenv("YOUR_NAME", "Murodov"),
    )


@app.route("/appointments")
def appointments():
    rows = []
    if os.path.exists(APPOINTMENTS_FILE):
        with open(APPOINTMENTS_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    return jsonify({"total": len(rows), "appointments": rows})


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print(f"  AI Chatbot for {CONFIG['clinic_name']}")
    print("=" * 50)
    print("  Demo:         http://localhost:5000/demo")
    print("  Appointments: http://localhost:5000/appointments")
    print("=" * 50 + "\n")
    app.run(debug=True, port=5000)
