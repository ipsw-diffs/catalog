from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ipsw_diff_catalog.discovery import TrackPolicy, _observed_sources, discover
from ipsw_diff_catalog.model import CatalogError, JsonObject
from tests.helpers import commit_all, git, init_repo

_APPLEDB_ORIGIN = "https://github.com/littlebyteorg/appledb.git"


@dataclass(frozen=True)
class _Release:
    version: str
    build: str
    released: str
    beta: bool = False
    rc: bool = False


_BASELINE = _Release("27.0 beta 5", "A1", "2026-08-10", beta=True)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _source(
    release: _Release,
    *,
    device: str = "Device1,1",
    sha256: str | None = None,
) -> JsonObject:
    return {
        "type": "ipsw",
        "deviceMap": [device],
        "links": [
            {
                "url": (
                    "https://updates.cdn-apple.com/fixture/"
                    f"{device}_{release.version.split(' ', maxsplit=1)[0]}_"
                    f"{release.build}_Restore.ipsw"
                ),
                "active": True,
            }
        ],
        "hashes": {"sha2-256": sha256 or release.build.lower().ljust(64, "0")},
        "size": 1000 + len(release.build),
        "prerequisiteBuild": {"Builds": None},
    }


def _appledb_record(
    release: _Release,
    *,
    appledb_platform: str = "iOS",
    device: str = "Device1,1",
    source: JsonObject | None = None,
) -> JsonObject:
    return {
        "osStr": appledb_platform,
        "version": release.version,
        "build": release.build,
        "released": release.released,
        "beta": release.beta,
        "rc": release.rc,
        "sources": [source or _source(release, device=device)],
    }


def _policy(  # noqa: PLR0913
    anchor: _Release = _BASELINE,
    *,
    identifier: str = "ios-27",
    platform: str = "iOS",
    appledb_platform: str = "iOS",
    device: str = "Device1,1",
    major: int = 27,
) -> JsonObject:
    return {
        "schema_version": 2,
        "id": identifier,
        "platform": platform,
        "appledb_platform": appledb_platform,
        "device": device,
        "major_version": major,
        "anchor": {
            "version": anchor.version,
            "build": anchor.build,
            "released": f"{anchor.released}T00:00:00Z",
            "beta": anchor.beta,
            "rc": anchor.rc,
        },
    }


def _manifest(
    previous: str = "A0",
    next_build: str = "A1",
    *,
    platform: str = "iOS",
    major: int = 27,
) -> JsonObject:
    return {
        "schema_version": 1,
        "id": f"ios-{major}.0-{previous}-{next_build}",
        "platform": platform,
        "major_version": major,
        "device": "archived-device-label",
        "from": {
            "version": f"{major}.0",
            "build": previous,
            "input": f"Archive_{major}.0_{previous}_Restore.ipsw",
        },
        "to": {
            "version": f"{major}.0",
            "build": next_build,
            "input": f"Archive_{major}.0_{next_build}_Restore.ipsw",
        },
        "payload": {
            "path": "diffs/example",
            "entrypoint": "diffs/example/README.md",
            "tracked_file_count": 1,
            "logical_bytes": 1,
            "git_tree": "0" * 40,
        },
        "source": {
            "repository": "https://github.com/example/source",
            "commit": "0" * 40,
            "path": "example",
        },
    }


@dataclass(frozen=True)
class _Fixture:
    policy: Path
    manifests: Path
    appledb: Path
    commit: str
    sources: Path


def _envelope(
    releases: tuple[_Release, ...], sources: tuple[JsonObject, ...], platform: str
) -> JsonObject:
    records: list[JsonObject] = []
    for source in sources:
        links = source["links"]
        hashes = source["hashes"]
        assert isinstance(links, list)
        assert isinstance(links[0], dict)
        assert isinstance(hashes, dict)
        url = str(links[0]["url"])
        release = next(release for release in releases if f"_{release.build}_" in url)
        records.append(
            {
                "os": platform,
                "version": release.version,
                "build": release.build,
                "release_date": release.released,
                "channel": "beta" if release.beta else "rc" if release.rc else "release",
                "artifacts": [
                    {
                        "source_type": source["type"],
                        "delivery": "full",
                        "prerequisite_builds": [],
                        "devices": source["deviceMap"],
                        "active_url": url,
                        "sha256": hashes["sha2-256"],
                        "sha1": None,
                        "size": source["size"],
                    }
                ],
            }
        )
    return {"schema_version": 1, "releases": records}


