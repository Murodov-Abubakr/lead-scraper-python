#!/usr/bin/env python3
"""
Dental AI Chatbot — Flask Server

Run:  python server.py
Demo: http://localhost:5000/demo
Dashboard: http://localhost:5000/dashboard
Widget embed: <script src="http://localhost:5000/widget.js"></script>
"""

import csv, json, logging, os, re, secrets, smtplib, functools, time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from urllib.parse import urlparse as _urlparse
from flask import Flask, request, jsonify, render_template_string, Response, session, redirect, url_for
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

_COLOR_RE    = re.compile(r'^#[0-9a-fA-F]{3,6}$')
_SESSION_TTL = 7200  # seconds — clean up sessions idle for 2+ hours

def _safe_color(raw: str) -> str:
    c = raw if raw.startswith("#") else f"#{raw}"
    return c if _COLOR_RE.match(c) else "#2563eb"

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from bot import chat, CONFIG, is_after_hours
from analytics import (track_conversation, track_booking, get_stats,
                        track_clinic_conversation, track_clinic_booking, get_clinic_stats)
from calendar_integration import create_appointment_event
from db import DATABASE_URL, get_db

try:
    from flask_cors import CORS
    _cors_available = True
except ImportError:
    _cors_available = False

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _limiter_available = True
except ImportError:
    _limiter_available = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # 64 KB max request body

_secret = os.getenv("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    logging.warning("SECRET_KEY not set — Flask sessions will reset on every restart")
app.secret_key = _secret

_is_prod = not bool(os.getenv("FLASK_DEBUG"))
app.config.update(
    SESSION_COOKIE_SECURE=_is_prod,       # HTTPS-only in prod; allow HTTP in local dev
    SESSION_COOKIE_HTTPONLY=True,          # not accessible to JavaScript
    SESSION_COOKIE_SAMESITE="Lax",         # blocks cross-origin CSRF
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

# Redis-backed server-side sessions — enables real logout (stolen cookies stop working)
_redis_url = os.getenv("REDIS_URL") or os.getenv("SESSION_REDIS_URL")
if _redis_url:
    try:
        import redis as _redis_lib
        from flask_session import Session as _FlaskSession
        app.config["SESSION_TYPE"]       = "redis"
        app.config["SESSION_REDIS"]      = _redis_lib.from_url(_redis_url, decode_responses=False)
        app.config["SESSION_KEY_PREFIX"] = "vicere:"
        app.config["SESSION_USE_SIGNER"] = True
        _FlaskSession(app)
        logging.info("Sessions: Redis backend active — server-side revocation enabled")
    except ImportError:
        logging.warning("flask-session/redis packages missing — falling back to cookie sessions")
else:
    logging.warning("REDIS_URL not set — sessions stored in signed cookies (server-side revocation unavailable)")

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
if not DASHBOARD_PASSWORD:
    raise RuntimeError(
        "DASHBOARD_PASSWORD environment variable is required. "
        "Set it in your .env file or Railway variables."
    )

if _cors_available:
    _allowed_origins = os.getenv("CORS_ORIGINS", "").split(",")
    _allowed_origins = [o.strip() for o in _allowed_origins if o.strip()]
    if not _allowed_origins:
        logging.warning("CORS_ORIGINS not set — defaulting to '*'. Set CORS_ORIGINS in production.")
    CORS(app, origins=_allowed_origins if _allowed_origins else "*")

if _limiter_available:
    _limiter_uri = os.getenv("RATELIMIT_STORAGE_URI") or os.getenv("REDIS_URL") or "memory://"
    if _limiter_uri == "memory://":
        logging.warning("RATELIMIT_STORAGE_URI not set — rate limits are per-worker, not shared across Gunicorn workers")
    limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri=_limiter_uri)

APPOINTMENTS_FILE  = os.path.join(os.path.dirname(__file__), "appointments.csv")
sessions: dict      = {}   # {session_key: {history, _ts}}
_clinic_cache: dict = {}  # {clinic_id: (config_dict, fetched_at)}
_key_cache: dict    = {}  # {widget_key: (clinic_id, fetched_at)}

_CACHE_TTL      = 300  # seconds — re-fetch clinic config after 5 minutes
_MAX_CACHE_SIZE = 500  # evict all when exceeded to prevent memory DoS


# ── Security helpers ───────────────────────────────────────────────────────────

@app.after_request
def _security_headers(response):
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-Content-Type-Options"]     = "nosniff"
    response.headers["Referrer-Policy"]            = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]         = "geolocation=(), microphone=(), camera=()"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    return response


def _verify_password(pwd: str, stored: str) -> bool:
    """Constant-time password check supporting bcrypt/pbkdf2 hashes and legacy plaintext."""
    if not stored:
        # Still run a comparison so response time is indistinguishable from a real check
        secrets.compare_digest(pwd.encode(), b"")
        return False
    if stored.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored, pwd)
    # Legacy plaintext (backwards compat for existing clinics)
    return secrets.compare_digest(pwd.encode(), stored.encode())


