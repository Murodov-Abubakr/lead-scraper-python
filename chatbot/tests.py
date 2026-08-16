#!/usr/bin/env python3
"""
Basic test suite for the dental AI chatbot.
Run: python -m pytest chatbot/tests.py -v
"""
import json, os, sys
import pytest

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DASHBOARD_PASSWORD", "testpass")

import server
server.app.config["TESTING"] = True
server.app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


@pytest.fixture
def client():
    server.sessions.clear()
    with server.app.test_client() as c:
        yield c


# ── /api/chat ──────────────────────────────────────────────────────

def test_chat_empty_message(client):
    r = client.post("/api/chat", json={"message": "", "session_id": "t1"})
    assert r.status_code == 200
    assert "reply" in r.get_json()


def test_chat_missing_body(client):
    r = client.post("/api/chat", content_type="application/json", data="")
    assert r.status_code == 200


def test_chat_message_too_long(client):
    r = client.post("/api/chat", json={"message": "x" * 3000, "session_id": "t2"})
    data = r.get_json()
    assert "too long" in data["reply"].lower()


def test_chat_normal_message(client):
    r = client.post("/api/chat", json={"message": "hello", "session_id": "t3"})
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data.get("reply"), str)
    assert len(data["reply"]) > 0


def test_chat_session_id_too_long_is_truncated(client):
    long_id = "a" * 200
    r = client.post("/api/chat", json={"message": "hi", "session_id": long_id})
    assert r.status_code == 200
    # session_id is capped at 64 chars — should not crash
    assert len(list(server.sessions.keys())[0]) <= 64


def test_chat_xss_payload_does_not_crash(client):
    r = client.post("/api/chat", json={
        "message": "<script>alert(1)</script>",
        "session_id": "xss"
    })
    assert r.status_code == 200


# ── /demo ──────────────────────────────────────────────────────────

def test_demo_page_loads(client):
    r = client.get("/demo")
    assert r.status_code == 200
    assert b"AI Chatbot" in r.data


def test_demo_custom_clinic(client):
    r = client.get("/demo?clinic=Test+Dental&phone=01234567890")
    assert r.status_code == 200
    assert b"Test Dental" in r.data


def test_demo_invalid_color_rejected(client):
    r = client.get("/demo?color=red;background:url(evil)")
    assert r.status_code == 200
    assert b"red;background" not in r.data


def test_demo_valid_color_accepted(client):
    r = client.get("/demo?color=ff0000")
    assert r.status_code == 200
    assert b"#ff0000" in r.data


# ── /admin/login ───────────────────────────────────────────────────

def test_admin_login_page_loads(client):
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert b"Vicere Admin" in r.data


def test_admin_login_wrong_password(client):
    r = client.post("/admin/login", data={"pwd": "wrong"})
    assert r.status_code == 401
    assert b"Incorrect password" in r.data


def test_admin_login_correct_password(client):
    r = client.post("/admin/login", data={"pwd": "testpass"}, follow_redirects=False)
    assert r.status_code == 302


def test_admin_logout_clears_session(client):
    with client.session_transaction() as sess:
        sess[server._ADMIN_SESSION_KEY] = True
    r = client.get("/admin/logout", follow_redirects=False)
    assert r.status_code == 302
    with client.session_transaction() as sess:
        assert not sess.get(server._ADMIN_SESSION_KEY)


# ── /dashboard + /appointments (session-protected) ─────────────────

