from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from ipsw_diff_catalog.model import CatalogError, MigrationSpec
from ipsw_diff_catalog.verify import materialize_manifest, record, validate_source, verify
from tests.helpers import commit_all, git, populate_destination

if TYPE_CHECKING:
    from pathlib import Path

    from tests.helpers import Repositories

EXPECTED_FILE_COUNT = 3
EXPECTED_LOGICAL_BYTES = 138


def test_verify_and_record_round_trip(repositories: Repositories, tmp_path: Path) -> None:
    destination_commit = populate_destination(repositories)
    result = verify(
        repositories.spec,
        repositories.source,
        repositories.destination,
        destination_commit,
    )
    assert result.source == result.destination
    assert result.destination_commit == destination_commit
    assert result.source.file_count == EXPECTED_FILE_COUNT
    assert result.source.logical_bytes == EXPECTED_LOGICAL_BYTES
    assert result.source.tree == git(repositories.source, "rev-parse", "HEAD:source-diff")

    entry_path = record(
        repositories.spec,
        repositories.source,
        repositories.destination,
        destination_commit,
        tmp_path / "entries",
    )
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    assert entry["destination"]["commit"] == destination_commit
    assert entry["integrity"]["git_tree"] == result.source.tree


def test_materialize_manifest_from_immutable_source(
    repositories: Repositories,
    tmp_path: Path,
) -> None:
    output = tmp_path / repositories.spec.destination.manifest_path
    inventory = materialize_manifest(
        repositories.spec,
        repositories.source,
        output,
        check=False,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == repositories.spec.manifest(inventory)
    assert (
        materialize_manifest(
            repositories.spec,
            repositories.source,
            output,
            check=True,
        )
        == inventory
    )


def test_materialize_manifest_refuses_differing_output(
    repositories: Repositories,
    tmp_path: Path,
) -> None:
    output = tmp_path / "manifest.json"
    output.write_text("{}\n", encoding="utf-8")

    with pytest.raises(CatalogError, match="refusing to overwrite differing manifest"):
        materialize_manifest(
            repositories.spec,
            repositories.source,
            output,
            check=False,
        )
    with pytest.raises(CatalogError, match="manifest differs from measured source facts"):
        materialize_manifest(
            repositories.spec,
            repositories.source,
            output,
            check=True,
        )


def test_materialize_manifest_accepts_ipsw_status_outputs(
    repositories: Repositories,
    tmp_path: Path,
) -> None:
    payload = repositories.source / "source-diff"
    readme = payload / "README.md"
    note = "> ℹ️ Skipped `sandbox`: this kernelcache is not supported\n\n"  # noqa: RUF001
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("## Inputs", note + "## Inputs"),
        encoding="utf-8",
    )
    (payload / "status.json").write_text(
        '{"complete": true, "failed_sections": [], "skipped_sections": []}\n',
        encoding="utf-8",
    )
    source_commit = commit_all(repositories.source, "skip note and status")
    spec = replace(
        repositories.spec,
        source=replace(repositories.spec.source, commit=source_commit),
    )

    inventory = materialize_manifest(spec, repositories.source, tmp_path / "m.json", check=False)

    assert inventory.file_count == EXPECTED_FILE_COUNT + 1


def test_readme_metadata_mismatch_fails(repositories: Repositories) -> None:
    value = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    value["from"]["version"] = "9.9"
    spec = MigrationSpec.from_object(value)
    with pytest.raises(CatalogError, match="report title mismatch"):
        validate_source(spec, repositories.source)


def test_duplicate_inputs_section_fails(repositories: Repositories) -> None:
    readme = repositories.source / repositories.spec.source.path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n## Inputs\n\n- `Device1,1_1.0_A1_Restore.ipsw`\n",
        encoding="utf-8",
    )
    source_commit = commit_all(repositories.source, "duplicate inputs")
    value = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    value["source"]["commit"] = source_commit
    spec = MigrationSpec.from_object(value)
    with pytest.raises(CatalogError, match="exactly one '## Inputs' or '## IPSWs'"):
        validate_source(spec, repositories.source)


def test_legacy_ipsws_section_passes(repositories: Repositories) -> None:
    readme = repositories.source / repositories.spec.source.path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("## Inputs", "## IPSWs"),
        encoding="utf-8",
    )
    source_commit = commit_all(repositories.source, "legacy IPSWs heading")
    value = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    value["source"]["commit"] = source_commit
    spec = MigrationSpec.from_object(value)
    validate_source(spec, repositories.source)


