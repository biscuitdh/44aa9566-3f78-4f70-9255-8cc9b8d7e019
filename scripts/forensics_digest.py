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
import re
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
DEFAULT_LEDGER = DEFAULT_MD_DIR / "reported-notices.json"
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
        self.watch_orgs = [
            (str(o.get("label") or o.get("match") or ""), str(o.get("match") or "").casefold())
            for o in (raw.get("watch_orgs") or [])
            if isinstance(o, dict) and o.get("match")
        ]

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
        org = str(notice.get("organization") or "").casefold()
        org_hits = [label for label, frag in self.watch_orgs if frag in org]
        if org_hits:
            return "review", [f"office:{label}" for label in org_hits]
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


def bootstrap_ledger(md_dir: Path, report_date: str) -> dict[str, str]:
    """Recover the announced-notice ledger from the dated Markdown digests already committed.

    Used the first time the ledger runs, so a fresh ledger does not flag every notice in the
    window as unreported. Only digests older than the current report count, and `latest.md` is
    skipped because it is a copy of the newest dated file. Returns (ledger, digests scanned).
    """
    seen: dict[str, str] = {}
    scanned = 0
    for path in sorted(md_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md")):
        if path.stem >= report_date:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        scanned += 1
        for nid in re.findall(r"sam\.gov/opp/([0-9a-zA-Z]{16,})/view", text):
            seen.setdefault(nid, path.stem)
    return seen, scanned


def load_ledger(
    path: Path, md_dir: Path, report_date: str, group_key: str
) -> tuple[dict[str, str], bool, bool]:
    """Return (notice_id -> date first announced, bootstrapped, seed_only).

    `seed_only` means there is nothing to compare against yet — no ledger and no earlier digests —
    so the run should record what it sees instead of declaring the whole window unreported.
    """
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        announced = data.get("announced") if isinstance(data, dict) else None
        stored_group = str(data.get("group") or group_key) if isinstance(data, dict) else group_key
        # A ledger built for another keyword group would mark unrelated notices as announced.
        if isinstance(announced, dict) and stored_group == group_key:
            return {str(k): str(v) for k, v in announced.items()}, False, False
    ledger, scanned = bootstrap_ledger(md_dir, report_date)
    return ledger, True, scanned == 0


def save_ledger(
    path: Path,
    ledger: dict[str, str],
    rows: list[dict[str, Any]],
    report_date: str,
    group_key: str,
) -> int:
    """Record every notice in this digest, returning how many were added."""
    added = 0
    for row in rows:
        nid = str(row.get("notice_id") or "")
        if nid and nid not in ledger:
            ledger[nid] = report_date
            added += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "description": (
            "Notice IDs already announced in a digest, mapped to the report date that first "
            "listed them. Lets the digest flag notices that arrived after the previous run "
            "and would otherwise never appear as new."
        ),
        "group": group_key,
        "updated": report_date,
        "count": len(ledger),
        "announced": dict(sorted(ledger.items(), key=lambda kv: (kv[1], kv[0]))),
    }
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    return added


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


def solicitation_key(rec: dict[str, Any]) -> str:
    return str(rec.get("solicitation_number") or "").strip().upper()


# Enough of a solicitation number to name the contracting office and fiscal year (e.g. HT942726).
SOL_PREFIX_LEN = 8


