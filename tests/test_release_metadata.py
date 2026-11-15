from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import pytest

from ipsw_diff_catalog import cli
from ipsw_diff_catalog.model import CatalogError, JsonObject
from ipsw_diff_catalog.release_metadata import (
    AppleDBCoverageMissingError,
    PallasLabelSources,
    import_release_metadata,
)
from ipsw_diff_catalog.release_registry import load_release_labels
from ipsw_diff_catalog.render import load_entries
from tests.helpers import commit_all, git, init_repo

if TYPE_CHECKING:
    from pathlib import Path


_APPLEDB_ORIGIN = "https://github.com/littlebyteorg/appledb.git"
_EXPECTED_RELEASES = 2


def _entry(platform: str = "iOS", device: str = "Device1,1") -> JsonObject:
    repository_name = platform.lower()
    return {
        "schema_version": 1,
        "id": f"{repository_name}-27.0-A1-A2",
        "platform": platform,
        "major_version": 27,
        "device": device,
        "from": {
            "version": "27.0",
            "build": "A1",
            "input": f"{device}_27.0_A1_Restore.ipsw",
        },
        "to": {
            "version": "27.0",
            "build": "A2",
            "input": f"{device}_27.0_A2_Restore.ipsw",
        },
        "source": {
            "repository": "https://github.com/example/source",
            "commit": "a" * 40,
            "path": "example",
        },
        "destination": {
            "repository": f"https://github.com/ipsw-diff/{repository_name}-27",
            "commit": "b" * 40,
            "payload_path": "diffs/example",
            "entrypoint": "diffs/example/README.md",
            "manifest_path": "manifests/example.json",
        },
        "integrity": {
            "git_tree": "c" * 40,
            "tracked_file_count": 1,
            "logical_bytes": 1,
        },
    }


def _appledb_record(
    build: str,
    version: str,
    *,
    platform: str = "iOS",
    beta: bool = False,
    rc: bool = False,
) -> JsonObject:
    return {
        "osStr": platform,
        "version": version,
        "build": build,
        "released": "2026-08-10",
        "beta": beta,
        "rc": rc,
        "ignored_upstream_field": {"allowed": True},
    }


