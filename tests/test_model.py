from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ipsw_diff_catalog.model import CatalogError, MigrationSpec, parse_json_object

if TYPE_CHECKING:
    from tests.helpers import Repositories


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source.path", "../escape"),
        ("destination.payload_path", "elsewhere/diff"),
        ("destination.manifest_path", "manifests/not-json"),
    ],
)
def test_spec_rejects_unsafe_or_misplaced_paths(
    repositories: Repositories,
    field: str,
    value: str,
) -> None:
    data = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    section, name = field.split(".")
    data[section][name] = value
    with pytest.raises(CatalogError):
        MigrationSpec.from_object(data)


def test_spec_rejects_unknown_fields(repositories: Repositories) -> None:
    data = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    data["guessed_platform"] = True
    with pytest.raises(CatalogError, match=r"extra=.*guessed_platform"):
        MigrationSpec.from_object(data)


def test_spec_accepts_explicit_toc_entrypoint(repositories: Repositories) -> None:
    data = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    payload_path = data["destination"]["payload_path"]
    data["destination"]["entrypoint"] = f"{payload_path}/TOC.md"

    spec = MigrationSpec.from_object(data)

    assert spec.entrypoint == f"{payload_path}/TOC.md"
    assert spec.source_entrypoint == f"{spec.source.path}/TOC.md"
    assert MigrationSpec.from_object(spec.to_object()) == spec


@pytest.mark.parametrize("name", ["INDEX.md", "docs/TOC.md"])
def test_spec_rejects_unsupported_entrypoint(
    repositories: Repositories,
    name: str,
) -> None:
    data = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    payload_path = data["destination"]["payload_path"]
    data["destination"]["entrypoint"] = f"{payload_path}/{name}"

    with pytest.raises(CatalogError, match=r"README\.md or TOC\.md"):
        MigrationSpec.from_object(data)


def test_json_rejects_duplicate_keys() -> None:
    with pytest.raises(CatalogError, match="duplicate key 'build'"):
        parse_json_object(
            b'{"release":{"build":"A1","build":"A2"}}',
            "test document",
        )


def test_spec_rejects_input_path(repositories: Repositories) -> None:
    data = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    data["from"]["input"] = "../Device1,1_1.0_A1_Restore.ipsw"
    with pytest.raises(CatalogError, match="plain IPSW or full-OTA input filename"):
        MigrationSpec.from_object(data)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("platform", "watchOS", "platform must be exactly"),
        ("major_version", 2, "must match the destination version"),
    ],
)
def test_spec_rejects_unsupported_routing(
    repositories: Repositories,
    field: str,
    value: object,
    message: str,
) -> None:
    data = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    data[field] = value
    with pytest.raises(CatalogError, match=message):
        MigrationSpec.from_object(data)


@pytest.mark.parametrize(
    ("name", "accepted"),
    [
        ("Mac17,6_1.0_A1_OTA.zip", True),
        ("UniversalMac_1.0_A1_Restore.ipsw", True),
        ("a3cdb8a53ac6beaaf73701ffa54b184b0de8e3a0.zip", False),
        ("Mac17,6_1.0_A1_OTA.aea", False),
    ],
)
def test_spec_accepts_ipsw_and_named_full_ota_inputs(
    repositories: Repositories, name: str, *, accepted: bool
) -> None:
    data = json.loads(repositories.spec_path.read_text(encoding="utf-8"))
    data["from"]["input"] = name
    if accepted:
        assert MigrationSpec.from_object(data).previous.input_name == name
    else:
        with pytest.raises(CatalogError, match="plain IPSW or full-OTA input filename"):
            MigrationSpec.from_object(data)
