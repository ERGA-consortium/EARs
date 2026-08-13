"""Storage layer: conflict handling, and recording a merge atomically."""

import pytest

from ear_bot.roster import EAR_REVIEWS_CSV, REVIEWERS_CSV, Roster, RosterError

from .conftest import HEADER, ROWS


def test_two_writes_in_one_run_both_succeed(repo, roster):
    """find_reviewer() updates the roster once per PR in a single sweep."""
    roster.apply(reviewers={"alice"}, busy=True)
    roster.apply(reviewers={"bob"}, busy=True)
    assert len(repo.writes) == 2
    assert repo.roster_rows()["bob"]["Working PRs"] == "1"


def test_conflict_preserves_the_other_runs_change(repo, roster):
    """The whole point of the SHA guard: never silently undo a concurrent write.

    Another run credits carol while we are marking bob busy.  Our retry must
    keep carol's change and add ours on top, not replay our stale snapshot.
    """
    concurrent = f"{HEADER}\n" + ROWS.replace(
        "carol,Carol C,Genoscope,Y,2,2025-03-03,Y,0,1000",
        "carol,Carol C,Genoscope,Y,3,2026-01-01,Y,0,1111",
    )
    repo.commit_concurrently(REVIEWERS_CSV, concurrent)

    roster.apply(reviewers={"bob"}, busy=True)

    rows = repo.roster_rows()
    assert rows["carol"]["Calling Score"] == "1111", "concurrent change was clobbered"
    assert rows["carol"]["Total Reviews"] == "3", "concurrent change was clobbered"
    assert rows["bob"]["Working PRs"] == "1", "our own change was lost"


def test_conflict_does_not_double_apply_our_change(repo, roster):
    """Re-running the mutation on retry must not apply it twice."""
    repo.commit_concurrently(REVIEWERS_CSV, f"{HEADER}\n{ROWS}")
    roster.apply(reviewers={"alice"}, busy=True)
    # alice starts at 1; exactly one increment must land.
    assert repo.roster_rows()["alice"]["Working PRs"] == "2"


def test_failed_write_leaves_no_uncommitted_counters(repo, roster):
    """If every retry fails, the in-memory roster must not keep the edits."""
    def always_conflict(_repo):
        _repo.files[REVIEWERS_CSV][1] = "moved-again"

    repo.before_write[REVIEWERS_CSV] = always_conflict
    repo.commit_concurrently = lambda *a, **k: None
    original = repo.update_file

    def always_reject(path, message, content, sha):
        if path == REVIEWERS_CSV:
            raise RuntimeError("409")
        return original(path, message, content, sha)

    repo.update_file = always_reject
    with pytest.raises(RuntimeError):
        roster.apply(reviewers={"alice"}, busy=True)
    assert roster.data, "roster was left empty"
    assert next(r for r in roster.data if r["Github ID"] == "alice")[
        "Working PRs"
    ] == "1", "uncommitted edit survived the failure"


def test_unknown_reviewer_raises_in_strict_mode(roster):
    with pytest.raises(RosterError):
        roster.apply(reviewers={"ghost"}, busy=False)


def test_release_path_skips_unknown_and_frees_the_rest(repo, roster):
    """A departed reviewer must not block CLEAR for everyone else."""
    roster.apply(reviewers={"ghost", "alice"}, busy=False, strict=False)
    assert len(repo.writes) == 1
    assert repo.roster_rows()["alice"]["Working PRs"] == "0"


def test_blank_reviewer_id_is_treated_as_missing(roster):
    """get_user_info() returns "" for a deleted account."""
    assert roster.missing([""]) == {""}
    with pytest.raises(RosterError):
        roster.apply(reviewers={""}, busy=False)


ROW = ["https://github.com/o/r/pull/9"] + [""] * 13


def test_record_review_writes_nothing_when_reviewer_unknown(repo, roster):
    with pytest.raises(RosterError):
        roster.record_review(
            row_values=ROW, reviewers=["ghost"], institution="Sanger",
            submitted_at="2026-01-01",
        )
    assert repo.writes == []


def test_record_review_is_idempotent(repo):
    repo.files[EAR_REVIEWS_CSV][0] = f"PR URL,Species\n{ROW[0]},Foo\n"
    roster = Roster(repo)
    assert roster.record_review(
        row_values=ROW, reviewers=["alice"], institution="Sanger",
        submitted_at="2026-01-01",
    ) is False
    assert repo.writes == []


def test_record_review_logs_before_updating_counters(repo, roster):
    """Order matters: the log row is the key that makes a re-run safe."""
    assert roster.record_review(
        row_values=ROW, reviewers=["alice"], institution="Sanger",
        submitted_at="2026-01-01",
    ) is True
    assert [p for p, _ in repo.writes] == [EAR_REVIEWS_CSV, REVIEWERS_CSV]
    alice = repo.roster_rows()["alice"]
    assert alice["Working PRs"] == "0"
    assert alice["Total Reviews"] == "4"


def test_review_log_append_survives_a_conflict(repo, roster):
    """A concurrent append must be preserved, not overwritten."""
    repo.commit_concurrently(
        EAR_REVIEWS_CSV, "PR URL,Species\nhttps://github.com/o/r/pull/8,Other\n"
    )
    roster.record_review(
        row_values=ROW, reviewers=["alice"], institution="Sanger",
        submitted_at="2026-01-01",
    )
    urls = [r[0] for r in repo.review_rows()[1:]]
    assert "https://github.com/o/r/pull/8" in urls, "concurrent row was lost"
    assert ROW[0] in urls, "our row was lost"


def test_timeout_penalty_is_applied(repo, roster):
    roster.apply(reviewers={"alice"}, busy=False, fined_reviewers={"alice"})
    assert repo.roster_rows()["alice"]["Calling Score"] == "1001"


def test_real_roster_round_trips_byte_for_byte():
    """Guards against the bot reformatting the whole file on its first write."""
    from rev import get_EAR_reviewer as g

    src = open("rev/reviewers_list.csv").read()
    assert g.format_csv(g.parse_csv(src), g.csv_fieldnames(src)).strip() == src.strip()


def test_exists_reports_missing_and_present(repo):
    from ear_bot.roster import exists

    assert exists(repo, REVIEWERS_CSV)
    assert not exists(repo, "Assembly_Reports/x/x_EAR.yaml")


def test_concurrent_log_hit_does_not_double_apply_counters(repo, roster):
    """If another run logs the row first, it owns the counters too."""
    repo.commit_concurrently(
        EAR_REVIEWS_CSV, f"PR URL,Species\n{ROW[0]},Foo\n"
    )
    before = repo.roster_rows()["alice"]
    assert roster.record_review(
        row_values=ROW, reviewers=["alice"], institution="Sanger",
        submitted_at="2026-01-01",
    ) is False
    after = repo.roster_rows()["alice"]
    assert after["Total Reviews"] == before["Total Reviews"]
    assert after["Working PRs"] == before["Working PRs"]


def test_timeout_penalty_applies_with_no_reviewers_to_release(repo, roster):
    """A reviewer asked twice then accepting leaves `reviewers` empty."""
    roster.apply(reviewers=set(), busy=False, fined_reviewers={"alice"})
    assert repo.roster_rows()["alice"]["Calling Score"] == "1001"


def test_unknown_fined_reviewer_is_ignored(repo, roster):
    roster.apply(reviewers=set(), busy=False, fined_reviewers={"ghost"})
    assert repo.writes == []
