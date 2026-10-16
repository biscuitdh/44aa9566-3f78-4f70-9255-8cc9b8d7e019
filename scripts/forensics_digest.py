#!/usr/bin/env python3
"""Build a keyword-focused digest (default: Forensics) from the SAM tracker history.

Reads the rolling history written by scripts/sam_search.py and emits:
  * a Markdown digest (reports/forensics/latest.md + a dated copy)
  * a standalone HTML page for GitHub Pages (docs/v/forensics.html)
  * a short stdout summary suitable for a CI job summary

The digest never touches SAM.gov; it only re-cuts data the daily search already
collected, so it is safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY = ROOT / "data" / "history.json"
DEFAULT_ARCHIVE = ROOT / "data" / "archive" / "notices-master.json"
DEFAULT_GROUPS = ROOT / "config" / "watch_groups.json"
DEFAULT_OUT_HTML = ROOT / "docs" / "v" / "forensics.html"
DEFAULT_MD_DIR = ROOT / "reports" / "forensics"
DEFAULT_GROUP = "forensics"


class Group:
    def __init__(self, key: str, raw: dict[str, Any]) -> None:
        self.key = key
        self.label = str(raw.get("label") or key.title())
        self.strong_terms = [str(t) for t in (raw.get("strong_terms") or [])]
        self.weak_terms = [str(t) for t in (raw.get("weak_terms") or [])]
        self.title_keywords = [str(t).casefold() for t in (raw.get("title_keywords") or [])]
        self._strong = {t.casefold() for t in self.strong_terms}
        self._weak = {t.casefold() for t in self.weak_terms}

    @property
    def all_terms(self) -> list[str]:
        return self.strong_terms + self.weak_terms

    def classify(self, notice: dict[str, Any]) -> tuple[str, list[str]] | None:
        """Return (confidence, reasons) for a matching notice, else None."""
        matched = [str(t) for t in (notice.get("matched_terms") or [])]
        strong_hits = [t for t in matched if t.casefold() in self._strong]
        weak_hits = [t for t in matched if t.casefold() in self._weak]
        title = str(notice.get("title") or "").casefold()
        kw_hits = [k for k in self.title_keywords if k and k in title]

        if strong_hits or kw_hits:
            reasons = strong_hits + [f"title:{k}" for k in kw_hits if k not in {s.casefold() for s in strong_hits}]
            return "confirmed", reasons or strong_hits
        if weak_hits:
            return "review", weak_hits
        return None


def load_groups(path: Path) -> dict[str, Group]:
    if not path.is_file():
        raise SystemExit(f"Watch group config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, dict) or not groups:
        raise SystemExit(f"No groups defined in {path}")
    return {k: Group(k, v) for k, v in groups.items() if isinstance(v, dict)}


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"Unexpected JSON structure in {path}")
    return data


def notice_url(rec: dict[str, Any]) -> str:
    url = str(rec.get("url") or "").strip()
    if url:
        return url
    nid = str(rec.get("notice_id") or "").strip()
    return f"https://sam.gov/opp/{nid}/view" if nid else ""


def deadline_date(rec: dict[str, Any]) -> date | None:
    raw = str(rec.get("response_deadline") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


def sort_key(rec: dict[str, Any]) -> tuple[int, str]:
    posted = str(rec.get("posted_date") or "")
    return (0 if rec.get("is_new") else 1, posted)


def collect(
    history: dict[str, Any],
    group: Group,
    report_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split matching notices in the history window into confirmed and review rows."""
    confirmed: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for rec in (history.get("notices") or {}).values():
        if not isinstance(rec, dict):
            continue
        verdict = group.classify(rec)
        if not verdict:
            continue
        confidence, reasons = verdict
        row = dict(rec)
        row["match_reasons"] = reasons
        row["is_new"] = str(rec.get("first_seen_date") or "") == report_date
        (confirmed if confidence == "confirmed" else review).append(row)
    confirmed.sort(key=sort_key, reverse=True)
    review.sort(key=sort_key, reverse=True)
    confirmed.sort(key=lambda r: 0 if r.get("is_new") else 1)
    review.sort(key=lambda r: 0 if r.get("is_new") else 1)
    return confirmed, review


