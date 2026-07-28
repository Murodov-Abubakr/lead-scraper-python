#!/usr/bin/env python3
"""
Personalized Video Creator + Email Sender

Reads dental_leads.csv, creates a 1-minute personalized video per business
showing their website problems, adds a neural voiceover, and sends it via email.

Usage:
    python video_maker.py --test                    # process first lead only
    python video_maker.py --limit 10               # process 10 leads
    python video_maker.py                          # process all unsent leads
    python video_maker.py --lead "Smith Dental"    # process one by name
"""

import argparse
import asyncio
import csv
import logging
import os
import re
import shutil
import smtplib
import subprocess
import tempfile
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

LEADS_FILE = "dental_leads.csv"
SENT_FILE  = "dental_sent.csv"
VIDEOS_DIR = "videos"

SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
YOUR_NAME       = os.getenv("YOUR_NAME", "Murodov")

VIDEO_W, VIDEO_H = 1280, 720
FPS            = 24
FRAME_DURATION = 3   # seconds per slide

# ── Palette ──
BG_TOP      = (8,  12,  28)
BG_BOTTOM   = (18, 28,  58)
ACCENT_BLUE = (56, 189, 248)
ACCENT_DARK = (14, 52,  100)
RED_COLOR   = (239, 68,  68)
RED_DARK    = (70,  18,  18)
GREEN_COLOR = (52,  211, 153)
GREEN_DARK  = (6,   50,  35)
WHITE       = (255, 255, 255)
LIGHT_GRAY  = (148, 163, 184)
DARK_CARD   = (20,  35,  65)

os.makedirs(VIDEOS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# DESIGN HELPERS
# ═══════════════════════════════════════════════════════════════════

def draw_gradient_bg(img, top=BG_TOP, bottom=BG_BOTTOM):
    """Paint a vertical gradient onto img, return a fresh Draw object."""
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for y in range(h):
        t = y / (h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return ImageDraw.Draw(img)


def load_font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf"  if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibrib.ttf" if bold else "C:/Windows/Fonts/calibri.ttf",
        "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def wrap_text(text, font, max_width, draw):
    words = text.split()
    lines, current = [], []
    for word in words:
        test = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] > max_width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def draw_progress_bar(draw, current, total, y, h=8):
    x1, x2 = 60, VIDEO_W - 60
    draw.rounded_rectangle([x1, y, x2, y + h], radius=4, fill=(30, 45, 80))
    filled = int((x2 - x1) * (current / total))
    if filled > 8:
        draw.rounded_rectangle([x1, y, x1 + filled, y + h], radius=4, fill=ACCENT_BLUE)


def draw_glow_bar(draw, x1, y1, x2, y2, color, layers=3):
    """Simulate a soft glow by drawing progressively thinner/lighter rects."""
    r, g, b = color
    for i in range(layers, 0, -1):
        pad = i * 2
        faded = (min(255, r + 40), min(255, g + 40), min(255, b + 40))
        try:
            draw.rounded_rectangle(
                [x1 - pad, y1 - pad // 2, x2 + pad, y2 + pad // 2],
                radius=4, fill=faded
            )
        except Exception:
            pass
    draw.rounded_rectangle([x1, y1, x2, y2], radius=3, fill=color)

# ═══════════════════════════════════════════════════════════════════
# SCREENSHOT
# ═══════════════════════════════════════════════════════════════════

def take_website_screenshot(url, output_path):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--hide-scrollbars")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts,
        )
        driver.set_page_load_timeout(20)
        try:
            driver.get(url if url.startswith("http") else "https://" + url)
            time.sleep(3)
            driver.save_screenshot(output_path)
            return True
        finally:
            driver.quit()
    except Exception as e:
        log.warning("Screenshot failed for %s: %s", url, e)
        return False

# ═══════════════════════════════════════════════════════════════════
# FRAMES
# ═══════════════════════════════════════════════════════════════════

def make_title_frame(business_name, city, problems_count=0):
    img  = Image.new("RGB", (VIDEO_W, VIDEO_H), BG_TOP)
    draw = draw_gradient_bg(img)

    # ── Top accent bar ──
    draw.rectangle([0, 0, VIDEO_W, 5], fill=ACCENT_BLUE)

    # ── "WEBSITE AUDIT REPORT" badge ──
    draw.rounded_rectangle([60, 36, 328, 72], radius=6, fill=ACCENT_DARK)
    draw.rounded_rectangle([60, 36, 328, 72], radius=6, outline=ACCENT_BLUE, width=1)
    draw.text((78, 47), "📋  WEBSITE AUDIT REPORT", font=load_font(17), fill=ACCENT_BLUE)

    # ── Red divider accent ──
    draw.rectangle([60, 92, 150, 96], fill=RED_COLOR)

    # ── Business name ──
    f_big = load_font(52, bold=True)
    lines = wrap_text(business_name, f_big, VIDEO_W - 340, draw)
    y = 112
    for line in lines[:2]:
        draw.text((60, y), line, font=f_big, fill=WHITE)
        y += 66

    # ── City ──
    draw.text((62, y + 14), f"📍  {city}", font=load_font(26), fill=LIGHT_GRAY)

    # ── Right: issues badge ──
    bx1, by1, bx2, by2 = VIDEO_W - 230, 100, VIDEO_W - 50, 290
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=16, fill=RED_DARK)
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=16, outline=RED_COLOR, width=2)
    num_str = str(problems_count) if problems_count else "?"
    # Large number centered
    f_num = load_font(80, bold=True)
    nb = draw.textbbox((0, 0), num_str, font=f_num)
    nx = bx1 + (bx2 - bx1 - (nb[2] - nb[0])) // 2
    draw.text((nx, by1 + 20), num_str, font=f_num, fill=RED_COLOR)
    draw.text((bx1 + 30, by2 - 55), "ISSUES", font=load_font(22, bold=True), fill=RED_COLOR)
    draw.text((bx1 + 32, by2 - 28), "FOUND", font=load_font(22, bold=True), fill=RED_COLOR)

    # ── Bottom taglines ──
    draw.rectangle([0, VIDEO_H - 112, VIDEO_W, VIDEO_H - 111], fill=(30, 50, 100))
    draw.text(
        (60, VIDEO_H - 98),
        "We found issues on your website that may be costing you patients every day.",
        font=load_font(24), fill=LIGHT_GRAY,
    )
    draw.text(
        (60, VIDEO_H - 56),
        "Watch this 60-second audit to see exactly what we found →",
        font=load_font(23, bold=True), fill=ACCENT_BLUE,
    )

    # ── Bottom accent bar ──
    draw.rectangle([0, VIDEO_H - 5, VIDEO_W, VIDEO_H], fill=ACCENT_BLUE)
    return img


