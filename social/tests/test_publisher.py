from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import entry_document, sample_entry
from pytest import MonkeyPatch

from ipsw_diff_social import catalog, publisher
from ipsw_diff_social.models import CatalogEntry, DataError, DiffFact, ReleaseNames
from ipsw_diff_social.publisher import MAX_POST_LENGTH, RunConfig, compose_message
from ipsw_diff_social.state import PublisherState, StateIssue
from ipsw_diff_social.xpost import NETWORKS, Post, PublishError, XPost

OLD_COMMIT = "1" * 40
CURRENT_COMMIT = "2" * 40


@dataclass(frozen=True)
class FakeXPost(XPost):
    executable: Path = Path("xpost")
    failing: frozenset[str] = frozenset()
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def validate(self, network: str, post: Post) -> None:
        self.calls.append(("validate", network, post.link))

    def publish(self, network: str, post: Post) -> None:
        self.calls.append(("publish", network, post.link))
        if network in self.failing:
            raise PublishError(f"xpost exited 1 on {network}")

    def published(self) -> list[str]:
        return [network for action, network, _ in self.calls if action == "publish"]


class FakeStore:
    def __init__(self, issue: StateIssue | None = None) -> None:
        self.issue = issue
        self.created: PublisherState | None = None
        self.saved: list[PublisherState] = []

    def find(self) -> StateIssue | None:
        return self.issue

    def create(self, state: PublisherState) -> StateIssue:
        self.created = deepcopy(state)
        return StateIssue(number=7, state=state)

    def save(self, _number: int, state: PublisherState) -> None:
        self.saved.append(deepcopy(state))


def _entry(entry_id: str) -> CatalogEntry:
    value = entry_document()
    value["id"] = entry_id
    return CatalogEntry.from_value(value)


def _config(
    tmp_path: Path, xpost: XPost | None, mode: str = "publish", entry_id: str | None = None
) -> RunConfig:
    base = tmp_path / "base.png"
    base.write_bytes(b"present")
    return RunConfig(
        mode=mode,
        base_image=base,
        output_dir=tmp_path / "output",
        xpost=xpost,
        entry_id=entry_id,
    )


def _post(entry: CatalogEntry, tmp_path: Path) -> Post:
    return Post(
        message=f"New ipsw-diff: {entry.entry_id}",
        link=entry.destination.page_url,
        image=tmp_path / "card.jpg",
        alt_text="card",
    )


def _catalog(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    store: FakeStore,
    entries: dict[str, list[CatalogEntry]],
) -> None:
    """Serve catalog revisions, release names and posts without any network."""
    monkeypatch.setattr(publisher, "fetch_catalog_commit", lambda _session: CURRENT_COMMIT)
    monkeypatch.setattr(publisher, "fetch_catalog", lambda _session, commit: entries[commit])
    monkeypatch.setattr(publisher, "fetch_release_names", lambda _session: ReleaseNames(names={}))
    monkeypatch.setattr(publisher, "_state_store", lambda: store)
    monkeypatch.setattr(
        publisher, "_prepare", lambda _session, _config, entry, _names: _post(entry, tmp_path)
    )


def test_compose_message_carries_title_and_facts_and_fits_beside_the_link() -> None:
    names = ReleaseNames(
        names={
            ("iOS", "24A5418b"): "27.0 beta 6",
            ("iOS", "24A5424a"): "27.0 beta 7",
        }
    )
    entry = sample_entry()

    message = compose_message(entry, names, [DiffFact(area="Mach-O", change="updated", count=97)])

    assert message.startswith("New ipsw-diff: iOS 27.0 beta 6 → iOS 27.0 beta 7")
    assert "• Mach-O: 97 items updated" in message
    assert entry.destination.page_url not in message
    assert len(f"{message}\n\n{entry.destination.page_url}") <= MAX_POST_LENGTH


