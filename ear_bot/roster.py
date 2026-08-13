"""Roster and review-log storage for the EAR bot.

Everything that reads or writes the two CSV files in ``rev/`` lives here:

    rev/reviewers_list.csv   the reviewer roster and its scores
    rev/EAR_reviews.csv      the append-only log of completed reviews

Two rules this module exists to enforce:

1. **A write never silently loses another run's changes.**  ``update_if_unchanged``
   sends the blob SHA the content was based on, so GitHub rejects the write if
   the file moved underneath us.  On rejection the caller's data is re-read and
   the change is re-applied, rather than clobbering or giving up.

2. **Recording a completed review is all-or-nothing.**  ``record_review``
   validates every precondition before writing anything, so a merge is either
   fully recorded or not recorded at all.  It writes the review-log row before
   the roster counters, because that row is the key a re-run checks: getting
   only as far as the row leaves a reviewer uncredited and visibly still busy,
   whereas applying the counters first and failing leaves nothing to show it
   happened, so a re-run applies them again and corrupts scores silently.
"""

import csv
import io

from github import UnknownObjectException

from rev import get_EAR_reviewer

REVIEWERS_CSV = "rev/reviewers_list.csv"
EAR_REVIEWS_CSV = "rev/EAR_reviews.csv"

# How many times to re-read and re-apply after a conflicting write.
MAX_WRITE_ATTEMPTS = 3


class RosterError(Exception):
    """A precondition for updating the roster was not met."""


def replace(repo, path, message, content):
    """Write ``path`` unconditionally, creating it if absent.

    For files only this bot produces, where there is no concurrent writer to
    conflict with.  Use ``update_if_unchanged`` for the shared CSVs.
    """
    try:
        contents = repo.get_contents(path)
        if isinstance(contents, list):
            raise RosterError(f"Expected a file, got a directory: {path}")
        result = repo.update_file(path, message, content, contents.sha)
        print(f"Updated {path} file.")
    except UnknownObjectException:
        result = repo.create_file(path, message, content)
        print(f"Created {path} file.")
    return result["content"].sha


def update_if_unchanged(repo, path, message, content, sha):
    """Write ``path`` only if its current blob still matches ``sha``.

    ``sha`` is required: an optional one would make forgetting it re-introduce
    the lost-update race this function exists to prevent.  Returns the new blob
    SHA so a caller writing the same path repeatedly can carry it forward.
    """
    result = repo.update_file(path, message, content, sha)
    print(f"Updated {path} file.")
    return result["content"].sha


def exists(repo, path):
    """True if ``path`` is already committed."""
    try:
        repo.get_contents(path)
        return True
    except UnknownObjectException:
        return False


def _fetch(repo, path):
    contents = repo.get_contents(path)
    if isinstance(contents, list):
        raise RosterError(f"Expected a file, got a directory: {path}")
    text = contents.decoded_content.decode("utf-8")
    if not text:
        raise RosterError(f"The CSV file is empty: {path}")
    return text, contents.sha