def _write_json(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    entries = tmp_path / "entries"
    entries.mkdir()
    _write_json(entries / "ios-27.0-A1-A2.json", _entry())

    appledb = tmp_path / "appledb"
    init_repo(appledb)
    git(appledb, "remote", "add", "origin", _APPLEDB_ORIGIN)
    _write_json(
        appledb / "osFiles/iOS/1x - 27.x/A1.json", _appledb_record("A1", "27.0 beta", beta=True)
    )
    _write_json(
        appledb / "osFiles/iOS/1x - 27.x/A2.json", _appledb_record("A2", "27.0 beta 2", beta=True)
    )
    return entries, appledb, commit_all(appledb, "AppleDB fixture")


def test_import_writes_pinned_curated_registry_and_checks_it(tmp_path: Path) -> None:
    entries, appledb, commit = _fixture(tmp_path)
    output = tmp_path / "metadata/releases.json"

    result = import_release_metadata(entries, appledb, commit, output, check=False)
    first = output.read_bytes()
    checked = import_release_metadata(entries, appledb, commit, output, check=True)

    assert result.release_count == _EXPECTED_RELEASES
    assert result.beta_count == _EXPECTED_RELEASES
    assert result.rc_count == 0
    assert checked.checked
    assert output.read_bytes() == first
    assert json.loads(first) == {
        "schema_version": 1,
        "source": {
            "repository": "https://github.com/littlebyteorg/appledb",
            "commit": commit,
        },
        "releases": [
            {
                "platform": "iOS",
                "build": "A1",
                "display_version": "27.0 beta",
                "channel": "beta",
                "beta": True,
                "rc": False,
                "released": "2026-08-10",
                "source_path": "osFiles/iOS/1x - 27.x/A1.json",
            },
            {
                "platform": "iOS",
                "build": "A2",
                "display_version": "27.0 beta 2",
                "channel": "beta",
                "beta": True,
                "rc": False,
                "released": "2026-08-10",
                "source_path": "osFiles/iOS/1x - 27.x/A2.json",
            },
        ],
    }


def test_import_reads_the_pinned_commit_not_the_worktree(tmp_path: Path) -> None:
    entries, appledb, commit = _fixture(tmp_path)
    record = appledb / "osFiles/iOS/1x - 27.x/A2.json"
    _write_json(record, _appledb_record("A2", "27.0 beta 99", beta=True))

    output = tmp_path / "releases.json"
    import_release_metadata(entries, appledb, commit, output, check=False)

    versions = [
        release["display_version"] for release in json.loads(output.read_bytes())["releases"]
    ]
    assert versions == ["27.0 beta", "27.0 beta 2"]


def test_import_selects_ipados_records_for_ipad_catalog_entries(tmp_path: Path) -> None:
    entries = tmp_path / "entries"
    entries.mkdir()
    _write_json(entries / "ios-27.0-A1-A2.json", _entry(device="iPad"))

    appledb = tmp_path / "appledb"
    init_repo(appledb)
    git(appledb, "remote", "add", "origin", _APPLEDB_ORIGIN)
    for build in ("A1", "A2"):
        _write_json(
            appledb / f"osFiles/iPadOS/1x - 27.x/{build}.json",
            _appledb_record(build, "27.0", platform="iPadOS"),
        )
        _write_json(
            appledb / f"osFiles/iOS/1x - 27.x/{build}.json",
            _appledb_record(build, "27.0"),
        )
    commit = commit_all(appledb, "iPadOS fixture with iOS decoys")
    output = tmp_path / "releases.json"

    import_release_metadata(entries, appledb, commit, output, check=False)

    releases = json.loads(output.read_bytes())["releases"]
    assert [record["platform"] for record in releases] == ["iOS", "iOS"]
    assert [record["source_path"] for record in releases] == [
        "osFiles/iPadOS/1x - 27.x/A1.json",
        "osFiles/iPadOS/1x - 27.x/A2.json",
    ]


def test_import_rejects_ipados_record_with_mismatched_os_identity(tmp_path: Path) -> None:
    entries = tmp_path / "entries"
    entries.mkdir()
    _write_json(entries / "ios-27.0-A1-A2.json", _entry(device="iPad"))

    appledb = tmp_path / "appledb"
    init_repo(appledb)
    git(appledb, "remote", "add", "origin", _APPLEDB_ORIGIN)
    for build in ("A1", "A2"):
        _write_json(
            appledb / f"osFiles/iPadOS/1x - 27.x/{build}.json",
            _appledb_record(build, "27.0", platform="iOS"),
        )
    commit = commit_all(appledb, "mismatched iPadOS fixture")

    with pytest.raises(CatalogError, match=r"osStr differs: expected iPadOS"):
        import_release_metadata(entries, appledb, commit, tmp_path / "releases.json", check=False)


def test_import_accepts_rc_label_and_iso_timestamp(tmp_path: Path) -> None:
    entries, appledb, _commit = _fixture(tmp_path)
    record = _appledb_record("A2", "27.0 RC 2", rc=True)
    record["released"] = "2026-08-10T17:30:00+00:00"
    _write_json(appledb / "osFiles/iOS/1x - 27.x/A2.json", record)
    commit = commit_all(appledb, "record RC")

    result = import_release_metadata(
        entries,
        appledb,
        commit,
        tmp_path / "releases.json",
        check=False,
    )

    assert result.beta_count == 1
    assert result.rc_count == 1


def test_import_rejects_missing_and_ambiguous_builds(tmp_path: Path) -> None:
    entries, appledb, _commit = _fixture(tmp_path)
    missing_commit = git(appledb, "rm", "-q", "osFiles/iOS/1x - 27.x/A2.json")
    assert missing_commit == ""
    missing_commit = commit_all(appledb, "remove build")
    with pytest.raises(AppleDBCoverageMissingError, match=r"missing=.*A2"):
        import_release_metadata(
            entries, appledb, missing_commit, tmp_path / "missing.json", check=False
        )

    _write_json(
        appledb / "osFiles/iOS/2x - 27.x/A1.json",
        _appledb_record("A1", "27.0 beta", beta=True),
    )
    ambiguous_commit = commit_all(appledb, "duplicate build")
    with pytest.raises(CatalogError, match=r"ambiguous=.*A1") as ambiguous:
        import_release_metadata(
            entries,
            appledb,
            ambiguous_commit,
            tmp_path / "ambiguous.json",
            check=False,
        )
    assert not isinstance(ambiguous.value, AppleDBCoverageMissingError)


def test_cli_exits_with_wait_status_only_for_builds_appledb_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries, appledb, _commit = _fixture(tmp_path)
    git(appledb, "rm", "-q", "osFiles/iOS/1x - 27.x/A2.json")
    missing_commit = commit_all(appledb, "remove build")

    def run(commit: str) -> object:
        monkeypatch.setattr(
            "sys.argv",
            [
                "ipsw-diff-catalog",
                "release-metadata",
                "--entries-dir",
                str(entries),
                "--appledb-repo",
                str(appledb),
                "--appledb-commit",
                commit,
                "--output",
                str(tmp_path / "releases.json"),
            ],
        )
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        return exit_info.value.code

    assert run(missing_commit) == cli.EXIT_WAIT_FOR_APPLEDB
    assert run("0" * 40) == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"osStr": "macOS"}, "osStr differs"),
        ({"build": "A3"}, "build differs"),
        ({"version": "28.0 beta 2"}, "version differs"),
        ({"beta": True, "rc": True}, "both beta and RC"),
        ({"beta": False}, "version qualifier conflicts"),
        ({"version": "27.0"}, "version qualifier conflicts"),
        ({"beta": False, "rc": True}, "version qualifier conflicts"),
        ({"released": "not-a-date"}, "ISO 8601"),
    ],
)
def test_import_rejects_mutated_appledb_facts(
    tmp_path: Path,
    mutation: JsonObject,
    message: str,
) -> None:
    entries, appledb, _commit = _fixture(tmp_path)
    record = _appledb_record("A2", "27.0 beta 2", beta=True)
    record.update(mutation)
    _write_json(appledb / "osFiles/iOS/1x - 27.x/A2.json", record)
    commit = commit_all(appledb, "mutate record")

    with pytest.raises(CatalogError, match=message):
        import_release_metadata(entries, appledb, commit, tmp_path / "releases.json", check=False)


