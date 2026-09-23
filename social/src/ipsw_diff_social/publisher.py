from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import requests

from ipsw_diff_social.catalog import (
    fetch_catalog,
    fetch_catalog_commit,
    fetch_release_names,
    fetch_text,
    parse_diff_facts,
)
from ipsw_diff_social.models import CatalogEntry, DataError, DiffFact, ReleaseNames
from ipsw_diff_social.render import card_alt_text, release_title, render_card
from ipsw_diff_social.state import GitHubIssueStore, PublisherState, StateIssue
from ipsw_diff_social.xpost import NETWORKS, Post, PublishError, XPost

MODES = ("check", "publish", "bootstrap", "dry-run")
MAX_POSTS_PER_RUN = 3
# xpost counts every character of the message, the blank line and the link against X's
# limit; Bluesky and Mastodon allow more.
MAX_POST_LENGTH = 280
# Bluesky rejects image blobs over 1,000,000 bytes.
MAX_CARD_BYTES = 950_000


@dataclass(frozen=True)
class RunConfig:
    mode: str
    base_image: Path
    output_dir: Path
    xpost: XPost | None = None
    networks: tuple[str, ...] = NETWORKS
    entry_id: str | None = None


def _fits(message: str, link: str) -> bool:
    return len(f"{message}\n\n{link}") <= MAX_POST_LENGTH


def compose_message(entry: CatalogEntry, names: ReleaseNames, facts: list[DiffFact]) -> str:
    """The richest message that fits beside the entry's link, as xpost measures it for X."""
    link = entry.destination.page_url
    header = unicodedata.normalize("NFC", f"New ipsw-diff: {release_title(entry, names)}")
    lines = [unicodedata.normalize("NFC", f"• {fact.summary}") for fact in facts]
    while lines:
        message = f"{header}\n\n" + "\n".join(lines)
        if _fits(message, link):
            return message
        lines.pop()
    available = max(0, MAX_POST_LENGTH - len(f"\n\n{link}"))
    return header[:available].rstrip()


def _prepare(
    session: requests.Session, config: RunConfig, entry: CatalogEntry, names: ReleaseNames
) -> Post:
    facts = parse_diff_facts(fetch_text(session, entry.destination.raw_url))
    card = config.output_dir / f"{entry.entry_id}.jpg"
    render_card(config.base_image, card, entry, names, facts)
    size = card.stat().st_size
    if size > MAX_CARD_BYTES:
        raise DataError(f"card is {size:,} bytes; Bluesky accepts at most {MAX_CARD_BYTES:,}")
    return Post(
        message=compose_message(entry, names, facts),
        link=entry.destination.page_url,
        image=card,
        alt_text=card_alt_text(entry, names, facts),
    )


def _describe(error: Exception) -> str:
    detail = " ".join(str(error).split()) or "no error detail"
    return f"{type(error).__name__}: {detail[:500]}"


def _state_store() -> GitHubIssueStore:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    return GitHubIssueStore(repository=repository, token=token)


def _require_xpost(config: RunConfig) -> XPost:
    if config.xpost is None:
        raise ValueError(f"{config.mode} mode needs --xpost")
    return config.xpost


def _dry_run(session: requests.Session, config: RunConfig, commit: str) -> int:
    xpost = _require_xpost(config)
    if config.entry_id is None:
        raise ValueError("dry-run requires --entry-id because catalog order is not chronological")
    entry = next(
        (item for item in fetch_catalog(session, commit) if item.entry_id == config.entry_id),
        None,
    )
    if entry is None:
        raise ValueError(f"catalog entry not found: {config.entry_id}")
    post = _prepare(session, config, entry, fetch_release_names(session))
    for network in config.networks:
        xpost.validate(network, post)
    print(post.text)
    print(f"Alt text: {post.alt_text}")
    print(f"Rendered preview: {post.image}")
    return 0


def _has_work(store: GitHubIssueStore, commit: str) -> bool:
    issue = store.find()
    return issue is None or issue.state.catalog_commit != commit or bool(issue.state.queue)


