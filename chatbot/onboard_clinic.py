#!/usr/bin/env python3
"""
Vicere Clinic Onboarding Tool
Scrapes a clinic website, extracts all info with Gemini, saves to Supabase.

Usage:
    python onboard_clinic.py https://brightsmile.co.uk
    python onboard_clinic.py https://brightsmile.co.uk --color 1a73e8 --email owner@clinic.com
"""

import json, os, re, sys, argparse, secrets
from urllib.parse import urlparse
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def _fetch_page(url: str, session) -> tuple[str, list]:
    """Returns (clean_text, list_of_internal_absolute_hrefs)."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlparse as _up

    resp = session.get(url, timeout=12)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    base = _up(url)
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        abs_href = urljoin(url, href).split("#")[0].split("?")[0]
        parsed = _up(abs_href)
        if parsed.netloc == base.netloc and abs_href != url:
            links.append({"url": abs_href, "text": a.get_text(strip=True)})

    for tag in soup(["script", "style", "nav", "footer", "meta", "link", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text, links


def _ai_pick_links(home_text: str, links: list, client, base_url: str) -> list:
    """Ask Gemini which links are worth crawling for clinic info."""
    from google.genai import types as genai_types

    link_list = "\n".join(
        f'{i+1}. [{l["text"] or "no text"}] {l["url"]}'
        for i, l in enumerate(links[:60])
    )

    prompt = f"""You are helping scrape a dental clinic website to build an AI chatbot.

Homepage content (summary):
{home_text[:2000]}

Links found on this page:
{link_list}

Which of these links likely contain useful clinic information such as:
- Team / doctors / staff
- Services / treatments
- About us / our story
- Contact / location / hours
- Awards / accreditations
- Pricing / payment plans
- Patient testimonials / reviews

Return ONLY a JSON array of the URLs you want to crawl, e.g.:
["https://example.com/about", "https://example.com/team"]

Return at most 8 URLs. Return ONLY the JSON array, nothing else."""

    resp = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=prompt,
        config=genai_types.GenerateContentConfig(max_output_tokens=500),
    )
    raw = resp.text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    try:
        chosen = json.loads(raw)
        return [u for u in chosen if isinstance(u, str)]
    except Exception:
        return []


def fetch_website(url: str, client) -> str:
    """Fetches homepage, lets AI pick relevant sub-pages, returns combined text."""
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; VicereBot/1.0)"

    print("   Fetching homepage...")
    home_text, links = _fetch_page(url, session)

    print(f"   Found {len(links)} links — asking AI which to crawl...")
    chosen_urls = _ai_pick_links(home_text, links, client, url)
    print(f"   AI selected {len(chosen_urls)} pages: {[u.split('/')[-1] or u for u in chosen_urls]}")

    pages_text = [f"=== Homepage ===\n{home_text[:3500]}"]
    for page_url in chosen_urls:
        try:
            page_text, _ = _fetch_page(page_url, session)
            label = page_url.rstrip("/").split("/")[-1] or page_url
            pages_text.append(f"=== {label} ===\n{page_text[:3000]}")
            print(f"   + crawled: {label}")
        except Exception as e:
            print(f"   ! skipped {page_url}: {e}")

    return "\n\n".join(pages_text)[:25000]


def extract_with_gemini(text: str, url: str, client) -> dict:
    from google.genai import types as genai_types

    prompt = f"""Extract all information about this dental clinic from their website and return ONLY valid JSON.

Website: {url}

Content:
{text}

Return a JSON object with every piece of information you can find. Include all fields you find — do not skip anything. Use these field names where applicable:

clinic_name, phone, emergency_phone, email, address, hours (object with days as keys),
services (array), doctors (array of objects with name/qualified/speciality),
accreditations (array), languages (array), parking, transport, nhs_or_private,
new_patient_offer, payment_plans, waiting_time, cancellation_policy, age_groups,
years_established, reviews_summary, awards, booking_url, welcome_message

For welcome_message write a friendly greeting using the clinic name.
For nhs_or_private use "nhs", "private", or "mixed".