def make_website_frame(screenshot_path, business_name, problems):
    img  = Image.new("RGB", (VIDEO_W, VIDEO_H), BG_TOP)
    draw = draw_gradient_bg(img)

    # ── Paste screenshot with shadow ──
    if screenshot_path and os.path.exists(screenshot_path):
        try:
            ss = Image.open(screenshot_path).convert("RGB")
            ss = ss.resize((VIDEO_W - 120, VIDEO_H - 180), Image.LANCZOS)
            # Shadow
            draw.rectangle([68, 88, 68 + ss.width + 6, 88 + ss.height + 6], fill=(0, 0, 0))
            img.paste(ss, (60, 82))
        except Exception:
            pass

    draw = ImageDraw.Draw(img)

    # ── Top bar ──
    draw.rectangle([0, 0, VIDEO_W, 52], fill=(5, 10, 25))
    draw.text((22, 14), f"🌐  {business_name} — Current Website", font=load_font(22), fill=WHITE)

    # ── Bottom warning banner ──
    draw.rectangle([0, VIDEO_H - 72, VIDEO_W, VIDEO_H], fill=RED_COLOR)
    draw.text(
        (30, VIDEO_H - 54),
        f"⚠  {len(problems)} issues found — watch each one below",
        font=load_font(27, bold=True), fill=WHITE,
    )
    return img


PROBLEM_DB = {
    "no_website": (
        "❌", "No Website Found",
        "80% of patients search online before choosing a dentist.\nYou are completely invisible to all of them.",
        "SERVICE: Professional dental website — built in 7 days",
    ),
    "no_https": (
        "🔓", "Website Not Secure  (No HTTPS)",
        "Every browser shows a red \"Not Secure\" warning on your site.\nPatients see this the moment they arrive and click away.",
        "SERVICE: SSL security upgrade — same-day fix",
    ),
    "no_mobile_support": (
        "📱", "Not Mobile Friendly",
        "70% of patients search from their phone.\nYour site is broken and frustrating to use on mobile.",
        "SERVICE: Full mobile-responsive redesign",
    ),
    "no_online_booking": (
        "📅", "No Online Booking",
        "Patients who visit after 6 pm or on weekends cannot book.\nThey simply book with your competitor instead.",
        "SERVICE: AI booking system — works 24 / 7 automatically",
    ),
    "no_ai_chatbot": (
        "🤖", "No AI Chatbot or Live Chat",
        "Patient questions go unanswered outside office hours.\nThose patients book with whoever responds first.",
        "SERVICE: AI chatbot — answers questions & books appointments",
    ),
    "uses_zocdoc": (
        "💸", "Paying Per Patient via Zocdoc",
        "Zocdoc charges a fee for every single patient you acquire.\nYou are renting patients instead of owning your booking.",
        "SERVICE: Your own AI booking — flat monthly fee, keep 100%",
    ),
}


