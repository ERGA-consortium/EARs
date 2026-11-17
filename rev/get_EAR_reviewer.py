# get_EAR_reviewer.py
# by Diego De Panis
# ERGA Sequencing and Assembly Committee
# note: AI tools were used to improve/clean/comment this version of the code

# This should be the single source of truth for reviewer selection and scoring.
# The EAR bot (ear_bot/ear_bot_reviewer.py) imports these functions; the CLI
# below uses the same ones...
version = "v26.11.12"

import argparse
import csv
import io
import random
import re
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Canonical institution name -> lowercase aliases recognised in free-text
# affiliations. The canonical name itself is always recognised too. An alias
# must not touch other letters or digits, so "uniba" does not match "unibas"
# but "SciLifeLab_Uppsala" and "CNAG-CRG" still match.
INSTITUTIONS = {
    "CNAG": ["cnag"],
    "Sanger": ["sanger", "wsi"],
    "Genoscope": ["genoscope"],
    "SciLifeLab": ["scilifelab"],
    "IZW": ["izw"],
    "UNIFI": ["unifi"],
    "UNIBA": ["uniba"],
}

# Project tag (the PR's "Project:" value) -> bonus for reviewers at one of
# the project's institutions. An institution can belong to several projects.
# Every institution listed here must exist in INSTITUTIONS.
PROJECT_BONUSES = {
    "ERGA-BGE": {
        "bonus": 50,
        "institutions": ["CNAG", "Sanger", "Genoscope", "SciLifeLab", "IZW", "UNIFI", "UNIBA"],
    },
    # Example of a second project with overlapping membership:
    # "CBP": {"bonus": 50, "institutions": ["CNAG"]},
}

# Selection scoring, applied on top of each reviewer's Calling Score.
NEVER_REVIEWED_BONUS = 50       # Last Review is NA
SUPERVISOR_PENALTY = 5          # Supervisor is Y
WORKING_PR_PENALTY = 30         # per PR the reviewer is working on or was asked about
RECENT_REVIEW_PENALTY = 30      # on the day of the last review...
RECENT_REVIEW_WINDOW_DAYS = 30  # ...shrinking evenly to 0 over this many days

# Bookkeeping applied to the CSV by record_review() and record_timeouts().
REVIEW_DONE_SCORE_CHANGE = -1          # reviewer who completed a review
TIMEOUT_SCORE_CHANGE = 1               # reviewer who let a request time out
SAME_INSTITUTION_SCORE_CHANGE = 1      # everyone at the requester's institution, per merged EAR

REVIEWERS_CSV_URL = "https://api.github.com/repos/ERGA-consortium/EARs/contents/rev/reviewers_list.csv"


def _check_config():
    for project, cfg in PROJECT_BONUSES.items():
        for institution in cfg["institutions"]:
            if institution not in INSTITUTIONS:
                raise ValueError(
                    f"PROJECT_BONUSES['{project}'] lists '{institution}', "
                    f"which is not in INSTITUTIONS"
                )


_check_config()

_INSTITUTION_PATTERNS = [
    (
        canonical,
        re.compile(
            r"(?<![a-z0-9])(?:"
            + "|".join(re.escape(alias) for alias in [canonical.lower(), *aliases])
            + r")(?![a-z0-9])"
        ),
    )
    for canonical, aliases in INSTITUTIONS.items()
]

# ---------------------------------------------------------------------------
# CSV input / output
# ---------------------------------------------------------------------------


