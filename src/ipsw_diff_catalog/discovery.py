from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from urllib.parse import unquote, urlparse

from ipsw_diff_catalog.git import (
    blob_at_path,
    ensure_repository,
    require_origin,
    resolve_commit,
    tree_paths,
)
from ipsw_diff_catalog.model import CatalogError, JsonObject, parse_json_object, read_json_object
from ipsw_diff_catalog.pallas import PallasBuild, PallasDeferral, read_pallas

_APPLEDB_REPOSITORY = "https://github.com/littlebyteorg/appledb"
_IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*(?: [A-Za-z0-9]+)*")
_BUILD = re.compile(r"[A-Za-z0-9]+")
_DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9,._-]*")
_IPSW_INPUT = re.compile(r"[A-Za-z0-9][A-Za-z0-9,._+-]*\.ipsw")
_OTA_URL_NAME = re.compile(r"[0-9a-f]{40}\.zip")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BETA_QUALIFIER = re.compile(r"beta(?: [1-9][0-9]*)?")
_RC_QUALIFIER = re.compile(r"RC(?: [1-9][0-9]*)?")
_CATALOG_TO_APPLEDB = {
    "iOS": frozenset({"iOS", "iPadOS"}),
    "macOS": frozenset({"macOS"}),
}
_TRACK_SCHEMA_VERSION = 2
SourceKind = Literal["ipsw", "ota"]
MetadataSource = Literal["appledb", "pallas"]


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CatalogError(f"{context} must be an object")
    return cast("JsonObject", value)


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise CatalogError(f"{context} must be an array")
    return cast("list[object]", value)