def _problem_info(problem_key):
    if problem_key.startswith("old_design_"):
        yr = problem_key.split("_")[-1]
        return (
            "🕰", f"Outdated Design — Last Updated {yr}",
            f"A website from {yr} signals your practice is behind the times.\nModern patients expect a modern, professional experience.",
            "SERVICE: Complete website redesign — modern & fast",
        )
    if problem_key.startswith("slow_site_"):
        score = problem_key.split("_")[-1]
        return (
            "🐢", f"Slow Website — Speed Score {score} / 100",
            f"Your site scores only {score} out of 100 for speed.\nVisitors leave within 3 seconds when a page is slow.",
            "SERVICE: Speed optimization — we target 90+ score",
        )
    if problem_key.startswith("bad_seo_"):
        score = problem_key.split("_")[-1]
        return (
            "🔍", f"Poor SEO — Score {score} / 100",
            f"Your site scores only {score} out of 100 for SEO.\nNew patients searching Google simply cannot find you.",
            "SERVICE: SEO optimization — rank for local searches",
        )
    return PROBLEM_DB.get(problem_key, (
        "⚠", problem_key.replace("_", " ").title(),
        "This issue is affecting your patient acquisition.",
        "SERVICE: We can fix this for you",
    ))


def make_problem_frame(problem_key, index, total):
    img  = Image.new("RGB", (VIDEO_W, VIDEO_H), BG_TOP)
    draw = draw_gradient_bg(img)

    # ── Left red accent bar ──
    draw.rectangle([0, 0, 6, VIDEO_H], fill=RED_COLOR)

    icon, title, desc, service = _problem_info(problem_key)

    # ── Issue label ──
    draw.text((30, 30), f"ISSUE  {index}  OF  {total}", font=load_font(16), fill=RED_COLOR)
    draw.rectangle([30, 58, 220, 61], fill=RED_COLOR)

    # ── Icon ──
    draw.text((30, 76), icon, font=load_font(68), fill=WHITE)

    # ── Title ──
    title_lines = wrap_text(title, load_font(40, bold=True), VIDEO_W - 160, draw)
    ty = 88
    for line in title_lines[:2]:
        draw.text((115, ty), line, font=load_font(40, bold=True), fill=WHITE)
        ty += 52

    # ── Description ──
    y = max(ty + 20, 210)
    for line in desc.split("\n"):
        draw.text((30, y), line, font=load_font(27), fill=LIGHT_GRAY)
        y += 44

    # ── Service box ──
    svc_y = VIDEO_H - 195
    draw.rounded_rectangle([30, svc_y, VIDEO_W - 30, svc_y + 72], radius=10, fill=GREEN_DARK)
    draw.rounded_rectangle([30, svc_y, VIDEO_W - 30, svc_y + 72], radius=10, outline=GREEN_COLOR, width=2)
    draw.text((55, svc_y + 22), f"✅  {service}", font=load_font(26, bold=True), fill=GREEN_COLOR)

    # ── Progress bar ──
    draw_progress_bar(draw, index, total, VIDEO_H - 28, h=10)

    # Slide counter
    counter = f"{index} / {total}"
    cb = draw.textbbox((0, 0), counter, font=load_font(16))
    draw.text((VIDEO_W - (cb[2] - cb[0]) - 20, VIDEO_H - 52), counter,
              font=load_font(16), fill=LIGHT_GRAY)

    return img


