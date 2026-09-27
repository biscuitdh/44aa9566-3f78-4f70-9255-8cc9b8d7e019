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
| Announced-notice ledger | `reports/forensics/reported-notices.json` |
| CI job summary | one-line count of confirmed / new / due-soon hits |

Sections: **new today**, **not previously reported**, **amended / re-issued**, **deadlines within
30 days**, **all confirmed matches in the window**, **needs review**, and a per-term count.

Every table carries a **closes in** column (`closed` / `today` / *N* days), and the header reports
how many confirmed notices are still accepting responses. A notice stays in the window for about 15
days after SAM stops returning it, so most of the confirmed list is usually already closed and the
confirmed count on its own overstates what is actionable.

An award notice has nothing left to respond to, so its deadline and closes-in cells are always
empty — while the fact that matters, the winning vendor, was collected and then dropped. Tables
holding at least one award now render an **awarded to** column, decided from the rows rather than
passed in by the caller so a new section cannot omit it. Award *amounts* are not shown: SAM's
search index has never returned one for any of the awards this watch has matched.

A notice counts as new only on the date the tracker first saw it, so anything the SAM search picked
up *after* a digest had already been written would never be announced. The ledger
(`reports/forensics/reported-notices.json`) records every notice ID a digest has listed; confirmed
notices missing from it are flagged **not previously reported** with the date they were first seen.
Delete the ledger to rebuild it from the committed dated digests. `--no-ledger` falls back to
first-seen-date only; `--no-ledger-update` reads it without writing.

SAM.gov mints a fresh notice ID whenever a solicitation is amended, so the same solicitation
reappears as a first-time record — often with a pushed-back deadline. The digest matches on
solicitation number (across `data/history.json` and the durable archive) to keep those out of the
new-today count, list them under **amended** with the old → new deadline, and drop the superseded
copy from the deadline, confirmed, needs-review, per-term and all-time archive counts so nothing is
counted twice. Superseded copies are still drawn in the tables, greyed out, and every count that
hides some says how many records it covers — e.g. `28 (33 records incl. 5 superseded by an
amendment)`. The **all-time archive total** is where this matters most: unlike the 15-day window it
is never purged, so every revision a solicitation has ever had accumulates in it. On 2026-09-21 its
126 matching records covered only **99** distinct notices, 18 solicitations accounting for the rest.

Keywords live in `config/watch_groups.json`:

- `strong_terms` / `title_keywords` → confirmed (e.g. `Forensic`, `GrayKey`, `Magnet Forensics`,
  `Amped Authenticate`, a title mentioning `digital evidence`)
- `weak_terms` → review only. Short acronyms (`DC3`, `MSAB`, `XRY`, `Axiom`) also match inside
  unrelated titles, so they are listed separately instead of polluting the confirmed list.
- `watch_orgs` → review only. Contracting offices that buy forensics tooling under a bare vendor
  name (Searchlight Cyber, Chainalysis, BitMindz), which carries no forensics keyword. A notice
  from one of these offices with no keyword hit is surfaced for review rather than confirmed.
  Check an office's record in `data/archive/notices-master.json` before adding it.

A small share of SAM records arrive with an empty `organization`, which hid the buyer from the
report and left `watch_orgs` with nothing to match. The digest now fills that field from other
records sharing the same solicitation-number prefix, which identifies the contracting office, and
labels the value `(inferred from solicitation number)` so it is never mistaken for SAM data.
Prefixes whose records name more than one organization are left blank rather than guessed.

SAM.gov occasionally resets a connection mid-run. The affected term returns no hits, but the
search still exits 0 and publishes, so a term that never ran looks exactly like a term that found
nothing. `http_get_json` now retries transient resets and 5xx responses, and the digest header
reports what actually ran — `SAM search coverage: 26/26 terms queried without error`. When a term
is still missing after the retries, the report leads with a **degraded search** warning naming the
terms that returned nothing, flagging those the watch matches on, and saying the counts are a
floor. On 2026-09-19 six of 23 terms failed this way, `Forensic` among them.

Multi-word terms are a second, quieter way to lose coverage. SAM's search backend ignores the
quoting and ORs the tokens, so `Digital forensics` nominally matches tens of thousands of notices
and the search has to confirm the phrase itself. It used to do that against the description the
search response carries — which SAM truncates at roughly 250 characters. Any phrase appearing later
in the body was discarded, so in practice a multi-word term could only ever match a **title**, and
none had produced a single hit in the window in over thirty runs. The phrase check now runs after
the posted-date filter, which leaves a handful of candidates a day, and falls back to fetching the
notice's full description for those. The first run with the fix confirmed two body-only matches the
old path had been dropping, including a Secret Service award matching `Digital forensics`.

Terms are not only keywords. Forensic software is often bought under a generic title — IRS Criminal
Investigation bought two `Amped AUTHENTICATE` licences under the title *Software Licensing and
Training*, in a notice whose body never uses the word "forensic". Vendor and product names are
therefore first-class search terms, and a vendor whose name is an ordinary word belongs in
`config/search_terms.json` only as a **phrase**: `Amped` alone collides with DARPA's AMPED
programme, while `Amped Authenticate` goes through the phrase check above and is as precise as
`Cellebrite`.

The digest only re-cuts `data/history.json`; it never calls SAM.gov, so it is safe to re-run.
Add a group to `config/watch_groups.json` and pass `--group <key>` for other watch lists.

## Copy for Trello

Each result row on the HTML report has **Copy for Trello**. Click to copy a card-ready block (title, SAM link, terms, deadline, etc.), then paste into a new Trello card. No Trello API keys required.