def _get_clinic(clinic_id: str) -> dict | None:
    if not clinic_id:
        return None
    cached = _clinic_cache.get(clinic_id)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]
    if not DATABASE_URL:
        return None
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT config, owner_email, widget_color, allowed_domain FROM clinics WHERE id = %s",
                (clinic_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            config, owner_email, widget_color, allowed_domain = row
            result = {
                **(config or {}),
                "owner_email":    owner_email    or (config or {}).get("owner_email", ""),
                "widget_color":   widget_color   or (config or {}).get("widget_color", "#2563eb"),
                "allowed_domain": allowed_domain or "",
            }
            if len(_clinic_cache) >= _MAX_CACHE_SIZE:
                _clinic_cache.clear()
            _clinic_cache[clinic_id] = (result, time.time())
            return result
    except Exception as e:
        app.logger.error("Clinic lookup failed: %s", e)
        return None


def _get_clinic_by_key(widget_key: str) -> tuple:
    """Resolve an unguessable widget_key → (clinic_id, cfg). Returns (None, None) on miss."""
    if not widget_key or not DATABASE_URL:
        return None, None
    cached = _key_cache.get(widget_key)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        clinic_id = cached[0]
        return clinic_id, _get_clinic(clinic_id)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM clinics WHERE widget_key = %s", (widget_key,))
            row = cur.fetchone()
            if not row:
                return None, None
            clinic_id = row[0]
            if len(_key_cache) >= _MAX_CACHE_SIZE:
                _key_cache.clear()
            _key_cache[widget_key] = (clinic_id, time.time())
            return clinic_id, _get_clinic(clinic_id)
    except Exception as e:
        app.logger.error("Widget key lookup failed: %s", e)
        return None, None


def _origin_allowed(allowed_domain: str) -> bool:
    """Return True if the request's Referer/Origin matches the clinic's allowed domain."""
    if not allowed_domain:
        return True  # no restriction configured — allow (legacy / dev)
    for header in ("Referer", "Origin"):
        val = request.headers.get(header, "")
        if val:
            host = _urlparse(val).netloc.lower().split(":")[0].lstrip("www.")
            allowed = allowed_domain.lower().lstrip("www.")
            if host == allowed or host.endswith("." + allowed):
                return True
    return False


_ADMIN_SESSION_KEY = "vicere_admin"

_ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vicere Admin — Sign In</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0f172a;min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:20px}
  .card{background:#1e293b;border-radius:16px;padding:40px;
        box-shadow:0 24px 64px rgba(0,0,0,.5);width:100%;max-width:360px;
        border:1px solid #334155}
  .logo{text-align:center;margin-bottom:28px}
  .logo h1{font-size:20px;font-weight:700;color:#f1f5f9}
  .logo p{font-size:12px;color:#475569;margin-top:6px}
  label{display:block;font-size:11px;font-weight:600;color:#94a3b8;
        margin-bottom:6px;letter-spacing:.06em;text-transform:uppercase}
  input{width:100%;background:#0f172a;border:1.5px solid #334155;
        border-radius:10px;padding:11px 14px;font-size:14px;color:#f1f5f9;
        outline:none;transition:border-color .2s;margin-bottom:20px}
  input:focus{border-color:#6366f1}
  button{width:100%;background:#6366f1;color:white;border:none;
         border-radius:10px;padding:13px;font-size:15px;font-weight:600;
         cursor:pointer;transition:opacity .2s}
  button:hover{opacity:.88}
  .err{background:#450a0a;border:1px solid #7f1d1d;color:#fca5a5;
       border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:18px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>Vicere Admin</h1>
    <p>Internal dashboard access only</p>
  </div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/admin/login">
    <label>Admin Password</label>
    <input type="password" name="pwd" placeholder="Dashboard password" autofocus required>
    <button type="submit">Sign In →</button>
  </form>
</div>
</body>
</html>"""


def require_admin(f):
    """Session-based admin auth — replaces the old ?pwd= query-string pattern."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get(_ADMIN_SESSION_KEY):
            # API callers get 401 JSON; browser navigation gets a redirect
            if request.path.startswith("/api/") or request.path == "/appointments":
                return jsonify({"error": "Authentication required. Sign in at /admin/login"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/login", methods=["GET"])
def admin_login():
    return render_template_string(_ADMIN_LOGIN_HTML, error="")


@app.route("/admin/login", methods=["POST"])
@(limiter.limit("5 per minute") if _limiter_available else lambda f: f)
def admin_login_post():
    pwd = request.form.get("pwd", "")
    if not secrets.compare_digest(pwd.encode(), DASHBOARD_PASSWORD.encode()):
        return render_template_string(_ADMIN_LOGIN_HTML, error="Incorrect password."), 401
    session.clear()
    session[_ADMIN_SESSION_KEY] = True
    session.permanent = True
    return redirect(url_for("dashboard"))


@app.route("/admin/logout")
def admin_logout():
    session.pop(_ADMIN_SESSION_KEY, None)
    return redirect(url_for("admin_login"))


# ═══════════════════════════════════════════════════════════════════
# APPOINTMENT STORAGE + NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════

def save_appointment(data: dict, clinic_id: str = ""):
    if DATABASE_URL:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO appointments (name, phone, service, date, new_patient, clinic_id) VALUES (%s, %s, %s, %s, %s, %s)",
                (data.get("name", ""), data.get("phone", ""),
                 data.get("service", ""), data.get("date", ""),
                 data.get("new_patient", ""), clinic_id),
            )
    else:
        is_new = not os.path.exists(APPOINTMENTS_FILE)
        with open(APPOINTMENTS_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["name", "phone", "service", "date", "new_patient", "timestamp"])
            if is_new:
                w.writeheader()
            w.writerow({k: data.get(k, "") for k in ["name", "phone", "service", "date", "new_patient", "timestamp"]})


def notify_clinic(appointment: dict, clinic_config: dict = None):
    sender   = os.getenv("SENDER_EMAIL")
    password = os.getenv("SENDER_PASSWORD")
    cfg      = clinic_config or CONFIG
    owner    = cfg.get("owner_email") or CONFIG.get("owner_email", sender)
    if not sender or not password:
        return

    body = f"""New appointment booked via AI chatbot!

Patient: {appointment.get('name')}
Phone:   {appointment.get('phone')}
Service: {appointment.get('service')}
Date:    {appointment.get('date')}
Booked:  {appointment.get('timestamp')}

Please call the patient to confirm their exact appointment time.
"""
    msg            = MIMEText(body)
    msg["Subject"] = f"New Booking — {appointment.get('name')} — {appointment.get('service')}"
    msg["From"]    = f"Vicere AI Receptionist <{sender}>"
    msg["To"]      = owner

    def _send():
        try:
            smtp_host = os.getenv("SMTP_HOST", "smtp.zoho.eu")
            smtp_port = int(os.getenv("SMTP_PORT", "465"))
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as s:
                s.login(sender, password)
                s.send_message(msg)
            app.logger.info("Email notification sent to %s", owner)
        except Exception:
            app.logger.error("Email notification failed")

    import threading
    threading.Thread(target=_send, daemon=True).start()


def _start_session_cleanup():
    """Background thread that evicts stale sessions every 5 minutes."""
    import threading

    def _loop():
        while True:
            time.sleep(300)
            cutoff = time.time() - _SESSION_TTL
            stale = [k for k, v in list(sessions.items()) if v.get("_ts", 0) < cutoff]
            for sid in stale:
                sessions.pop(sid, None)
            if stale:
                app.logger.debug("Evicted %d stale sessions", len(stale))

    threading.Thread(target=_loop, daemon=True, name="session-cleanup").start()


# ═══════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════════════
# CHAT API
# ═══════════════════════════════════════════════════════════════════

@app.route("/api/chat", methods=["POST"])
@(limiter.limit("10 per minute") if _limiter_available else lambda f: f)
def chat_endpoint():
    data = request.get_json(silent=True) or {}
    message    = str(data.get("message", "")).strip()
    session_id = str(data.get("session_id", "default"))[:64]
    clinic_id  = str(data.get("clinic_id", ""))[:64]

    if not message:
        return jsonify({"reply": "Please type a message."})

    now           = time.time()
    session_key   = f"{clinic_id}:{session_id}" if clinic_id else session_id
    clinic_config = _get_clinic(clinic_id)
    session       = sessions.get(session_key, {})

    if not session:
        try:
            track_conversation()
            if clinic_id:
                track_clinic_conversation(clinic_id)
        except Exception as e:
            app.logger.warning("Analytics tracking failed: %s", e)

    reply, session, appointment = chat(message, session, clinic_config, clinic_id)
    session["_ts"] = now
    sessions[session_key] = session

    if appointment:
        try:
            save_appointment(appointment, clinic_id)
        except Exception as e:
            app.logger.error("Failed to save appointment: %s", e)
        notify_clinic(appointment, clinic_config)
        create_appointment_event(appointment)
        try:
            track_booking()
            if clinic_id:
                track_clinic_booking(clinic_id)
        except Exception as e:
            app.logger.warning("Booking tracking failed: %s", e)

    return jsonify({"reply": reply, "appointment_booked": bool(appointment)})


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "clinic_name":     CONFIG["clinic_name"],
        "welcome_message": CONFIG.get("welcome_message", f"Hi! How can I help you at {CONFIG['clinic_name']}?"),
        "widget_color":    CONFIG.get("widget_color", "#2563eb"),
        "after_hours":     is_after_hours(),
    })


@app.route("/api/stats", methods=["GET"])
@require_admin
def stats_endpoint():
    try:
        return jsonify(get_stats())
    except Exception as e:
        app.logger.error("Stats DB error: %s", e)
        return jsonify({"error": "Database unavailable", "total_conversations": 0,
                        "total_bookings": 0, "today_conversations": 0,
                        "today_bookings": 0, "conversion_rate": 0,
                        "peak_hour": "—", "hourly": {}, "daily_last7": {}})


@app.route("/appointments", methods=["GET"])
@require_admin
def appointments():
    rows = []
    if DATABASE_URL:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT name, phone, service, date, new_patient, booked_at
                    FROM appointments ORDER BY booked_at DESC LIMIT 200
                """)
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    r = dict(zip(cols, row))
                    booked_at = r.pop("booked_at", None)
                    r["timestamp"] = booked_at.strftime("%Y-%m-%d %H:%M") if booked_at else ""
                    rows.append(r)
        except Exception as e:
            app.logger.error("Appointments DB error: %s", e)
    else:
        if os.path.exists(APPOINTMENTS_FILE):
            with open(APPOINTMENTS_FILE, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
    return jsonify({"total": len(rows), "appointments": rows})


# ═══════════════════════════════════════════════════════════════════
# WIDGET.JS — embeddable script
# ═══════════════════════════════════════════════════════════════════

@app.route("/widget.js")
def widget_js():
    widget_key = request.args.get("key", "")[:64]
    clinic_id  = ""
    clinic_cfg = None

    if widget_key:
        clinic_id, clinic_cfg = _get_clinic_by_key(widget_key)
        if clinic_cfg and not _origin_allowed(clinic_cfg.get("allowed_domain", "")):
            return Response("// Unauthorized domain", status=403, mimetype="application/javascript")
        if not clinic_cfg:
            return Response("// Invalid widget key", status=403, mimetype="application/javascript")
    else:
        # Legacy ?id= fallback (for already-deployed embeds)
        clinic_id  = request.args.get("id", "")[:64]
        clinic_cfg = _get_clinic(clinic_id) if clinic_id else None

    cfg          = clinic_cfg or {}
    clinic_name  = cfg.get("clinic_name", request.args.get("clinic", CONFIG["clinic_name"]))[:80]
    color        = _safe_color(cfg.get("widget_color", request.args.get("color", CONFIG.get("widget_color", "#2563eb"))))
    welcome_raw  = cfg.get("welcome_message", request.args.get("welcome", CONFIG.get("welcome_message", f"Hi! How can I help at {clinic_name}?")))[:200]
    server_url   = request.host_url.rstrip("/")
    # Use json.dumps for all values injected into JS string literals — proper escaping
    js_clinic_name = json.dumps(clinic_name)
    js_clinic_id   = json.dumps(clinic_id)
    js_welcome     = json.dumps(welcome_raw)
    js_server_url  = json.dumps(server_url)
    js_color       = json.dumps(color)

    js = f"""
(function() {{
  'use strict';
  if (document.getElementById('dc-widget')) return;

  const SERVER      = {js_server_url};
  const COLOR       = {js_color};
  const CLINIC_ID   = {js_clinic_id};
  const CLINIC_NAME = {js_clinic_name};
  const WELCOME_MSG = {js_welcome};
  const SESSION_ID = Array.from(crypto.getRandomValues(new Uint8Array(9))).map(b => b.toString(36)).join('');

  /* ── Styles ── */
  const style = document.createElement('style');
  style.textContent = `
    #dc-btn {{
      position:fixed; bottom:24px; right:24px; z-index:99999;
      width:60px; height:60px; border-radius:50%;
      background:${{COLOR}}; color:white; font-size:26px;
      display:flex; align-items:center; justify-content:center;
      cursor:pointer; box-shadow:0 4px 20px rgba(0,0,0,0.25);
      transition:transform .2s; user-select:none;
    }}
    #dc-btn:hover {{ transform:scale(1.1); }}
    #dc-panel {{
      position:fixed; bottom:100px; right:24px; z-index:99999;
      width:360px; height:520px; border-radius:20px;
      background:white; box-shadow:0 8px 40px rgba(0,0,0,0.18);
      display:none; flex-direction:column; overflow:hidden;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    }}
    #dc-header {{
      background:${{COLOR}}; padding:16px 18px;
      display:flex; align-items:center; gap:12px; flex-shrink:0;
    }}
    #dc-avatar {{
      width:38px; height:38px; border-radius:50%;
      background:rgba(255,255,255,.25);
      display:flex; align-items:center; justify-content:center; font-size:18px;
    }}
    #dc-title {{ color:white; }}
    #dc-title .name {{ font-weight:700; font-size:15px; }}
    #dc-title .status {{ font-size:11px; opacity:.85; }}
    .dc-dot {{ width:7px; height:7px; background:#4ade80;
               border-radius:50%; display:inline-block; margin-right:4px; }}
    #dc-msgs {{
      flex:1; overflow-y:auto; padding:16px;
      display:flex; flex-direction:column; gap:12px;
    }}
    .dc-msg {{ max-width:80%; display:flex; flex-direction:column; gap:3px; }}
    .dc-msg.bot  {{ align-self:flex-start; }}
    .dc-msg.user {{ align-self:flex-end; }}
    .dc-bubble {{
      padding:10px 14px; border-radius:16px;
      font-size:13px; line-height:1.5; white-space:pre-wrap;
    }}
    .dc-msg.bot  .dc-bubble {{ background:#f1f5f9; color:#1e293b; border-bottom-left-radius:4px; }}
    .dc-msg.user .dc-bubble {{ background:${{COLOR}}; color:white; border-bottom-right-radius:4px; }}
    .dc-time {{ font-size:10px; color:#94a3b8; padding:0 4px; }}
    .dc-msg.user .dc-time {{ text-align:right; }}
    .dc-typing {{
      display:flex; gap:4px; padding:10px 14px;
      background:#f1f5f9; border-radius:16px; border-bottom-left-radius:4px;
      width:fit-content;
    }}
    .dc-typing span {{
      width:7px; height:7px; background:#94a3b8;
      border-radius:50%; animation:dc-bounce 1.2s infinite;
    }}
    .dc-typing span:nth-child(2) {{ animation-delay:.2s; }}
    .dc-typing span:nth-child(3) {{ animation-delay:.4s; }}
    @keyframes dc-bounce {{ 0%,80%,100% {{ transform:translateY(0); }}
                            40% {{ transform:translateY(-5px); }} }}
    #dc-input-row {{
      display:flex; gap:8px; padding:12px;
      border-top:1px solid #e2e8f0; flex-shrink:0;
    }}
    #dc-input {{
      flex:1; border:1px solid #e2e8f0; border-radius:20px;
      padding:9px 16px; font-size:13px; outline:none;
      transition:border-color .2s;
    }}
    #dc-input:focus {{ border-color:${{COLOR}}; }}
    #dc-send {{
      background:${{COLOR}}; color:white; border:none;
      border-radius:50%; width:38px; height:38px;
      cursor:pointer; font-size:16px;
    }}
    #dc-powered {{ text-align:center; padding:6px; font-size:10px; color:#cbd5e1; flex-shrink:0; }}
  `;
  document.head.appendChild(style);

  /* ── Button ── */
  const btn = document.createElement('div');
  btn.id  = 'dc-btn';
  btn.innerHTML = '🦷';
  btn.title = 'Chat with us';
  document.body.appendChild(btn);

  /* ── Panel ── */
  const panel = document.createElement('div');
  panel.id = 'dc-panel';
  panel.innerHTML = `
    <div id="dc-header">
      <div id="dc-avatar">🦷</div>
      <div id="dc-title">
        <div class="name">${{CLINIC_NAME}}</div>
        <div class="status"><span class="dc-dot"></span>AI Assistant · 24/7</div>
      </div>
    </div>
    <div id="dc-msgs"></div>
    <div id="dc-input-row">
      <input id="dc-input" placeholder="Type a message..." />
      <button id="dc-send">➤</button>
    </div>
    <div id="dc-powered">Powered by AI</div>
  `;
  document.body.appendChild(panel);

  /* ── Logic ── */
  let opened = false;

  function getTime() {{
    return new Date().toLocaleTimeString([], {{hour:'2-digit', minute:'2-digit'}});
  }}

  function addMsg(text, who) {{
    const msgs   = document.getElementById('dc-msgs');
    const d      = document.createElement('div');
    d.className  = 'dc-msg ' + who;
    const bubble = document.createElement('div');
    bubble.className = 'dc-bubble';
    text.split('\\n').forEach(function(line, i, arr) {{
      bubble.appendChild(document.createTextNode(line));
      if (i < arr.length - 1) bubble.appendChild(document.createElement('br'));
    }});
    const timeEl = document.createElement('div');
    timeEl.className   = 'dc-time';
    timeEl.textContent = getTime();
    d.appendChild(bubble);
    d.appendChild(timeEl);
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
  }}

  function showTyping() {{
    const msgs = document.getElementById('dc-msgs');
    const d    = document.createElement('div');
    d.id = 'dc-typing-ind'; d.className = 'dc-msg bot';
    d.innerHTML = '<div class="dc-typing"><span></span><span></span><span></span></div>';
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
  }}

  function hideTyping() {{
    const t = document.getElementById('dc-typing-ind');
    if (t) t.remove();
  }}

  async function send() {{
    const input = document.getElementById('dc-input');
    const text  = input.value.trim();
    if (!text) return;
    input.value = '';
    addMsg(text, 'user');
    showTyping();
    try {{
      const r = await fetch(SERVER + '/api/chat', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{message: text, session_id: SESSION_ID, clinic_id: CLINIC_ID}})
      }});
      const data = await r.json();
      hideTyping();
      addMsg(data.reply, 'bot');
    }} catch(e) {{
      hideTyping();
      addMsg('Sorry, something went wrong. Please call us directly.', 'bot');
    }}
  }}

  async function openPanel() {{
    panel.style.display = 'flex';
    if (!opened) {{
      opened = true;
      showTyping();
      try {{
        const r = await fetch(SERVER + '/api/chat', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{message: 'hi', session_id: SESSION_ID, clinic_id: CLINIC_ID}})
        }});
        const data = await r.json();
        hideTyping();
        addMsg(data.reply, 'bot');
      }} catch(e) {{
        hideTyping();
        addMsg(WELCOME_MSG, 'bot');
      }}
    }}
    setTimeout(() => document.getElementById('dc-input').focus(), 100);
  }}

  btn.onclick = (e) => {{
    e.stopPropagation();
    if (panel.style.display === 'flex') {{
      panel.style.display = 'none';
    }} else {{
      openPanel();
    }}
  }};

  panel.onclick = (e) => e.stopPropagation();

  document.getElementById('dc-send').onclick = send;
  document.getElementById('dc-input').onkeypress = e => {{
    if (e.key === 'Enter') send();
  }};
}})();
"""
    return Response(js, mimetype="application/javascript")


# ═══════════════════════════════════════════════════════════════════
# ANALYTICS DASHBOARD
# ═══════════════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ clinic_name }} — Chatbot Dashboard</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f8fafc; color:#1e293b; padding:32px 24px; }
  h1 { font-size:22px; margin-bottom:4px; }
  .sub { color:#64748b; font-size:14px; margin-bottom:32px; }

  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
           gap:16px; margin-bottom:32px; }
  .card { background:white; border-radius:14px; padding:20px;
          box-shadow:0 1px 4px rgba(0,0,0,.07); }
  .card .label { font-size:12px; color:#64748b; margin-bottom:6px; }
  .card .value { font-size:32px; font-weight:700; color:{{ color }}; }
  .card .sub2  { font-size:12px; color:#94a3b8; margin-top:4px; }

  .section { background:white; border-radius:14px; padding:24px;
             box-shadow:0 1px 4px rgba(0,0,0,.07); margin-bottom:20px; }
  .section h2 { font-size:15px; font-weight:600; margin-bottom:20px; }

  .bar-row { display:flex; align-items:center; gap:12px; margin-bottom:10px; }
  .bar-label { width:40px; font-size:12px; color:#64748b; text-align:right; flex-shrink:0; }
  .bar-track { flex:1; background:#f1f5f9; border-radius:6px; height:12px; overflow:hidden; }
  .bar-fill  { height:100%; background:{{ color }}; border-radius:6px;
               transition:width .4s ease; }
  .bar-count { width:32px; font-size:12px; color:#94a3b8; }

  .appt-table { width:100%; border-collapse:collapse; font-size:13px; }
  .appt-table th { text-align:left; padding:8px 12px; border-bottom:2px solid #e2e8f0;
                   color:#64748b; font-weight:600; }
  .appt-table td { padding:10px 12px; border-bottom:1px solid #f1f5f9; }
  .appt-table tr:hover td { background:#f8fafc; }
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:32px;flex-wrap:wrap;gap:12px">
  <div>
    <h1>{{ clinic_name }} — AI Chatbot</h1>
    <p class="sub" style="margin-bottom:0">Live dashboard · Auto-refreshes every 60 seconds</p>
  </div>
  <a href="/admin/logout" style="font-size:12px;color:#64748b;text-decoration:none;padding:7px 14px;border:1px solid #e2e8f0;border-radius:8px;white-space:nowrap">Sign out</a>
</div>

<div class="cards">
  <div class="card">
    <div class="label">Total Conversations</div>
    <div class="value" id="total_conv">—</div>
    <div class="sub2" id="today_conv">Loading...</div>
  </div>
  <div class="card">
    <div class="label">Appointments Booked</div>
    <div class="value" id="total_book">—</div>
    <div class="sub2" id="today_book">Loading...</div>
  </div>
  <div class="card">
    <div class="label">Conversion Rate</div>
    <div class="value" id="conv_rate">—</div>
    <div class="sub2">chat → booking</div>
  </div>
  <div class="card">
    <div class="label">Peak Hour</div>
    <div class="value" id="peak_hour">—</div>
    <div class="sub2">most active time</div>
  </div>
</div>

<div class="section">
  <h2>Activity by Hour (today)</h2>
  <div id="hourly_chart"></div>
</div>

<div class="section">
  <h2>Recent Appointments</h2>
  <table class="appt-table">
    <thead>
      <tr><th>Name</th><th>Phone</th><th>Service</th><th>Date</th><th>Booked</th></tr>
    </thead>
    <tbody id="appt_body">
      <tr><td colspan="5" style="color:#94a3b8;padding:20px;">Loading...</td></tr>
    </tbody>
  </table>
</div>

<script>
const COLOR = '{{ color }}';

async function loadStats() {
  try {
    const r    = await fetch('/api/stats');
    const data = await r.json();

    document.getElementById('total_conv').textContent  = data.total_conversations;
    document.getElementById('today_conv').textContent  = `${data.today_conversations} today`;
    document.getElementById('total_book').textContent  = data.total_bookings;
    document.getElementById('today_book').textContent  = `${data.today_bookings} today`;
    document.getElementById('conv_rate').textContent   = data.conversion_rate + '%';
    document.getElementById('peak_hour').textContent   = data.peak_hour;

    // Hourly chart
    const hourly  = data.hourly || {};
    const maxVal  = Math.max(...Object.values(hourly), 1);
    const chart   = document.getElementById('hourly_chart');
    chart.innerHTML = '';
    for (let h = 0; h < 24; h++) {
      const val = hourly[String(h)] || 0;
      const pct = Math.round(val / maxVal * 100);
      chart.innerHTML += `
        <div class="bar-row">
          <div class="bar-label">${String(h).padStart(2,'0')}h</div>
          <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
          <div class="bar-count">${val}</div>
        </div>`;
    }
  } catch(e) { console.error(e); }
}

function escHtml(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str || '—'));
  return d.innerHTML;
}

async function loadAppointments() {
  try {
    const r    = await fetch('/appointments');
    const data = await r.json();
    const tbody = document.getElementById('appt_body');
    if (!data.appointments.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:#94a3b8;padding:20px;">No appointments yet.</td></tr>';
      return;
    }
    tbody.innerHTML = data.appointments.slice(-20).reverse().map(a =>
      `<tr>
        <td>${escHtml(a.name)}</td>
        <td>${escHtml(a.phone)}</td>
        <td>${escHtml(a.service)}</td>
        <td>${escHtml(a.date)}</td>
        <td style="color:#94a3b8">${escHtml(a.timestamp)}</td>
      </tr>`
    ).join('');
  } catch(e) { console.error(e); }
}

loadStats();
loadAppointments();
setInterval(() => { loadStats(); loadAppointments(); }, 60000);
</script>
</body>
</html>"""


