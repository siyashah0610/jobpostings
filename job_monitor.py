#!/usr/bin/env python3
"""
Daily job posting monitor for Anthropic, OpenAI, Google, Meta, NVIDIA, and Waymo.

What it does:
  1. Fetches current open roles from each company.
  2. Filters to titles matching your keyword list (see KEYWORDS below).
  3. Compares against data/seen_jobs.json to figure out what's NEW since last run.
  4. Writes docs/index.html -- a dashboard you can open locally or publish via
     GitHub Pages -- showing "New since last check" plus all matching open roles.

Run it: python job_monitor.py
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "seen_jobs.json"
DASHBOARD_FILE = ROOT / "docs" / "index.html"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalJobMonitor/1.0)"}

# ---------------------------------------------------------------------------
# Roles you care about. Matched case-insensitively against job titles.
# This is the main thing you'll want to edit/tune over time.
# ---------------------------------------------------------------------------
KEYWORDS = [
    # Engineering roles
    r"\bsoftware engineer", r"\bswe\b", r"\bbackend\b", r"\bfront[- ]?end\b",
    r"\bfull[- ]?stack\b", r"\bsite reliability\b", r"\bsystems engineer\b",
    r"\bengineer\b",
    # Product roles
    r"\bproduct manager\b", r"\bproduct management\b", r"\bproduct\b",
    # AI/ML roles
    r"\bapplied (ai|scientist)\b", r"\bmachine learning engineer\b", r"\bml engineer\b",
    r"\bai engineer\b", r"\bapplied ml\b", r"\bai product\b",
    # Solutions/Architecture roles
    r"\bsolutions? architect\b", r"\bsolutions? engineer\b", r"\barchitect\b",
    # Sales/Account/Business Development
    r"\bsales\b", r"\baccount executive\b", r"\baccount manager\b",
    r"\bbusiness development\b", r"\bgo[- ]to[- ]market\b", r"\bpartnerships?\b",
    # Strategy roles
    r"\bstrategy\b", r"\bstrategic\b",
    # Business/Operations roles
    r"\bbusiness analyst\b", r"\banalytics?\b", r"\boperations\b", r"\bops\b",
    r"\bprogram manager\b", r"\bprogram management\b",
    r"\bmanager\b", r"\bdirector\b",
    # Finance/Commercial
    r"\bfinance\b", r"\bcommercial\b",
    # Marketing
    r"\bmarketing\b",
]
KEYWORD_RE = re.compile("|".join(KEYWORDS), re.IGNORECASE)


def matches_interest(title: str) -> bool:
    return bool(KEYWORD_RE.search(title or ""))


def is_us_location(location: str) -> bool:
    """Check if a location is in the United States.

    Returns True if:
    - Location is empty/unknown (assume it could be US)
    - Location contains US state abbreviations
    - Location contains "United States" or "USA"
    - Location is a major US city

    Returns False if location indicates non-US country.
    """
    if not location:
        return True  # Include jobs without location data

    location_upper = location.upper()

    # Check for explicit NON-US country indicators first
    non_us_countries = {
        "UK", "UNITED KINGDOM", "ENGLAND", "LONDON",
        "AUSTRALIA", "SYDNEY", "MELBOURNE",
        "CANADA", "TORONTO", "VANCOUVER",
        "JAPAN", "TOKYO", "OSAKA",
        "SINGAPORE", "HONG KONG",
        "FRANCE", "PARIS", "LYON",
        "GERMANY", "BERLIN", "MUNICH", "DACH",
        "NETHERLANDS", "AMSTERDAM",
        "IRELAND", "DUBLIN",
        "SPAIN", "MADRID", "BARCELONA",
        "ITALY", "ROME", "MILAN",
        "SOUTH KOREA", "SEOUL",
        "INDIA", "BANGALORE", "MUMBAI",
        "ISRAEL", "TEL AVIV",
        "UAE", "DUBAI", "ABU DHABI",
        "MEXICO", "BUENOS AIRES", "LATIN AMERICA",
        "NEW ZEALAND", "AUCKLAND",
        "EMEA", "APAC", "ASEAN",
    }

    for country in non_us_countries:
        if country in location_upper:
            return False

    # US states (abbreviations)
    us_states = {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"
    }

    # Check for explicit US indicators
    if "UNITED STATES" in location_upper or "USA" in location_upper or "US-" in location_upper:
        return True

    # Check for state abbreviations (e.g., "CA", "NY")
    for state in us_states:
        if f", {state}" in location_upper or f" {state}" in location_upper or f"-{state}" in location_upper:
            return True

    # Check for major US cities
    us_cities = {
        "SAN FRANCISCO", "LOS ANGELES", "NEW YORK", "SEATTLE", "AUSTIN",
        "DENVER", "CHICAGO", "BOSTON", "MIAMI", "PORTLAND",
        "MOUNTAIN VIEW", "PALO ALTO", "SUNNYVALE", "CUPERTINO",
    }

    for city in us_cities:
        if city in location_upper:
            return True

    # Remote or unclear - include if it says "remote" without country
    if "REMOTE" in location_upper and not any(c in location_upper for c in non_us_countries):
        return True

    # If we can't determine, exclude to be safe
    return False


# ---------------------------------------------------------------------------
# Per-company fetchers. Each returns a list of dicts:
#   {"id": str, "company": str, "title": str, "location": str, "url": str}
#
# Confidence level (see README for details):
#   Anthropic, OpenAI  -> solid, official public JSON APIs
#   NVIDIA             -> reliable pattern (Workday's internal JSON endpoint),
#                         but Workday can rate-limit or shift facet formats
#   Google              -> best-effort HTML scrape of careers.google.com
#   Meta, Waymo         -> most fragile: neither publishes a public jobs API,
#                         so these scrape whatever structure is embedded in
#                         the page today. Most likely to need fixing later.
#
# Every fetcher is called inside a try/except in main(), so one breaking
# does not take down the others.
# ---------------------------------------------------------------------------

def fetch_anthropic():
    url = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "id": f"anthropic-{j['id']}",
            "company": "Anthropic",
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
        })
    return jobs


def fetch_openai():
    url = "https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=false"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "id": f"openai-{j['id']}",
            "company": "OpenAI",
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
        })
    return jobs


def fetch_nvidia():
    url = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    jobs = []
    offset = 0
    limit = 20
    while True:
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        r = requests.post(
            url, json=payload,
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            path = p.get("externalPath", "")
            jobs.append({
                "id": f"nvidia-{path}",
                "company": "NVIDIA",
                "title": p.get("title", ""),
                "location": p.get("locationsText", ""),
                "url": f"https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite{path}",
            })
        offset += limit
        total = data.get("total", 0)
        if offset >= total or offset > 3000:
            break
        time.sleep(0.3)
    return jobs


def fetch_google():
    """Fetch Google jobs using headless browser if available."""
    jobs = []
    if not PLAYWRIGHT_AVAILABLE:
        return jobs

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("https://www.google.com/careers", wait_until="networkidle", timeout=60000)

            # Wait for job listings to load
            page.wait_for_timeout(3000)
            html = page.content()

            # Extract job titles and links
            for match in re.finditer(r'"title"\s*:\s*"([^"]{5,200})"', html):
                title = match.group(1).strip()
                if len(title) > 5:
                    jobs.append({
                        "id": f"google-{len(jobs)}",
                        "company": "Google",
                        "title": title,
                        "location": "",
                        "url": "https://www.google.com/careers",
                    })

            browser.close()
    except Exception as e:
        pass

    return jobs[:100]


def fetch_waymo():
    """Fetch Waymo jobs using headless browser."""
    jobs = []
    if not PLAYWRIGHT_AVAILABLE:
        return jobs

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("https://waymo.com/joinus/", wait_until="networkidle", timeout=60000)

            # Wait for job listings to load
            page.wait_for_timeout(3000)
            html = page.content()

            # Extract job titles
            for match in re.finditer(r'"title"\s*:\s*"([^"]{3,150})"', html):
                title = match.group(1).strip()
                if len(title) > 3:
                    jobs.append({
                        "id": f"waymo-{len(jobs)}",
                        "company": "Waymo",
                        "title": title,
                        "location": "",
                        "url": "https://waymo.com/joinus/",
                    })

            browser.close()
    except Exception:
        pass

    return jobs[:100]


def fetch_meta():
    """Fetch Meta jobs using headless browser."""
    jobs = []
    if not PLAYWRIGHT_AVAILABLE:
        return jobs

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("https://www.metacareers.com/jobs", wait_until="networkidle", timeout=60000)

            # Wait for job listings to load
            page.wait_for_timeout(3000)
            html = page.content()

            # Extract job titles and IDs
            for match in re.finditer(r'"title"\s*:\s*"([^"]{3,150})"', html):
                title = match.group(1).strip()
                if len(title) > 3:
                    jobs.append({
                        "id": f"meta-{len(jobs)}",
                        "company": "Meta",
                        "title": title,
                        "location": "",
                        "url": "https://www.metacareers.com/jobs",
                    })

            browser.close()
    except Exception:
        pass

    return jobs[:100]


FETCHERS = {
    "Anthropic": fetch_anthropic,
    "OpenAI": fetch_openai,
    "NVIDIA": fetch_nvidia,
    "Google": fetch_google,
    "Waymo": fetch_waymo,
    "Meta": fetch_meta,
}


def load_state():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {}


def save_state(state):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def build_dashboard(all_matching, new_today, errors, state):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def escape(s):
        return (
            (s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        )

    def job_row(job, is_new=False):
        badge = '<span class="new-badge">NEW</span>' if is_new else ""
        loc = f'<span class="loc">{escape(job.get("location") or "")}</span>' if job.get("location") else ""
        status = state.get(job["id"], {}).get("apply_status", "need_to_apply")

        status_options = [
            ("need_to_apply", "Need to Apply"),
            ("dont_want_to_apply", "Don't Want to Apply"),
            ("applied", "Applied")
        ]

        status_html = '<div class="status-toggle">'
        for status_val, status_label in status_options:
            checked = 'checked' if status == status_val else ''
            status_html += f'''<label class="status-option">
              <input type="radio" name="status-{escape(job['id'])}" value="{status_val}" {checked} data-job-id="{escape(job['id'])}">
              <span>{status_label}</span>
            </label>'''
        status_html += '</div>'

        return f"""
        <div class="job-row">
          <div class="job-info">
            <a href="{escape(job['url'])}" target="_blank" rel="noopener" class="job-link">
              <span class="company-tag company-{job['company'].lower()}">{escape(job['company'])}</span>
              <span class="title">{escape(job['title'])}</span>
              {loc}
              {badge}
            </a>
          </div>
          {status_html}
        </div>"""

    by_company = {}
    for j in all_matching:
        by_company.setdefault(j["company"], []).append(j)

    new_ids = {j["id"] for j in new_today}

    base_html_template = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Radar{page_title}</title>
<style>
  :root {{
    --ink: #0b1220;
    --panel: #111a2c;
    --line: #21304a;
    --text: #dbe4f3;
    --muted: #7f92b3;
    --signal: #35e0a1;
    --accent: #5b8cff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--ink);
    color: var(--text);
    font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
    line-height: 1.5;
    padding: 40px 20px 80px;
  }}
  .wrap {{ max-width: 780px; margin: 0 auto; }}
  header {{ margin-bottom: 32px; }}
  .eyebrow {{
    color: var(--signal);
    font-size: 12px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .eyebrow::before {{
    content: "";
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--signal);
    box-shadow: 0 0 0 0 rgba(53,224,161,0.6);
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0% {{ box-shadow: 0 0 0 0 rgba(53,224,161,0.5); }}
    70% {{ box-shadow: 0 0 0 8px rgba(53,224,161,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(53,224,161,0); }}
  }}
  h1 {{
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 34px;
    margin: 10px 0 4px;
    font-weight: 400;
    color: #fff;
  }}
  h1 a {{ color: inherit; text-decoration: none; }}
  h1 a:hover {{ color: var(--accent); }}
  .subtitle {{ color: var(--muted); font-size: 13px; }}
  nav {{
    display: flex;
    gap: 16px;
    margin-top: 16px;
    flex-wrap: wrap;
  }}
  nav a {{
    color: var(--accent);
    text-decoration: none;
    font-size: 12px;
    padding: 4px 8px;
    border: 1px solid var(--line);
    border-radius: 3px;
  }}
  nav a:hover {{ background: var(--panel); }}
  nav a.active {{ background: var(--accent); color: var(--ink); }}
  h2 {{
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
    padding-bottom: 8px;
    margin: 36px 0 4px;
    display: flex;
    align-items: baseline;
    gap: 8px;
  }}
  .count {{ color: var(--accent); font-size: 12px; }}
  .job-row {{
    padding: 12px 4px;
    border-bottom: 1px solid var(--line);
    font-size: 14px;
  }}
  .job-info {{
    margin-bottom: 8px;
  }}
  .job-link {{
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    color: var(--text);
    flex-wrap: wrap;
  }}
  .job-row:hover {{ background: var(--panel); }}
  .title {{ flex: 1; min-width: 200px; }}
  .loc {{ color: var(--muted); font-size: 12px; }}
  .company-tag {{
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 3px;
    border: 1px solid var(--line);
    color: var(--muted);
    white-space: nowrap;
  }}
  .new-badge {{
    background: var(--signal);
    color: #06231a;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 3px;
    letter-spacing: 0.05em;
  }}
  .status-toggle {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .status-option {{
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    cursor: pointer;
  }}
  .status-option input {{
    cursor: pointer;
  }}
  .status-option span {{
    color: var(--muted);
  }}
  .status-option input:checked + span {{
    color: var(--signal);
    font-weight: 700;
  }}
  .empty {{ color: var(--muted); font-size: 13px; padding: 12px 4px; }}
  .error-banner {{
    margin-top: 32px;
    padding: 12px 16px;
    border: 1px solid #5a3a1a;
    background: #201409;
    color: #e0b378;
    font-size: 12px;
    border-radius: 4px;
  }}
  .error-banner ul {{ margin: 6px 0 0; padding-left: 18px; }}
  footer {{ margin-top: 48px; color: var(--muted); font-size: 11px; }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="eyebrow">Scanning six companies daily</div>
      <h1><a href="index.html">Job Radar</a></h1>
      <div class="subtitle">Last checked {now}</div>
      <nav>
        {nav_links}
      </nav>
    </header>

    {content}

    {error_banner}

    <footer>{footer_text}</footer>
  </div>

  <script>
    document.querySelectorAll('input[type="radio"]').forEach(input => {{
      input.addEventListener('change', function() {{
        const jobId = this.dataset.jobId;
        const status = this.value;
        localStorage.setItem('job-status-' + jobId, status);
      }});
    }});

    document.querySelectorAll('input[type="radio"]').forEach(input => {{
      const jobId = input.dataset.jobId;
      const savedStatus = localStorage.getItem('job-status-' + jobId);
      if (savedStatus && input.value === savedStatus) {{
        input.checked = true;
      }}
    }});
  </script>
</body>
</html>"""

    # Generate navigation links
    nav_links = '<a href="index.html" class="active">Home</a>'
    for company in sorted(FETCHERS.keys()):
        nav_links += f'<a href="{company.lower()}.html">{company}</a>'

    error_banner = ""
    if errors:
        items = "".join(f"<li>{escape(e)}</li>" for e in errors)
        error_banner = f"""
        <div class="error-banner">
          <strong>Some sources didn't load this run:</strong>
          <ul>{items}</ul>
        </div>"""

    # Build home page (all new listings)
    new_section = "".join(job_row(j, is_new=True) for j in new_today) if new_today else \
        '<p class="empty">No new matching postings since the last check.</p>'

    home_content = f"""
    <h2>New since last check <span class="count">{len(new_today)}</span></h2>
    {new_section}"""

    footer_text = f"Tracking {len(state)} previously-seen matching postings across {len(FETCHERS)} companies."

    home_html = base_html_template.format(
        page_title="",
        nav_links=nav_links,
        content=home_content,
        error_banner=error_banner,
        footer_text=footer_text,
        now=now
    )

    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.write_text(home_html)

    # Build individual company pages
    for company in sorted(FETCHERS.keys()):
        jobs = sorted(by_company.get(company, []), key=lambda j: j["title"])

        new_jobs = [j for j in jobs if j["id"] in new_ids]
        old_jobs = [j for j in jobs if j["id"] not in new_ids]

        new_rows = "".join(job_row(j, is_new=True) for j in new_jobs) if new_jobs else ""
        old_rows = "".join(job_row(j, is_new=False) for j in old_jobs) if old_jobs else ""

        new_section_company = f'<h2>New <span class="count">{len(new_jobs)}</span></h2>\n{new_rows}' if new_jobs else ""
        no_jobs_msg = '<p class="empty">No matching postings for this company.</p>' if not jobs else ""
        old_section_company = f'<h2>All Listings <span class="count">{len(old_jobs)}</span></h2>\n{old_rows}' if old_jobs else no_jobs_msg

        company_content = f"{new_section_company}\n{old_section_company}"

        company_html = base_html_template.format(
            page_title=f" - {company}",
            nav_links=nav_links,
            content=company_content,
            error_banner="",
            footer_text=footer_text,
            now=now
        )

        company_file = DASHBOARD_FILE.parent / f"{company.lower()}.html"
        company_file.write_text(company_html)