def archive_notices(archive_path: Path | None) -> dict[str, Any]:
    """Notices from the durable archive, or an empty map when it is missing or unreadable."""
    if archive_path is None or not archive_path.is_file():
        return {}
    try:
        data = json.loads(archive_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    notices = data.get("notices") if isinstance(data, dict) else None
    return notices if isinstance(notices, dict) else {}


def build_office_index(
    history: dict[str, Any], archive_path: Path | None
) -> dict[str, str]:
    """Map solicitation-number prefix -> organization, for prefixes with a single buyer.

    A small share of SAM records arrive with an empty `organization` (14 of 680 in the archive
    on 2026-09-14), which hides the buyer from the report and from the `watch_orgs` rule. The
    leading characters of a solicitation number identify the contracting office, so a blank
    field can be filled from sibling records that do carry one. Prefixes where records disagree
    are dropped rather than guessed: at this prefix length only 1.7% of them are ambiguous.
    """
    seen: dict[str, set[str]] = {}
    for src in (history.get("notices") or {}, archive_notices(archive_path)):
        for rec in src.values():
            if not isinstance(rec, dict):
                continue
            org = str(rec.get("organization") or "").strip()
            prefix = solicitation_key(rec)[:SOL_PREFIX_LEN]
            if org and len(prefix) == SOL_PREFIX_LEN:
                seen.setdefault(prefix, set()).add(org)
    return {prefix: next(iter(orgs)) for prefix, orgs in seen.items() if len(orgs) == 1}


def with_inferred_organization(
    rec: dict[str, Any], office_index: dict[str, str]
) -> dict[str, Any]:
    """Fill a blank `organization` from the solicitation prefix, flagging it as inferred.

    Returns the record unchanged when it already names an organization or no confident
    inference exists, so callers can apply it unconditionally.
    """
    if str(rec.get("organization") or "").strip():
        return rec
    org = office_index.get(solicitation_key(rec)[:SOL_PREFIX_LEN])
    if not org:
        return rec
    filled = dict(rec)
    filled["organization"] = org
    filled["org_inferred"] = True
    return filled


def revision_keys(rec: dict[str, Any]) -> list[str]:
    """Keys under which this record's revision chain can be recognised.

    The solicitation number groups a chain for most records, but 8 of 704 archive records carry
    none (5 of them confirmed forensics), leaving their revisions undetectable. SAM's search index
    also returns `parentNoticeId`, the id of a notice's first revision, so `parent_notice_id or
    notice_id` names the chain directly: every revision reports the same parent, and the first
    revision — which reports none — is that parent. Both keys are returned, so a record indexed
    under one still matches a sibling that only carries the other.
    """
    keys: list[str] = []
    sol = solicitation_key(rec)
    if sol:
        keys.append(f"sol:{sol}")
    chain = str(rec.get("parent_notice_id") or "").strip() or str(rec.get("notice_id") or "").strip()
    if chain:
        keys.append(f"chain:{chain}")
    return keys


def build_revision_index(
    history: dict[str, Any], archive_path: Path | None
) -> dict[str, dict[str, dict[str, Any]]]:
    """Map revision key -> notice_id -> record, across history and the durable archive.

    SAM.gov mints a fresh notice_id for each revision of a notice and serves only the latest, so
    the tracker sees an amended notice as a first-time record. The archive is included because the
    superseded record often predates the 15-day history window.
    """
    index: dict[str, dict[str, dict[str, Any]]] = {}
    sources: list[dict[str, Any]] = [history.get("notices") or {}]
    if archive_path is not None and archive_path.is_file():
        try:
            data = json.loads(archive_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and isinstance(data.get("notices"), dict):
            sources.append(data["notices"])

    for src in sources:
        for rec in src.values():
            if not isinstance(rec, dict):
                continue
            nid = str(rec.get("notice_id") or "")
            if not nid:
                continue
            for key in revision_keys(rec):
                slot = index.setdefault(key, {})
                prev = slot.get(nid)
                if prev is None or str(rec.get("first_seen_date") or "") < str(prev.get("first_seen_date") or ""):
                    slot[nid] = rec
    return index


def chain_siblings(
    rec: dict[str, Any], index: dict[str, dict[str, dict[str, Any]]]
) -> dict[str, dict[str, Any]]:
    """Other records in this record's revision chain, keyed by notice_id.

    A record is indexed under every key its chain answers to, so the same sibling can surface
    under more than one of them; keying by notice_id stops `count_predecessors` double-counting it.
    """
    nid = str(rec.get("notice_id") or "")
    siblings: dict[str, dict[str, Any]] = {}
    for key in revision_keys(rec):
        for other_id, other in (index.get(key) or {}).items():
            if other_id and other_id != nid:
                siblings.setdefault(other_id, other)
    return siblings


def find_predecessor(
    rec: dict[str, Any],
    index: dict[str, dict[str, dict[str, Any]]],
    cutoff: str,
) -> dict[str, Any] | None:
    """Latest record before `cutoff` in this record's revision chain, under a different notice_id.

    A solicitation can be re-issued more than twice, so the comparison has to be against the
    generation immediately before this one; using the earliest would report a deadline change
    spanning the whole chain rather than what this amendment actually changed.
    """
    best: dict[str, Any] | None = None
    for other in chain_siblings(rec, index).values():
        first_seen = str(other.get("first_seen_date") or "")
        if not first_seen or first_seen >= cutoff:
            continue
        if best is None or first_seen > str(best.get("first_seen_date") or ""):
            best = other
    return best


def count_predecessors(
    rec: dict[str, Any],
    index: dict[str, dict[str, dict[str, Any]]],
    cutoff: str,
) -> int:
    """How many earlier notice_ids belong to this record's revision chain."""
    total = 0
    for other in chain_siblings(rec, index).values():
        first_seen = str(other.get("first_seen_date") or "")
        if first_seen and first_seen < cutoff:
            total += 1
    return total


def find_successor(
    rec: dict[str, Any],
    index: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    """Later record in this record's revision chain, under a different notice_id."""
    first_seen = str(rec.get("first_seen_date") or "")
    best: dict[str, Any] | None = None
    for other in chain_siblings(rec, index).values():
        other_seen = str(other.get("first_seen_date") or "")
        if not other_seen or other_seen <= first_seen:
            continue
        if best is None or other_seen > str(best.get("first_seen_date") or ""):
            best = other
    return best


def row_rank(rec: dict[str, Any]) -> int:
    if rec.get("is_superseded"):
        return 4
    if rec.get("is_new"):
        return 0
    if rec.get("is_backlog"):
        return 1
    return 2 if rec.get("is_amended") else 3


def sort_key(rec: dict[str, Any]) -> tuple[int, str]:
    posted = str(rec.get("posted_date") or "")
    return (row_rank(rec), posted)


def collect(
    history: dict[str, Any],
    group: Group,
    report_date: str,
    index: dict[str, dict[str, dict[str, Any]]] | None = None,
    reported: dict[str, str] | None = None,
    office_index: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split matching notices in the history window into confirmed and review rows.

    `reported` maps notice_id -> date the notice was first announced in a digest. A notice
    missing from it that was first seen before today is flagged `is_backlog`: the SAM
    search picked it up after the previous digest ran, so no digest has ever announced it.
    Pass None to disable that check and judge notices by first-seen date alone.
    """
    index = index or {}
    office_index = office_index or {}
    ledger_enabled = reported is not None
    reported = reported or {}
    try:
        report_day: date | None = date.fromisoformat(report_date)
    except ValueError:
        report_day = None
    confirmed: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for rec in (history.get("notices") or {}).values():
        if not isinstance(rec, dict):
            continue
        # Before classifying, so a watch office still matches when SAM left the field empty.
        rec = with_inferred_organization(rec, office_index)
        verdict = group.classify(rec)
        if not verdict:
            continue
        confidence, reasons = verdict
        row = dict(rec)
        row["match_reasons"] = reasons
        first_seen = str(rec.get("first_seen_date") or "")
        first_seen_today = first_seen == report_date
        nid = str(rec.get("notice_id") or "")
        unreported = ledger_enabled and bool(nid) and nid not in reported and not first_seen_today
        # An amendment that landed after the previous digest still needs its predecessor found.
        predecessor = (
            find_predecessor(rec, index, first_seen or report_date)
            if first_seen_today or unreported
            else None
        )
        row["is_amended"] = (first_seen_today or unreported) and predecessor is not None
        row["is_new"] = first_seen_today and predecessor is None
        row["is_backlog"] = unreported and predecessor is None
        row["is_superseded"] = find_successor(rec, index) is not None
        due = deadline_date(rec)
        if due is not None and report_day is not None:
            row["days_left"] = (due - report_day).days
        if predecessor is not None:
            row["prev_deadline"] = predecessor.get("response_deadline") or ""
            row["prev_first_seen"] = predecessor.get("first_seen_date") or ""
            row["revision"] = count_predecessors(rec, index, first_seen or report_date) + 1
        (confirmed if confidence == "confirmed" else review).append(row)
    confirmed.sort(key=sort_key, reverse=True)
    review.sort(key=sort_key, reverse=True)
    confirmed.sort(key=row_rank)
    review.sort(key=row_rank)
    return confirmed, review


def term_counts(rows: list[dict[str, Any]], group: Group) -> list[tuple[str, int]]:
    """Count notices per group term. Filters superseded revisions here rather than trusting
    callers to pass live rows: the headline counts each learned that separately (PRs #23, #25)
    and this count was left behind both times."""
    counts: dict[str, int] = {}
    for row in live_only(rows):
        for term in row.get("matched_terms") or []:
            key = str(term)
            if key.casefold() in {t.casefold() for t in group.all_terms}:
                counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold()))


def archive_total(
    path: Path,
    group: Group,
    office_index: dict[str, str] | None = None,
    revision_index: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> tuple[int, int] | None:
    """All-time (notices, records) matching in the durable archive, if present.

    Superseded revisions are dropped here rather than at the call site, the way term_counts()
    does it: this is the fourth count to need that (PRs #23, #25, #28) and the archive is where
    the residue is largest, because unlike the 15-day window it is never purged, so every
    revision a solicitation has ever had accumulates in it. Builds its own revision index when
    a caller supplies none, so the unfiltered record count is not reachable by accident.
    """
    notices = archive_notices(path)
    if not notices:
        return None
    office_index = office_index or {}
    if revision_index is None:
        revision_index = build_revision_index({"notices": notices}, path)
    records = [
        rec
        for rec in notices.values()
        if isinstance(rec, dict)
        and group.classify(with_inferred_organization(rec, office_index))
    ]
    live = [rec for rec in records if find_successor(rec, revision_index) is None]
    return len(live), len(records)


def upcoming(rows: list[dict[str, Any]], today: date, horizon_days: int) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        if row.get("is_superseded"):
            continue
        due = deadline_date(row)
        if due is None or due < today:
            continue
        if (due - today).days <= horizon_days:
            row = dict(row)
            row["days_left"] = (due - today).days
            out.append(row)
    out.sort(key=lambda r: r["days_left"])
    return out


def org_label(rec: dict[str, Any]) -> str:
    org = str(rec.get("organization") or "")
    if org and rec.get("org_inferred"):
        return f"{org} (inferred from solicitation number)"
    return org


def days_left_label(rec: dict[str, Any]) -> str:
    n = rec.get("days_left")
    if n is None:
        return "—"
    if n < 0:
        return "closed"
    if n == 0:
        return "today"
    return "1 day" if n == 1 else f"{n} days"


def open_breakdown(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Split live rows into still-accepting-responses, past-deadline and undated.

    A notice lingers in the window for ~15 days after SAM stops serving it, so most
    of the confirmed table is usually closed and the headline count on its own
    overstates how much is actually actionable.
    """
    still_open = already_closed = undated = 0
    for row in rows:
        n = row.get("days_left")
        if n is None:
            undated += 1
        elif n < 0:
            already_closed += 1
        else:
            still_open += 1
    return still_open, already_closed, undated


def deadline_change(rec: dict[str, Any]) -> str:
    prev = str(rec.get("prev_deadline") or "")[:16]
    now = str(rec.get("response_deadline") or "")[:16]
    if not prev:
        return "—"
    change = "unchanged" if prev == now else f"{prev} → {now}"
    revision = int(rec.get("revision") or 0)
    if revision > 2:
        change += f" ({ordinal(revision)} issue)"
    return change


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def md_table(
    rows: list[dict[str, Any]],
    show_days_left: bool = False,
    show_deadline_change: bool = False,
    show_first_seen: bool = False,
) -> str:
    if not rows:
        return "_None._\n"
    head = ["Posted", "Notice", "Matched", "Type", "Deadline"]
    if show_first_seen:
        head.append("First seen")
    if show_days_left:
        head.append("Closes in")
    if show_deadline_change:
        head.append("Deadline change")
    head.append("Organization")
    lines = [
        "| " + " | ".join(head) + " |",
        "| " + " | ".join("---" for _ in head) + " |",
    ]
    for r in rows:
        title = str(r.get("title") or "(no title)").replace("|", "\\|")
        url = notice_url(r)
        link = f"[{title}]({url})" if url else title
        if r.get("is_superseded"):
            flag = "_superseded_ "
        elif r.get("is_new"):
            flag = "**NEW** "
        elif r.get("is_backlog"):
            flag = "**UNREPORTED** "
        elif r.get("is_amended"):
            flag = "**AMENDED** "
        else:
            flag = ""
        matched = "; ".join(str(x) for x in (r.get("match_reasons") or r.get("matched_terms") or [])).replace("|", "\\|")
        org = org_label(r).replace("|", "\\|")
        cells = [
            str(r.get("posted_date") or "—"),
            f"{flag}{link}",
            matched or "—",
            str(r.get("type") or "—"),
            str(r.get("response_deadline") or "—")[:16],
        ]
        if show_first_seen:
            cells.append(str(r.get("first_seen_date") or "—"))
        if show_days_left:
            cells.append(days_left_label(r))
        if show_deadline_change:
            cells.append(deadline_change(r))
        cells.append(org or "—")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def live_only(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop revisions a later notice ID has replaced, so a count is of notices, not records."""
    return [r for r in rows if not r.get("is_superseded")]


def records_note(rows: list[dict[str, Any]], live: list[dict[str, Any]]) -> str:
    return records_note_counts(len(rows), len(live))


def records_note_counts(records: int, live: int) -> str:
    """The records-vs-notices disclosure, from counts rather than row lists.

    The archive total is a count all the way down (its rows are never rendered), so it needs
    this wording without having the lists to hand.
    """
    if records == live:
        return ""
    return f" ({records} records incl. {records - live} superseded by an amendment)"


def activity_groups(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """New / unreported / amended rows. collect() sets the three flags exclusively."""
    return (
        [r for r in rows if r.get("is_new")],
        [r for r in rows if r.get("is_backlog")],
        [r for r in rows if r.get("is_amended")],
    )


def review_activity(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("is_new") or r.get("is_backlog") or r.get("is_amended")]


def review_note(rows: list[dict[str, Any]]) -> str:
    """Disclose review-side activity that a confirmed-only count leaves out."""
    if not rows:
        return ""
    return f" (confirmed only; {len(rows)} also in needs review)"


def review_note_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    return f" <span class='muted'>+{len(rows)} in review</span>"


# sam_search.py records a failed term as "Frontend term='Forensic': Network error: ...".
_TERM_IN_ERROR = re.compile(r"term=(?:'([^']*)'|\"([^\"]*)\")")


def search_health(
    history: dict[str, Any], group: Group, report_date: str
) -> dict[str, Any]:
    """How much of the SAM query behind this report actually ran.

    A term that errors contributes no hits, but sam_search.py still exits 0 and publishes,
    so a degraded day is indistinguishable from a quiet one in every count the digest
    prints. Terms the watch group matches on are reported separately: those are the ones
    whose failure can hide a notice from this digest specifically.
    """
    runs = [
        r
        for r in (history.get("runs") or [])
        if isinstance(r, dict) and str(r.get("run_date") or "") == report_date
    ]
    if not runs:
        return {"known": False}

    per_run_failed: list[set[str]] = []
    for run in runs:
        failed = set()
        for message in run.get("errors") or []:
            found = _TERM_IN_ERROR.search(str(message))
            if found:
                failed.add(found.group(1) or found.group(2))
        per_run_failed.append(failed)
    # Only a term that failed in *every* run of the day is a real gap — a re-run that
    # succeeded already filled it in.
    unresolved = set.intersection(*per_run_failed)

    best = max(runs, key=lambda r: int(r.get("hit_count") or 0))
    watch_terms = {t.casefold() for t in group.all_terms}
    term_count = int(best.get("term_count") or 0)
    return {
        "known": True,
        "runs": len(runs),
        "term_count": term_count,
        "searched": max(0, term_count - len(unresolved)),
        "hit_count": int(best.get("hit_count") or 0),
        "blind_terms": sorted(t for t in unresolved if t.casefold() in watch_terms),
        "other_failed": sorted(t for t in unresolved if t.casefold() not in watch_terms),
        "degraded": bool(unresolved),
    }


def health_headline(health: dict[str, Any]) -> str:
    """One line stating how many terms were actually queried."""
    if not health.get("known"):
        return "SAM search coverage: no run recorded for this date"
    total, searched = health.get("term_count") or 0, health.get("searched") or 0
    if not health.get("degraded"):
        return f"SAM search coverage: **{searched}/{total}** terms queried without error"
    missed = total - searched
    return (
        f"SAM search coverage: **{searched}/{total}** terms — {missed} failed, "
        f"so every count below is a floor"
    )


def _health_sentences(health: dict[str, Any]) -> list[str]:
    blind = health.get("blind_terms") or []
    other = health.get("other_failed") or []
    lines = [
        "The SAM.gov query behind this report errored on "
        f"{(health.get('term_count') or 0) - (health.get('searched') or 0)} of "
        f"{health.get('term_count') or 0} search terms, so a notice that only those terms "
        "would have matched is absent from the data — not absent from SAM.",
    ]
    if blind:
        lines.append(
            "Terms this watch matches on that returned nothing: "
            + ", ".join(f"`{t}`" for t in blind)
            + ". Read today's 'new' and 'confirmed' counts as a lower bound."
        )
    if other:
        lines.append(
            "Other terms that failed: " + ", ".join(f"`{t}`" for t in other) + "."
        )
    if not blind:
        lines.append(
            "No term this watch matches on was affected, so the forensics counts are "
            "still a fair reading of what SAM returned."
        )
    return lines


def health_block_md(health: dict[str, Any]) -> list[str]:
    if not health.get("degraded"):
        return []
    body = "\n".join(f"> {s}" for s in _health_sentences(health))
    return ["", "> **Degraded search — treat these counts as a floor.**", ">", body, ""]


def health_block_html(health: dict[str, Any]) -> str:
    if not health.get("degraded"):
        return ""
    items = "".join(
        f"<li>{html.escape(s).replace('`', '')}</li>" for s in _health_sentences(health)
    )
    return (
        "<div class='card warn'><strong>Degraded search — treat these counts as a "
        f"floor.</strong><ul>{items}</ul></div>"
    )


def build_markdown(
    group: Group,
    report_date: str,
    confirmed: list[dict[str, Any]],
    review: list[dict[str, Any]],
    due_soon: list[dict[str, Any]],
    meta: dict[str, Any],
) -> str:
    live = live_only(confirmed)
    live_review = live_only(review)
    # Superseded revisions are excluded: render_rows() already shows them as _superseded_ rather
    # than NEW/UNREPORTED/AMENDED, and the live successor carries whatever needs acting on.
    new_rows, backlog_rows, amended_rows = activity_groups(live)
    new_review, backlog_review, amended_review = activity_groups(live_review)
    changed_review = review_activity(live_review)
    still_open, already_closed, undated = open_breakdown(live)
    counts = term_counts(confirmed + review, group)
    superseded_note = records_note(confirmed, live)
    parts = [
        f"# {group.label} watch — {report_date}",
        "",
        f"- Window: `{meta.get('window_from') or '?'}` → `{meta.get('window_to') or '?'}` "
        f"({meta.get('window_days') or '?'} day(s) of history)",
        f"- Confirmed {group.label.lower()} notices in window: **{len(live)}**{superseded_note}",
        f"- Of those, still accepting responses: **{still_open}** "
        f"({already_closed} past deadline, {undated} with no deadline)",
        f"- New solicitations (first seen {report_date}): **{len(new_rows)}**{review_note(new_review)}",
        f"- Not yet reported by any digest (arrived after the previous run): "
        f"**{len(backlog_rows)}**{review_note(backlog_review)}",
        f"- Amended/re-issued (same solicitation, new notice ID): "
        f"**{len(amended_rows)}**{review_note(amended_review)}",
        f"- Needs review (ambiguous acronym, or a watch-office notice with no keyword): "
        f"**{len(live_review)}**{records_note(review, live_review)}",
    ]
    if meta.get("archive_total") is not None:
        parts.append(
            f"- All-time in durable archive: **{meta['archive_total']}**"
            f"{records_note_counts(meta.get('archive_records') or 0, meta['archive_total'])}"
        )
    health = meta.get("search_health") or {}
    parts.append(f"- {health_headline(health)}")
    parts += health_block_md(health)
    parts += [
        "",
        f"## New today ({len(new_rows)})",
        "",
        md_table(new_rows),
        "",
        f"## Not previously reported ({len(backlog_rows)})",
        "",
        "The daily SAM search runs on its own schedule, so notices can land after a digest has already",
        "been written. These were first seen on an earlier date but no digest has listed them as new.",
        "",
        md_table(backlog_rows, show_first_seen=True, show_days_left=True),
        "",
        f"## Amended / re-issued ({len(amended_rows)})",
        "",
        "SAM.gov issues a fresh notice ID when a solicitation is amended, so these are already-tracked",
        "solicitations reappearing under a new ID rather than fresh opportunities.",
        "",
        md_table(amended_rows, show_deadline_change=True, show_first_seen=True),
        "",
        f"## Response deadlines within {meta.get('horizon_days')} days ({len(due_soon)})",
        "",
        md_table(due_soon, show_days_left=True),
        "",
        f"## All confirmed matches in window ({len(live)})",
        "",
        md_table(confirmed, show_days_left=True),
        "",
        f"## Needs-review activity ({len(changed_review)})",
        "",
        "The three counts above are confirmed-only, so an ambiguous row that arrived today, was",
        "re-issued, or was never announced is otherwise visible only as a badge in the table below.",
        "",
        md_table(changed_review, show_days_left=True, show_deadline_change=True, show_first_seen=True),
        "",
        f"## Needs review — ambiguous match only ({len(live_review)})",
        "",
        "Matched only by a short acronym that SAM also finds inside unrelated text, or posted by a",
        "contracting office on the watch list with no forensics keyword in the notice at all",
        "(`office:` reasons) — those are usually vendor-name buys worth a look.",
        "",
        md_table(review, show_days_left=True),
        "",
        "## Term breakdown",
        "",
        "Counts notices, like the headline figures above: a solicitation re-issued under a new ID",
        "counts once, not once per record. Adding up the Matched column of the tables above will",
        "read higher, because those tables also render the superseded revisions.",
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


def html_table(
    rows: list[dict[str, Any]],
    empty: str,
    show_days_left: bool = False,
    show_deadline_change: bool = False,
    show_first_seen: bool = False,
) -> str:
    if not rows:
        return f"<p class='muted'><em>{html.escape(empty)}</em></p>"
    body = []
    for r in rows:
        url = notice_url(r)
        title = html.escape(str(r.get("title") or "(no title)"))
        link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url else title
        if r.get("is_superseded"):
            badge = '<span class="badge superseded">SUPERSEDED</span> '
        elif r.get("is_new"):
            badge = '<span class="badge new">NEW</span> '
        elif r.get("is_backlog"):
            badge = '<span class="badge backlog">UNREPORTED</span> '
        elif r.get("is_amended"):
            badge = '<span class="badge amended">AMENDED</span> '
        else:
            badge = ""
        matched = html.escape("; ".join(str(x) for x in (r.get("match_reasons") or r.get("matched_terms") or [])))
        first_seen_cell = ""
        if show_first_seen:
            first_seen_cell = f"<td>{html.escape(str(r.get('first_seen_date') or ''))}</td>"
        days_cell = ""
        if show_days_left:
            n = r.get("days_left")
            # 3 days is roughly the last point where a bid is still practical
            urgent = " class='urgent'" if isinstance(n, int) and 0 <= n <= 3 else ""
            days_cell = f"<td{urgent}>{html.escape(days_left_label(r))}</td>"
        change_cell = ""
        if show_deadline_change:
            change_cell = f"<td>{html.escape(deadline_change(r))}</td>"
        if r.get("is_superseded"):
            cls = "is-superseded"
        elif r.get("is_new"):
            cls = "is-new"
        elif r.get("is_backlog"):
            cls = "is-backlog"
        elif r.get("is_amended"):
            cls = "is-amended"
        else:
            cls = ""
        body.append(
            "<tr class='{cls}'>"
            "<td>{posted}</td><td>{badge}{link}</td><td>{matched}</td>"
            "<td>{typ}</td><td>{deadline}</td>{first_seen}{days}{change}<td>{org}</td>"
            "</tr>".format(
                cls=cls,
                posted=html.escape(str(r.get("posted_date") or "")),
                badge=badge,
                link=link,
                matched=matched,
                typ=html.escape(str(r.get("type") or "")),
                deadline=html.escape(str(r.get("response_deadline") or "")[:16]),
                first_seen=first_seen_cell,
                days=days_cell,
                change=change_cell,
                org=html.escape(org_label(r)),
            )
        )
    first_seen_head = "<th>First seen</th>" if show_first_seen else ""
    days_head = "<th>Closes in</th>" if show_days_left else ""
    change_head = "<th>Deadline change</th>" if show_deadline_change else ""
    return (
        "<table><thead><tr>"
        f"<th>Posted</th><th>Notice</th><th>Matched</th><th>Type</th><th>Deadline</th>"
        f"{first_seen_head}{days_head}{change_head}<th>Organization</th>"
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
    live = live_only(confirmed)
    live_review = live_only(review)
    # Superseded revisions are excluded: render_rows() already shows them as _superseded_ rather
    # than NEW/UNREPORTED/AMENDED, and the live successor carries whatever needs acting on.
    new_rows, backlog_rows, amended_rows = activity_groups(live)
    new_review, backlog_review, amended_review = activity_groups(live_review)
    changed_review = review_activity(live_review)
    still_open, _, _ = open_breakdown(live)
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
      --line: #d8e0ea; --new: #e8f6e8; --amended: #fdf3e0; --backlog: #e9f0fb;
      --accent: #1f4e79; --link: #0b5cab;
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
    tr.is-backlog {{ background: var(--backlog); }}
    tr.is-amended {{ background: var(--amended); }}
    tr.is-superseded {{ color: var(--muted); }}
    tr.is-superseded a {{ color: var(--muted); }}
    a {{ color: var(--link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge {{
      display: inline-block; color: #fff;
      font-size: 0.7rem; font-weight: 700; padding: 2px 6px; border-radius: 999px;
    }}
    .badge.new {{ background: #2e7d32; }}
    .badge.backlog {{ background: #1f4e79; }}
    .badge.amended {{ background: #a6600a; }}
    .badge.superseded {{ background: #8a94a0; }}
    table.counts {{ max-width: 320px; }}
    td.urgent {{ color: #b3261e; font-weight: 700; white-space: nowrap; }}
    .card.warn {{ background: #fff4f4; border-color: #f0c2c2; }}
    .card.warn ul {{ margin: 8px 0 0; padding-left: 20px; color: var(--ink); }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{html.escape(group.label)} watch</h1>
    <div class="meta">
      Report date: <strong>{html.escape(report_date)}</strong>
      · Window <code>{html.escape(str(meta.get('window_from') or ''))}</code>
        → <code>{html.escape(str(meta.get('window_to') or ''))}</code>
      · Confirmed: <strong>{len(live)}</strong>
      · Still open: <strong>{still_open}</strong>
      · New today: <strong>{len(new_rows)}</strong>{review_note_html(new_review)}
      · Not previously reported: <strong>{len(backlog_rows)}</strong>{review_note_html(backlog_review)}
      · Amended: <strong>{len(amended_rows)}</strong>{review_note_html(amended_review)}
      · Needs review: <strong>{len(live_review)}</strong>{archive_line}
    </div>
    <p class="meta">
      Keyword-filtered view of the <a href="./">full daily tracker</a>.
      Matching terms: {html.escape(', '.join(group.all_terms))}.
      · {html.escape(health_headline(meta.get('search_health') or {}).replace('**', ''))}
    </p>
  </div>
  {health_block_html(meta.get('search_health') or {})}

  <h2>New today ({len(new_rows)})</h2>
  {html_table(new_rows, 'No new forensics notices first seen today.')}

  <h2>Not previously reported ({len(backlog_rows)})</h2>
  <p class="meta">The daily SAM search runs on its own schedule, so notices can land after a digest
  has already been written. These were first seen on an earlier date but no digest has listed them
  as new.</p>
  {html_table(backlog_rows, 'Nothing missed by earlier digests.', show_first_seen=True, show_days_left=True)}

  <h2>Amended / re-issued ({len(amended_rows)})</h2>
  <p class="meta">SAM.gov issues a fresh notice ID when a solicitation is amended, so these are
  already-tracked solicitations reappearing under a new ID rather than fresh opportunities.</p>
  {html_table(amended_rows, 'Nothing re-issued.', show_deadline_change=True, show_first_seen=True)}

  <h2>Response deadlines within {meta.get('horizon_days')} days ({len(due_soon)})</h2>
  {html_table(due_soon, 'Nothing due in that window.', show_days_left=True)}

  <h2>All confirmed matches in window ({len(live)})</h2>
  {html_table(confirmed, 'No confirmed matches in the current window.', show_days_left=True)}

  <h2>Needs-review activity ({len(changed_review)})</h2>
  <p class="meta">The three counts above are confirmed-only, so an ambiguous row that arrived today,
  was re-issued, or was never announced is otherwise visible only as a badge in the table below.</p>
  {html_table(changed_review, 'No ambiguous rows changed today.', show_days_left=True, show_deadline_change=True, show_first_seen=True)}

  <h2>Needs review — ambiguous acronym, or a watch-office notice with no keyword ({len(live_review)})</h2>
  {html_table(review, 'Nothing pending review.', show_days_left=True)}

  <h2>Term breakdown</h2>
  <p class="meta">Counts notices, like the headline figures above: a solicitation re-issued under a
  new ID counts once, not once per record. Adding up the Matched column of the tables above will
  read higher, because those tables also render the superseded revisions.</p>
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
    health: dict[str, Any] | None = None,
) -> str:
    live = live_only(confirmed)
    live_review = live_only(review)
    # Superseded revisions are excluded: render_rows() already shows them as _superseded_ rather
    # than NEW/UNREPORTED/AMENDED, and the live successor carries whatever needs acting on.
    new_rows, backlog_rows, amended_rows = activity_groups(live)
    changed_review = review_activity(live_review)
    still_open, _, _ = open_breakdown(live)
    lines = [
        f"{group.label} watch {report_date}: {len(live)} confirmed ({still_open} still open), "
        f"{len(new_rows)} new, {len(backlog_rows)} not previously reported, "
        f"{len(amended_rows)} amended, {len(live_review)} to review, {len(due_soon)} due soon.",
    ]
    health = health or {}
    if health.get("degraded"):
        blind = health.get("blind_terms") or []
        lines.insert(
            0,
            f"DEGRADED SEARCH: only {health.get('searched')}/{health.get('term_count')} "
            f"terms queried without error"
            + (f"; no results for {', '.join(blind)}" if blind else "")
            + " — the counts below are a floor.",
        )
    if changed_review:
        lines.append(
            f"Needs-review activity (not in the counts above): {len(changed_review)} — "
            + "; ".join(
                f"{'new' if r.get('is_new') else 'unreported' if r.get('is_backlog') else 'amended'}"
                f" {str(r.get('title') or '')[:60]}"
                for r in changed_review
            )
        )
    if new_rows:
        for r in new_rows:
            terms = ", ".join(str(x) for x in (r.get("match_reasons") or []))
            lines.append(f"- [{r.get('posted_date')}] {str(r.get('title') or '')[:88]} ({terms})")
    for r in backlog_rows:
        terms = ", ".join(str(x) for x in (r.get("match_reasons") or []))
        lines.append(
            f"- unreported until now [first seen {r.get('first_seen_date')}] "
            f"{str(r.get('title') or '')[:76]} ({terms})"
        )
    for r in amended_rows:
        lines.append(
            f"- amended [{r.get('posted_date')}] {str(r.get('title') or '')[:70]} "
            f"(deadline {deadline_change(r)})"
        )
    if not new_rows and not backlog_rows:
        # Nothing new, so lead with what is about to close instead of the newest postings.
        lines.append("No new notices today; nearest deadlines:")
        for r in due_soon[:10]:
            lines.append(
                f"- closes in {days_left_label(r)} ({str(r.get('response_deadline') or '')[:10]}) "
                f"{str(r.get('title') or '')[:88]}"
            )
        if len(due_soon) > 10:
            lines.append(f"... and {len(due_soon) - 10} more within the horizon")
        if not due_soon:
            lines.append("- nothing closing within the horizon")
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
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help="Ledger of notice IDs already announced in a digest",
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        help="Ignore the ledger and do not update it (every notice is judged by first-seen date only)",
    )
    parser.add_argument(
        "--no-ledger-update",
        action="store_true",
        help="Read the ledger but leave it unchanged (dry run)",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the stdout summary")
    args = parser.parse_args(argv)

    groups = load_groups(args.groups)
    group = groups.get(args.group)
    if group is None:
        raise SystemExit(f"Group '{args.group}' not in {args.groups} (have: {', '.join(sorted(groups))})")

    history = load_json(args.history)
    day_keys = sorted((history.get("days") or {}).keys())
    report_date = args.date or (day_keys[-1] if day_keys else date.today().isoformat())

    archive_path = None if args.no_archive else args.archive
    index = build_revision_index(history, archive_path)
    office_index = build_office_index(history, archive_path)
    if args.no_ledger:
        ledger: dict[str, str] = {}
        bootstrapped = seed_only = False
    else:
        ledger, bootstrapped, seed_only = load_ledger(
            args.ledger, args.md_dir, report_date, group.key
        )
    # With no ledger and no earlier digests there is no baseline, so only record what we see.
    confirmed, review = collect(
        history,
        group,
        report_date,
        index,
        None if args.no_ledger or seed_only else ledger,
        office_index,
    )
    meta = resolve_window(history, report_date, int(history.get("retention_days") or 15))
    meta["horizon_days"] = args.horizon_days
    archive_counts = (
        None
        if args.no_archive
        else archive_total(args.archive, group, office_index, index)
    )
    meta["archive_total"] = archive_counts[0] if archive_counts else None
    meta["archive_records"] = archive_counts[1] if archive_counts else None
    meta["search_health"] = search_health(history, group, report_date)

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

    if not args.no_ledger and not args.no_ledger_update:
        added = save_ledger(args.ledger, ledger, confirmed + review, report_date, group.key)
        if not args.quiet:
            origin = " (bootstrapped from earlier digests)" if bootstrapped else ""
            print(f"Ledger:   {args.ledger} — {len(ledger)} announced, +{added} this run{origin}")

    if not args.quiet:
        print(
            stdout_summary(
                group, report_date, confirmed, review, due_soon, meta["search_health"]
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
