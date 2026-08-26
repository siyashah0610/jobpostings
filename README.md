# Job Radar

Checks Anthropic, OpenAI, Google, Meta, NVIDIA, and Waymo daily for new postings
matching your keywords (software engineering, product management, applied AI,
solutions architect, sales, etc.) and publishes a dashboard of what's new.

## Setup (10 minutes)

1. **Create a new GitHub repo** (public or private both work) and push these
   files to it.

2. **Turn on GitHub Pages**
   Repo → Settings → Pages → under "Build and deployment", set Source to
   **GitHub Actions**. That's it — no branch to pick.

3. **Turn on Actions permissions**
   Repo → Settings → Actions → General → under "Workflow permissions", select
   **Read and write permissions**. This lets the workflow commit the daily
   results back to the repo.

4. **Trigger the first run manually**
   Repo → Actions tab → "Daily Job Check" → Run workflow. After it finishes
   (~1–2 min), your dashboard will be live at
   `https://<your-username>.github.io/<repo-name>/`.

After that, it runs automatically every day at 13:00 UTC (6am PT). Change the
`cron:` line in `.github/workflows/daily-job-check.yml` to adjust the time —
[crontab.guru](https://crontab.guru) helps with the syntax.

## Running it locally instead

```
pip install -r requirements.txt
python job_monitor.py
open docs/index.html
```

## Tuning what counts as a match

Edit the `KEYWORDS` list at the top of `job_monitor.py`. It's a list of regex
fragments matched case-insensitively against job titles — add or remove terms
freely, e.g. `r"\brecruiter\b"` or `r"\bdesigner\b"`.

## How reliable is each company's connector?

Companies don't all publish job data the same way, so these are not equally
sturdy:

| Company    | Method                              | Confidence |
|------------|--------------------------------------|------------|
| Anthropic  | Official public Greenhouse API        | Solid |
| OpenAI     | Official public Ashby API             | Solid |
| NVIDIA     | Workday's internal JSON endpoint      | Good — a well-established pattern, but Workday can throttle or restructure facets |
| Google     | HTML scrape of careers.google.com     | Best-effort — page structure could shift |
| Waymo      | HTML/embedded-JSON scrape of waymo.com/joinus | Fragile — Waymo has no public API |
| Meta       | HTML scrape of metacareers.com        | Fragile — Meta has no public API, most likely to need fixing |

The script is written so **one company failing doesn't break the others** —
you'll see an error banner on the dashboard for whichever source didn't load,
and everything else still updates normally.

## Fixing a broken connector

If Google, Meta, or Waymo's scraper stops finding jobs (dashboard shows 0 for
that company, or an error), their page structure likely changed. To fix:

1. Open that company's careers page in a browser with DevTools open (Network
   tab), search for a role, and reload.
2. Look for the XHR/fetch request that returns job data — check its response
   for a JSON payload if you're lucky, or view the page source for embedded
   `<script>` JSON if not.
3. Update the corresponding `fetch_<company>()` function in `job_monitor.py`
   to match the new structure.

## Notes

- This only reads public job-listing pages — same data you'd see visiting each
  careers site yourself. Be a reasonable citizen: the daily schedule here is
  intentionally light (once a day, not continuous polling).
- `data/seen_jobs.json` is the full history of every matching posting ever
  seen, with the date first spotted — useful if you want to look back later.