def test_explicit_toc_entrypoint_verifies_and_populates_metadata(
    repositories: Repositories,
) -> None:
    payload = repositories.source / repositories.spec.source.path
    (payload / "README.md").rename(payload / "TOC.md")
    source_commit = commit_all(repositories.source, "legacy TOC entrypoint")
    spec = replace(
        repositories.spec,
        source=replace(repositories.spec.source, commit=source_commit),
        destination=replace(
            repositories.spec.destination,
            entrypoint=f"{repositories.spec.destination.payload_path}/TOC.md",
        ),
    )

    inventory = validate_source(spec, repositories.source)
    manifest = spec.manifest(inventory)
    payload_metadata = manifest["payload"]
    assert isinstance(payload_metadata, dict)
    assert payload_metadata["entrypoint"] == f"{spec.destination.payload_path}/TOC.md"


def test_mixed_input_sections_fail(repositories: Repositories) -> None:
    readme = repositories.source / repositories.spec.source.path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n## IPSWs\n\n- `Device1,1_1.0_A1_Restore.ipsw`\n",
        encoding="utf-8",
    )
    source_commit = commit_all(repositories.source, "mixed input headings")
    value = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    value["source"]["commit"] = source_commit
    spec = MigrationSpec.from_object(value)
    with pytest.raises(CatalogError, match="found 2"):
        validate_source(spec, repositories.source)


def test_non_ipsw_inputs_fail(repositories: Repositories) -> None:
    readme = repositories.source / repositories.spec.source.path / "README.md"
    content = readme.read_text(encoding="utf-8")
    readme.write_text(
        content.replace(
            "Device1,1_1.0_A1_Restore.ipsw",
            "Device1,1_1.0_A1_Restore.aea",
        ),
        encoding="utf-8",
    )
    source_commit = commit_all(repositories.source, "non-IPSW input")
    value = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    value["source"]["commit"] = source_commit
    spec = MigrationSpec.from_object(value)
    with pytest.raises(CatalogError, match="non-IPSW"):
        validate_source(spec, repositories.source)


def test_extra_inputs_content_fails(repositories: Repositories) -> None:
    readme = repositories.source / repositories.spec.source.path / "README.md"
    content = readme.read_text(encoding="utf-8")
    readme.write_text(
        content.replace("\n## Data\n", "\nAdditional input note\n\n## Data\n"),
        encoding="utf-8",
    )
    source_commit = commit_all(repositories.source, "extra inputs content")
    value = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    value["source"]["commit"] = source_commit
    spec = MigrationSpec.from_object(value)
    with pytest.raises(CatalogError, match="report inputs differ"):
        validate_source(spec, repositories.source)


def test_mutable_destination_revision_is_rejected(repositories: Repositories) -> None:
    populate_destination(repositories)
    with pytest.raises(CatalogError, match="full lowercase SHA-1"):
        verify(
            repositories.spec,
            repositories.source,
            repositories.destination,
            "HEAD",
        )


def test_payload_mutation_breaks_verification(repositories: Repositories) -> None:
    populate_destination(repositories)
    payload = (
        repositories.destination / repositories.spec.destination.payload_path / "nested/payload.md"
    )
    payload.write_text("mutated\n", encoding="utf-8")
    destination_commit = commit_all(repositories.destination, "mutate payload")
    with pytest.raises(CatalogError, match="payload inventory mismatch"):
        verify(
            repositories.spec,
            repositories.source,
            repositories.destination,
            destination_commit,
        )


def test_manifest_mutation_breaks_verification(repositories: Repositories) -> None:
    populate_destination(repositories)
    manifest_path = repositories.destination / repositories.spec.destination.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["payload"]["logical_bytes"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination_commit = commit_all(repositories.destination, "mutate manifest")
    with pytest.raises(CatalogError, match="manifest differs"):
        verify(
            repositories.spec,
            repositories.source,
            repositories.destination,
            destination_commit,
        )


def test_record_never_overwrites_different_entry(
    repositories: Repositories,
    tmp_path: Path,
) -> None:
    destination_commit = populate_destination(repositories)
    entries = tmp_path / "entries"
    path = entries / f"{repositories.spec.identifier}.json"
    entries.mkdir()
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="refusing to overwrite"):
        record(
            repositories.spec,
            repositories.source,
            repositories.destination,
            destination_commit,
            entries,
        )