def download_csv(url=REVIEWERS_CSV_URL):
    import requests  # only the CLI downloads; the bot passes the text in

    try:
        response = requests.get(
            url,
            headers={
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        print(f"Failed to download data from the repo: {e}")
        return None


def parse_csv(csv_str):
    """Parse CSV text into a list of dicts of strings, in header order.

    Handles quoted fields (commas inside values). Raises ValueError if a row
    has more fields than the header, instead of silently shifting columns.
    """
    reader = csv.DictReader(io.StringIO(csv_str.strip()))
    rows = []
    for row in reader:
        if None in row:
            raise ValueError(
                f"CSV line {reader.line_num} has more fields than the header: {row[None]}"
            )
        rows.append({key.strip(): (value or "").strip() for key, value in row.items()})
    return rows


def to_csv(rows, headers=None, include_header=True):
    """Serialise rows (dicts) to CSV text, quoting values only when needed.

    For rows without commas or quotes the output is identical to joining the
    values with commas. Use include_header=False to append rows to a file.
    """
    if headers is None:
        if not rows:
            raise ValueError("headers are required when there are no rows")
        headers = list(rows[0].keys())
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=headers, lineterminator="\n")
    if include_header:
        writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def normalize_institution(institution):
    """Map a free-text affiliation to its canonical institution name.

    Unknown institutions are returned stripped and lowercased.
    """
    text = (institution or "").strip().lower()
    for canonical, pattern in _INSTITUTION_PATTERNS:
        if pattern.search(text):
            return canonical
    return text


def parse_date(date_str):
    """Parse YYYY-MM-DD; return None for NA or empty."""
    value = (date_str or "").strip()
    if value in ("", "NA"):
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _as_list(tags):
    if not tags:
        return []
    if isinstance(tags, str):
        return [tags]
    return list(tags)


def _id_set(ids):
    """Lowercased set of GitHub IDs from None, one ID, or an iterable."""
    if not ids:
        return set()
    if isinstance(ids, str):
        ids = [ids]
    return {i.strip().lower() for i in ids if i and i.strip()}


def _int(row, column, default=None):
    value = (row.get(column) or "").strip()
    if value == "":
        if default is None:
            raise ValueError(f"{row.get('Github ID', '?')}: '{column}' is empty")
        return default
    return int(value)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def project_bonus(institution, tags):
    """Return (bonus, project) for the best matching project, or (0, None).

    If several tags match, only the largest bonus applies, once.
    """
    normalized = normalize_institution(institution)
    best = (0, None)
    for tag in _as_list(tags):
        cfg = PROJECT_BONUSES.get(tag)
        if cfg and normalized in cfg["institutions"] and cfg["bonus"] > best[0]:
            best = (cfg["bonus"], tag)
    return best


def recent_review_penalty(last_review, today=None):
    """Points subtracted for a recent review: RECENT_REVIEW_PENALTY on the day
    of the review, shrinking evenly to 0 at RECENT_REVIEW_WINDOW_DAYS."""
    reviewed = parse_date(last_review)
    if reviewed is None:
        return 0
    days = max(0, ((today or date.today()) - reviewed).days)
    if days >= RECENT_REVIEW_WINDOW_DAYS:
        return 0
    return RECENT_REVIEW_PENALTY * (RECENT_REVIEW_WINDOW_DAYS - days) // RECENT_REVIEW_WINDOW_DAYS


def score_breakdown(reviewer, tags=None, today=None):
    """Return (adjusted score, list of human-readable adjustments)."""
    score = _int(reviewer, "Calling Score")
    adjustments = []

    if parse_date(reviewer.get("Last Review")) is None:
        score += NEVER_REVIEWED_BONUS
        adjustments.append(f"+{NEVER_REVIEWED_BONUS} never reviewed")

    bonus, project = project_bonus(reviewer.get("Institution"), tags)
    if bonus:
        score += bonus
        adjustments.append(f"+{bonus} {project}")

    if reviewer.get("Supervisor") == "Y":
        score -= SUPERVISOR_PENALTY
        adjustments.append(f"-{SUPERVISOR_PENALTY} supervisor")

    working_prs = _int(reviewer, "Working PRs", default=0)
    if working_prs:
        score -= WORKING_PR_PENALTY * working_prs
        adjustments.append(f"-{WORKING_PR_PENALTY * working_prs} working on {working_prs} PR(s)")

    recent = recent_review_penalty(reviewer.get("Last Review"), today)
    if recent:
        score -= recent
        days = ((today or date.today()) - parse_date(reviewer["Last Review"])).days
        adjustments.append(f"-{recent} reviewed {days} day(s) ago")

    return score, adjustments


def adjust_score(reviewer, tags=None, today=None):
    return score_breakdown(reviewer, tags, today)[0]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _tie_key(candidate):
    # Highest score, then fewest reviews, then oldest last review
    # (never reviewed counts as oldest).
    return (
        -candidate["Adjusted Score"],
        candidate["Total Reviews"],
        candidate["Parsed Last Review"] or date.min,
    )


def select_best_reviewer(data, calling_institution, tags=None, exclude_ids=None, today=None, rng=None):
    """Pick a reviewer.

    Eligible: Active == 'Y', from a different institution than the requester,
    and not in exclude_ids (case-insensitive; pass the requester, the PR's
    supervisor and anyone already asked on the PR).

    Returns (all eligible candidates sorted best first, [selected] or [], reason).
    Candidates are copies; `data` is not modified.
    """
    rng = rng or random
    excluded = _id_set(exclude_ids)
    calling = normalize_institution(calling_institution)

    candidates = []
    for reviewer in data:
        if reviewer.get("Active") != "Y":
            continue
        if normalize_institution(reviewer.get("Institution")) == calling:
            continue
        if reviewer.get("Github ID", "").strip().lower() in excluded:
            continue
        score, adjustments = score_breakdown(reviewer, tags, today)
        candidates.append({
            **reviewer,
            "Adjusted Score": score,
            "Adjustments": adjustments,
            "Total Reviews": _int(reviewer, "Total Reviews", default=0),
            "Parsed Last Review": parse_date(reviewer.get("Last Review")),
        })

    if not candidates:
        return [], [], "no eligible candidates"

    candidates.sort(key=_tie_key)
    top_score = candidates[0]["Adjusted Score"]
    tied_on_score = [c for c in candidates if c["Adjusted Score"] == top_score]
    if len(tied_on_score) == 1:
        return candidates, tied_on_score, "highest adjusted score"

    best = _tie_key(candidates[0])
    finalists = [c for c in tied_on_score if _tie_key(c) == best]
    if len(finalists) == 1:
        return candidates, finalists, "tied on score; fewest reviews, then oldest last review"

    selected = rng.choice(finalists)
    return candidates, [selected], "random pick among candidates tied on score, reviews and last review date"


def select_random_supervisor(data, exclude_id, calling_institution, rng=None):
    """Pick a random active supervisor from a different institution.

    exclude_id can be one GitHub ID or several (case-insensitive).
    """
    rng = rng or random
    excluded = _id_set(exclude_id)
    calling = normalize_institution(calling_institution)
    supervisors = [
        reviewer for reviewer in data
        if reviewer.get("Supervisor") == "Y"
        and reviewer.get("Active") == "Y"
        and reviewer.get("Github ID", "").strip().lower() not in excluded
        and normalize_institution(reviewer.get("Institution")) != calling
    ]
    if not supervisors:
        return None
    return rng.choice(supervisors)


# ---------------------------------------------------------------------------
# Bookkeeping (updates `data` in place; the caller writes it back with to_csv)
# ---------------------------------------------------------------------------


def _rows_for(data, github_ids):
    ids = _id_set(github_ids)
    return [row for row in data if row.get("Github ID", "").strip().lower() in ids]


def _add(row, column, delta, minimum=None):
    value = _int(row, column, default=0) + delta
    if minimum is not None:
        value = max(minimum, value)
    row[column] = str(value)


def record_assignment(data, github_ids, busy=True):
    """Working PRs +1 for each ID (busy=True, e.g. when asked to review),
    or -1, not below 0 (busy=False, e.g. declined, released or cleared)."""
    for row in _rows_for(data, github_ids):
        _add(row, "Working PRs", 1 if busy else -1, minimum=0)


def record_timeouts(data, github_ids):
    """Calling Score + TIMEOUT_SCORE_CHANGE for each reviewer who let a
    review request time out."""
    for row in _rows_for(data, github_ids):
        _add(row, "Calling Score", TIMEOUT_SCORE_CHANGE)


def record_review(data, reviewer_id, review_date, requester_institution):
    """Apply a merged EAR.

    The reviewer: Working PRs -1, Calling Score + REVIEW_DONE_SCORE_CHANGE,
    Total Reviews +1, Last Review = review_date (YYYY-MM-DD or a date).
    Everyone at the requester's institution: Calling Score
    + SAME_INSTITUTION_SCORE_CHANGE.
    """
    if isinstance(review_date, (date, datetime)):
        review_date = review_date.strftime("%Y-%m-%d")
    parse_date(review_date)  # validate the format

    for row in _rows_for(data, reviewer_id):
        _add(row, "Working PRs", -1, minimum=0)
        _add(row, "Calling Score", REVIEW_DONE_SCORE_CHANGE)
        _add(row, "Total Reviews", 1)
        row["Last Review"] = review_date

    requester = normalize_institution(requester_institution)
    if requester:
        for row in data:
            if normalize_institution(row.get("Institution")) == requester:
                _add(row, "Calling Score", SAME_INSTITUTION_SCORE_CHANGE)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

REPORT_COLUMNS = [
    "Github ID", "Full Name", "Institution", "Total Reviews", "Last Review",
    "Working PRs", "Calling Score", "Adjusted Score", "Adjustments",
]


def _cell(row, column):
    value = row.get(column, "")
    if isinstance(value, list):
        return ", ".join(value) if value else "-"
    return str(value)


def format_report(all_candidates, top_candidates, reason, now=None):
    """The selection report the CLI prints and the bot posts on the PR."""
    now = now or datetime.now()
    lines = [
        "*****",
        "EAR Reviewer Selection Process",
        f"Date: {now.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    if not all_candidates:
        lines.append("No eligible candidates found.")
        return "\n".join(lines) + "\n"

    selected = top_candidates[0] if top_candidates else None
    rows = list(all_candidates)
    if selected is not None:
        rows = [selected] + [r for r in rows if r is not selected]

    widths = {
        col: max(len(col), *(len(_cell(r, col)) for r in rows))
        for col in REPORT_COLUMNS
    }
    header = " | ".join(col.ljust(widths[col]) for col in REPORT_COLUMNS).rstrip()
    lines += ["All Eligible Candidates:", "", header, "-" * len(header)]
    lines += [" | ".join(_cell(r, col).ljust(widths[col]) for col in REPORT_COLUMNS).rstrip() for r in rows]

    if selected is None:
        lines += ["", "No suitable reviewer found at the moment."]
        return "\n".join(lines) + "\n"

    adjustments = ", ".join(selected["Adjustments"]) or "none"
    lines += [
        "",
        f"Selected reviewer: {selected['Full Name']} ({selected['Github ID']})",
        "The decision was based on:",
        f"- different institution ('{selected['Institution']}')",
        f"- active ('{selected['Active']}')",
        f"- working on {selected.get('Working PRs') or '0'} PR(s) currently",
        f"- score adjustments: {adjustments}",
        f"- {reason} ({selected['Adjusted Score']})",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Select a candidate for EAR reviewing. Also can select a supervisor.")
    parser.add_argument("-i", "--institution", required=True, help="Institution of the person requesting the review.")
    parser.add_argument("-t", "--tag", nargs='*', default=[], help="Project tag(s) of the EAR, such as 'ERGA-BGE'. Reviewers from a project's institutions get that project's bonus (see PROJECT_BONUSES).")
    parser.add_argument("-u", "--user", help="Github user ID of the person requesting the review. Required with --supervisor; also excluded from reviewer selection.")
    parser.add_argument("-x", "--exclude", nargs='*', default=[], help="Github IDs to leave out, such as the PR's supervisor or reviewers already asked.")
    parser.add_argument("-s", "--supervisor", action="store_true", help="Flag to select a supervisor randomly.")
    parser.add_argument("--csv", help="Read the reviewers list from this local file instead of downloading it from GitHub.")
    parser.add_argument("-v", "--version", action="version", version=version, help="Show script's version and exit.")
    args = parser.parse_args()

    if args.csv:
        with open(args.csv, encoding="utf-8") as fh:
            csv_data = fh.read()
    else:
        csv_data = download_csv()
    if not csv_data:
        print("Failed to process reviewers data.")
        return

    data = parse_csv(csv_data)
    if not data:
        print("No data available from the source.")
        return

    if args.supervisor:
        if not args.user:
            print("Github user ID must be provided with --user when using --supervisor.")
            return
        selected_supervisor = select_random_supervisor(data, [args.user, *args.exclude], args.institution)
        if selected_supervisor:
            print(f"Selected supervisor: {selected_supervisor['Full Name']} ({selected_supervisor['Github ID']})")
        else:
            print("No eligible supervisors found.")
        return

    exclude_ids = [*args.exclude, *([args.user] if args.user else [])]
    all_candidates, top_candidates, reason = select_best_reviewer(data, args.institution, args.tag, exclude_ids)
    print(format_report(all_candidates, top_candidates, reason), end="")


if __name__ == "__main__":
    main()