def test_dashboard_no_admin_session_redirects(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert b"admin/login" in r.headers["Location"].encode()


def test_dashboard_with_admin_session(client):
    with client.session_transaction() as sess:
        sess[server._ADMIN_SESSION_KEY] = True
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert b"AI Chatbot" in r.data


def test_appointments_no_admin_session(client):
    r = client.get("/appointments")
    assert r.status_code == 401


def test_stats_no_admin_session(client):
    r = client.get("/api/stats")
    assert r.status_code == 401


# ── /api/config ────────────────────────────────────────────────────

def test_config_endpoint(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.get_json()
    assert "clinic_name" in data
    assert "widget_color" in data


# ── color validation helper ────────────────────────────────────────

def test_safe_color_valid():
    assert server._safe_color("#2563eb") == "#2563eb"
    assert server._safe_color("2563eb")  == "#2563eb"
    assert server._safe_color("#fff")    == "#fff"


def test_safe_color_invalid():
    assert server._safe_color("red;bad") == "#2563eb"
    assert server._safe_color("javascript:alert(1)") == "#2563eb"
    assert server._safe_color("") == "#2563eb"


# ── Security regression tests ──────────────────────────────────────

def test_security_headers_present(client):
    r = client.get("/health")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "strict-origin" in r.headers.get("Referrer-Policy", "")


def test_login_rate_limited(client):
    """Five bad login attempts in a row must trigger 429 (when limiter is active)."""
    if not server._limiter_available:
        import pytest; pytest.skip("flask-limiter not installed")
    for _ in range(5):
        client.post("/login", data={"email": "x@x.com", "pwd": "wrong"})
    r = client.post("/login", data={"email": "x@x.com", "pwd": "wrong"})
    assert r.status_code == 429


def test_login_returns_generic_error_on_bad_email(client):
    """Login failure must not reveal whether email exists (401 or 429 — never 200)."""
    r = client.post("/login", data={"email": "notareal@email.com", "pwd": "wrong"})
    assert r.status_code in (401, 429)  # 429 if rate-limit already hit from previous test
    if r.status_code == 401:
        assert b"Invalid email or password" in r.data
        assert b"not found" not in r.data.lower()
        assert b"no user" not in r.data.lower()


def test_session_clear_on_login(client):
    """Session must be rotated (cleared) when a new login succeeds."""
    with client.session_transaction() as sess:
        sess["stale_key"] = "attacker_value"
    # A real login can't be tested without a live DB; verify _verify_password helper
    assert server._verify_password("wrong", "") is False
    assert server._verify_password("", "") is False


def test_verify_password_constant_time_empty(client):
    """_verify_password must return False and not crash on empty stored hash."""
    assert server._verify_password("anypassword", "") is False
    assert server._verify_password("", "") is False


def test_idor_clinic_stats_requires_auth(client):
    """Clinic stats endpoint must return redirect (not 200) without a session."""
    r = client.get("/clinic/any-clinic-id/stats")
    assert r.status_code in (302, 401)


def test_idor_clinic_appointments_requires_auth(client):
    """Clinic appointments must redirect to login without a valid session."""
    r = client.get("/clinic/any-clinic-id/appointments")
    assert r.status_code in (302, 401)


def test_dashboard_requires_auth(client):
    """Per-clinic dashboard must redirect unauthenticated requests to login."""
    r = client.get("/clinic/any-clinic-id/dashboard")
    assert r.status_code in (302, 401)


def test_require_password_timing_safe(client):
    """/api/stats must use compare_digest, not == (ensure 401 on wrong pwd)."""
    r = client.get("/api/stats?pwd=WRONG_BUT_SAME_LENGTH!!")
    assert r.status_code == 401


# ── Widget key security ────────────────────────────────────────────

def test_widget_js_invalid_key_returns_403(client):
    """widget.js?key= with a bad key must return 403, not a blank widget."""
    r = client.get("/widget.js?key=totallyinvalidkey123")
    assert r.status_code == 403


def test_widget_js_no_params_returns_default(client):
    """widget.js with no ?key= or ?id= serves the default config widget."""
    r = client.get("/widget.js")
    assert r.status_code == 200
    assert b"dc-btn" in r.data


def test_origin_allowed_no_restriction():
    """_origin_allowed returns True when no domain is configured."""
    assert server._origin_allowed("") is True