def _fixture(  # noqa: PLR0913
    tmp_path: Path,
    releases: tuple[_Release, ...] = (_BASELINE,),
    *,
    policy: JsonObject | None = None,
    appledb_platform: str = "iOS",
    device: str = "Device1,1",
    observed: tuple[JsonObject, ...] | None = None,
) -> _Fixture:
    policy_path = tmp_path / "track.json"
    _write_json(policy_path, policy or _policy())
    manifests = tmp_path / "manifests"
    _write_json(manifests / "baseline.json", _manifest())

    appledb = tmp_path / "appledb"
    init_repo(appledb)
    git(appledb, "remote", "add", "origin", _APPLEDB_ORIGIN)
    for release in releases:
        _write_json(
            appledb / f"osFiles/{appledb_platform}/1x - 27.x/{release.build}.json",
            _appledb_record(
                release,
                appledb_platform=appledb_platform,
                device=device,
            ),
        )
    commit = commit_all(appledb, "AppleDB fixture")
    sources = tmp_path / "ipsw-sources.json"
    source_rows = (
        observed
        if observed is not None
        else tuple(_source(release, device=device) for release in releases)
    )
    _write_json(sources, _envelope(releases, source_rows, appledb_platform))
    return _Fixture(policy_path, manifests, appledb, commit, sources)


def _discover(fixture: _Fixture):  # noqa: ANN202
    return discover(
        fixture.policy,
        fixture.manifests,
        fixture.appledb,
        fixture.commit,
        fixture.sources,
    )


