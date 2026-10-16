# SAM daily search

Automated SAM.gov opportunity keyword checks. Produces Excel + HTML reports.

## Public report

After GitHub Pages is enabled and a workflow has run:

- HTML: https://biscuitdh.github.io/44aa9566-3f78-4f70-9255-8cc9b8d7e019/v/
- Forensics watch: https://biscuitdh.github.io/44aa9566-3f78-4f70-9255-8cc9b8d7e019/v/forensics.html
- Excel: https://biscuitdh.github.io/44aa9566-3f78-4f70-9255-8cc9b8d7e019/v/SAM-daily-latest.xlsx

Low-profile path (`/v/`), `noindex`, robots disallow. Content is public SAM.gov notice metadata only.

## GitHub Actions (once)

1. *(Optional)* Secret `SAM_API_KEY` — not required for Actions (frontend only)  
2. **Settings → Pages → Source:** GitHub Actions  
3. **Settings → Actions → General → Workflow permissions:** Read and write  
4. **Actions → SAM daily search → Run workflow** (first run)

Schedule: `0 9 * * *` UTC (≈ 04:00 EST / 05:00 EDT).

CI uses **moderate pacing** (`--robot`, ~3s between terms — typically a few minutes, not ~20). Local runs can use slower human pacing if you prefer.

## Local (optional)

```bash
git clone https://github.com/biscuitdh/44aa9566-3f78-4f70-9255-8cc9b8d7e019.git
cd 44aa9566-3f78-4f70-9255-8cc9b8d7e019
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add SAM_API_KEY if using API mode
.venv/bin/python scripts/sam_search.py --days 1 --source both --no-latest-sync
```

Terms: `config/search_terms.json`.

## History retention

Reports keep a **rolling 15 days** of day buckets and notices (`--history-days 15`).
Each scheduled search uses a **15-day posted-date window** (`--days 15`) so the tracker accumulates a two-week record.


## Long-running archive vs 15-day webpage

| What | Where | Retention |
|------|--------|-----------|
| Public webpage / Excel on Pages | `docs/v/` | **Rolling 15 days** |
| Durable notice list | `data/archive/notices-master.json` + `.csv` | **Keeps growing** (not purged) |
| Per-day snapshots | `data/archive/days/YYYY-MM-DD.json` | Kept in git |
| Append audit log | `data/archive/notices-append.jsonl` | Append-only |

The site stays lean; the repo keeps the long history under `data/archive/` (not deployed to Pages).

## Forensics watch (daily keyword check)

The full tracker also carries broad terms (`Cyber`, `IRS`, `EC2`), so every run builds a
forensics-only cut of the same data:

```bash
python scripts/forensics_digest.py            # after a search, or any time
```

Outputs:

| What | Where |
|------|-------|
| Public page | `docs/v/forensics.html` → `/v/forensics.html` (linked from the main report) |
| Daily Markdown record | `reports/forensics/YYYY-MM-DD.md` + `reports/forensics/latest.md` |
| CI job summary | one-line count of confirmed / new / due-soon hits |

Sections: **new today**, **deadlines within 30 days**, **all confirmed matches in the window**,
**needs review**, and a per-term count.

Keywords live in `config/watch_groups.json`:

- `strong_terms` / `title_keywords` → confirmed (e.g. `Forensic`, `GrayKey`, `Magnet Forensics`,
  a title mentioning `digital evidence`)
- `weak_terms` → review only. Short acronyms (`DC3`, `MSAB`, `XRY`, `Axiom`) also match inside
  unrelated titles, so they are listed separately instead of polluting the confirmed list.

The digest only re-cuts `data/history.json`; it never calls SAM.gov, so it is safe to re-run.
Add a group to `config/watch_groups.json` and pass `--group <key>` for other watch lists.

## Copy for Trello

Each result row on the HTML report has **Copy for Trello**. Click to copy a card-ready block (title, SAM link, terms, deadline, etc.), then paste into a new Trello card. No Trello API keys required.