def make_cta_frame(your_name):
    img  = Image.new("RGB", (VIDEO_W, VIDEO_H), BG_TOP)
    draw = draw_gradient_bg(img, BG_TOP, (8, 28, 18))

    # ── Top green bar ──
    draw.rectangle([0, 0, VIDEO_W, 5], fill=GREEN_COLOR)

    # ── Headline ──
    draw.text((60, 46), "Ready to Fix All This?", font=load_font(54, bold=True), fill=WHITE)
    draw.rectangle([60, 118, 200, 122], fill=GREEN_COLOR)
    draw.text((60, 132), "Book a free 15-minute call — no obligation, no pressure",
              font=load_font(24), fill=LIGHT_GRAY)

    # ── Bullet list ──
    bullets = [
        "Full audit walkthrough — we review every issue together",
        "Custom fix plan built specifically for your practice",
        "Live demo of the AI booking chatbot",
        "Transparent pricing — no surprise fees",
    ]
    y = 210
    for text in bullets:
        draw.text((60, y), "✅", font=load_font(26), fill=GREEN_COLOR)
        draw.text((102, y + 2), text, font=load_font(24), fill=WHITE)
        y += 52

    # ── Contact card ──
    cy = VIDEO_H - 198
    draw.rounded_rectangle([60, cy, VIDEO_W - 60, cy + 142], radius=14, fill=ACCENT_DARK)
    draw.rounded_rectangle([60, cy, VIDEO_W - 60, cy + 142], radius=14, outline=ACCENT_BLUE, width=2)

    draw.text((90, cy + 18), your_name, font=load_font(34, bold=True), fill=WHITE)
    draw.text((90, cy + 62), f"📧  {SENDER_EMAIL or 'contact@yourdomain.com'}",
              font=load_font(23), fill=ACCENT_BLUE)
    draw.text((90, cy + 100), "💬  Reply to this email to book your free call",
              font=load_font(21), fill=LIGHT_GRAY)

    # ── Bottom green bar ──
    draw.rectangle([0, VIDEO_H - 5, VIDEO_W, VIDEO_H], fill=GREEN_COLOR)
    return img

# ═══════════════════════════════════════════════════════════════════
# VOICE  (edge-tts neural → gTTS fallback)
# ═══════════════════════════════════════════════════════════════════

def generate_voice(script, output_path):
    # Try Microsoft neural voice first (edge-tts, completely free)
    try:
        import edge_tts

        async def _run():
            communicate = edge_tts.Communicate(script, "en-US-JennyNeural", rate="+5%")
            await communicate.save(output_path)

        try:
            asyncio.run(_run())
        except RuntimeError:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(_run())

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            log.info("  Voice: edge-tts (neural) ✓")
            return True
    except Exception as e:
        log.warning("edge-tts failed: %s — trying gTTS", e)

    # Fallback: gTTS
    try:
        from gtts import gTTS
        tts = gTTS(script, lang="en", tld="com")
        tts.save(output_path)
        log.info("  Voice: gTTS (fallback) ✓")
        return True
    except Exception as e:
        log.warning("gTTS also failed: %s", e)
        return False

# ═══════════════════════════════════════════════════════════════════
# VIDEO ASSEMBLY
# ═══════════════════════════════════════════════════════════════════

def build_video(lead, output_path):
    name     = lead["business_name"]
    city     = lead.get("city", "")
    website  = lead.get("website", "")
    problems = [p.strip() for p in lead.get("problems", "").split("|") if p.strip()]
    pitch    = lead.get("pitch", "")

    frames = []

    # 1. Title — 3 s
    frames += [make_title_frame(name, city, len(problems))] * (FPS * 3)

    # 2. Website screenshot — 3 s
    ss_path = None
    if website:
        ss_path = output_path.replace(".mp4", "_ss.png")
        take_website_screenshot(website, ss_path)
    frames += [make_website_frame(ss_path, name, problems)] * (FPS * 3)

    # 3. Problem slides — 3 s each (max 6)
    visible = problems[:6]
    for i, prob in enumerate(visible, 1):
        frames += [make_problem_frame(prob, i, len(visible))] * (FPS * FRAME_DURATION)

    # 4. CTA — 4 s
    frames += [make_cta_frame(YOUR_NAME)] * (FPS * 4)

    tmpdir = tempfile.mkdtemp()
    try:
        for idx, frame in enumerate(frames):
            frame.save(os.path.join(tmpdir, f"frame_{idx:05d}.png"))

        audio_path = output_path.replace(".mp4", ".mp3")
        has_audio  = generate_voice(pitch, audio_path)

        import imageio_ffmpeg
        ffmpeg        = imageio_ffmpeg.get_ffmpeg_exe()
        frame_pattern = os.path.join(tmpdir, "frame_%05d.png")

        if has_audio:
            args = [
                ffmpeg, "-y",
                "-framerate", str(FPS), "-i", frame_pattern,
                "-i", audio_path,
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-shortest", "-c:a", "aac", "-b:a", "128k",
                output_path, "-loglevel", "error",
            ]
        else:
            args = [
                ffmpeg, "-y",
                "-framerate", str(FPS), "-i", frame_pattern,
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                output_path, "-loglevel", "error",
            ]

        ret = subprocess.run(args, capture_output=True).returncode
        return ret == 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if ss_path and os.path.exists(ss_path):
            os.remove(ss_path)