def test_compose_message_counts_the_full_link_and_drops_trailing_facts() -> None:
    value = entry_document()
    destination = value["destination"]
    assert isinstance(destination, dict)
    destination["entrypoint"] = f"diffs/{'long-path/' * 8}README.md"
    entry = CatalogEntry.from_value(value)
    facts = [
        DiffFact(area="Mach-O", change="updated", count=97),
        DiffFact(area="filesystem", change="added", count=26),
        DiffFact(area="DSC", change="removed", count=3),
    ]

    message = compose_message(entry, ReleaseNames(names={}), facts)

    assert facts[0].summary in message
    assert facts[-1].summary not in message
    assert len(f"{message}\n\n{entry.destination.page_url}") <= MAX_POST_LENGTH


def test_compose_message_truncates_an_oversized_header() -> None:
    value = entry_document()
    value["from"] = {"version": "a" * 200, "build": "24A5418b"}
    value["to"] = {"version": "b" * 200, "build": "24A5424a"}
    entry = CatalogEntry.from_value(value)

    message = compose_message(entry, ReleaseNames(names={}), [])

    assert message.startswith("New ipsw-diff: iOS aaa")
    assert len(f"{message}\n\n{entry.destination.page_url}") == MAX_POST_LENGTH


def test_first_run_validates_the_catalog_and_bootstraps_without_posting(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    store = FakeStore()
    xpost = FakeXPost()
    _catalog(monkeypatch, tmp_path, store, {CURRENT_COMMIT: [sample_entry()]})

    assert publisher.run(_config(tmp_path, xpost)) == 0
    assert store.created == PublisherState(catalog_commit=CURRENT_COMMIT)
    assert xpost.calls == []


def test_publish_checkpoints_the_queue_then_posts_to_every_network_in_order(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    old, new = _entry("old"), _entry("new")
    store = FakeStore(StateIssue(number=7, state=PublisherState(catalog_commit=OLD_COMMIT)))
    xpost = FakeXPost()
    _catalog(monkeypatch, tmp_path, store, {OLD_COMMIT: [old], CURRENT_COMMIT: [old, new]})

    assert publisher.run(_config(tmp_path, xpost)) == 0

    assert store.saved[0].catalog_commit == CURRENT_COMMIT
    assert store.saved[0].queue == {"new"}
    assert set(store.saved[1].pending["new"]) == set(NETWORKS)
    assert [action for action, _, _ in xpost.calls[:3]] == ["validate"] * 3
    assert xpost.published() == list(NETWORKS)
    assert set(store.saved[-1].posts["new"]) == set(NETWORKS)
    assert store.saved[-1].pending == {}


def test_one_network_failing_leaves_only_it_pending_and_fails_the_run(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    entry = sample_entry()
    state = PublisherState(catalog_commit=CURRENT_COMMIT, queue={entry.entry_id})
    store = FakeStore(StateIssue(number=7, state=state))
    xpost = FakeXPost(failing=frozenset({"mastodon"}))
    _catalog(monkeypatch, tmp_path, store, {CURRENT_COMMIT: [entry]})

    assert publisher.run(_config(tmp_path, xpost)) == 1

    assert xpost.published() == list(NETWORKS)
    assert set(state.pending[entry.entry_id]) == {"mastodon"}
    assert set(state.posts[entry.entry_id]) == {"bluesky", "twitter"}
    assert state.queue == set()


def test_requeued_entry_posts_only_to_networks_that_have_not_posted_it(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    entry = sample_entry()
    state = PublisherState(
        catalog_commit=CURRENT_COMMIT,
        queue={entry.entry_id},
        posts={entry.entry_id: {"bluesky": "t", "twitter": "t"}},
    )
    store = FakeStore(StateIssue(number=7, state=state))
    xpost = FakeXPost()
    _catalog(monkeypatch, tmp_path, store, {CURRENT_COMMIT: [entry]})

    assert publisher.run(_config(tmp_path, xpost)) == 0

    assert xpost.published() == ["mastodon"]
    assert set(state.posts[entry.entry_id]) == set(NETWORKS)


def test_preparation_failure_stays_pending_without_blocking_later_entries(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    first, second = _entry("a-first"), _entry("b-second")
    state = PublisherState(catalog_commit=CURRENT_COMMIT, queue={first.entry_id, second.entry_id})
    store = FakeStore(StateIssue(number=7, state=state))
    xpost = FakeXPost()
    _catalog(monkeypatch, tmp_path, store, {CURRENT_COMMIT: [first, second]})

    def prepare(_session: object, _config: RunConfig, entry: CatalogEntry, _names: object) -> Post:
        if entry.entry_id == first.entry_id:
            raise OSError("missing immutable README")
        return _post(entry, tmp_path)

    monkeypatch.setattr(publisher, "_prepare", prepare)

    assert publisher.run(_config(tmp_path, xpost)) == 1

    assert set(state.pending[first.entry_id]) == set(NETWORKS)
    assert set(state.posts[second.entry_id]) == set(NETWORKS)
    assert {link for _, _, link in xpost.calls} == {second.destination.page_url}


def test_removed_catalog_entry_fails_closed_before_posting(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    kept, removed = _entry("kept"), _entry("removed")
    store = FakeStore(StateIssue(number=7, state=PublisherState(catalog_commit=OLD_COMMIT)))
    xpost = FakeXPost()
    _catalog(monkeypatch, tmp_path, store, {OLD_COMMIT: [kept, removed], CURRENT_COMMIT: [kept]})

    with pytest.raises(DataError, match="removed immutable entry IDs"):
        publisher.run(_config(tmp_path, xpost))
    assert xpost.calls == []
    assert store.saved == []


def test_bootstrap_clears_the_queue_and_pending_without_posting(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state = PublisherState(
        catalog_commit=OLD_COMMIT, queue={"queued"}, pending={"stuck": {"twitter": "t"}}
    )
    store = FakeStore(StateIssue(number=7, state=state))
    _catalog(monkeypatch, tmp_path, store, {CURRENT_COMMIT: [sample_entry()]})

    assert publisher.run(_config(tmp_path, None, mode="bootstrap")) == 0
    assert store.saved[-1] == PublisherState(catalog_commit=CURRENT_COMMIT)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("destination", {"commit": "b" * 40}),
        ("destination", {"entrypoint": "diffs/changed/README.md"}),
        ("destination", {"repository": "https://github.com/ipsw-diffs/ios-27"}),
        ("from", {"version": "27.0 beta 6"}),
        ("device", "iPhone18,2"),
        ("source", {"commit": "c" * 40}),
        ("integrity", {"tracked_file_count": 2}),
    ],
)
def test_existing_row_changes_fail_before_enqueue_or_checkpoint(
    field_name: str, replacement: object, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    document = entry_document()
    document["source"] = {"commit": "a" * 40}
    document["integrity"] = {"tracked_file_count": 1}
    old = CatalogEntry.from_value(document)
    changed = deepcopy(document)
    nested = changed.get(field_name)
    if isinstance(nested, dict) and isinstance(replacement, dict):
        nested.update(replacement)
    else:
        changed[field_name] = replacement
    state = PublisherState(catalog_commit=OLD_COMMIT, queue={old.entry_id})
    before = deepcopy(state)
    store = FakeStore(StateIssue(number=7, state=state))
    xpost = FakeXPost()
    _catalog(
        monkeypatch,
        tmp_path,
        store,
        {
            OLD_COMMIT: [old],
            CURRENT_COMMIT: [CatalogEntry.from_value(changed), _entry("new")],
        },
    )

    with pytest.raises(DataError, match="changed immutable entry IDs"):
        publisher.run(_config(tmp_path, xpost))

    assert state == before
    assert store.saved == []
    assert xpost.calls == []


def test_reordering_rows_and_object_keys_does_not_create_changes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    document = entry_document()
    destination = document["destination"]
    assert isinstance(destination, dict)
    reordered = dict(reversed(list(document.items())))
    reordered["destination"] = dict(reversed(list(destination.items())))
    other = _entry("other")
    state = PublisherState(catalog_commit=OLD_COMMIT)
    store = FakeStore(StateIssue(number=7, state=state))
    xpost = FakeXPost()
    _catalog(
        monkeypatch,
        tmp_path,
        store,
        {
            OLD_COMMIT: [CatalogEntry.from_value(document), other],
            CURRENT_COMMIT: [other, CatalogEntry.from_value(reordered)],
        },
    )

    assert publisher.run(_config(tmp_path, xpost)) == 0
    assert state.catalog_commit == CURRENT_COMMIT
    assert state.queue == set()
    assert xpost.calls == []


@pytest.mark.parametrize("phase", ["first-publish", "first-bootstrap", "bootstrap", "sync"])
@pytest.mark.parametrize("scope", ["catalog", "entry"])
def test_unsupported_schema_never_consumes_the_catalog(
    phase: str, scope: str, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    row = entry_document()
    document: dict[str, object] = {"schema_version": 1, "entries": [row]}
    (document if scope == "catalog" else row)["schema_version"] = 2
    state = PublisherState(
        catalog_commit=OLD_COMMIT, queue={"queued"}, pending={"stuck": {"twitter": "t"}}
    )
    before = deepcopy(state)
    store = FakeStore(None if phase.startswith("first-") else StateIssue(number=7, state=state))
    xpost = FakeXPost()
    monkeypatch.setattr(publisher, "fetch_catalog_commit", lambda _session: CURRENT_COMMIT)
    monkeypatch.setattr(publisher, "_state_store", lambda: store)
    monkeypatch.setattr(
        catalog,
        "fetch_json",
        lambda _session, url: (
            document if CURRENT_COMMIT in url else {"schema_version": 1, "entries": []}
        ),
    )
    mode = "bootstrap" if "bootstrap" in phase else "publish"

    with pytest.raises(DataError, match=rf"{scope}\.schema_version"):
        publisher.run(_config(tmp_path, xpost, mode=mode))

    assert state == before
    assert store.created is None
    assert store.saved == []
    assert xpost.calls == []


def test_publish_without_xpost_fails_before_touching_state(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(
        publisher, "fetch_catalog_commit", lambda _session: pytest.fail("must fail first")
    )

    with pytest.raises(ValueError, match="needs --xpost"):
        publisher.run(_config(tmp_path, None))


def test_dry_run_validates_every_network_and_posts_nothing(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entry = sample_entry()
    xpost = FakeXPost()
    _catalog(monkeypatch, tmp_path, FakeStore(), {CURRENT_COMMIT: [entry]})

    config = _config(tmp_path, xpost, mode="dry-run", entry_id=entry.entry_id)
    assert publisher.run(config) == 0

    assert [action for action, _, _ in xpost.calls] == ["validate"] * len(NETWORKS)
    assert entry.destination.page_url in capsys.readouterr().out


def test_dry_run_requires_an_explicit_id(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(publisher, "fetch_catalog_commit", lambda _session: CURRENT_COMMIT)

    with pytest.raises(ValueError, match="requires --entry-id"):
        publisher.run(_config(tmp_path, FakeXPost(), mode="dry-run"))


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        (None, "has_work=true"),
        (StateIssue(7, PublisherState(catalog_commit=OLD_COMMIT)), "has_work=true"),
        (
            StateIssue(7, PublisherState(catalog_commit=CURRENT_COMMIT, queue={"e"})),
            "has_work=true",
        ),
        (StateIssue(7, PublisherState(catalog_commit=CURRENT_COMMIT)), "has_work=false"),
    ],
)
def test_check_reports_work_without_writing_state(
    issue: StateIssue | None,
    expected: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore(issue)
    monkeypatch.setattr(publisher, "fetch_catalog_commit", lambda _session: CURRENT_COMMIT)
    monkeypatch.setattr(publisher, "_state_store", lambda: store)

    assert publisher.run(_config(tmp_path, None, mode="check")) == 0
    assert capsys.readouterr().out.strip() == expected
    assert store.saved == []
    assert store.created is None