@app.route("/dashboard")
@require_admin
def dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        clinic_name=CONFIG["clinic_name"],
        color=CONFIG.get("widget_color", "#2563eb"),
    )


# ═══════════════════════════════════════════════════════════════════
# DEMO PAGE (unchanged structure, now powered by Gemini)
# ═══════════════════════════════════════════════════════════════════

DEMO_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ clinic_name }} — AI Chatbot Demo</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f1f5f9; min-height:100vh;
         display:flex; align-items:center; justify-content:center; padding:20px; }
  .wrapper { max-width:480px; width:100%; }
  .label { text-align:center; margin-bottom:16px; color:#64748b; font-size:14px; }
  .label strong { color:#1e293b; font-size:18px; display:block; margin-bottom:4px; }
  .widget { background:white; border-radius:20px;
            box-shadow:0 20px 60px rgba(0,0,0,.15); overflow:hidden; }
  .header { background:{{ color }}; padding:18px 20px;
            display:flex; align-items:center; gap:12px; }
  .avatar { width:42px; height:42px; background:rgba(255,255,255,.25);
            border-radius:50%; display:flex; align-items:center;
            justify-content:center; font-size:20px; }
  .hinfo .name { color:white; font-weight:700; font-size:16px; }
  .hinfo .status { color:white; font-size:12px; opacity:.85; }
  .dot { width:8px; height:8px; background:#4ade80;
         border-radius:50%; display:inline-block; margin-right:5px; }
  .msgs { height:420px; overflow-y:auto; padding:20px;
          display:flex; flex-direction:column; gap:14px; }
  .msg { max-width:82%; display:flex; flex-direction:column; gap:4px; }
  .msg.bot  { align-self:flex-start; }
  .msg.user { align-self:flex-end; }
  .bubble { padding:12px 16px; border-radius:18px;
            font-size:14px; line-height:1.55; white-space:pre-wrap; }
  .msg.bot  .bubble { background:#f1f5f9; color:#1e293b; border-bottom-left-radius:4px; }
  .msg.user .bubble { background:{{ color }}; color:white; border-bottom-right-radius:4px; }
  .time { font-size:11px; color:#94a3b8; padding:0 4px; }
  .msg.user .time { text-align:right; }
  .typing { display:flex; gap:4px; padding:12px 16px;
            background:#f1f5f9; border-radius:18px; border-bottom-left-radius:4px;
            width:fit-content; }
  .typing span { width:8px; height:8px; background:#94a3b8;
                 border-radius:50%; animation:bounce 1.2s infinite; }
  .typing span:nth-child(2) { animation-delay:.2s; }
  .typing span:nth-child(3) { animation-delay:.4s; }
  @keyframes bounce { 0%,80%,100% { transform:translateY(0); }
                      40% { transform:translateY(-6px); } }
  .input-row { display:flex; gap:10px; padding:16px; border-top:1px solid #e2e8f0; }
  .input-row input { flex:1; border:1px solid #e2e8f0; border-radius:25px;
                     padding:10px 18px; font-size:14px; outline:none;
                     transition:border-color .2s; }
  .input-row input:focus { border-color:{{ color }}; }
  .input-row button { background:{{ color }}; color:white; border:none;
                      border-radius:50%; width:42px; height:42px;
                      cursor:pointer; font-size:18px; }
  .powered { text-align:center; padding:10px; font-size:11px; color:#94a3b8; }
</style>
</head>
<body>
<div class="wrapper">
  <div class="label">
    <strong>AI Chatbot Demo</strong>
    Powered by Gemini · This is how it looks on your website
  </div>
  <div class="widget">
    <div class="header">
      <div class="avatar">🦷</div>
      <div class="hinfo">
        <div class="name">{{ clinic_name }}</div>
        <div class="status"><span class="dot"></span>AI Assistant · Online 24/7</div>
      </div>
    </div>
    <div class="msgs" id="msgs"></div>
    <div class="input-row">
      <input id="inp" placeholder="Type a message..."
             onkeypress="if(event.key==='Enter') send()">
      <button onclick="send()">➤</button>
    </div>
  </div>
  <div class="powered">Powered by Gemini AI · Built by {{ your_name }}</div>
</div>
<script>
const SID = Array.from(crypto.getRandomValues(new Uint8Array(9))).map(b=>b.toString(36)).join('');
function ts() { return new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); }
function addMsg(t,w) {
  const m=document.getElementById('msgs'), d=document.createElement('div');
  d.className='msg '+w;
  const b=document.createElement('div'); b.className='bubble';
  t.split('\\n').forEach(function(l,i,a){
    b.appendChild(document.createTextNode(l));
    if(i<a.length-1) b.appendChild(document.createElement('br'));
  });
  const ti=document.createElement('div'); ti.className='time'; ti.textContent=ts();
  d.appendChild(b); d.appendChild(ti); m.appendChild(d); m.scrollTop=m.scrollHeight;
}
function showTyping() {
  const m=document.getElementById('msgs'), d=document.createElement('div');
  d.id='typ'; d.className='msg bot';
  d.innerHTML='<div class="typing"><span></span><span></span><span></span></div>';
  m.appendChild(d); m.scrollTop=m.scrollHeight;
}
function hideTyping() { const t=document.getElementById('typ'); if(t)t.remove(); }
async function send() {
  const inp=document.getElementById('inp'), text=inp.value.trim();
  if(!text) return; inp.value=''; addMsg(text,'user'); showTyping();
  const r=await fetch('/api/chat',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:text,session_id:SID})});
  const d=await r.json(); hideTyping(); addMsg(d.reply,'bot');
}
window.onload=async()=>{
  showTyping();
  const r=await fetch('/api/chat',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:'hi',session_id:SID})});
  const d=await r.json(); hideTyping(); addMsg(d.reply,'bot');
};
</script>
</body>
</html>"""


@app.route("/demo")
def demo():
    clinic_id   = request.args.get("id", "")[:64]
    clinic_cfg  = _get_clinic(clinic_id) if clinic_id else None
    cfg         = clinic_cfg or {}
    clinic_name = cfg.get("clinic_name", request.args.get("clinic", CONFIG["clinic_name"]))[:80]
    color       = _safe_color(cfg.get("widget_color", request.args.get("color", CONFIG.get("widget_color", "#2563eb"))))
    phone       = cfg.get("phone", request.args.get("phone", CONFIG["phone"]))[:30]
    return render_template_string(
        DEMO_HTML,
        clinic_name=clinic_name,
        color=color,
        your_name=os.getenv("YOUR_NAME", "Vicere"),
        phone=phone,
    )


# ═══════════════════════════════════════════════════════════════════
# CENTRAL LOGIN  (app.vicere.co.uk/login — email + password)
# ═══════════════════════════════════════════════════════════════════

_CENTRAL_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vicere — Sign In</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0f172a;min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:20px}
  .card{background:#1e293b;border-radius:20px;padding:44px 40px;
        box-shadow:0 24px 64px rgba(0,0,0,.5);width:100%;max-width:400px;
        border:1px solid #334155}
  .logo{text-align:center;margin-bottom:32px}
  .logo-mark{display:inline-flex;align-items:center;justify-content:center;
             width:52px;height:52px;border-radius:14px;
             background:linear-gradient(135deg,#6366f1,#8b5cf6);
             font-size:24px;margin-bottom:14px;box-shadow:0 8px 24px rgba(99,102,241,.35)}
  .logo h1{font-size:22px;font-weight:700;color:#f1f5f9;letter-spacing:-.02em}
  .logo p{font-size:13px;color:#64748b;margin-top:5px}
  .field{margin-bottom:18px}
  label{display:block;font-size:11px;font-weight:600;color:#94a3b8;
        margin-bottom:7px;letter-spacing:.06em;text-transform:uppercase}
  input{width:100%;background:#0f172a;border:1.5px solid #334155;
        border-radius:10px;padding:12px 14px;font-size:14px;color:#f1f5f9;
        outline:none;transition:border-color .2s}
  input::placeholder{color:#475569}
  input:focus{border-color:#6366f1}
  button{width:100%;background:linear-gradient(135deg,#6366f1,#8b5cf6);
         color:white;border:none;border-radius:10px;padding:14px;
         font-size:15px;font-weight:600;cursor:pointer;
         transition:opacity .2s;margin-top:6px;
         box-shadow:0 4px 14px rgba(99,102,241,.4)}
  button:hover{opacity:.88}
  .err{background:#450a0a;border:1px solid #7f1d1d;color:#fca5a5;
       border-radius:8px;padding:11px 14px;font-size:13px;margin-bottom:18px}
  .footer{text-align:center;margin-top:24px;font-size:12px;color:#475569}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-mark">🦷</div>
    <h1>Vicere</h1>
    <p>Clinic Dashboard Portal</p>
  </div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <div class="field">
      <label>Email Address</label>
      <input type="email" name="email" placeholder="owner@yourclinic.co.uk"
             value="{{ email }}" autofocus required>
    </div>
    <div class="field">
      <label>Password</label>
      <input type="password" name="pwd" placeholder="Your dashboard password" required>
    </div>
    <button type="submit">Sign In →</button>
  </form>
  <div class="footer">Powered by Vicere AI · Dental Chatbot Platform</div>
</div>
</body>
</html>"""


