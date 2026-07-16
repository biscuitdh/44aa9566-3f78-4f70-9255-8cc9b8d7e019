# SAM daily search

Automated SAM.gov opportunity keyword checks. Produces Excel + HTML reports.

## Public report

After GitHub Pages is enabled and a workflow has run:

- HTML: https://biscuitdh.github.io/44aa9566-3f78-4f70-9255-8cc9b8d7e019/v/
- Excel: https://biscuitdh.github.io/44aa9566-3f78-4f70-9255-8cc9b8d7e019/v/SAM-daily-latest.xlsx

Low-profile path (`/v/`), `noindex`, robots disallow. Content is public SAM.gov notice metadata only.

## GitHub Actions (once)

1. **Settings → Secrets → Actions:** `SAM_API_KEY` = your SAM public API key  
2. **Settings → Pages → Source:** GitHub Actions  
3. **Settings → Actions → General → Workflow permissions:** Read and write  
4. **Actions → SAM daily search → Run workflow** (first run)

Schedule: `0 9 * * *` UTC (≈ 04:00 EST / 05:00 EDT).

## Local (optional)

```bash
git clone https://github.com/biscuitdh/44aa9566-3f78-4f70-9255-8cc9b8d7e019.git
cd 44aa9566-3f78-4f70-9255-8cc9b8d7e019
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add SAM_API_KEY if using API mode
.venv/bin/python scripts/sam_search.py --days 1 --source both --no-latest-sync
```

Terms: `config/search_terms.json`.
