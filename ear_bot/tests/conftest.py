"""Fakes standing in for the GitHub API, so the bot can be tested offline.

Only the surface the bot actually touches is modelled.  The important one is
FakeRepo, which enforces GitHub's real contract for update_file: the blob SHA
you pass must match the current one, or the write is rejected.  That is what
makes the concurrency tests meaningful.
"""

import csv
import datetime as dt
import io
import os
import sys

import pytest
from github import UnknownObjectException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

REVIEWERS_CSV = "rev/reviewers_list.csv"
EAR_REVIEWS_CSV = "rev/EAR_reviews.csv"

HEADER = (
    "Github ID,Full Name,Institution,Supervisor,Total Reviews,"
    "Last Review,Active,Working PRs,Calling Score"
)
ROWS = (
    "alice,Alice A,Sanger,N,3,2025-01-01,Y,1,1000\n"
    "bob,Bob B,CNAG,N,1,2025-02-02,Y,0,1000\n"
    "carol,Carol C,Genoscope,Y,2,2025-03-03,Y,0,1000\n"
)


class Conflict(Exception):
    """Stands in for the 409 PyGithub raises on a stale SHA."""


class FakeContents:
    def __init__(self, text, sha, path):
        self.decoded_content = text.encode()
        self.sha = sha
        self.path = path


class FakeRepo:
    def __init__(self, roster=None, reviews="PR URL,Species\n"):
        self.files = {
            REVIEWERS_CSV: [roster or f"{HEADER}\n{ROWS}", "roster-0"],
            EAR_REVIEWS_CSV: [reviews, "reviews-0"],
        }
        self.writes = []
        self._n = 0
        # Callables invoked once, just before a write to that path, to
        # simulate another workflow run committing underneath us.
        self.before_write = {}

    def get_contents(self, path):
        if path not in self.files:
            raise UnknownObjectException(404, "Not Found", None)
        text, sha = self.files[path]
        return FakeContents(text, sha, path)

    def update_file(self, path, message, content, sha):
        hook = self.before_write.pop(path, None)
        if hook:
            hook(self)
        if sha != self.files[path][1]:
            raise Conflict(f"409: have {self.files[path][1]}, got {sha}")
        self._n += 1
        self.files[path] = [content, f"{path}-{self._n}"]
        self.writes.append((path, content))
        return {"content": FakeContents(content, self.files[path][1], path)}

    def create_file(self, path, message, content):
        self._n += 1
        self.files[path] = [content, f"{path}-{self._n}"]
        self.writes.append((path, content))
        return {"content": FakeContents(content, self.files[path][1], path)}

    # -- helpers for assertions ------------------------------------------
    def roster_rows(self):
        text = self.files[REVIEWERS_CSV][0]
        return {r["Github ID"]: r for r in csv.DictReader(io.StringIO(text.strip()))}

    def review_rows(self):
        text = self.files[EAR_REVIEWS_CSV][0]
        return [r for r in csv.reader(io.StringIO(text.strip())) if r]

    def commit_concurrently(self, path, new_text):
        """Register a one-shot concurrent commit to ``path``."""

        def hook(repo):
            repo._n += 1
            repo.files[path] = [new_text, f"{path}-concurrent-{repo._n}"]

        self.before_write[path] = hook


class FakeUser:
    def __init__(self, login, name=None):
        self.login = login
        self.name = name


class FakeReview:
    def __init__(self, login, state):
        self.user = FakeUser(login) if login is not None else None
        self.state = state


class FakeReviews(list):
    @property
    def totalCount(self):
        return len(self)

    @property
    def reversed(self):
        return FakeReviews(list(self)[::-1])


class FakeComment:
    def __init__(self, body, bot=True, login="erga-ear-bot[bot]"):
        self.body = body
        self.raw_data = {"user": {"type": "Bot" if bot else "User"}}
        self.user = FakeUser(login)
        self.created_at = dt.datetime.now(dt.timezone.utc)


class FakeComments(list):
    @property
    def reversed(self):
        return FakeComments(list(self)[::-1])


class FakeEvent:
    def __init__(self, event, requested_reviewer=None):
        self.event = event
        self.requested_reviewer = (
            FakeUser(requested_reviewer) if requested_reviewer else None
        )


class FakeIssue:
    def __init__(self, events):
        self._events = events

    def get_events(self):
        return self._events


class FakePR:
    def __init__(
        self,
        number=1,
        author="researcher",
        assignee="supervisor",
        reviews=(),
        comments=(),
        requested=(),
        events=(),
        labels=("ERGA-BGE",),
        body="- Project: ERGA-BGE\n- Affiliation: Sanger\n- Species: Foo\n",
        state="open",
        files=("Assembly_Reports/x/x_EAR.pdf",),
    ):
        self.number = number
        self.user = FakeUser(author)
        self.assignee = FakeUser(assignee) if assignee else None
        self.assignees = [self.assignee] if assignee else []
        self.html_url = f"https://github.com/o/r/pull/{number}"
        self._reviews = FakeReviews(reviews)
        self._comments = FakeComments(comments)
        self.requested_reviewers = [FakeUser(r) for r in requested]
        self._events = list(events)
        self._labels = list(labels)
        self.body = body
        self.state = state
        self._files = list(files)
        self.created_at = dt.datetime.now(dt.timezone.utc)
        self.comments = []
        self.labels_added = []
        self.review_requests = []

    def get_reviews(self):
        return self._reviews

    def get_issue_comments(self):
        return self._comments

    def get_labels(self):
        return [type("L", (), {"name": n})() for n in self._labels]

    def get_review_requests(self):
        return (
            type("P", (), {"totalCount": len(self.requested_reviewers)})(),
            type("P", (), {"totalCount": 0})(),
        )

    def get_files(self):
        return [
            type("F", (), {"filename": f, "blob_url": f"https://x/{f}", "status": "added"})()
            for f in self._files
        ]

    def as_issue(self):
        return FakeIssue(self._events)

    def create_issue_comment(self, body):
        self.comments.append(body)

    def add_to_labels(self, label):
        self.labels_added.append(label)

    def remove_from_labels(self, label):
        if label in self._labels:
            self._labels.remove(label)

    def add_to_assignees(self, user):
        self.assignees.append(FakeUser(user))

    def create_review_request(self, users):
        self.review_requests.extend(users)


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def roster(repo):
    from ear_bot.roster import Roster

    return Roster(repo)


@pytest.fixture
def bot():
    """An EARBotReviewer with __init__ bypassed, since it does network I/O."""
    from ear_bot.ear_bot_reviewer import EARBotReviewer

    b = object.__new__(EARBotReviewer)
    b.valid_projects = ["ERGA-BGE", "ERGA-Pilot", "ERGA-Community"]
    b.repo = None
    b.roster = None
    return b
