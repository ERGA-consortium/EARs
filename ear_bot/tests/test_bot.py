"""Comment parsing, review-state detection, and the merge path."""

from .conftest import FakeComment, FakePR, FakeEvent, FakeReview


# --- comment parsing --------------------------------------------------------

def test_okay_is_accepted(bot):
    assert bot._says("Okay", r"\bok(ay)?\b")


def test_greeting_before_ok_is_accepted(bot):
    assert bot._says("Hi Diego,\n\nOK, happy to supervise.", r"\bok(ay)?\b")


def test_ok_inside_another_word_is_not_a_confirmation(bot):
    for text in ("Looks good to me", "I took a look", "this is broken"):
        assert not bot._says(text, r"\bok(ay)?\b"), text


def test_quoting_the_bot_is_not_a_confirmation(bot):
    quoted = "> Please reply to this message only with **OK**\nI cannot supervise this one"
    assert not bot._says(quoted, r"\bok(ay)?\b")


def test_clear_is_recognised_on_a_later_line(bot):
    text = "Hi bot, this one is abandoned.\n@erga-ear-bot CLEAR"
    assert bot._says(text, r"@erga-ear-bot\s+clear")


def test_quoted_clear_is_ignored(bot):
    text = "> please instruct me with @erga-ear-bot clear\nLeaving it open for now"
    assert not bot._says(text, r"@erga-ear-bot\s+clear")


OPTS = {"yes": r"\byes\b", "no": r"\bno\b"}


def test_decline_mentioning_yes_later_is_still_a_decline(bot):
    """The regression that made the bot appoint someone who had just refused."""
    text = "No, sorry I can't.\nMaybe ask @alice - yes, she knows this genus."
    assert bot._decision(text, OPTS) == "no"


def test_greeting_before_yes_is_an_acceptance(bot):
    assert bot._decision("Hi!\nYes, happy to review.", OPTS) == "yes"


def test_plain_answers_still_work(bot):
    assert bot._decision("Yes", OPTS) == "yes"
    assert bot._decision("No", OPTS) == "no"


def test_ambiguous_line_is_not_guessed(bot):
    assert bot._decision("yes or no, I am not sure", OPTS) is None


# --- review state -----------------------------------------------------------

def asked(login):
    return FakeComment(f"Hi @{login}, do you agree to review this assembly?")


def test_appointed_reviewers_comment_review_blocks_resolicitation(bot):
    """GitHub clears the review request once they submit anything."""
    pr = FakePR(reviews=[FakeReview("rev", "COMMENTED")], comments=[asked("rev")])
    assert bot._review_in_progress(pr)


def test_passerby_comment_review_does_not_block(bot):
    pr = FakePR(reviews=[FakeReview("passerby", "COMMENTED")], comments=[asked("rev")])
    assert not bot._review_in_progress(pr)


def test_passerby_verdict_does_not_block_when_nobody_is_appointed(bot):
    """A stranger must not be able to freeze a PR on a public repo."""
    pr = FakePR(reviews=[FakeReview("passerby", "CHANGES_REQUESTED")], comments=[])
    assert not bot._review_in_progress(pr)


def test_timed_out_reviewers_old_review_does_not_block_forever(bot):
    """Only the person currently on the hook counts, not the whole history."""
    pr = FakePR(
        reviews=[FakeReview("first", "COMMENTED")],
        comments=[asked("first"), asked("second")],  # newest first after .reversed
    )
    # _search_comment_user reads newest-first, so "second" is current.
    assert not bot._review_in_progress(pr)


def test_hand_assigned_reviewer_blocks(bot):
    pr = FakePR(reviews=[FakeReview("manual", "COMMENTED")], comments=[], requested=["manual"])
    assert bot._review_in_progress(pr)


def test_dismissed_approval_still_counts_as_a_verdict(bot):
    pr = FakePR(reviews=[FakeReview("rev", "DISMISSED")], comments=[asked("rev")])
    assert len(bot._binding_reviews(pr)) == 1


def test_deleted_account_review_does_not_crash(bot):
    """review.user is None when the author deleted their account."""
    pr = FakePR(reviews=[FakeReview(None, "APPROVED")], comments=[asked("rev")])
    assert bot._reviews_by(pr, {"rev"}) == []
    bot._review_in_progress(pr)  # must not raise


# --- approve_reviewer authorisation ----------------------------------------

def run_approve(bot, pr, approver):
    bot.pr_number = "1"
    bot.reviewer = approver
    bot.repo = type("R", (), {"get_pull": staticmethod(lambda n: pr)})()
    try:
        bot.approve_reviewer()
    except SystemExit:
        pass
    return pr.comments


def test_outsider_approval_is_not_thanked(bot):
    pr = FakePR(reviews=[FakeReview("outsider", "APPROVED")], comments=[asked("rev")])
    assert run_approve(bot, pr, "outsider") == []


def test_appointed_reviewer_is_thanked(bot):
    pr = FakePR(reviews=[FakeReview("rev", "APPROVED")], comments=[asked("rev")])
    assert any("for the review" in c for c in run_approve(bot, pr, "rev"))


def test_hand_assigned_reviewer_is_thanked(bot):
    """requested_reviewers is empty by then; the request event still records it."""
    pr = FakePR(
        reviews=[FakeReview("manual", "APPROVED")],
        comments=[],
        requested=[],
        events=[FakeEvent("review_requested", "manual")],
    )
    assert any("for the review" in c for c in run_approve(bot, pr, "manual"))


def test_stranger_without_a_request_event_is_not_thanked(bot):
    pr = FakePR(
        reviews=[FakeReview("stranger", "APPROVED")],
        comments=[],
        events=[FakeEvent("labeled")],
    )
    assert run_approve(bot, pr, "stranger") == []
