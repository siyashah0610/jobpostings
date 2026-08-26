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

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "seen_jobs.json"
DASHBOARD_FILE = ROOT / "docs" / "index.html"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalJobMonitor/1.0)"}

# ---------------------------------------------------------------------------
# Roles you care about. Matched case-insensitively against job titles.
# This is the main thing you'll want to edit/tune over time.
# ---------------------------------------------------------------------------
KEYWORDS = [
    r"\bsoftware engineer", r"\bswe\b", r"\bbackend\b", r"\bfront[- ]?end\b",
    r"\bfull[- ]?stack\b", r"\bsite reliability\b", r"\bsystems engineer\b",
    r"\bproduct manager\b", r"\bproduct management\b",
    r"\bapplied (ai|scientist)\b", r"\bmachine learning engineer\b", r"\bml engineer\b",
    r"\bai engineer\b", r"\bapplied ml\b", r"\bai product\b",
    r"\bsolutions? architect\b", r"\bsolutions? engineer\b",
    r"\bsales\b", r"\baccount executive\b", r"\baccount manager\b",
    r"\bbusiness development\b", r"\bgo[- ]to[- ]market\b", r"\bpartnerships?\b",
]
KEYWORD_RE = re.compile("|".join(KEYWORDS), re.IGNORECASE)


def matches_interest(title: str) -> bool:
    return bool(KEYWORD_RE.search(title or ""))


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
    """Best-effort scrape of Google's public careers search-results pages."""
    jobs = []
    base = "https://www.google.com/about/careers/applications/jobs/results/"
    for page in range(1, 16):
        r = requests.get(base, params={"page": page, "hl": "en"}, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            break
        html = r.text
        # Job result links look like /about/careers/applications/jobs/results/<id>-<slug>
        found = re.findall(
            r'href="(/about/careers/applications/jobs/results/(\d+)-[^"?#]+)"[^>]*>\s*([^<]{3,200})',
            html,
        )
        if not found:
            break
        for href, jid, title in found:
            title = title.strip()
            if not title:
                continue
            jobs.append({
                "id": f"google-{jid}",
                "company": "Google",
                "title": title,
                "location": "",
                "url": f"https://www.google.com{href}",
            })
        time.sleep(0.3)
    seen, deduped = set(), []
    for j in jobs:
        if j["id"] not in seen:
            seen.add(j["id"])
            deduped.append(j)
    return deduped


def fetch_waymo():
    """Best-effort: waymo.com/joinus is a JS app with no documented public
    API. This looks for embedded JSON in the page. Likely needs adjustment --
    see README's troubleshooting section."""
    jobs = []
    r = requests.get("https://waymo.com/joinus/", headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    blob_text = m.group(1) if m else html
    for jm in re.finditer(r'"title":"([^"]{3,150})"[^}]{0,300}?"id":"?([\w-]+)"?', blob_text):
        title, jid = jm.group(1), jm.group(2)
        jobs.append({
            "id": f"waymo-{jid}",
            "company": "Waymo",
            "title": title,
            "location": "",
            "url": "https://waymo.com/joinus/",
        })
    seen, deduped = set(), []
    for j in jobs:
        if j["id"] not in seen:
            seen.add(j["id"])
            deduped.append(j)
    return deduped


def fetch_meta():
    """Best-effort: Meta does not publish a public careers API. Scrapes
    whatever job data is embedded in metacareers.com/jobs today. This is the
    most likely connector to break -- see README's troubleshooting section."""
    jobs = []
    r = requests.get("https://www.metacareers.com/jobs", headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text
    for jm in re.finditer(r'"job_req_id":"(\d+)"[^}]{0,300}?"title":"([^"]{3,150})"', html):
        jid, title = jm.group(1), jm.group(2)
        jobs.append({
            "id": f"meta-{jid}",
            "company": "Meta",
            "title": title,
            "location": "",
            "url": f"https://www.metacareers.com/jobs/{jid}",
        })
    seen, deduped = set(), []
    for j in jobs:
        if j["id"] not in seen:
            seen.add(j["id"])
            deduped.append(j)
    return deduped


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

    def job_row(job, is_new=False):
        badge = '<span class="new-badge">NEW</span>' if is_new else ""
        loc = f'<span class="loc">{escape(job.get("location") or "")}</span>' if job.get("location") else ""
        return f"""
        <a class="job-row" href="{escape(job['url'])}" target="_blank" rel="noopener">
          <span class="company-tag company-{job['company'].lower()}">{escape(job['company'])}</span>
          <span class="title">{escape(job['title'])}</span>
          {loc}
          {badge}
        </a>"""

    def escape(s):
        return (
            (s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        )

    by_company = {}
    for j in all_matching:
        by_company.setdefault(j["company"], []).append(j)

    new_ids = {j["id"] for j in new_today}

    new_section = "".join(job_row(j, is_new=True) for j in new_today) if new_today else \
        '<p class="empty">No new matching postings since the last check.</p>'

    company_sections = ""
    for company in FETCHERS.keys():
        jobs = sorted(by_company.get(company, []), key=lambda j: j["title"])
        if not jobs:
            continue
        rows = "".join(job_row(j, is_new=(j["id"] in new_ids)) for j in jobs)
        company_sections += f"""
        <section class="company-section">
          <h2>{escape(company)} <span class="count">{len(jobs)}</span></h2>
          {rows}
        </section>"""

    error_banner = ""
    if errors:
        items = "".join(f"<li>{escape(e)}</li>" for e in errors)
        error_banner = f"""
        <div class="error-banner">
          <strong>Some sources didn't load this run:</strong>
          <ul>{items}</ul>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Radar</title>
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
  .subtitle {{ color: var(--muted); font-size: 13px; }}
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
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 4px;
    border-bottom: 1px solid var(--line);
    text-decoration: none;
    color: var(--text);
    font-size: 14px;
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
      <h1>Job Radar</h1>
      <div class="subtitle">Last checked {now}</div>
    </header>

    <h2>New since last check <span class="count">{len(new_today)}</span></h2>
    {new_section}

    {company_sections}

    {error_banner}

    <footer>Tracking {len(state)} previously-seen matching postings across {len(FETCHERS)} companies.</footer>
  </div>
</body>
</html>"""
    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.write_text(html)


def main():
    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_matching = []
    new_today = []
    errors = []

    for company, fetch in FETCHERS.items():
        print(f"Checking {company}...")
        try:
            postings = fetch()
        except Exception as e:
            print(f"  ! {company} failed: {e}")
            errors.append(f"{company}: {e}")
            continue
        matched = [p for p in postings if matches_interest(p["title"])]
        print(f"  {len(postings)} total postings, {len(matched)} match your keywords")
        for job in matched:
            all_matching.append(job)
            if job["id"] not in state:
                state[job["id"]] = {
                    "title": job["title"],
                    "company": job["company"],
                    "url": job["url"],
                    "first_seen": today,
                }
                new_today.append(job)

    save_state(state)
    build_dashboard(all_matching, new_today, errors, state)

    print(f"\n{len(new_today)} new posting(s) today across {len(FETCHERS)} companies.")
    if errors:
        print("Errors:")
        for e in errors:
            print(" -", e)


if __name__ == "__main__":
    main()