def term_counts(rows: list[dict[str, Any]], group: Group) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in rows:
        for term in row.get("matched_terms") or []:
            key = str(term)
            if key.casefold() in {t.casefold() for t in group.all_terms}:
                counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold()))


def archive_total(path: Path, group: Group) -> int | None:
    """All-time count of matching notices in the durable archive, if present."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    notices = data.get("notices") if isinstance(data, dict) else None
    if not isinstance(notices, dict):
        return None
    return sum(1 for rec in notices.values() if isinstance(rec, dict) and group.classify(rec))


def upcoming(rows: list[dict[str, Any]], today: date, horizon_days: int) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        due = deadline_date(row)
        if due is None or due < today:
            continue
        if (due - today).days <= horizon_days:
            row = dict(row)
            row["days_left"] = (due - today).days
            out.append(row)
    out.sort(key=lambda r: r["days_left"])
    return out


def md_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_None._\n"
    lines = [
        "| Posted | Notice | Matched | Type | Deadline | Organization |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        title = str(r.get("title") or "(no title)").replace("|", "\\|")
        url = notice_url(r)
        link = f"[{title}]({url})" if url else title
        flag = "**NEW** " if r.get("is_new") else ""
        matched = "; ".join(str(x) for x in (r.get("match_reasons") or r.get("matched_terms") or [])).replace("|", "\\|")
        org = str(r.get("organization") or "").replace("|", "\\|")
        lines.append(
            f"| {r.get('posted_date') or '—'} | {flag}{link} | {matched or '—'} | "
            f"{r.get('type') or '—'} | {str(r.get('response_deadline') or '—')[:16]} | {org or '—'} |"
        )
    return "\n".join(lines) + "\n"


def build_markdown(
    group: Group,
    report_date: str,
    confirmed: list[dict[str, Any]],
    review: list[dict[str, Any]],
    due_soon: list[dict[str, Any]],
    meta: dict[str, Any],
) -> str:
    new_rows = [r for r in confirmed if r.get("is_new")]
    counts = term_counts(confirmed + review, group)
    parts = [
        f"# {group.label} watch — {report_date}",
        "",
        f"- Window: `{meta.get('window_from') or '?'}` → `{meta.get('window_to') or '?'}` "
        f"({meta.get('window_days') or '?'} day(s) of history)",
        f"- Confirmed {group.label.lower()} notices in window: **{len(confirmed)}**",
        f"- New (first seen {report_date}): **{len(new_rows)}**",
        f"- Needs review (ambiguous acronym match only): **{len(review)}**",
    ]
    if meta.get("archive_total") is not None:
        parts.append(f"- All-time in durable archive: **{meta['archive_total']}**")
    parts += [
        "",
        f"## New today ({len(new_rows)})",
        "",
        md_table(new_rows),
        "",
        f"## Response deadlines within {meta.get('horizon_days')} days ({len(due_soon)})",
        "",
        md_table(due_soon),
        "",
        f"## All confirmed matches in window ({len(confirmed)})",
        "",
        md_table(confirmed),
        "",
        f"## Needs review — ambiguous match only ({len(review)})",
        "",
        md_table(review),
        "",
        "## Term breakdown",
        "",
    ]
    if counts:
        parts.append("| Term | Notices |")
        parts.append("| --- | --- |")
        parts.extend(f"| {term} | {n} |" for term, n in counts)
    else:
        parts.append("_No term hits in window._")
    parts += [
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC from `data/history.json`.",
        "",
    ]
    return "\n".join(parts)


def html_table(rows: list[dict[str, Any]], empty: str) -> str:
    if not rows:
        return f"<p class='muted'><em>{html.escape(empty)}</em></p>"
    body = []
    for r in rows:
        url = notice_url(r)
        title = html.escape(str(r.get("title") or "(no title)"))
        link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
        badge = '<span class="badge new">NEW</span> ' if r.get("is_new") else ""
        matched = html.escape("; ".join(str(x) for x in (r.get("match_reasons") or r.get("matched_terms") or [])))
        body.append(
            "<tr class='{cls}'>"
            "<td>{posted}</td><td>{badge}{link}</td><td>{matched}</td>"
            "<td>{typ}</td><td>{deadline}</td><td>{org}</td>"
            "</tr>".format(
                cls="is-new" if r.get("is_new") else "",
                posted=html.escape(str(r.get("posted_date") or "")),
                badge=badge,
                link=link,
                matched=matched,
                typ=html.escape(str(r.get("type") or "")),
                deadline=html.escape(str(r.get("response_deadline") or "")[:16]),
                org=html.escape(str(r.get("organization") or "")),
            )
        )
    return (
        "<table><thead><tr>"
        "<th>Posted</th><th>Notice</th><th>Matched</th><th>Type</th><th>Deadline</th><th>Organization</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def build_html(
    group: Group,
    report_date: str,
    confirmed: list[dict[str, Any]],
    review: list[dict[str, Any]],
    due_soon: list[dict[str, Any]],
    meta: dict[str, Any],
) -> str:
    new_rows = [r for r in confirmed if r.get("is_new")]
    counts = term_counts(confirmed + review, group)
    counts_html = "".join(
        f"<tr><td>{html.escape(term)}</td><td>{n}</td></tr>" for term, n in counts
    ) or "<tr><td colspan='2'><em>No term hits in window</em></td></tr>"
    archive_line = (
        f" · All-time archive: <strong>{meta['archive_total']}</strong>"
        if meta.get("archive_total") is not None
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="robots" content="noindex,nofollow" />
  <title>{html.escape(group.label)} watch — {html.escape(report_date)}</title>
  <style>
    :root {{
      --bg: #f6f8fb; --card: #fff; --ink: #1a2332; --muted: #5b6b7c;
      --line: #d8e0ea; --new: #e8f6e8; --accent: #1f4e79; --link: #0b5cab;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 24px; background: var(--bg); color: var(--ink); line-height: 1.45;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    h1 {{ margin: 0 0 8px; font-size: 1.55rem; color: var(--accent); }}
    h2 {{ color: var(--accent); font-size: 1.12rem; margin: 24px 0 6px; }}
    .card {{
      background: var(--card); border: 1px solid var(--line); border-radius: 12px;
      padding: 16px 18px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.04);
    }}
    .meta, .muted {{ color: var(--muted); }}
    .meta code {{ background: #eef2f7; padding: 1px 6px; border-radius: 4px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 0.92rem; background: var(--card); }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px 6px; text-align: left; vertical-align: top; }}
    th {{ background: #eef3f8; color: var(--accent); }}
    tr.is-new {{ background: var(--new); }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge.new {{
      display: inline-block; background: #2e7d32; color: #fff;
      font-size: 0.7rem; font-weight: 700; padding: 2px 6px; border-radius: 999px;
    }}
    table.counts {{ max-width: 320px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{html.escape(group.label)} watch</h1>
    <div class="meta">
      Report date: <strong>{html.escape(report_date)}</strong>
      · Window <code>{html.escape(str(meta.get('window_from') or ''))}</code>
        → <code>{html.escape(str(meta.get('window_to') or ''))}</code>
      · Confirmed: <strong>{len(confirmed)}</strong>
      · New today: <strong>{len(new_rows)}</strong>
      · Needs review: <strong>{len(review)}</strong>{archive_line}
    </div>
    <p class="meta">
      Keyword-filtered view of the <a href="./">full daily tracker</a>.
      Matching terms: {html.escape(', '.join(group.all_terms))}.
    </p>
  </div>

  <h2>New today ({len(new_rows)})</h2>
  {html_table(new_rows, 'No new forensics notices first seen today.')}

  <h2>Response deadlines within {meta.get('horizon_days')} days ({len(due_soon)})</h2>
  {html_table(due_soon, 'Nothing due in that window.')}

  <h2>All confirmed matches in window ({len(confirmed)})</h2>
  {html_table(confirmed, 'No confirmed matches in the current window.')}

  <h2>Needs review — ambiguous acronym match only ({len(review)})</h2>
  {html_table(review, 'Nothing pending review.')}

  <h2>Term breakdown</h2>
  <table class="counts"><thead><tr><th>Term</th><th>Notices</th></tr></thead>
  <tbody>{counts_html}</tbody></table>

  <p class="meta" style="margin-top:24px;">
    Generated {html.escape(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'))} UTC from data/history.json ·
    <a href="./">back to full tracker</a>
  </p>
</body>
</html>
"""