def _sync_queue(
    session: requests.Session, store: GitHubIssueStore, issue: StateIssue, commit: str
) -> None:
    """Queue additions only after rejecting removed or changed canonical rows."""
    state = issue.state
    if state.catalog_commit == commit:
        return
    previous = {
        entry.entry_id: entry.canonical_row
        for entry in fetch_catalog(session, state.catalog_commit)
    }
    current = {entry.entry_id: entry.canonical_row for entry in fetch_catalog(session, commit)}
    removed = previous.keys() - current.keys()
    if removed:
        raise DataError(f"catalog removed immutable entry IDs: {sorted(removed)}")
    changed = {entry_id for entry_id in previous if previous[entry_id] != current[entry_id]}
    if changed:
        raise DataError(f"catalog changed immutable entry IDs: {sorted(changed)}")
    state.enqueue(current.keys() - previous.keys())
    state.catalog_commit = commit
    store.save(issue.number, state)


@dataclass(frozen=True)
class _Publication:
    session: requests.Session
    config: RunConfig
    xpost: XPost
    store: GitHubIssueStore
    issue: StateIssue
    names: ReleaseNames

    def _save(self) -> None:
        self.store.save(self.issue.number, self.issue.state)

    def publish(self, entry: CatalogEntry) -> bool:
        """Post to each network that hasn't posted the entry. False if any network failed."""
        state = self.issue.state
        networks = state.unposted(entry.entry_id, self.config.networks)
        if not networks:
            state.finish(entry.entry_id)
            self._save()
            return True
        # Persisted before anything else, so a crash leaves a marker instead of a lost entry.
        state.mark_pending(entry.entry_id, networks)
        self._save()
        try:
            post = _prepare(self.session, self.config, entry, self.names)
            for network in networks:
                self.xpost.validate(network, post)
        except Exception as error:
            print(f"Preparing {entry.entry_id} failed ({_describe(error)}); left pending.")
            return False

        confirmed = True
        for network in networks:
            try:
                self.xpost.publish(network, post)
            except PublishError as error:
                print(f"{entry.entry_id} not confirmed ({error}); left pending for review.")
                confirmed = False
                continue
            state.mark_posted(entry.entry_id, network)
            self._save()
            print(f"Published {entry.entry_id} to {network}.")
        return confirmed


def _publish(session: requests.Session, config: RunConfig, commit: str) -> int:
    store = _state_store()
    issue = store.find()
    if issue is None or config.mode == "bootstrap":
        # Validate even when suppressing existing entries; never checkpoint unsupported data.
        fetch_catalog(session, commit)
    if issue is None:
        created = store.create(PublisherState(catalog_commit=commit))
        print(f"Bootstrapped catalog revision {commit} in state issue #{created.number}.")
        return 0
    if config.mode == "bootstrap":
        issue.state.catalog_commit = commit
        issue.state.queue.clear()
        issue.state.pending.clear()
        store.save(issue.number, issue.state)
        print(f"Bootstrapped catalog revision {commit} in state issue #{issue.number}.")
        return 0

    _sync_queue(session, store, issue, commit)
    state = issue.state
    if state.pending:
        print(f"{len(state.pending)} entries are pending review in state issue #{issue.number}.")
    if not state.queue:
        print("No new catalog entries to publish.")
        return 0

    entries = {entry.entry_id: entry for entry in fetch_catalog(session, commit)}
    missing = state.queue - entries.keys()
    if missing:
        raise DataError(f"publisher queue references missing catalog entries: {sorted(missing)}")
    publication = _Publication(
        session=session,
        config=config,
        xpost=_require_xpost(config),
        store=store,
        issue=issue,
        names=fetch_release_names(session),
    )
    results = [
        publication.publish(entries[entry_id])
        for entry_id in sorted(state.queue)[:MAX_POSTS_PER_RUN]
    ]
    return 0 if all(results) else 1


def run(config: RunConfig) -> int:
    if config.mode not in MODES:
        raise ValueError(f"unsupported mode: {config.mode}")
    if not config.base_image.is_file():
        raise FileNotFoundError(f"base image does not exist: {config.base_image}")
    if config.mode in {"publish", "dry-run"}:
        _require_xpost(config)

    session = requests.Session()
    commit = fetch_catalog_commit(session)
    if config.mode == "check":
        print(f"has_work={str(_has_work(_state_store(), commit)).lower()}")
        return 0
    config.output_dir.mkdir(parents=True, exist_ok=True)
    if config.mode == "dry-run":
        return _dry_run(session, config, commit)
    return _publish(session, config, commit)