@app.route("/login", methods=["GET"])
def central_login():
    return render_template_string(_CENTRAL_LOGIN_HTML, error="", email="")


@app.route("/login", methods=["POST"])
@(limiter.limit("5 per minute") if _limiter_available else lambda f: f)
def central_login_post():
    email = request.form.get("email", "").strip().lower()[:254]
    pwd   = request.form.get("pwd", "")

    def _fail():
        return render_template_string(
            _CENTRAL_LOGIN_HTML,
            error="Invalid email or password.",
            email=email,
        ), 401

    if not email or not pwd:
        return _fail()

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, dashboard_password FROM clinics WHERE lower(owner_email) = %s LIMIT 1",
                (email,),
            )
            row = cur.fetchone()
    except Exception as e:
        app.logger.error("Central login DB error: %s", e)
        return render_template_string(
            _CENTRAL_LOGIN_HTML,
            error="A server error occurred. Please try again.",
            email=email,
        ), 500

    clinic_id  = row[0] if row else None
    stored_pwd = row[1] if row else ""
    # Always verify to prevent email enumeration via response-time side-channel
    valid = _verify_password(pwd, stored_pwd)
    if not clinic_id or not valid:
        return _fail()

    session.clear()  # prevent session fixation
    session["clinic_id"] = clinic_id
    session.permanent = True
    return redirect(url_for("clinic_dashboard", clinic_id=clinic_id))