def test_discover_reports_current_anchor_with_exact_source_identity(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    decision = _discover(fixture).to_object()

    assert decision["status"] == "current"
    assert decision["edges"] == []
    assert decision["appledb"] == {
        "repository": "https://github.com/littlebyteorg/appledb",
        "commit": fixture.commit,
    }
    anchor = decision["anchor"]
    assert isinstance(anchor, dict)
    assert anchor["build"] == "A1"
    assert anchor["beta"] is True
    assert anchor["source"] == {
        "input": "Device1,1_27.0_A1_Restore.ipsw",
        "url": "https://updates.cdn-apple.com/fixture/Device1,1_27.0_A1_Restore.ipsw",
        "sha256": "a1".ljust(64, "0"),
        "size": 1002,
    }


def test_discover_queues_every_consecutive_beta_rc_and_release(tmp_path: Path) -> None:
    beta = _Release("27.0 beta 6", "A2", "2026-08-17", beta=True)
    rc = _Release("27.0 RC", "A3", "2026-08-24", rc=True)
    final = _Release("27.0", "A4", "2026-08-31")
    fixture = _fixture(tmp_path, (_BASELINE, beta, rc, final))

    decision = _discover(fixture).to_object()

    assert decision["status"] == "candidate"
    edges = decision["edges"]
    assert isinstance(edges, list)
    assert [
        (edge["from"]["build"], edge["to"]["build"]) for edge in edges if isinstance(edge, dict)
    ] == [("A1", "A2"), ("A2", "A3"), ("A3", "A4")]
    assert edges[1]["to"]["rc"] is True
    assert edges[2]["to"]["beta"] is False
    assert edges[2]["to"]["rc"] is False
    assert decision["source_inventory"] == {"observed": 4, "selected": 4}


def test_discover_keeps_parallel_version_trains_separate(tmp_path: Path) -> None:
    anchor = _Release("27.1 beta", "A1", "2026-04-02", beta=True)
    beta_two = _Release("27.1 beta 2", "A2", "2026-04-08", beta=True)
    prior_final = _Release("27.0", "B0", "2026-03-31")
    patch = _Release("27.0.1", "B1", "2026-04-15")
    beta_three = _Release("27.1 beta 3", "A3", "2026-04-22", beta=True)
    releases = (prior_final, anchor, beta_two, patch, beta_three)
    fixture = _fixture(tmp_path, releases, policy=_policy(anchor))
    _write_json(fixture.manifests / "prior-final.json", _manifest("Z9", "B0"))

    decision = _discover(fixture).to_object()

    edges = decision["edges"]
    assert isinstance(edges, list)
    assert {
        (edge["from"]["build"], edge["to"]["build"]) for edge in edges if isinstance(edge, dict)
    } == {("A1", "A2"), ("A2", "A3"), ("B0", "B1")}
    assert ("A2", "B1") not in {
        (edge["from"]["build"], edge["to"]["build"]) for edge in edges if isinstance(edge, dict)
    }


@pytest.mark.parametrize(
    ("identifier", "platform", "appledb_platform", "device", "major"),
    [
        ("ios-12", "iOS", "iOS", "iPhone7,1", 12),
        ("ios-17", "iOS", "iPadOS", "iPad7,5", 17),
        ("macos-15", "macOS", "macOS", "Mac16,1", 15),
        ("macos-27", "macOS", "macOS", "Mac18,5", 27),
    ],
)
def test_policy_accepts_reviewed_ios_ipados_and_macos_routes(
    identifier: str,
    platform: str,
    appledb_platform: str,
    device: str,
    major: int,
) -> None:
    anchor = _Release(f"{major}.0", "A1", "2026-08-10")
    policy = TrackPolicy.from_object(
        _policy(
            anchor,
            identifier=identifier,
            platform=platform,
            appledb_platform=appledb_platform,
            device=device,
            major=major,
        )
    )

    assert policy.identifier == identifier
    assert policy.appledb_platform == appledb_platform


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 1, "schema_version must be 2"),
        ("id", "ios-26", "id must match"),
        ("appledb_platform", "watchOS", "platform route is unsupported"),
    ],
)
def test_policy_rejects_unreviewed_routes(field: str, value: object, message: str) -> None:
    policy = _policy()
    policy[field] = value

    with pytest.raises(CatalogError, match=message):
        TrackPolicy.from_object(policy)


def test_discover_rejects_missing_ipsw_observation(tmp_path: Path) -> None:
    next_release = _Release("27.0 beta 6", "A2", "2026-08-17", beta=True)
    fixture = _fixture(
        tmp_path,
        (_BASELINE, next_release),
        observed=(_source(_BASELINE),),
    )

    with pytest.raises(CatalogError, match="source coverage for A2 differs"):
        _discover(fixture)


def test_discover_rejects_ipsw_hash_mutation(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        observed=(_source(_BASELINE, sha256="f" * 64),),
    )

    with pytest.raises(CatalogError, match="source facts differ for build A1"):
        _discover(fixture)


def test_discover_rejects_ambiguous_same_day_builds(tmp_path: Path) -> None:
    first = _Release("27.0 beta 6", "A2", "2026-08-17", beta=True)
    second = _Release("27.0 beta 6", "A3", "2026-08-17", beta=True)
    fixture = _fixture(tmp_path, (_BASELINE, first, second))

    with pytest.raises(CatalogError, match="release order is ambiguous"):
        _discover(fixture)


def test_discover_recognizes_pending_build_already_in_manifests(tmp_path: Path) -> None:
    next_release = _Release("27.0 beta 6", "A2", "2026-08-17", beta=True)
    fixture = _fixture(tmp_path, (_BASELINE, next_release))
    _write_json(fixture.manifests / "already-known.json", _manifest("A1", "A2"))

    decision = _discover(fixture)

    assert decision.status == "current"