# ═══════════════════════════════════════════════════════════════════
# EMAIL
# ═══════════════════════════════════════════════════════════════════

def send_email(to_email, business_name, video_path, problems_list):
    if not to_email or not SENDER_EMAIL or not SENDER_PASSWORD:
        return False

    problems_readable = [p.replace("_", " ").title() for p in problems_list[:4]]
    bullets = "\n".join(f"  • {p}" for p in problems_readable)
    subject = f"{business_name} — I found {len(problems_list)} issues on your website"

    body = f"""Hi {business_name} team,

I was searching for a dentist in your area and came across your practice.

I ran a quick audit of your website and recorded a short 1-minute video
showing exactly what I found. I've attached it to this email.

Quick summary of issues found:
{bullets}

These are likely costing you new patients every single day.

I'd love to walk you through a fix — free 15-minute call, no obligation.

Best,
{YOUR_NAME}

P.S. The video shows your actual website and exactly where each issue appears.

---
To unsubscribe from future emails, reply with UNSUBSCRIBE.
"""

    try:
        msg = MIMEMultipart()
        msg["From"]    = SENDER_EMAIL
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if video_path and os.path.exists(video_path):
            with open(video_path, "rb") as f:
                part = MIMEBase("video", "mp4")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment",
                                filename=os.path.basename(video_path))
                msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        log.error("Email failed: %s", e)
        return False

# ═══════════════════════════════════════════════════════════════════
# SENT TRACKER
# ═══════════════════════════════════════════════════════════════════

def load_sent():
    sent = set()
    if not os.path.exists(SENT_FILE):
        return sent
    with open(SENT_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sent.add(r.get("business_name", "").lower())
    return sent


def mark_sent(lead, video_path, emailed):
    is_new = not os.path.exists(SENT_FILE) or os.path.getsize(SENT_FILE) == 0
    with open(SENT_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["business_name", "email", "phone",
                                          "video_path", "emailed", "timestamp"])
        if is_new:
            w.writeheader()
        w.writerow({
            "business_name": lead["business_name"],
            "email":         lead.get("email", ""),
            "phone":         lead.get("phone", ""),
            "video_path":    video_path,
            "emailed":       emailed,
            "timestamp":     time.strftime("%Y-%m-%d %H:%M"),
        })

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def process_leads(leads, limit=None):
    sent  = load_sent()
    count = 0

    for lead in leads:
        if limit and count >= limit:
            break

        name = lead.get("business_name", "").strip()
        if not name or name.lower() in sent:
            continue

        problems = [p.strip() for p in lead.get("problems", "").split("|") if p.strip()]
        if not problems:
            continue

        log.info("Processing: %s (%d problems)", name, len(problems))

        safe_name  = re.sub(r"[^\w]", "_", name)[:40]
        video_path = os.path.join(VIDEOS_DIR, f"{safe_name}.mp4")

        success = build_video(lead, video_path)
        if not success:
            log.warning("Video build failed for %s", name)
            video_path = None

        email   = lead.get("email", "")
        emailed = False
        if email and video_path:
            emailed = send_email(email, name, video_path, problems)
            log.info("  Email %s → %s", "sent" if emailed else "FAILED", email)
        elif not email:
            log.info("  No email — video saved for manual outreach")

        mark_sent(lead, video_path or "", emailed)
        count += 1
        log.info("  ✓ Done: %s | video=%s | emailed=%s", name, bool(video_path), emailed)

    log.info("Processed %d leads. Videos in: %s/", count, VIDEOS_DIR)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--test",  action="store_true", help="Process 1 lead only")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--lead",  default=None, help="Process specific business by name")
    args = p.parse_args()

    if not os.path.exists(LEADS_FILE):
        log.error("%s not found. Run lead_scraper.py first.", LEADS_FILE)
        return

    leads = list(csv.DictReader(open(LEADS_FILE, encoding="utf-8")))
    log.info("Loaded %d leads from %s", len(leads), LEADS_FILE)

    if args.lead:
        leads = [l for l in leads if args.lead.lower() in l["business_name"].lower()]
    elif args.test:
        leads = leads[:1]

    process_leads(leads, limit=args.limit)


if __name__ == "__main__":
    main()
