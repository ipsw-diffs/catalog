from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TypeGuard

import requests

STATE_TITLE = "[automation] ipsw-diff social publisher state"
STATE_MARKER = "<!-- ipsw-diff-social-state:v3 -->"
STATE_ACTOR = "github-actions[bot]"
SCHEMA_VERSION = 3
_STATE_PATTERN = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_FULL_OID = re.compile(r"[0-9a-f]{40}")
MAX_PENDING = 100
MAX_QUEUE = 500
MAX_POST_RECEIPTS = 100
MAX_ISSUE_BODY_BYTES = 60_000

# entry ID -> network -> UTC timestamp
NetworkTimes = dict[str, dict[str, str]]


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


class _IssueSession(Protocol):
    def get(self, url: str, **kwargs: object) -> _Response: ...

    def post(self, url: str, **kwargs: object) -> _Response: ...

    def patch(self, url: str, **kwargs: object) -> _Response: ...


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass
class PublisherState:
    """Which catalog entries are queued, in flight, or posted, per network.

    A network is marked pending before xpost runs and posted only after it succeeds, so a
    crash or an ambiguous failure leaves a pending marker that stops automatic retries.
    """

    catalog_commit: str
    queue: set[str] = field(default_factory=set)
    pending: NetworkTimes = field(default_factory=dict)
    posts: NetworkTimes = field(default_factory=dict)

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "catalog_commit": self.catalog_commit,
            "queue": sorted(self.queue),
            "pending": {
                entry: dict(sorted(times.items())) for entry, times in self.pending.items()
            },
            "posts": self.posts,
        }

    @classmethod
    def from_document(cls, value: object) -> PublisherState:
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported publisher state schema")
        catalog_commit = value.get("catalog_commit")
        queue = value.get("queue")
        pending = value.get("pending")
        posts = value.get("posts")
        if not isinstance(catalog_commit, str) or _FULL_OID.fullmatch(catalog_commit) is None:
            raise ValueError("publisher state catalog_commit must be a full lowercase SHA")
        if not isinstance(queue, list) or not all(isinstance(item, str) for item in queue):
            raise ValueError("publisher state queue must be a string array")
        if not _network_times(pending) or not _network_times(posts):
            raise ValueError("publisher state pending and posts must map entries to networks")
        queue_set = set(queue)
        if queue_set & set(pending):
            raise ValueError("publisher state queue and pending must not overlap")
        for entry_id, networks in pending.items():
            if not networks:
                raise ValueError(f"publisher state pending entry {entry_id} lists no network")
            if set(networks) & set(posts.get(entry_id, {})):
                raise ValueError(f"publisher state {entry_id} is both pending and posted")
        if len(queue_set) > MAX_QUEUE:
            raise ValueError("publisher state queue exceeds its safety limit")
        if len(pending) > MAX_PENDING:
            raise ValueError("publisher state pending map exceeds its safety limit")
        if len(posts) > MAX_POST_RECEIPTS:
            raise ValueError("publisher state post receipts exceed their safety limit")
        return cls(
            catalog_commit=catalog_commit,
            queue=queue_set,
            pending={entry: dict(times) for entry, times in pending.items()},
            posts={entry: dict(times) for entry, times in posts.items()},
        )

    def enqueue(self, entry_ids: set[str]) -> None:
        additions = entry_ids - set(self.pending) - set(self.posts)
        if len(self.queue | additions) > MAX_QUEUE:
            raise ValueError(
                f"catalog added more than the {MAX_QUEUE}-entry queue safety limit; "
                "bootstrap or publish the batch manually"
            )
        self.queue.update(additions)

    def unposted(self, entry_id: str, networks: tuple[str, ...]) -> tuple[str, ...]:
        """The networks that have not posted this entry yet, in the given order."""
        done = self.posts.get(entry_id, {})
        return tuple(network for network in networks if network not in done)

    def mark_pending(self, entry_id: str, networks: tuple[str, ...]) -> None:
        if entry_id not in self.pending and len(self.pending) >= MAX_PENDING:
            raise ValueError(
                f"publisher has {MAX_PENDING} pending entries; review the state issue first"
            )
        self.queue.discard(entry_id)
        timestamp = _now()
        self.pending.setdefault(entry_id, {}).update(dict.fromkeys(networks, timestamp))

    def mark_posted(self, entry_id: str, network: str) -> None:
        networks = self.pending.get(entry_id, {})
        networks.pop(network, None)
        if not networks:
            self.pending.pop(entry_id, None)
        # Re-inserting moves the entry to the end, so trimming drops the oldest receipts.
        receipts = self.posts.pop(entry_id, {})
        receipts[network] = _now()
        self.posts[entry_id] = receipts
        while len(self.posts) > MAX_POST_RECEIPTS:
            del self.posts[next(iter(self.posts))]

    def finish(self, entry_id: str) -> None:
        """Drop an entry with nothing left to post from the queue."""
        self.queue.discard(entry_id)