def test_discover_rejects_a_merged_train_that_skipped_an_intermediate(tmp_path: Path) -> None:
    middle = _Release("27.0 beta 6", "A2", "2026-08-17", beta=True)
    later = _Release("27.0 RC", "A3", "2026-08-24", rc=True)
    fixture = _fixture(tmp_path, (_BASELINE, middle, later))
    _write_json(fixture.manifests / "skipped.json", _manifest("A1", "A3"))

    with pytest.raises(CatalogError, match="merged build A3 is missing expected edge A2 -> A3"):
        _discover(fixture)


def test_discover_rejects_wrong_predecessor_for_a_merged_build(tmp_path: Path) -> None:
    next_release = _Release("27.0 beta 6", "A2", "2026-08-17", beta=True)
    fixture = _fixture(tmp_path, (_BASELINE, next_release))
    _write_json(fixture.manifests / "wrong-edge.json", _manifest("A0", "A2"))

    with pytest.raises(CatalogError, match="merged build A2 is missing expected edge A1 -> A2"):
        _discover(fixture)


def test_discover_rejects_anchor_without_manifest_endpoint(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _write_json(fixture.manifests / "baseline.json", _manifest("A0", "OTHER"))

    with pytest.raises(CatalogError, match="anchor is not backed"):
        _discover(fixture)


def test_discover_keeps_immutable_anchor_after_outgoing_manifest(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _write_json(fixture.manifests / "outgoing.json", _manifest("A1", "A2"))

    decision = _discover(fixture)

    assert decision.status == "current"


def test_discover_ignores_historical_source_rows_but_records_cardinality(tmp_path: Path) -> None:
    historical = _Release("27.0 beta 4", "A0", "2026-08-03", beta=True)
    fixture = _fixture(tmp_path, (historical, _BASELINE))

    decision = _discover(fixture).to_object()

    assert decision["status"] == "current"
    assert decision["source_inventory"] == {"observed": 2, "selected": 1}


def test_discover_ignores_release_without_reviewed_device_source(tmp_path: Path) -> None:
    other = _Release("27.0 beta 6", "A2", "2026-08-17", beta=True)
    fixture = _fixture(
        tmp_path,
        (_BASELINE, other),
        observed=(_source(_BASELINE), _source(other, device="Other1,1")),
    )
    other_path = fixture.appledb / "osFiles/iOS/1x - 27.x/A2.json"
    _write_json(other_path, _appledb_record(other, device="Other1,1"))
    commit = commit_all(fixture.appledb, "limit A2 to another device")
    fixture = _Fixture(fixture.policy, fixture.manifests, fixture.appledb, commit, fixture.sources)

    decision = _discover(fixture).to_object()

    assert decision["status"] == "current"


def test_discover_ignores_non_downloadable_appledb_stub(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    stub = {
        "osStr": "iOS",
        "version": "27.0",
        "build": "STUB1",
    }
    _write_json(fixture.appledb / "osFiles/iOS/1x - 27.x/STUB1.json", stub)
    commit = commit_all(fixture.appledb, "add non-downloadable stub")
    fixture = _Fixture(fixture.policy, fixture.manifests, fixture.appledb, commit, fixture.sources)

    decision = _discover(fixture)

    assert decision.status == "current"


def test_discover_rejects_compatible_source_without_release_date(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    incomplete = {
        "osStr": "iOS",
        "version": "27.0 beta 6",
        "build": "A2",
        "beta": True,
        "sources": [_source(_Release("27.0 beta 6", "A2", "2026-08-17", beta=True))],
    }
    _write_json(fixture.appledb / "osFiles/iOS/1x - 27.x/A2.json", incomplete)
    commit = commit_all(fixture.appledb, "add incomplete downloadable record")
    fixture = _Fixture(fixture.policy, fixture.manifests, fixture.appledb, commit, fixture.sources)

    with pytest.raises(CatalogError, match="compatible IPSW source but no release date"):
        _discover(fixture)


def test_discover_rejects_mutated_anchor_metadata(tmp_path: Path) -> None:
    changed = _Release("27.0 RC", "A1", "2026-08-10", rc=True)
    fixture = _fixture(tmp_path, (changed,), observed=(_source(changed),))

    with pytest.raises(CatalogError, match="anchor metadata differs"):
        _discover(fixture)


def test_discover_rejects_substituted_appledb_origin(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    git(fixture.appledb, "remote", "set-url", "origin", "https://github.com/example/not-appledb")

    with pytest.raises(CatalogError, match="AppleDB origin differs"):
        _discover(fixture)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1, "releases": []},
        {"schema_version": 1, "releases": {}},
    ],
)
def test_discover_rejects_invalid_inventory_envelope(tmp_path: Path, value: object) -> None:
    fixture = _fixture(tmp_path)
    _write_json(fixture.sources, value)
    with pytest.raises(CatalogError):
        _discover(fixture)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("release", "os", "macOS"),
        ("release", "build", "OTHER"),
        ("release", "version", "27.0 beta 4"),
        ("release", "release_date", "2026-08-11"),
        ("release", "release_date", None),
        ("release", "channel", "release"),
        ("release", "channel", []),
        ("release", "artifacts", []),
        ("artifact", "source_type", "ota"),
        ("artifact", "delivery", "delta"),
        ("artifact", "prerequisite_builds", ["A0"]),
        ("artifact", "devices", ["Other1,1"]),
        ("artifact", "active_url", None),
        ("artifact", "active_url", "https://example.com/test.ipsw"),
        ("artifact", "sha256", None),
        ("artifact", "sha256", "f" * 64),
        ("artifact", "size", None),
        ("artifact", "size", 0),
        ("artifact", "size", True),
        ("artifact", "size", 42),
    ],
)
def test_discover_rejects_selected_envelope_mutation(
    tmp_path: Path, section: str, field: str, value: object
) -> None:
    fixture = _fixture(tmp_path)
    envelope = json.loads(fixture.sources.read_text())
    release = envelope["releases"][0]
    target = release if section == "release" else release["artifacts"][0]
    target[field] = value
    _write_json(fixture.sources, envelope)
    with pytest.raises(CatalogError):
        _discover(fixture)