def test_import_rejects_stale_output_and_wrong_origin(tmp_path: Path) -> None:
    entries, appledb, commit = _fixture(tmp_path)
    output = tmp_path / "releases.json"
    output.write_text("stale\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="release metadata is stale"):
        import_release_metadata(entries, appledb, commit, output, check=True)

    git(appledb, "remote", "set-url", "origin", "https://github.com/example/not-appledb")
    with pytest.raises(CatalogError, match="AppleDB origin differs"):
        import_release_metadata(entries, appledb, commit, output, check=False)


def _pallas_ota(  # noqa: PLR0913
    build: str = "A2",
    *,
    os: str = "iOS",
    version: str = "27.0",
    kind: str = "beta",
    documentation_id: str = "iOS27Beta2",
    posting_date: str | None = None,
) -> JsonObject:
    return {
        "os": os,
        "version": version,
        "build": build,
        "channel": {"kind": kind, "documentation_id": documentation_id},
        "posting_date": posting_date,
        "delivery": "full",
        "prerequisite": None,
        "supported_devices": ["Device1,1"],
        "url": f"https://updates.cdn-apple.com/fixture/{'d' * 64}.aea",
        "sha256": "e" * 64,
    }


def _pallas_file(path: Path, *otas: JsonObject) -> Path:
    _write_json(path, {"schema_version": 1, "otas": list(otas)})
    return path


def _fixture_without_a2(tmp_path: Path) -> tuple[Path, Path, str]:
    entries, appledb, _commit = _fixture(tmp_path)
    git(appledb, "rm", "-q", "osFiles/iOS/1x - 27.x/A2.json")
    return entries, appledb, commit_all(appledb, "remove build")


@pytest.mark.parametrize(
    ("posting_date", "released"),
    [(None, "2026-11-17"), ("2026-11-16", "2026-11-16")],
    ids=["seed-without-posting-date", "posting-date"],
)
def test_import_labels_a_build_appledb_lacks_from_pallas(
    tmp_path: Path, posting_date: str | None, released: str
) -> None:
    entries, appledb, commit = _fixture_without_a2(tmp_path)
    pallas = PallasLabelSources(
        (_pallas_file(tmp_path / "pallas-beta.json", _pallas_ota(posting_date=posting_date)),),
        date(2026, 11, 17),
    )
    output = tmp_path / "releases.json"

    import_release_metadata(entries, appledb, commit, output, check=False, pallas=pallas)
    import_release_metadata(entries, appledb, commit, output, check=True, pallas=pallas)

    assert json.loads(output.read_text())["releases"][1] == {
        "platform": "iOS",
        "build": "A2",
        "display_version": "27.0 beta 2",
        "channel": "beta",
        "beta": True,
        "rc": False,
        "released": released,
        "source_path": "pallas:iOS27Beta2",
    }
    labels = load_release_labels(output, load_entries(entries))
    assert labels[("iOS", "A2")].display_version == "27.0 beta 2"


def test_import_keeps_appledb_as_the_record_once_it_has_the_build(tmp_path: Path) -> None:
    entries, appledb, commit = _fixture(tmp_path)
    pallas = PallasLabelSources(
        (_pallas_file(tmp_path / "pallas.json", _pallas_ota(documentation_id="iOS27Beta3")),),
        date(2026, 11, 17),
    )
    output = tmp_path / "releases.json"

    import_release_metadata(entries, appledb, commit, output, check=False, pallas=pallas)

    assert json.loads(output.read_text())["releases"][1]["source_path"] == (
        "osFiles/iOS/1x - 27.x/A2.json"
    )


@pytest.mark.parametrize(
    ("otas", "observed_on"),
    [
        ([], date(2026, 11, 17)),
        ([_pallas_ota(documentation_id="iOS28Beta2")], date(2026, 11, 17)),
        ([_pallas_ota(os="macOS")], date(2026, 11, 17)),
        ([_pallas_ota(version="27.1", documentation_id="iOS271Beta2")], date(2026, 11, 17)),
        ([_pallas_ota()], None),
        (
            [_pallas_ota(), _pallas_ota(documentation_id="iOS27Beta3")],
            date(2026, 11, 17),
        ),
        ([_pallas_ota(documentation_id="iOS27Beta1")], date(2026, 11, 17)),
    ],
    ids=[
        "not-served",
        "documentation-id-names-another-version",
        "other-platform",
        "other-base-version",
        "no-date",
        "conflicting-labels",
        "stale-documentation-id-repeats-an-appledb-label",
    ],
)
def test_import_waits_when_pallas_cannot_label_the_build(
    tmp_path: Path, otas: list[JsonObject], observed_on: date | None
) -> None:
    entries, appledb, commit = _fixture_without_a2(tmp_path)
    pallas = PallasLabelSources((_pallas_file(tmp_path / "pallas.json", *otas),), observed_on)

    with pytest.raises(AppleDBCoverageMissingError, match=r"missing=.*A2"):
        import_release_metadata(
            entries, appledb, commit, tmp_path / "releases.json", check=False, pallas=pallas
        )


def test_cli_labels_builds_from_pallas_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entries, appledb, commit = _fixture_without_a2(tmp_path)
    pallas = _pallas_file(tmp_path / "pallas-beta.json", _pallas_ota())
    output = tmp_path / "releases.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "ipsw-diff-catalog",
            "release-metadata",
            "--entries-dir",
            str(entries),
            "--appledb-repo",
            str(appledb),
            "--appledb-commit",
            commit,
            "--output",
            str(output),
            "--pallas-sources",
            str(pallas),
            "--observed-on",
            "2026-11-17",
        ],
    )

    cli.main()

    assert "releases=2 betas=2" in capsys.readouterr().out
    assert json.loads(output.read_text())["releases"][1]["released"] == "2026-11-17"