def stdout_summary(
    group: Group,
    report_date: str,
    confirmed: list[dict[str, Any]],
    review: list[dict[str, Any]],
    due_soon: list[dict[str, Any]],
) -> str:
    new_rows = [r for r in confirmed if r.get("is_new")]
    lines = [
        f"{group.label} watch {report_date}: {len(confirmed)} confirmed, "
        f"{len(new_rows)} new, {len(review)} to review, {len(due_soon)} due soon.",
    ]
    for r in new_rows or confirmed[:10]:
        terms = ", ".join(str(x) for x in (r.get("match_reasons") or []))
        lines.append(f"- [{r.get('posted_date')}] {str(r.get('title') or '')[:88]} ({terms})")
    if not new_rows and len(confirmed) > 10:
        lines.append(f"... and {len(confirmed) - 10} more in window")
    return "\n".join(lines)


def resolve_window(history: dict[str, Any], report_date: str, fallback_days: int) -> dict[str, Any]:
    days = sorted((history.get("days") or {}).keys())
    return {
        "window_from": days[0] if days else "",
        "window_to": days[-1] if days else report_date,
        "window_days": len(days) or fallback_days,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a keyword-focused digest from the SAM tracker history."
    )
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--group", default=DEFAULT_GROUP, help="Watch group key (default: forensics)")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE, help="Durable archive for all-time counts")
    parser.add_argument("--no-archive", action="store_true", help="Skip the all-time archive count")
    parser.add_argument("--date", default="", help="Report date YYYY-MM-DD (default: newest day in history)")
    parser.add_argument("--horizon-days", type=int, default=30, help="Deadline horizon for the due-soon section")
    parser.add_argument("--out-html", type=Path, default=DEFAULT_OUT_HTML)
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument("--md-dir", type=Path, default=DEFAULT_MD_DIR)
    parser.add_argument("--no-md", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Suppress the stdout summary")
    args = parser.parse_args(argv)

    groups = load_groups(args.groups)
    group = groups.get(args.group)
    if group is None:
        raise SystemExit(f"Group '{args.group}' not in {args.groups} (have: {', '.join(sorted(groups))})")

    history = load_json(args.history)
    day_keys = sorted((history.get("days") or {}).keys())
    report_date = args.date or (day_keys[-1] if day_keys else date.today().isoformat())

    confirmed, review = collect(history, group, report_date)
    meta = resolve_window(history, report_date, int(history.get("retention_days") or 15))
    meta["horizon_days"] = args.horizon_days
    meta["archive_total"] = None if args.no_archive else archive_total(args.archive, group)

    today = date.fromisoformat(report_date)
    due_soon = upcoming(confirmed, today, args.horizon_days)

    if not args.no_md:
        args.md_dir.mkdir(parents=True, exist_ok=True)
        text = build_markdown(group, report_date, confirmed, review, due_soon, meta)
        dated = args.md_dir / f"{report_date}.md"
        latest = args.md_dir / "latest.md"
        dated.write_text(text, encoding="utf-8")
        latest.write_text(text, encoding="utf-8")
        if not args.quiet:
            print(f"Markdown: {dated}")
            print(f"          {latest}")

    if not args.no_html:
        args.out_html.parent.mkdir(parents=True, exist_ok=True)
        args.out_html.write_text(
            build_html(group, report_date, confirmed, review, due_soon, meta), encoding="utf-8"
        )
        if not args.quiet:
            print(f"HTML:     {args.out_html}")

    if not args.quiet:
        print(stdout_summary(group, report_date, confirmed, review, due_soon))
    return 0


if __name__ == "__main__":
    sys.exit(main())