# ═══════════════════════════════════════════════════════════════════
# PER-CLINIC DASHBOARD  (cookie-based auth, no password in URL)
# ═══════════════════════════════════════════════════════════════════

_CLINIC_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ clinic_name }} — Sign In</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f1f5f9;min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:20px}
  .card{background:white;border-radius:16px;padding:40px;
        box-shadow:0 8px 40px rgba(0,0,0,.12);width:100%;max-width:380px}
  .logo{text-align:center;margin-bottom:28px}
  .logo .icon{font-size:36px;margin-bottom:8px}
  .logo h1{font-size:20px;font-weight:700;color:#1e293b}
  .logo p{font-size:13px;color:#64748b;margin-top:4px}
  label{display:block;font-size:12px;font-weight:600;color:#475569;
        margin-bottom:6px;letter-spacing:.03em}
  input[type=password]{width:100%;border:1.5px solid #e2e8f0;border-radius:10px;
    padding:11px 14px;font-size:14px;outline:none;transition:border-color .2s;
    margin-bottom:20px}
  input[type=password]:focus{border-color:{{ color }}}
  button{width:100%;background:{{ color }};color:white;border:none;
         border-radius:10px;padding:13px;font-size:15px;font-weight:600;
         cursor:pointer;transition:opacity .2s}
  button:hover{opacity:.88}
  .err{background:#fef2f2;border:1px solid #fecaca;color:#dc2626;
       border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:16px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="icon">🦷</div>
    <h1>{{ clinic_name }}</h1>
    <p>Chatbot Dashboard</p>
  </div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/clinic/{{ clinic_id }}/auth">
    <label>Dashboard Password</label>
    <input type="password" name="pwd" placeholder="Enter your password" autofocus required>
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>"""


_CLINIC_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ clinic_name }} — Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f8fafc;color:#1e293b;padding:28px 20px 60px}
  .topbar{display:flex;align-items:center;justify-content:space-between;
          margin-bottom:28px;flex-wrap:wrap;gap:12px}
  .brand{display:flex;align-items:center;gap:10px}
  .brand-dot{width:10px;height:10px;border-radius:50%;background:{{ color }}}
  .brand-name{font-size:18px;font-weight:700}
  .brand-sub{font-size:12px;color:#94a3b8;margin-top:2px}
  .logout{font-size:12px;color:#94a3b8;text-decoration:none;
          padding:6px 14px;border:1px solid #e2e8f0;border-radius:8px}
  .logout:hover{color:#ef4444;border-color:#fecaca}

  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
         gap:14px;margin-bottom:24px}
  .card{background:white;border-radius:14px;padding:20px;
        box-shadow:0 1px 4px rgba(0,0,0,.07)}
  .card .lbl{font-size:11px;color:#64748b;font-weight:600;
             letter-spacing:.04em;text-transform:uppercase;margin-bottom:6px}
  .card .val{font-size:30px;font-weight:700;color:{{ color }}}
  .card .sub{font-size:11px;color:#94a3b8;margin-top:4px}

  .section{background:white;border-radius:14px;padding:22px;
           box-shadow:0 1px 4px rgba(0,0,0,.07);margin-bottom:18px}
  .section h2{font-size:14px;font-weight:600;margin-bottom:18px;color:#334155}

  .bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
  .bar-lbl{width:36px;font-size:11px;color:#94a3b8;text-align:right;flex-shrink:0}
  .bar-track{flex:1;background:#f1f5f9;border-radius:4px;height:10px;overflow:hidden}
  .bar-fill{height:100%;background:{{ color }};border-radius:4px;transition:width .4s}
  .bar-cnt{width:28px;font-size:11px;color:#94a3b8}

  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;padding:8px 10px;border-bottom:2px solid #e2e8f0;
     color:#64748b;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase}
  td{padding:10px 10px;border-bottom:1px solid #f1f5f9}
  tr:hover td{background:#f8fafc}
  .badge-new{background:#dcfce7;color:#16a34a;font-size:10px;
             padding:2px 7px;border-radius:20px;font-weight:600}
  .ts{color:#94a3b8}

</style>
</head>
<body>
<div class="topbar">
  <div class="brand">
    <div class="brand-dot"></div>
    <div>
      <div class="brand-name">{{ clinic_name }}</div>
      <div class="brand-sub">AI Chatbot Dashboard · auto-refreshes every 60 s</div>
    </div>
  </div>
  <a href="/clinic/{{ clinic_id }}/logout" class="logout">Sign out</a>
</div>

<div class="cards">
  <div class="card">
    <div class="lbl">Total Chats</div>
    <div class="val" id="tc">—</div>
    <div class="sub" id="tc2">Loading…</div>
  </div>
  <div class="card">
    <div class="lbl">Appointments</div>
    <div class="val" id="tb">—</div>
    <div class="sub" id="tb2">Loading…</div>
  </div>
  <div class="card">
    <div class="lbl">Conversion</div>
    <div class="val" id="cr">—</div>
    <div class="sub">chat → booking</div>
  </div>
  <div class="card">
    <div class="lbl">Peak Hour</div>
    <div class="val" id="ph">—</div>
    <div class="sub">most active</div>
  </div>
</div>

<div class="section">
  <h2>Activity by Hour</h2>
  <div id="chart"></div>
</div>

<div class="section">
  <h2>Recent Appointments</h2>
  <table>
    <thead><tr><th>Name</th><th>Phone</th><th>Service</th><th>Date Requested</th><th>Booked</th></tr></thead>
    <tbody id="appt_body"><tr><td colspan="5" style="color:#94a3b8;padding:20px">Loading…</td></tr></tbody>
  </table>
</div>

<script>
const CID = {{ clinic_id_js }};
function esc(s){const d=document.createElement('div');d.appendChild(document.createTextNode(s||'—'));return d.innerHTML;}
async function loadStats(){
  try{
    const d=await(await fetch('/clinic/'+CID+'/stats')).json();
    document.getElementById('tc').textContent=d.total_conversations;
    document.getElementById('tc2').textContent=d.today_conversations+' today';
    document.getElementById('tb').textContent=d.total_bookings;
    document.getElementById('tb2').textContent=d.today_bookings+' today';
    document.getElementById('cr').textContent=d.conversion_rate+'%';
    document.getElementById('ph').textContent=d.peak_hour;
    const h=d.hourly||{},mx=Math.max(...Object.values(h),1),el=document.getElementById('chart');
    el.innerHTML='';
    for(let i=0;i<24;i++){
      const v=h[String(i)]||0,p=Math.round(v/mx*100);
      el.innerHTML+=`<div class="bar-row">
        <div class="bar-lbl">${String(i).padStart(2,'0')}h</div>
        <div class="bar-track"><div class="bar-fill" style="width:${p}%"></div></div>
        <div class="bar-cnt">${v}</div></div>`;
    }
  }catch(e){console.error(e);}
}
async function loadAppts(){
  try{
    const d=await(await fetch('/clinic/'+CID+'/appointments')).json();
    const tb=document.getElementById('appt_body');
    if(!d.appointments.length){
      tb.innerHTML='<tr><td colspan="5" style="color:#94a3b8;padding:20px">No appointments yet.</td></tr>';
      return;
    }
    tb.innerHTML=d.appointments.slice(0,20).map(a=>`<tr>
      <td>${esc(a.name)}</td><td>${esc(a.phone)}</td>
      <td>${esc(a.service)}</td><td>${esc(a.date)}</td>
      <td class="ts">${esc(a.timestamp)}</td></tr>`).join('');
  }catch(e){console.error(e);}
}
loadStats();loadAppts();
setInterval(()=>{loadStats();loadAppts();},60000);
</script>
</body>
</html>"""


def _clinic_auth_required(f):
    """Decorator: checks Flask session for matching clinic_id."""
    @functools.wraps(f)
    def decorated(clinic_id, *args, **kwargs):
        if session.get("clinic_id") != clinic_id:
            return redirect(url_for("central_login"))
        return f(clinic_id, *args, **kwargs)
    return decorated


def _get_clinic_or_404(clinic_id: str):
    cfg = _get_clinic(clinic_id)
    if not cfg:
        return None
    return cfg


@app.route("/clinic/<clinic_id>", methods=["GET"])
def clinic_login(clinic_id):
    clinic_id = clinic_id[:64]
    if session.get("clinic_id") == clinic_id:
        return redirect(url_for("clinic_dashboard", clinic_id=clinic_id))
    return redirect(url_for("central_login"))


@app.route("/clinic/<clinic_id>/auth", methods=["POST"])
@(limiter.limit("5 per minute") if _limiter_available else lambda f: f)
def clinic_auth(clinic_id):
    clinic_id = clinic_id[:64]
    cfg = _get_clinic_or_404(clinic_id)
    if not cfg:
        return Response("Clinic not found.", 404)

    pwd   = request.form.get("pwd", "")
    color = _safe_color(cfg.get("widget_color", "#2563eb"))

    stored_pwd = ""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT dashboard_password FROM clinics WHERE id = %s", (clinic_id,))
            row = cur.fetchone()
            if row:
                stored_pwd = row[0] or ""
    except Exception as e:
        app.logger.error("Clinic auth DB error: %s", e)

    if not _verify_password(pwd, stored_pwd):
        return render_template_string(
            _CLINIC_LOGIN_HTML,
            clinic_id=clinic_id,
            clinic_name=cfg.get("clinic_name", clinic_id),
            color=color,
            error="Incorrect password. Please try again.",
        )

    session.clear()  # prevent session fixation
    session["clinic_id"] = clinic_id
    session.permanent = True
    return redirect(url_for("clinic_dashboard", clinic_id=clinic_id))


@app.route("/clinic/<clinic_id>/dashboard")
@_clinic_auth_required
def clinic_dashboard(clinic_id):
    cfg   = _get_clinic_or_404(clinic_id)
    color = _safe_color(cfg.get("widget_color", "#2563eb"))
    return render_template_string(
        _CLINIC_DASHBOARD_HTML,
        clinic_id=clinic_id,
        clinic_id_js=json.dumps(clinic_id),
        clinic_name=cfg.get("clinic_name", clinic_id),
        color=color,
    )


@app.route("/clinic/<clinic_id>/stats")
@_clinic_auth_required
def clinic_stats(clinic_id):
    return jsonify(get_clinic_stats(clinic_id))


@app.route("/clinic/<clinic_id>/appointments")
@_clinic_auth_required
def clinic_appointments(clinic_id):
    rows = []
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT name, phone, service, date, new_patient, booked_at
                FROM appointments
                WHERE clinic_id = %s
                ORDER BY booked_at DESC LIMIT 100
            """, (clinic_id,))
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                booked_at = r.pop("booked_at", None)
                r["timestamp"] = booked_at.strftime("%Y-%m-%d %H:%M") if booked_at else ""
                rows.append(r)
    except Exception as e:
        app.logger.error("Clinic appointments error: %s", e)
    return jsonify({"total": len(rows), "appointments": rows})


@app.route("/clinic/<clinic_id>/logout")
def clinic_logout(clinic_id):
    session.pop("clinic_id", None)
    return redirect(url_for("central_login"))


# ═══════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════

_start_session_cleanup()

if __name__ == "__main__":
    print("\n" + "=" * 52)
    print(f"  AI Chatbot -- {CONFIG['clinic_name']}")
    print("=" * 52)
    print("  Demo:       http://localhost:5000/demo")
    print("  Dashboard:  http://localhost:5000/dashboard")
    print("  Embed code: http://localhost:5000/widget.js")
    print("  Stats API:  http://localhost:5000/api/stats")
    print("=" * 52 + "\n")
    app.run(debug=bool(os.getenv("FLASK_DEBUG")), port=5000)