def _string(value: object, context: str, pattern: re.Pattern[str] | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise CatalogError(f"{context} has an unsupported value")
    return value


def _integer(value: object, context: str) -> int:
    if type(value) is not int or value <= 0:
        raise CatalogError(f"{context} must be a positive integer")
    return value


def _boolean(value: object, context: str) -> bool:
    if type(value) is not bool:
        raise CatalogError(f"{context} must be a boolean")
    return value


def _major(version: str, context: str) -> int:
    match = re.match(r"([0-9]+)(?:\.|$)", version)
    if match is None:
        raise CatalogError(f"{context} has no numeric major")
    return int(match.group(1))


def _released(value: object, context: str) -> str:
    raw = _string(value, context)
    try:
        if "T" not in raw:
            parsed = datetime.combine(date.fromisoformat(raw), datetime.min.time(), tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise CatalogError(f"{context} must be an ISO 8601 date or timestamp") from error
    if parsed.tzinfo is None:
        raise CatalogError(f"{context} timestamp must include a timezone")
    parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_channel(version: str, *, beta: bool, rc: bool, context: str) -> None:
    if beta and rc:
        raise CatalogError(f"{context} cannot be both beta and RC")
    _base, separator, qualifier = version.partition(" ")
    expected = _BETA_QUALIFIER if beta else _RC_QUALIFIER if rc else None
    if expected is None:
        if separator:
            raise CatalogError(f"{context}.version qualifier conflicts with release flags")
    elif not separator or expected.fullmatch(qualifier) is None:
        raise CatalogError(f"{context}.version qualifier conflicts with release flags")


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    build: str
    released: str
    beta: bool
    rc: bool

    @classmethod
    def from_object(cls, value: object, context: str) -> ReleaseMetadata:
        data = _object(value, context)
        expected = {"version", "build", "released", "beta", "rc"}
        if set(data) != expected:
            raise CatalogError(
                f"{context} keys differ: missing={sorted(expected - set(data))}, "
                f"extra={sorted(set(data) - expected)}"
            )
        release = cls(
            version=_string(data["version"], f"{context}.version", _VERSION),
            build=_string(data["build"], f"{context}.build", _BUILD),
            released=_released(data["released"], f"{context}.released"),
            beta=_boolean(data["beta"], f"{context}.beta"),
            rc=_boolean(data["rc"], f"{context}.rc"),
        )
        _validate_channel(
            release.version,
            beta=release.beta,
            rc=release.rc,
            context=context,
        )
        return release

    @property
    def released_at(self) -> datetime:
        return datetime.strptime(self.released, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    @property
    def base_version(self) -> str:
        return self.version.partition(" ")[0]

    @property
    def version_key(self) -> tuple[int, ...]:
        return tuple(int(part) for part in self.base_version.split("."))

    def to_object(self) -> JsonObject:
        return {
            "version": self.version,
            "build": self.build,
            "released": self.released,
            "beta": self.beta,
            "rc": self.rc,
        }


def _source_kind(data: JsonObject) -> SourceKind | None:
    """Returns the kind of a full firmware source, or None for anything else."""
    if data.get("type") == "ipsw":
        return "ipsw"
    if data.get("type") == "ota" and not data.get("prerequisiteBuild"):
        return "ota"
    return None


def _active_apple_url(data: JsonObject, context: str) -> str:
    active_urls: list[str] = []
    for index, item in enumerate(_array(data["links"], f"{context}.links")):
        link = _object(item, f"{context}.links[{index}]")
        if _boolean(link.get("active", False), f"{context}.links[{index}].active"):
            active_urls.append(_string(link.get("url"), f"{context}.links[{index}].url"))
    if len(active_urls) != 1:
        raise CatalogError(f"{context} must have exactly one active link")
    url = active_urls[0]
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    allowed_host = hostname == "apple.com" or hostname.endswith(".apple.com")
    allowed_host = (
        allowed_host or hostname == "cdn-apple.com" or hostname.endswith(".cdn-apple.com")
    )
    if parsed.scheme != "https" or not allowed_host or parsed.query or parsed.fragment:
        raise CatalogError(f"{context} has an unsupported active URL")
    return url


def _input_name(url: str, kind: SourceKind, device: str, metadata: ReleaseMetadata) -> str:
    url_name = unquote(PurePosixPath(urlparse(url).path).name)
    if kind == "ipsw":
        if _IPSW_INPUT.fullmatch(url_name) is None:
            raise CatalogError("active URL has an unsupported IPSW filename")
        if f"_{metadata.build}_" not in url_name:
            raise CatalogError("IPSW filename does not contain its exact build")
        return url_name
    if _OTA_URL_NAME.fullmatch(url_name) is None:
        raise CatalogError("active URL has an unsupported OTA filename")
    # OTA files are named by digest, so name the input like an IPSW to keep the build in it.
    return f"{device}_{metadata.base_version}_{metadata.build}_OTA.zip"


@dataclass(frozen=True)
class FirmwareSource:
    kind: SourceKind
    input: str
    url: str
    size: int
    sha256: str

    @classmethod
    def from_object(
        cls,
        value: object,
        device: str,
        metadata: ReleaseMetadata,
        context: str,
    ) -> FirmwareSource:
        data = _object(value, context)
        required = {"type", "deviceMap", "links", "hashes", "size"}
        if not required <= set(data):
            raise CatalogError(f"{context} is missing keys: {sorted(required - set(data))}")
        kind = _source_kind(data)
        if kind is None:
            raise CatalogError(f"{context} must be an IPSW or a full OTA")
        devices = [
            _string(item, f"{context}.deviceMap")
            for item in _array(data["deviceMap"], f"{context}.deviceMap")
        ]
        if device not in devices:
            raise CatalogError(f"{context} does not select device {device}")
        url = _active_apple_url(data, context)
        try:
            input_name = _input_name(url, kind, device, metadata)
        except CatalogError as error:
            raise CatalogError(f"{context} {error}") from error
        size = _integer(data["size"], f"{context}.size")
        hashes = _object(data["hashes"], f"{context}.hashes")
        sha256 = _string(hashes.get("sha2-256"), f"{context}.hashes.sha2-256", _SHA256)
        return cls(kind=kind, input=input_name, url=url, size=size, sha256=sha256)

    def to_object(self) -> JsonObject:
        return {
            "kind": self.kind,
            "input": self.input,
            "url": self.url,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class AppleDBCandidate:
    metadata: ReleaseMetadata
    source_path: str | None
    sources: dict[SourceKind, tuple[object, str]]
    origin: MetadataSource = "appledb"

    @property
    def preferred_kind(self) -> SourceKind:
        return "ipsw" if "ipsw" in self.sources else "ota"

    def select(self, device: str, kind: SourceKind) -> AppleDBRelease:
        value, context = self.sources[kind]
        source = FirmwareSource.from_object(value, device, self.metadata, context)
        return AppleDBRelease(
            metadata=self.metadata,
            source_path=self.source_path,
            source=source,
            origin=self.origin,
        )


@dataclass(frozen=True)
class AppleDBRelease:
    metadata: ReleaseMetadata
    source_path: str | None
    source: FirmwareSource
    origin: MetadataSource = "appledb"

    def to_object(self) -> JsonObject:
        value = self.metadata.to_object()
        value["appledb_path"] = self.source_path
        value["metadata_source"] = self.origin
        value["source"] = self.source.to_object()
        return value


@dataclass(frozen=True)
class TrackPolicy:
    identifier: str
    platform: str
    appledb_platform: str
    device: str
    major_version: int
    anchor: ReleaseMetadata
    ota_fallback: bool

    @classmethod
    def from_object(cls, value: JsonObject) -> TrackPolicy:
        expected = {
            "schema_version",
            "id",
            "platform",
            "appledb_platform",
            "device",
            "major_version",
            "anchor",
        }
        optional = {"ota_fallback"}
        if not expected <= set(value) <= expected | optional:
            raise CatalogError(
                f"track policy keys differ: missing={sorted(expected - set(value))}, "
                f"extra={sorted(set(value) - expected - optional)}"
            )
        if (
            _integer(value["schema_version"], "track policy.schema_version")
            != _TRACK_SCHEMA_VERSION
        ):
            raise CatalogError("track policy.schema_version must be 2")
        policy = cls(
            identifier=_string(value["id"], "track policy.id", _IDENTIFIER),
            platform=_string(value["platform"], "track policy.platform"),
            appledb_platform=_string(value["appledb_platform"], "track policy.appledb_platform"),
            device=_string(value["device"], "track policy.device", _DEVICE),
            major_version=_integer(value["major_version"], "track policy.major_version"),
            anchor=ReleaseMetadata.from_object(value["anchor"], "track policy.anchor"),
            ota_fallback=_boolean(value.get("ota_fallback", False), "track policy.ota_fallback"),
        )
        if policy.ota_fallback and policy.platform != "macOS":
            raise CatalogError("track policy.ota_fallback is only reviewed for macOS tracks")
        allowed_appledb = _CATALOG_TO_APPLEDB.get(policy.platform)
        if allowed_appledb is None or policy.appledb_platform not in allowed_appledb:
            raise CatalogError("track policy platform route is unsupported")
        prefix = "ios" if policy.platform == "iOS" else "macos"
        if policy.identifier != f"{prefix}-{policy.major_version}":
            raise CatalogError("track policy id must match its catalog platform and major")
        if _major(policy.anchor.version, "track policy.anchor.version") != policy.major_version:
            raise CatalogError("track policy anchor version major must match major_version")
        return policy

    @classmethod
    def from_path(cls, path: Path) -> TrackPolicy:
        return cls.from_object(read_json_object(path))


@dataclass(frozen=True)
class DiscoveryEdge:
    previous: AppleDBRelease
    next: AppleDBRelease

    def __post_init__(self) -> None:
        if self.previous.source.kind != self.next.source.kind:
            raise CatalogError("a discovery edge must diff two inputs of the same kind")

    def to_object(self) -> JsonObject:
        return {
            "kind": self.previous.source.kind,
            "from": self.previous.to_object(),
            "to": self.next.to_object(),
        }


@dataclass(frozen=True)
class DiscoveryDecision:
    track: TrackPolicy
    appledb_commit: str
    anchor: AppleDBRelease
    latest: AppleDBRelease
    edges: tuple[DiscoveryEdge, ...]
    observed_source_count: int
    selected_source_count: int
    pallas_queried: bool = False
    pallas_used: tuple[str, ...] = ()
    pallas_deferred: tuple[PallasDeferral, ...] = ()

    @property
    def status(self) -> Literal["current", "candidate"]:
        return "candidate" if self.edges else "current"

    def to_object(self) -> JsonObject:
        return {
            "schema_version": 2,
            "track_id": self.track.identifier,
            "status": self.status,
            "platform": self.track.platform,
            "appledb_platform": self.track.appledb_platform,
            "device": self.track.device,
            "major_version": self.track.major_version,
            "appledb": {"repository": _APPLEDB_REPOSITORY, "commit": self.appledb_commit},
            "source_inventory": {
                "observed": self.observed_source_count,
                "selected": self.selected_source_count,
            },
            "pallas": {
                "queried": self.pallas_queried,
                "used": list(self.pallas_used),
                "deferred": [deferral.to_object() for deferral in self.pallas_deferred],
            },
            "anchor": self.anchor.to_object(),
            "latest": self.latest.to_object(),
            "edges": [edge.to_object() for edge in self.edges],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_object(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )


def _device_source_kind(value: object, policy: TrackPolicy, context: str) -> SourceKind | None:
    data = _object(value, context)
    kind = _source_kind(data)
    if kind is None or (kind == "ota" and not policy.ota_fallback):
        return None
    devices = [
        _string(item, f"{context}.deviceMap")
        for item in _array(data.get("deviceMap"), f"{context}.deviceMap")
    ]
    return kind if policy.device in devices else None


def _record_metadata(data: JsonObject, context: str) -> ReleaseMetadata:
    return ReleaseMetadata.from_object(
        {
            "version": data["version"],
            "build": data["build"],
            "released": data["released"],
            "beta": data.get("beta", False),
            "rc": data.get("rc", False),
        },
        context,
    )


def _compatible_source_values(
    data: JsonObject,
    policy: TrackPolicy,
    context: str,
) -> dict[SourceKind, list[tuple[object, str]]]:
    raw_sources = data.get("sources")
    if raw_sources is None:
        return {}
    compatible: dict[SourceKind, list[tuple[object, str]]] = {}
    for index, item in enumerate(_array(raw_sources, f"{context}.sources")):
        item_context = f"{context}.sources[{index}]"
        kind = _device_source_kind(item, policy, item_context)
        if kind is not None:
            compatible.setdefault(kind, []).append((item, item_context))
    return compatible


def _release_from_blob(
    raw: bytes,
    *,
    policy: TrackPolicy,
    source_path: str,
) -> AppleDBCandidate | None:
    context = f"AppleDB {source_path}"
    data = parse_json_object(raw, context)
    required = {"osStr", "version", "build"}
    if not required <= set(data):
        raise CatalogError(f"{context} is missing keys: {sorted(required - set(data))}")
    if _boolean(data.get("internal", False), f"{context}.internal"):
        return None
    if _string(data["osStr"], f"{context}.osStr") != policy.appledb_platform:
        raise CatalogError(f"{context}.osStr differs from the track policy")
    compatible = _compatible_source_values(data, policy, context)
    if not compatible:
        return None
    if "released" not in data:
        raise CatalogError(f"{context} has a compatible firmware source but no release date")
    metadata = _record_metadata(data, context)
    if _major(metadata.version, f"{context}.version") != policy.major_version:
        raise CatalogError(f"{context}.version differs from the track major")
    for kind, values in compatible.items():
        if len(values) != 1:
            label = "IPSW sources" if kind == "ipsw" else "full OTA sources"
            raise CatalogError(f"{context} has multiple {label} for {policy.device}")
    return AppleDBCandidate(
        metadata=metadata,
        source_path=source_path,
        sources={kind: values[0] for kind, values in compatible.items()},
    )


@dataclass(frozen=True)
class ObservedSource:
    release: JsonObject
    artifact: JsonObject
    context: str

    def select(self, device: str) -> tuple[ReleaseMetadata, FirmwareSource]:
        channel = self.release.get("channel")
        if channel not in ("release", "beta", "rc"):
            raise CatalogError(f"{self.context}.channel has an unsupported value")
        metadata = ReleaseMetadata.from_object(
            {
                "version": self.release.get("version"),
                "build": self.release.get("build"),
                "released": self.release.get("release_date"),
                "beta": channel == "beta",
                "rc": channel == "rc",
            },
            self.context,
        )
        artifact = self.artifact
        if (
            artifact.get("source_type") not in ("ipsw", "ota")
            or artifact.get("delivery") != "full"
            or artifact.get("prerequisite_builds") != []
        ):
            raise CatalogError(
                f"{self.context} must describe a full IPSW or OTA without prerequisites"
            )
        source = FirmwareSource.from_object(
            {
                "type": artifact.get("source_type"),
                "deviceMap": artifact.get("devices"),
                "links": [{"url": artifact.get("active_url"), "active": True}],
                "hashes": {"sha2-256": artifact.get("sha256")},
                "size": artifact.get("size"),
            },
            device,
            metadata,
            self.context,
        )
        return metadata, source


def _observed_sources(path: Path, platform: str) -> tuple[ObservedSource, ...]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CatalogError(f"cannot read ipsw source inventory {path}: {error}") from error
    try:
        value: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CatalogError(f"ipsw source inventory is not valid JSON: {path}") from error
    envelope = _object(value, "ipsw source inventory")
    if type(envelope.get("schema_version")) is not int or envelope["schema_version"] != 1:
        raise CatalogError("ipsw source inventory schema_version must be 1")
    sources: list[ObservedSource] = []
    for index, item in enumerate(_array(envelope.get("releases"), "ipsw releases")):
        context = f"ipsw releases[{index}]"
        release = _object(item, context)
        if release.get("os") != platform:
            raise CatalogError(f"{context}.os differs from the track policy")
        _string(release.get("build"), f"{context}.build", _BUILD)
        for artifact_index, artifact in enumerate(_array(release.get("artifacts"), context)):
            artifact_context = f"{context}.artifacts[{artifact_index}]"
            sources.append(
                ObservedSource(release, _object(artifact, artifact_context), artifact_context)
            )
    if not sources:
        raise CatalogError("ipsw returned no sources for the reviewed selector")
    return tuple(sources)


def _require_observation(
    candidate: AppleDBCandidate,
    kind: SourceKind,
    observed: tuple[ObservedSource, ...],
    device: str,
) -> AppleDBRelease:
    release = candidate.select(device, kind)
    matches = [
        value
        for value in observed
        if value.release["build"] == release.metadata.build
        and value.artifact.get("source_type") == kind
        and value.artifact.get("active_url") == release.source.url
    ]
    if len(matches) != 1:
        raise CatalogError(
            f"ipsw {kind} source coverage for {release.metadata.build} differs: "
            f"found {len(matches)}"
        )
    metadata, parsed = matches[0].select(device)
    if metadata != release.metadata:
        raise CatalogError(f"ipsw release metadata differs for build {release.metadata.build}")
    if parsed != release.source:
        raise CatalogError(f"ipsw source facts differ for build {release.metadata.build}")
    return release


def _appledb_candidates(
    repo: Path,
    commit: str,
    policy: TrackPolicy,
) -> tuple[AppleDBCandidate, ...]:
    root = f"osFiles/{policy.appledb_platform}"
    folder_suffix = f" - {policy.major_version}.x"
    paths = [
        path
        for path in tree_paths(repo, commit, root)
        if PurePosixPath(path).suffix == ".json"
        and PurePosixPath(path).parent.name.endswith(folder_suffix)
    ]
    if not paths:
        raise CatalogError(
            f"AppleDB has no {policy.appledb_platform} {policy.major_version} records"
        )
    candidates = tuple(
        candidate
        for path in sorted(paths)
        if (
            candidate := _release_from_blob(
                blob_at_path(repo, commit, path), policy=policy, source_path=path
            )
        )
        is not None
    )
    if not candidates:
        raise CatalogError("AppleDB has no records compatible with the reviewed device")
    builds = [candidate.metadata.build for candidate in candidates]
    if len(set(builds)) != len(builds):
        raise CatalogError("AppleDB has duplicate compatible build records")
    return tuple(sorted(candidates, key=lambda candidate: candidate.metadata.released_at))


def _manifest_inventory(
    manifests_dir: Path,
    policy: TrackPolicy,
) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    try:
        paths = sorted(manifests_dir.glob("*.json"))
    except OSError as error:
        raise CatalogError(f"cannot list manifests in {manifests_dir}: {error}") from error
    if not paths:
        raise CatalogError(f"no manifests found in {manifests_dir}")
    known: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for path in paths:
        data = read_json_object(path)
        if (
            data.get("platform") != policy.platform
            or data.get("major_version") != policy.major_version
        ):
            continue
        previous = _object(data.get("from"), f"{path} from")
        next_release = _object(data.get("to"), f"{path} to")
        previous_build = _string(previous.get("build"), f"{path} from.build", _BUILD)
        next_build = _string(next_release.get("build"), f"{path} to.build", _BUILD)
        known.update((previous_build, next_build))
        edges.add((previous_build, next_build))
    return frozenset(known), frozenset(edges)


def _candidate_groups(
    candidates: tuple[AppleDBCandidate, ...],
) -> dict[tuple[int, ...], tuple[AppleDBCandidate, ...]]:
    grouped: dict[tuple[int, ...], list[AppleDBCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.metadata.version_key, []).append(candidate)
    return {
        version: tuple(sorted(items, key=lambda item: item.metadata.released_at))
        for version, items in grouped.items()
    }


def _predecessor_for_new_train(
    version: tuple[int, ...],
    first: AppleDBCandidate,
    groups: dict[tuple[int, ...], tuple[AppleDBCandidate, ...]],
) -> AppleDBCandidate:
    lower_versions = sorted(
        (candidate for candidate in groups if candidate < version), reverse=True
    )
    for lower_version in lower_versions:
        finals = [
            candidate
            for candidate in groups[lower_version]
            if not candidate.metadata.beta
            and not candidate.metadata.rc
            and candidate.metadata.released_at < first.metadata.released_at
        ]
        if finals:
            return max(finals, key=lambda candidate: candidate.metadata.released_at)
    raise CatalogError(
        f"new release train {first.metadata.base_version} has no earlier final predecessor"
    )


def _require_unambiguous_train(
    chain: tuple[AppleDBCandidate, ...],
    version: tuple[int, ...],
) -> None:
    dates: dict[datetime, list[str]] = {}
    for candidate in chain:
        dates.setdefault(candidate.metadata.released_at, []).append(candidate.metadata.build)
    ambiguous = {when: builds for when, builds in dates.items() if len(builds) > 1}
    if ambiguous:
        detail = {when.isoformat(): sorted(builds) for when, builds in ambiguous.items()}
        label = ".".join(str(part) for part in version)
        raise CatalogError(f"release order is ambiguous in train {label}: {detail}")


def _plan_candidate_edges(
    policy: TrackPolicy,
    candidates: tuple[AppleDBCandidate, ...],
    known_builds: frozenset[str],
    manifest_edges: frozenset[tuple[str, str]],
) -> tuple[tuple[AppleDBCandidate, AppleDBCandidate], ...]:
    groups = _candidate_groups(candidates)
    expected: list[tuple[AppleDBCandidate, AppleDBCandidate]] = []
    for version in sorted(groups):
        releases = groups[version]
        after_anchor = tuple(
            candidate
            for candidate in releases
            if candidate.metadata.released_at > policy.anchor.released_at
        )
        if not after_anchor:
            continue
        prior_known = tuple(
            candidate
            for candidate in releases
            if candidate.metadata.build in known_builds
            and candidate.metadata.released_at <= policy.anchor.released_at
        )
        if prior_known:
            predecessor = max(prior_known, key=lambda candidate: candidate.metadata.released_at)
        else:
            predecessor = _predecessor_for_new_train(version, after_anchor[0], groups)
        chain = (predecessor, *after_anchor)
        _require_unambiguous_train(chain, version)
        expected.extend(pairwise(chain))

    unique = {
        (previous.metadata.build, next_release.metadata.build)
        for previous, next_release in expected
    }
    if len(unique) != len(expected):
        raise CatalogError("release planner produced duplicate edges")
    missing_edges: list[tuple[AppleDBCandidate, AppleDBCandidate]] = []
    for previous, next_release in expected:
        pair = (previous.metadata.build, next_release.metadata.build)
        if next_release.metadata.build in known_builds:
            if pair not in manifest_edges:
                raise CatalogError(
                    f"merged build {next_release.metadata.build} is missing expected edge "
                    f"{previous.metadata.build} -> {next_release.metadata.build}"
                )
        else:
            missing_edges.append((previous, next_release))

    destinations = {next_release.metadata.build for _previous, next_release in missing_edges}
    unsupported = sorted(
        previous.metadata.build
        for previous, _next_release in missing_edges
        if previous.metadata.build not in known_builds
        and previous.metadata.build not in destinations
    )
    if unsupported:
        raise CatalogError(f"planned edges have unpublished predecessors: {unsupported}")
    return tuple(
        sorted(
            missing_edges,
            key=lambda edge: (
                edge[1].metadata.released_at,
                edge[1].metadata.version_key,
                edge[1].metadata.build,
            ),
        )
    )


def _edge_kind(
    previous: AppleDBCandidate,
    next_release: AppleDBCandidate,
) -> SourceKind:
    for kind in ("ipsw", "ota"):
        if kind in previous.sources and kind in next_release.sources:
            return kind
    raise CatalogError(
        f"edge {previous.metadata.build} -> {next_release.metadata.build} has no firmware "
        "source kind available for both builds"
    )


def _load_observed(
    policy: TrackPolicy,
    ipsw_sources: Path,
    ota_sources: Path | None,
) -> tuple[ObservedSource, ...]:
    if policy.ota_fallback != (ota_sources is not None):
        raise CatalogError(
            "an OTA source inventory is required exactly when the track policy sets ota_fallback"
        )
    observed = _observed_sources(ipsw_sources, policy.appledb_platform)
    if ota_sources is not None:
        observed += _observed_sources(ota_sources, policy.appledb_platform)
    return observed


def _pallas_candidate(build: PallasBuild, policy: TrackPolicy) -> AppleDBCandidate | PallasDeferral:
    try:
        metadata = ReleaseMetadata.from_object(build.record, build.context)
        candidate = AppleDBCandidate(
            metadata=metadata,
            source_path=None,
            sources={"ota": (build.source, build.context)},
            origin="pallas",
        )
        candidate.select(policy.device, "ota")
    except CatalogError as error:
        return PallasDeferral(build.build, str(error))
    return candidate


def _pallas_candidates(
    policy: TrackPolicy,
    pallas_sources: tuple[Path, ...],
    appledb_builds: frozenset[str],
) -> tuple[tuple[AppleDBCandidate, ...], list[PallasDeferral]]:
    """Returns the full OTAs Apple serves that the pinned AppleDB commit does not record yet."""
    if pallas_sources and not policy.ota_fallback:
        raise CatalogError("Pallas OTA inventories need a track policy with ota_fallback")
    inventory = read_pallas(
        pallas_sources,
        platform=policy.appledb_platform,
        device=policy.device,
        major=policy.major_version,
    )
    candidates: list[AppleDBCandidate] = []
    deferred = [item for item in inventory.deferred if item.build not in appledb_builds]
    for build in inventory.builds:
        if build.build in appledb_builds:
            continue
        result = _pallas_candidate(build, policy)
        if isinstance(result, PallasDeferral):
            deferred.append(result)
        else:
            candidates.append(result)
    return tuple(candidates), deferred


def _plan_with_pallas(
    policy: TrackPolicy,
    candidates: tuple[AppleDBCandidate, ...],
    known_builds: frozenset[str],
    manifest_edges: frozenset[tuple[str, str]],
) -> tuple[tuple[tuple[AppleDBCandidate, AppleDBCandidate], ...], list[PallasDeferral]]:
    """Plans edges where a Pallas-only build is never a predecessor.

    Pallas labels can disagree with AppleDB, so a build known only from Pallas may end an edge
    but never start one; the next build waits until AppleDB records it.
    """
    deferred: list[PallasDeferral] = []
    while True:
        planned = _plan_candidate_edges(policy, candidates, known_builds, manifest_edges)
        blocked = {
            next_release.metadata.build: next_release
            for previous, next_release in planned
            if previous.origin == "pallas"
        }
        if not blocked:
            return planned, deferred
        for build, next_release in sorted(blocked.items()):
            if next_release.origin != "pallas":
                raise CatalogError(
                    f"AppleDB build {build} follows a build only Pallas serves; "
                    "wait for AppleDB to record the earlier build"
                )
            deferred.append(PallasDeferral(build, "its predecessor is Pallas-only"))
        candidates = tuple(
            candidate for candidate in candidates if candidate.metadata.build not in blocked
        )


def discover(  # noqa: PLR0913
    policy_path: Path,
    manifests_dir: Path,
    appledb_repo: Path,
    appledb_commit: str,
    ipsw_sources: Path,
    *,
    ota_sources: Path | None = None,
    pallas_sources: tuple[Path, ...] = (),
) -> DiscoveryDecision:
    policy = TrackPolicy.from_path(policy_path)
    repo = ensure_repository(appledb_repo)
    require_origin(repo, _APPLEDB_REPOSITORY, "AppleDB")
    commit = resolve_commit(repo, appledb_commit)
    observed = _load_observed(policy, ipsw_sources, ota_sources)
    appledb_candidates = _appledb_candidates(repo, commit, policy)
    known_builds, manifest_edges = _manifest_inventory(manifests_dir, policy)

    by_build = {candidate.metadata.build: candidate for candidate in appledb_candidates}
    anchor_candidate = by_build.get(policy.anchor.build)
    if anchor_candidate is None:
        raise CatalogError("track anchor has no compatible AppleDB firmware record")
    if anchor_candidate.metadata != policy.anchor:
        raise CatalogError("AppleDB anchor metadata differs from the track policy")
    pallas_candidates, deferred = _pallas_candidates(policy, pallas_sources, frozenset(by_build))
    candidates = tuple(
        sorted(
            (*appledb_candidates, *pallas_candidates),
            key=lambda candidate: candidate.metadata.released_at,
        )
    )
    # The anchor may be unpublished: it then starts the track, and older builds are skipped.
    planning_known = known_builds | {policy.anchor.build}
    planned, blocked = _plan_with_pallas(policy, candidates, planning_known, manifest_edges)
    blocked_builds = {item.build for item in blocked}
    planned_candidates = tuple(
        candidate for candidate in candidates if candidate.metadata.build not in blocked_builds
    )
    latest_candidate = max(planned_candidates, key=lambda candidate: candidate.metadata.released_at)

    selected: dict[tuple[str, SourceKind], AppleDBRelease] = {}

    def select(candidate: AppleDBCandidate, kind: SourceKind) -> AppleDBRelease:
        key = (candidate.metadata.build, kind)
        if key not in selected:
            if candidate.origin == "pallas":
                selected[key] = candidate.select(policy.device, kind)
            else:
                selected[key] = _require_observation(candidate, kind, observed, policy.device)
        return selected[key]

    edges = tuple(
        DiscoveryEdge(
            previous=select(previous, _edge_kind(previous, next_release)),
            next=select(next_release, _edge_kind(previous, next_release)),
        )
        for previous, next_release in planned
    )
    anchor = select(anchor_candidate, anchor_candidate.preferred_kind)
    latest = select(latest_candidate, latest_candidate.preferred_kind)
    used = sorted(
        {release.metadata.build for release in selected.values() if release.origin == "pallas"}
    )
    return DiscoveryDecision(
        track=policy,
        appledb_commit=commit,
        anchor=anchor,
        latest=latest,
        edges=edges,
        observed_source_count=len(observed),
        selected_source_count=len(selected),
        pallas_queried=bool(pallas_sources),
        pallas_used=tuple(used),
        pallas_deferred=tuple(sorted((*deferred, *blocked), key=lambda item: item.build)),
    )