def _network_times(value: object) -> TypeGuard[NetworkTimes]:
    return isinstance(value, dict) and all(
        isinstance(entry, str)
        and isinstance(times, dict)
        and all(isinstance(network, str) and isinstance(at, str) for network, at in times.items())
        for entry, times in value.items()
    )


def state_body(state: PublisherState) -> str:
    encoded = json.dumps(state.to_document(), indent=2)
    body = (
        f"{STATE_MARKER}\n"
        "This issue is machine-managed by the social publisher workflow.\n\n"
        f"```json\n{encoded}\n```\n"
    )
    if len(body.encode("utf-8")) > MAX_ISSUE_BODY_BYTES:
        raise ValueError("publisher state exceeds the bounded issue-body safety limit")
    return body


def parse_state_body(body: str) -> PublisherState:
    if STATE_MARKER not in body:
        raise ValueError("publisher state marker is missing")
    match = _STATE_PATTERN.search(body)
    if match is None:
        raise ValueError("publisher state JSON block is missing")
    return PublisherState.from_document(json.loads(match.group(1)))


@dataclass(frozen=True)
class StateIssue:
    number: int
    state: PublisherState


class GitHubIssueStore:
    def __init__(self, repository: str, token: str, session: _IssueSession | None = None):
        if repository.count("/") != 1:
            raise ValueError("GITHUB_REPOSITORY must use owner/name format")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        self._repository = repository
        self._session = session if session is not None else requests.Session()
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._base = f"https://api.github.com/repos/{repository}"

    def find(self) -> StateIssue | None:
        """Find the bot-created state issue without a fixed pagination cliff."""
        page = 1
        while True:
            response = self._session.get(
                f"{self._base}/issues",
                headers=self._headers,
                params={
                    "state": "all",
                    "creator": STATE_ACTOR,
                    "per_page": 100,
                    "page": page,
                    "sort": "created",
                    "direction": "desc",
                },
                timeout=(10, 30),
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise RuntimeError("GitHub issues response was not an array")
            for row in rows:
                if not isinstance(row, dict) or "pull_request" in row:
                    continue
                if row.get("title") != STATE_TITLE:
                    continue
                user = row.get("user")
                if not isinstance(user, dict) or user.get("login") != STATE_ACTOR:
                    continue
                number = row.get("number")
                body = row.get("body")
                if not isinstance(number, int) or not isinstance(body, str):
                    raise RuntimeError("GitHub state issue is malformed")
                return StateIssue(number=number, state=parse_state_body(body))
            if len(rows) < 100:
                return None
            page += 1

    def create(self, state: PublisherState) -> StateIssue:
        response = self._session.post(
            f"{self._base}/issues",
            headers=self._headers,
            json={"title": STATE_TITLE, "body": state_body(state)},
            timeout=(10, 30),
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("number"), int):
            raise RuntimeError("GitHub create-issue response did not contain an issue number")
        return StateIssue(number=value["number"], state=state)

    def save(self, issue_number: int, state: PublisherState) -> None:
        response = self._session.patch(
            f"{self._base}/issues/{issue_number}",
            headers=self._headers,
            json={"body": state_body(state)},
            timeout=(10, 30),
        )
        response.raise_for_status()
