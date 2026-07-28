# Shopify Agency Email Outreach

Automatically finds Shopify agency contact emails and sends personalized job applications.

---

## Setup

### 1. Install dependencies

```bash
pip install requests beautifulsoup4 python-dotenv
```

### 2. Configure your credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | What to put |
|---|---|
| `YOUR_NAME` | Your full name (appears in email sign-off and From header) |
| `EMAIL_PROVIDER` | `gmail` or `zoho` |
| `SENDER_EMAIL` | The address you're sending from |
| `SENDER_PASSWORD` | **App password** — see below |

### Getting a Gmail App Password

1. Go to your Google Account → **Security** → **2-Step Verification** (must be ON)
2. At the bottom of that page, click **App passwords**
3. Create one named "Shopify Outreach" — copy the 16-character code
4. Paste it as `SENDER_PASSWORD` (no spaces)

### Getting a Zoho App Password

1. Log in to Zoho → **My Account** → **Security** → **App Passwords**
2. Generate a new one and paste it as `SENDER_PASSWORD`

---

## Usage

Run the three steps independently or all at once:

```bash
# Step 1 — collect agency websites → agencies.csv
python shopify_outreach.py --collect

# Step 2 — scrape each website for a contact email → results.csv
python shopify_outreach.py --scrape

# Step 3 — send personalized application emails
python shopify_outreach.py --send

# Run all three in one go
python shopify_outreach.py
```

The script is **resumable**: if you stop it mid-way, re-running the same step picks up where it left off. Already-processed domains are skipped.

---

## Output files

| File | Contents |
|---|---|
| `agencies.csv` | `agency_name, domain, source` — all collected agencies |
| `results.csv` | `agency_name, domain, email, status, timestamp` — per-domain processing log |

**Status values in results.csv:**

| Status | Meaning |
|---|---|
| `email_found` | Email scraped — ready to send |
| `no_email_found` | No email discovered on their site |
| `sent` | Application sent successfully |
| `failed` | SMTP error — to retry, change status back to `email_found` |

---

## Configuration (top of script)

| Variable | Default | Description |
|---|---|---|
| `DAILY_SEND_CAP` | `100` | Max emails per calendar day |
| `SEND_DELAY` | `(30, 45)` | Seconds between sends (randomized) |
| `SCRAPE_DELAY` | `(3, 8)` | Seconds between page fetches |
| `CONTACT_PATHS` | list | Pages checked per domain for an email |
| `EMAIL_PRIORITY` | list | Preferred email local-part keywords |

---

## Manually adding agencies

If the Partner Directory scraper yields nothing (it's a JavaScript SPA and may not render server-side), you can populate `agencies.csv` by hand:

```csv
agency_name,domain,source
WeCommerce Agency,wecommerce.co,manual
Pixel Union,pixelunion.net,manual
Ethercycle,ethercycle.com,manual
```

Then run `--scrape` and `--send` as normal.

---

## Realistic expectations

**Partner Directory (`partners.shopify.com/directory/agencies`)** — primary source.
This is the most reliable. The page is a React SPA so a plain HTTP request may or may not return embedded JSON data. The scraper tries three strategies (JSON API probe, `__NEXT_DATA__` parse, HTML card parse) before giving up. If all three fail, populate `agencies.csv` manually.

**DuckDuckGo search results** — best-effort secondary source.
DuckDuckGo may rate-limit or block requests with no warning. The scraper adds delays and retries, but don't count on this yielding results. It's a bonus, not the foundation.

**Email scraping** — works well for agencies that publish their email visibly.
Many agencies use contact forms instead of plain email addresses — these will show up as `no_email_found` and there's nothing the scraper can do about that.

---

## Notes

- The script respects `robots.txt` for each domain before scraping.
- Random delays are used throughout to reduce the chance of being rate-limited or blocked.
- Emails are sent as plain text (no HTML) to maximize deliverability and avoid spam filters.
- Three email template variations are rotated randomly so consecutive sends aren't identical.