def main():
    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_matching = []
    new_today = []
    errors = []
    active_job_ids = set()

    for company, fetch in FETCHERS.items():
        print(f"Checking {company}...")
        try:
            postings = fetch()
        except Exception as e:
            print(f"  ! {company} failed: {e}")
            errors.append(f"{company}: {e}")
            continue
        matched = [p for p in postings if matches_interest(p["title"]) and is_us_location(p.get("location", ""))]
        print(f"  {len(postings)} total postings, {len(matched)} match your keywords")
        for job in matched:
            all_matching.append(job)
            active_job_ids.add(job["id"])
            if job["id"] not in state:
                state[job["id"]] = {
                    "title": job["title"],
                    "company": job["company"],
                    "url": job["url"],
                    "first_seen": today,
                    "apply_status": "need_to_apply",
                }
                new_today.append(job)

    # Remove stale jobs that are no longer listed
    stale_ids = set(state.keys()) - active_job_ids
    if stale_ids:
        print(f"Removing {len(stale_ids)} stale job(s) no longer active")
        for job_id in stale_ids:
            del state[job_id]

    save_state(state)
    build_dashboard(all_matching, new_today, errors, state)

    print(f"\n{len(new_today)} new posting(s) today across {len(FETCHERS)} companies.")
    if errors:
        print("Errors:")
        for e in errors:
            print(" -", e)


if __name__ == "__main__":
    main()