class Roster:
    """The reviewer roster, plus the review log it is updated alongside."""

    def __init__(self, repo):
        self.repo = repo
        self._load()

    def _load(self):
        text, self.sha = _fetch(self.repo, REVIEWERS_CSV)
        self.fieldnames = get_EAR_reviewer.csv_fieldnames(text)
        self.data = get_EAR_reviewer.parse_csv(text)

    def ids(self):
        return {row.get("Github ID", "").lower() for row in self.data}

    def missing(self, reviewers):
        """Which of ``reviewers`` are not on the roster.

        A blank ID counts as missing.  get_user_info() returns "" for a
        deleted GitHub account, and treating that as known let a merge be
        half-recorded: the log row was written with empty reviewer fields
        while the roster update quietly did nothing.
        """
        known = self.ids()
        return {r for r in reviewers if not r or r.lower() not in known}

    def _write(self, message, mutate):
        """Apply ``mutate`` to the roster rows and commit, retrying on conflict.

        ``mutate`` is a callable taking the row list and changing it in place.
        It is deliberately re-run rather than re-sent: on a conflict the local
        copy is discarded, the file is re-read, and the same change is applied
        to the *other run's* committed rows.  Re-sending our own snapshot would
        overwrite whatever they wrote, which is the lost update this class
        exists to prevent.
        """
        for attempt in range(MAX_WRITE_ATTEMPTS):
            mutate(self.data)
            try:
                self.sha = update_if_unchanged(
                    self.repo,
                    REVIEWERS_CSV,
                    message,
                    get_EAR_reviewer.format_csv(self.data, self.fieldnames),
                    self.sha,
                )
                return
            except Exception as exc:
                # Drop our uncommitted edits either way, so the caller is never
                # left holding counters that were never written.
                self._load()
                if attempt == MAX_WRITE_ATTEMPTS - 1:
                    raise
                print(f"Roster write rejected ({exc}); re-reading and retrying.")

    def apply(
        self,
        reviewers,
        busy,
        institution="",
        submitted_at="",
        fined_reviewers=(),
        message="Update reviewers list",
        strict=True,
    ):
        """Adjust counters for ``reviewers`` and commit.

        With ``strict`` (the default) an unknown ID raises before anything is
        touched, so a caller recording a review cannot half-apply a change set.

        Release paths pass ``strict=False``: freeing everyone else's counters
        matters more than refusing because one person has since left the
        consortium and been removed from the roster.  Without this, a single
        departed reviewer permanently blocks the CLEAR command for that PR.
        """
        # Checked before blanks are dropped, so a deleted account (which
        # get_user_info reports as "") is reported rather than silently
        # becoming a no-op.
        unknown = self.missing(reviewers)
        reviewers = {r.lower() for r in reviewers if r}
        if unknown:
            named = sorted(u or "(unknown user)" for u in unknown)
            if strict:
                raise RosterError(f"Not on the reviewers list: {', '.join(named)}")
            print(f"Not on the reviewers list, skipping: {', '.join(named)}")
            reviewers -= {u.lower() for u in unknown if u}

        # Not gated on `reviewers`: the timeout penalty is a separate change
        # set that happens to travel with it.  A reviewer who was asked twice
        # and then accepted leaves `reviewers` empty while still owing the
        # penalty, and returning early here dropped it silently.
        fined = {r.lower() for r in fined_reviewers if r} - self.missing(
            fined_reviewers
        )
        if not reviewers and not fined:
            print("No reviewers to update.")
            return

        def mutate(rows):
            for row in rows:
                row_id = row.get("Github ID", "").lower()
                score = int(row.get("Calling Score", 1000) or 1000)
                total = int(row.get("Total Reviews", 0) or 0)
                working = int(row.get("Working PRs", 0) or 0)

                if row_id in reviewers:
                    row["Working PRs"] = str(
                        working + 1 if busy else max(0, working - 1)
                    )
                    if submitted_at:
                        score -= 1
                        row["Calling Score"] = str(score)
                        row["Total Reviews"] = str(total + 1)
                        row["Last Review"] = submitted_at
                # Not an elif: a reviewer who timed out is always also in
                # `reviewers`, since both sets come from the same "do you agree
                # to review" comments.
                if row_id in fined:
                    score += 1
                    row["Calling Score"] = str(score)
                if (
                    institution
                    and row.get("Institution", "").lower() == institution.lower()
                ):
                    row["Calling Score"] = str(score + 1)

        self._write(message, mutate)
        print(f"Updated the reviewers list for {', '.join(sorted(reviewers))}.")

    def already_recorded(self, pr_url):
        """True if the review log already has a row for this PR.

        WF6 fires once per close, but a manual re-run would otherwise append a
        duplicate row for the same PR.
        """
        text, _ = _fetch(self.repo, EAR_REVIEWS_CSV)
        return any(
            row and row[0].strip() == pr_url
            for row in csv.reader(io.StringIO(text))
        )

    def record_review(self, row_values, reviewers, institution, submitted_at):
        """Append a review row and update the roster, or do neither.

        Every precondition is checked before the first write.  The log row is
        written *first* because it is the idempotency key: if the roster write
        then fails, a re-run sees the row and stops, leaving a reviewer
        uncredited, which is visible as a stuck Working PRs count.  The
        reverse order fails the other way -- the counters are applied with no
        row to record that it happened, so a re-run applies them again and
        corrupts scores silently.  Do not swap these.

        Returns False without touching the counters if the review was already
        logged, including when a concurrent run logged it between our check
        and our write.
        """
        pr_url = row_values[0]
        if self.already_recorded(pr_url):
            print(f"{pr_url} is already in the review log; nothing to record.")
            return False

        unknown = self.missing(reviewers)
        if unknown:
            raise RosterError(
                f"Not on the reviewers list: {', '.join(sorted(unknown or {'(unknown user)'}))}"
            )

        if not self._append_review(row_values):
            # Another run won the race and logged it.  It applies the counters
            # too, so applying them here as well would double-count.
            print(f"{pr_url} was logged by a concurrent run; leaving counters to it.")
            return False
        self.apply(
            reviewers=reviewers,
            busy=False,
            institution=institution,
            submitted_at=submitted_at,
        )
        print(f"Recorded the review for {pr_url}.")
        return True

    def _append_review(self, row_values):
        """Append one row to the review log, retrying on conflict.

        Re-reads on every attempt, so a row another run appended in the
        meantime is preserved rather than overwritten.  Returns True if this
        call wrote the row and False if it was already there, so the caller
        can tell "recorded" from "somebody else recorded it".
        """
        pr_url = row_values[0]
        for attempt in range(MAX_WRITE_ATTEMPTS):
            text, sha = _fetch(self.repo, EAR_REVIEWS_CSV)
            if any(
                row and row[0].strip() == pr_url
                for row in csv.reader(io.StringIO(text))
            ):
                return False
            buffer = io.StringIO()
            csv.writer(buffer, lineterminator="\n").writerow(row_values)
            if not text.endswith("\n"):
                text += "\n"
            try:
                update_if_unchanged(
                    self.repo,
                    EAR_REVIEWS_CSV,
                    "Add new EAR review",
                    text + buffer.getvalue(),
                    sha,
                )
                return True
            except Exception as exc:
                if attempt == MAX_WRITE_ATTEMPTS - 1:
                    raise
                print(f"Review log write rejected ({exc}); re-reading and retrying.")
