from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast


class CatalogError(Exception):
    """A fail-closed validation or integrity error."""


JsonObject = dict[str, object]

_FULL_OID = re.compile(r"[0-9a-f]{40}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,-]*")
_GITHUB_REPOSITORY = re.compile(
    r"https://github\.com/[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"
)
_PATH_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9,._+@-]*")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*(?: [A-Za-z0-9]+)*")
_BUILD = re.compile(r"[A-Za-z0-9]+")
_DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9,._-]*")
_INPUT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9,._+-]*(?:\.ipsw|_OTA\.zip)")


class _DuplicateJsonKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> JsonObject:
    value: JsonObject = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError(key)
        value[key] = item
    return value


def parse_json_object(raw: str | bytes, context: str) -> JsonObject:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except _DuplicateJsonKeyError as error:
        raise CatalogError(f"{context} contains duplicate key {error.args[0]!r}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogError(f"{context} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CatalogError(f"{context} must contain one JSON object")
    return cast("JsonObject", value)


def read_json_object(path: Path) -> JsonObject:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CatalogError(f"cannot read JSON object {path}: {error}") from error
    return parse_json_object(raw, str(path))


def canonical_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _exact_keys(value: JsonObject, expected: set[str], context: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CatalogError(f"{context} keys differ: missing={missing}, extra={extra}")


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CatalogError(f"{context} must be an object")
    return cast("JsonObject", value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int or value < 0:
        raise CatalogError(f"{context} must be a non-negative integer")
    return value


def _version(value: object, context: str) -> str:
    version = _string(value, context)
    if _VERSION.fullmatch(version) is None:
        raise CatalogError(f"{context} has an unsupported version format")
    return version


def _build(value: object, context: str) -> str:
    build = _string(value, context)
    if _BUILD.fullmatch(build) is None:
        raise CatalogError(f"{context} has an unsupported build format")
    return build


def _device(value: object, context: str) -> str:
    device = _string(value, context)
    if _DEVICE.fullmatch(device) is None:
        raise CatalogError(f"{context} has an unsupported device format")
    return device


def _input_name(value: object, context: str) -> str:
    input_name = _string(value, context)
    if _INPUT_NAME.fullmatch(input_name) is None:
        raise CatalogError(f"{context} must be a plain IPSW or full-OTA input filename")
    return input_name


def _full_oid(value: object, context: str) -> str:
    oid = _string(value, context)
    if _FULL_OID.fullmatch(oid) is None:
        raise CatalogError(f"{context} must be a full lowercase SHA-1 object ID")
    return oid


def _repository(value: object, context: str) -> str:
    repository = _string(value, context)
    if _GITHUB_REPOSITORY.fullmatch(repository) is None:
        raise CatalogError(f"{context} must be an https://github.com/OWNER/REPOSITORY URL")
    return repository.removesuffix(".git")


def _relative_path(value: object, context: str) -> str:
    raw = _string(value, context)
    path = PurePosixPath(raw)
    if path.is_absolute() or str(path) != raw or not path.parts:
        raise CatalogError(f"{context} must be a normalized relative POSIX path")
    if any(_PATH_PART.fullmatch(part) is None for part in path.parts):
        raise CatalogError(f"{context} contains an unsupported path component")
    return raw


def _validate_repository_route(
    platform: str,
    major_version: int,
    repository: str,
    context: str,
) -> None:
    if platform not in {"iOS", "macOS"}:
        raise CatalogError("platform must be exactly iOS or macOS")
    repository_name = f"{platform.lower()}-{major_version}"
    if not repository.endswith(f"/{repository_name}"):
        raise CatalogError(f"{context} must route to {repository_name}")


def _validate_catalog_paths(payload: str, entrypoint: str, manifest: str) -> None:
    allowed_entrypoints = {f"{payload}/README.md", f"{payload}/TOC.md"}
    if entrypoint not in allowed_entrypoints:
        raise CatalogError("destination.entrypoint must be the payload README.md or TOC.md")
    if not payload.startswith("diffs/"):
        raise CatalogError("destination.payload_path must be below diffs/")
    if not manifest.startswith("manifests/") or not manifest.endswith(".json"):
        raise CatalogError("destination.manifest_path must be a JSON file below manifests/")


@dataclass(frozen=True)
class Release:
    version: str
    build: str
    input_name: str

    @classmethod
    def from_object(cls, value: object, context: str) -> Release:
        data = _object(value, context)
        _exact_keys(data, {"version", "build", "input"}, context)
        release = cls(
            version=_version(data["version"], f"{context}.version"),
            build=_build(data["build"], f"{context}.build"),
            input_name=_input_name(data["input"], f"{context}.input"),
        )
        if release.build not in release.input_name:
            raise CatalogError(f"{context}.input does not contain declared build")
        return release

    def to_object(self) -> JsonObject:
        return {"version": self.version, "build": self.build, "input": self.input_name}


@dataclass(frozen=True)
class Source:
    repository: str
    commit: str
    path: str

    @classmethod
    def from_object(cls, value: object, context: str = "source") -> Source:
        data = _object(value, context)
        _exact_keys(data, {"repository", "commit", "path"}, context)
        return cls(
            repository=_repository(data["repository"], f"{context}.repository"),
            commit=_full_oid(data["commit"], f"{context}.commit"),
            path=_relative_path(data["path"], f"{context}.path"),
        )

    def to_object(self) -> JsonObject:
        return {"repository": self.repository, "commit": self.commit, "path": self.path}


@dataclass(frozen=True)
class Destination:
    repository: str
    payload_path: str
    entrypoint: str
    manifest_path: str

    @classmethod
    def from_object(cls, value: object) -> Destination:
        data = _object(value, "destination")
        required = {"repository", "payload_path", "manifest_path"}
        actual = set(data)
        unexpected = actual - required - {"entrypoint"}
        if not required <= actual or unexpected:
            raise CatalogError(
                "destination keys differ: "
                f"missing={sorted(required - actual)}, "
                f"extra={sorted(unexpected)}"
            )
        payload_path = _relative_path(data["payload_path"], "destination.payload_path")
        destination = cls(
            repository=_repository(data["repository"], "destination.repository"),
            payload_path=payload_path,
            entrypoint=_relative_path(
                data.get("entrypoint", f"{payload_path}/README.md"),
                "destination.entrypoint",
            ),
            manifest_path=_relative_path(data["manifest_path"], "destination.manifest_path"),
        )
        _validate_catalog_paths(
            destination.payload_path,
            destination.entrypoint,
            destination.manifest_path,
        )
        return destination


@dataclass(frozen=True)
class TreeIdentity:
    tree: str
    file_count: int
    modes: frozenset[str]


@dataclass(frozen=True)
class TreeInventory:
    tree: str
    file_count: int
    logical_bytes: int
    modes: frozenset[str]

    @property
    def identity(self) -> TreeIdentity:
        return TreeIdentity(tree=self.tree, file_count=self.file_count, modes=self.modes)


@dataclass(frozen=True)
class MigrationSpec:
    identifier: str
    platform: str
    major_version: int
    device: str
    previous: Release
    next: Release
    source: Source
    destination: Destination

    @classmethod
    def from_object(cls, value: JsonObject) -> MigrationSpec:
        _exact_keys(
            value,
            {
                "schema_version",
                "id",
                "platform",
                "major_version",
                "device",
                "from",
                "to",
                "source",
                "destination",
            },
            "migration spec",
        )
        if _integer(value["schema_version"], "schema_version") != 1:
            raise CatalogError("schema_version must be 1")
        identifier = _string(value["id"], "id")
        if _IDENTIFIER.fullmatch(identifier) is None:
            raise CatalogError("id must use letters, numbers, periods, commas, or hyphens")
        major_version = _integer(value["major_version"], "major_version")
        if major_version == 0:
            raise CatalogError("major_version must be positive")
        spec = cls(
            identifier=identifier,
            platform=_string(value["platform"], "platform"),
            major_version=major_version,
            device=_device(value["device"], "device"),
            previous=Release.from_object(value["from"], "from"),
            next=Release.from_object(value["to"], "to"),
            source=Source.from_object(value["source"]),
            destination=Destination.from_object(value["destination"]),
        )
        spec._validate_routing_policy()
        return spec

    @classmethod
    def from_path(cls, path: Path) -> MigrationSpec:
        return cls.from_object(read_json_object(path))

    @property
    def title(self) -> str:
        return (
            f"{self.previous.version} ({self.previous.build}) .vs "
            f"{self.next.version} ({self.next.build})"
        )

    @property
    def entrypoint(self) -> str:
        return self.destination.entrypoint

    @property
    def source_entrypoint(self) -> str:
        return f"{self.source.path}/{PurePosixPath(self.entrypoint).name}"

    def to_object(self) -> JsonObject:
        destination: JsonObject = {
            "repository": self.destination.repository,
            "payload_path": self.destination.payload_path,
            "manifest_path": self.destination.manifest_path,
        }
        if self.entrypoint != f"{self.destination.payload_path}/README.md":
            destination["entrypoint"] = self.entrypoint
        return {
            "schema_version": 1,
            "id": self.identifier,
            "platform": self.platform,
            "major_version": self.major_version,
            "device": self.device,
            "from": self.previous.to_object(),
            "to": self.next.to_object(),
            "source": self.source.to_object(),
            "destination": destination,
        }

    def _validate_routing_policy(self) -> None:
        next_major = re.match(r"(\d+)", self.next.version)
        if next_major is None or int(next_major.group(1)) != self.major_version:
            raise CatalogError("major_version must match the destination version")
        _validate_repository_route(
            self.platform,
            self.major_version,
            self.destination.repository,
            "destination.repository",
        )
        expected_payload = f"diffs/{self.source.path}"
        if self.destination.payload_path != expected_payload:
            raise CatalogError(f"destination.payload_path must be {expected_payload}")
        expected_manifest = f"manifests/{self.source.path}.json"
        if self.destination.manifest_path != expected_manifest:
            raise CatalogError(f"destination.manifest_path must be {expected_manifest}")

    def manifest(self, inventory: TreeInventory) -> JsonObject:
        return {
            "schema_version": 1,
            "id": self.identifier,
            "platform": self.platform,
            "major_version": self.major_version,
            "device": self.device,
            "from": self.previous.to_object(),
            "to": self.next.to_object(),
            "payload": {
                "path": self.destination.payload_path,
                "entrypoint": self.entrypoint,
                "tracked_file_count": inventory.file_count,
                "logical_bytes": inventory.logical_bytes,
                "git_tree": inventory.tree,
            },
            "source": self.source.to_object(),
        }

    def catalog_entry(self, inventory: TreeInventory, destination_commit: str) -> JsonObject:
        return {
            "schema_version": 1,
            "id": self.identifier,
            "platform": self.platform,
            "major_version": self.major_version,
            "device": self.device,
            "from": self.previous.to_object(),
            "to": self.next.to_object(),
            "source": self.source.to_object(),
            "destination": {
                "repository": self.destination.repository,
                "commit": destination_commit,
                "payload_path": self.destination.payload_path,
                "entrypoint": self.entrypoint,
                "manifest_path": self.destination.manifest_path,
            },
            "integrity": {
                "git_tree": inventory.tree,
                "tracked_file_count": inventory.file_count,
                "logical_bytes": inventory.logical_bytes,
            },
        }


@dataclass(frozen=True)
class CatalogEntry:
    data: JsonObject
    identifier: str
    platform: str
    major_version: int
    device: str
    previous: Release
    next: Release
    source: Source
    destination_repository: str
    destination_commit: str
    payload_path: str
    entrypoint: str
    manifest_path: str
    inventory: TreeInventory

    @classmethod
    def from_object(cls, value: JsonObject) -> CatalogEntry:
        _exact_keys(
            value,
            {
                "schema_version",
                "id",
                "platform",
                "major_version",
                "device",
                "from",
                "to",
                "source",
                "destination",
                "integrity",
            },
            "catalog entry",
        )
        if _integer(value["schema_version"], "schema_version") != 1:
            raise CatalogError("catalog entry schema_version must be 1")
        destination = _object(value["destination"], "destination")
        _exact_keys(
            destination,
            {"repository", "commit", "payload_path", "entrypoint", "manifest_path"},
            "destination",
        )
        integrity = _object(value["integrity"], "integrity")
        _exact_keys(
            integrity,
            {"git_tree", "tracked_file_count", "logical_bytes"},
            "integrity",
        )
        payload_path = _relative_path(destination["payload_path"], "destination.payload_path")
        entrypoint = _relative_path(destination["entrypoint"], "destination.entrypoint")
        manifest_path = _relative_path(
            destination["manifest_path"],
            "destination.manifest_path",
        )
        _validate_catalog_paths(payload_path, entrypoint, manifest_path)
        identifier = _string(value["id"], "id")
        if _IDENTIFIER.fullmatch(identifier) is None:
            raise CatalogError("catalog entry id has unsupported characters")
        inventory = TreeInventory(
            tree=_full_oid(integrity["git_tree"], "integrity.git_tree"),
            file_count=_integer(integrity["tracked_file_count"], "integrity.tracked_file_count"),
            logical_bytes=_integer(integrity["logical_bytes"], "integrity.logical_bytes"),
            modes=frozenset(),
        )
        if inventory.file_count == 0:
            raise CatalogError("integrity.tracked_file_count must be positive")
        platform = _string(value["platform"], "platform")
        major_version = _integer(value["major_version"], "major_version")
        if major_version == 0:
            raise CatalogError("major_version must be positive")
        destination_repository = _repository(
            destination["repository"],
            "destination.repository",
        )
        _validate_repository_route(
            platform,
            major_version,
            destination_repository,
            "destination.repository",
        )
        previous = Release.from_object(value["from"], "from")
        next_release = Release.from_object(value["to"], "to")
        next_major = re.match(r"(\d+)", next_release.version)
        if next_major is None or int(next_major.group(1)) != major_version:
            raise CatalogError("major_version must match the destination version")
        return cls(
            data=value,
            identifier=identifier,
            platform=platform,
            major_version=major_version,
            device=_device(value["device"], "device"),
            previous=previous,
            next=next_release,
            source=Source.from_object(value["source"]),
            destination_repository=destination_repository,
            destination_commit=_full_oid(destination["commit"], "destination.commit"),
            payload_path=payload_path,
            entrypoint=entrypoint,
            manifest_path=manifest_path,
            inventory=inventory,
        )
