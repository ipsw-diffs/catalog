from __future__ import annotations

import json

import pytest

from ipsw_diff_social.state import (
    MAX_POST_RECEIPTS,
    STATE_ACTOR,
    STATE_MARKER,
    STATE_TITLE,
    GitHubIssueStore,
    PublisherState,
    parse_state_body,
    state_body,
)

COMMIT = "a" * 40
NETWORKS = ("bluesky", "mastodon", "twitter")


def _body(**overrides: object) -> str:
    document = {
        "schema_version": 3,
        "catalog_commit": COMMIT,
        "queue": [],
        "pending": {},
        "posts": {},
        **overrides,
    }
    return f"{STATE_MARKER}\n```json\n{json.dumps(document)}\n```\n"


def test_round_trip_tracks_each_network_separately() -> None:
    state = PublisherState(catalog_commit=COMMIT, queue={"new"})
    state.mark_pending("new", NETWORKS)
    restored = parse_state_body(state_body(state))

    assert restored.queue == set()
    assert set(restored.pending["new"]) == set(NETWORKS)

    restored.mark_posted("new", "bluesky")

    assert set(restored.pending["new"]) == {"mastodon", "twitter"}
    assert set(restored.posts["new"]) == {"bluesky"}
    assert restored.unposted("new", NETWORKS) == ("mastodon", "twitter")

    restored.mark_posted("new", "mastodon")
    restored.mark_posted("new", "twitter")

    assert restored.pending == {}
    assert set(restored.posts["new"]) == set(NETWORKS)
    assert restored.unposted("new", NETWORKS) == ()


def test_state_retains_only_bounded_recent_post_receipts() -> None:
    state = PublisherState(catalog_commit=COMMIT)

    for index in range(MAX_POST_RECEIPTS + 5):
        entry_id = f"entry-{index:03}"
        state.mark_pending(entry_id, NETWORKS)
        for network in NETWORKS:
            state.mark_posted(entry_id, network)

    assert len(state.posts) == MAX_POST_RECEIPTS
    assert "entry-000" not in state.posts
    assert f"entry-{MAX_POST_RECEIPTS + 4:03}" in state.posts
    assert len(state_body(state).encode()) < 60_000


def test_enqueue_skips_entries_already_pending_or_posted() -> None:
    state = PublisherState(catalog_commit=COMMIT)
    state.mark_pending("pending", ("twitter",))
    state.mark_pending("posted", ("twitter",))
    state.mark_posted("posted", "twitter")

    state.enqueue({"pending", "posted", "new"})

    assert state.queue == {"new"}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"pending": {"e": {"twitter": "t"}}, "posts": {"e": {"twitter": "t"}}},
            "both pending and posted",
        ),
        ({"pending": {"e": {}}}, "lists no network"),
        ({"pending": {"e": {"twitter": "t"}}, "queue": ["e"]}, "must not overlap"),
        ({"pending": {"e": "twitter"}}, "map entries to networks"),
        ({"schema_version": 2}, "unsupported publisher state schema"),
    ],
)
def test_contradictory_or_older_state_is_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_state_body(_body(**overrides))


class FakeResponse:
    def __init__(self, value: object):
        self._value = value

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._value


class FakeSession:
    def __init__(self, pages: list[list[dict[str, object]]]):
        self._pages = pages
        self.get_params: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        del url
        params = kwargs.get("params")
        assert isinstance(params, dict)
        self.get_params.append(params)
        page = params.get("page")
        assert isinstance(page, int)
        return FakeResponse(self._pages[page - 1])

    def post(self, url: str, **_kwargs: object) -> FakeResponse:
        raise AssertionError(f"unexpected POST: {url}")

    def patch(self, url: str, **_kwargs: object) -> FakeResponse:
        raise AssertionError(f"unexpected PATCH: {url}")


def test_issue_store_uses_creator_filter_and_pages_until_state_issue() -> None:
    state = PublisherState(catalog_commit=COMMIT, queue={"trusted"})
    first_page: list[dict[str, object]] = [
        {
            "number": index,
            "title": "another issue",
            "body": "",
            "user": {"login": STATE_ACTOR},
        }
        for index in range(100)
    ]
    second_page: list[dict[str, object]] = [
        {
            "number": 101,
            "title": STATE_TITLE,
            "body": state_body(state),
            "user": {"login": STATE_ACTOR},
        }
    ]
    session = FakeSession([first_page, second_page])

    issue = GitHubIssueStore("ipsw-diffs/catalog", "token", session).find()

    assert issue is not None
    assert issue.number == 101
    assert issue.state.queue == {"trusted"}
    assert len(session.get_params) == 2
    assert session.get_params[0]["creator"] == STATE_ACTOR