def test_discover_rejects_duplicate_artifact(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    envelope = json.loads(fixture.sources.read_text())
    artifacts = envelope["releases"][0]["artifacts"]
    artifacts.append(artifacts[0])
    _write_json(fixture.sources, envelope)
    with pytest.raises(CatalogError, match="found 2"):
        _discover(fixture)


def test_discover_allows_nullable_unselected_history(tmp_path: Path) -> None:
    historical = _Release("27.0 beta 4", "A0", "2026-08-03", beta=True)
    fixture = _fixture(tmp_path, (historical, _BASELINE))
    envelope = json.loads(fixture.sources.read_text())
    artifact = envelope["releases"][0]["artifacts"][0]
    for key in ("active_url", "sha256", "sha1", "size"):
        artifact[key] = None
    _write_json(fixture.sources, envelope)
    assert _discover(fixture).status == "current"


def test_parse_captured_v3721_macos_inventory() -> None:
    # Unmodified CLI output; AppleDB 5e2fdf77bdf8b44bb6d1696cde033c793ebf06d0.
    path = Path(__file__).parent / "fixtures/appledb-v3.1.721-macos27.json"
    sources = _observed_sources(path, "macOS")
    expected_count = 2
    assert len(sources) == expected_count
    metadata, source = sources[1].select("Mac18,5")
    assert metadata.to_object() == {
        "version": "27.0",
        "build": "26A428",
        "released": "2026-09-14T00:00:00Z",
        "beta": False,
        "rc": False,
    }
    assert source.input == "UniversalMac_27.0_26A428_Restore.ipsw"
    assert source.sha256 == "2a5d3c695d501022b7fad9adaffcf2627bcb867d993fb5662dcd41bac99a2836"
    release_size = 26626436228
    assert source.size == release_size
    beta, beta_source = sources[0].select("Mac18,5")
    assert beta.beta is True
    assert beta.build == "26B5086k"
    beta_size = 26538648422
    assert beta_source.size == beta_size