Return ONLY the JSON object. No explanation, no markdown, no code blocks."""

    response = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=prompt,
        config=genai_types.GenerateContentConfig(max_output_tokens=2500),
    )

    raw = response.text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)


def generate_clinic_id(clinic_name: str) -> str:
    slug = clinic_name.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')[:50]


def save_to_supabase(clinic_id: str, owner_email: str, widget_color: str,
                     config: dict, dashboard_password: str,
                     widget_key: str, allowed_domain: str):
    sys.path.insert(0, os.path.dirname(__file__))
    from db import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clinics (id, owner_email, widget_color, config, dashboard_password, widget_key, allowed_domain)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                owner_email        = EXCLUDED.owner_email,
                widget_color       = EXCLUDED.widget_color,
                config             = EXCLUDED.config,
                dashboard_password = COALESCE(clinics.dashboard_password, EXCLUDED.dashboard_password),
                widget_key         = COALESCE(clinics.widget_key, EXCLUDED.widget_key),
                allowed_domain     = EXCLUDED.allowed_domain
        """, (clinic_id, owner_email, widget_color,
              json.dumps(config, ensure_ascii=False), dashboard_password,
              widget_key, allowed_domain))


def main():
    parser = argparse.ArgumentParser(description="Onboard a dental clinic")
    parser.add_argument("url", help="Clinic website URL")
    parser.add_argument("--color",  default="#2563eb", help="Widget colour hex e.g. 1a73e8")
    parser.add_argument("--email",  default="",        help="Clinic owner email for notifications")
    args = parser.parse_args()

    url = args.url if args.url.startswith("http") else f"https://{args.url}"
    color = args.color if args.color.startswith("#") else f"#{args.color}"

    from google import genai
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    print(f"\n  Vicere Clinic Onboarding")
    print(f"{'='*50}")
    print(f"  URL: {url}\n")

    print("1. Crawling website (AI-guided)...")
    try:
        text = fetch_website(url, client)
        print(f"   Done ({len(text)} chars total)\n")
    except Exception as e:
        print(f"   Failed: {e}")
        sys.exit(1)

    print("2. Extracting clinic info with Gemini...")
    try:
        config = extract_with_gemini(text, url, client)
        print(f"   Done ({len(config)} fields found)\n")
    except Exception as e:
        print(f"   Failed: {e}")
        sys.exit(1)

    clinic_name        = config.get("clinic_name", urlparse(url).netloc)
    clinic_id          = generate_clinic_id(clinic_name)
    dashboard_password = secrets.token_urlsafe(12)   # shown to Vicere only; stored hashed
    hashed_password    = generate_password_hash(dashboard_password)
    widget_key         = secrets.token_urlsafe(24)   # unguessable embed token
    allowed_domain     = urlparse(url).netloc         # e.g. citycentredental.co.uk

    print("3. Clinic summary:")
    print(f"   Name:          {clinic_name}")
    print(f"   ID:            {clinic_id}")
    print(f"   Phone:         {config.get('phone', 'Not found')}")
    print(f"   Address:       {config.get('address', 'Not found')}")
    print(f"   NHS/Private:   {config.get('nhs_or_private', 'Not found')}")
    print(f"   Services:      {len(config.get('services', []))} found")
    print(f"   Doctors:       {len(config.get('doctors', []))} found")
    print(f"   Languages:     {', '.join(config.get('languages', ['English']))}")
    print()

    print("4. Saving to Supabase...")
    try:
        save_to_supabase(clinic_id, args.email, color, config, hashed_password, widget_key, allowed_domain)
        print("   Done\n")
    except Exception as e:
        print(f"   Failed: {e}")
        print("\nExtracted config (save manually):")
        print(json.dumps(config, indent=2))
        sys.exit(1)

    server = "https://app.vicere.co.uk"
    print("=" * 50)
    print(f"  Onboarding complete!\n")
    print(f"  Embed code (paste before </body> on their website):")
    print(f'\n  <script src="{server}/widget.js?key={widget_key}"></script>\n')
    print(f"  Demo link:")
    print(f"  {server}/demo?id={clinic_id}\n")
    print(f"  Clinic dashboard (send these credentials to the clinic):")
    print(f"  URL:      {server}/clinic/{clinic_id}")
    print(f"  Password: {dashboard_password}\n")
    print(f"  Widget key (do not share — stored in DB):")
    print(f"  {widget_key}\n")
    print("  WARNING: Save the password — it cannot be recovered from the database.")
    print("=" * 50)


if __name__ == "__main__":
    main()
